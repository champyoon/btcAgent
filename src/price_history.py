"""BTC 일봉 히스토리 로컬 캐시 — SERVICE.md §7·§7-1 확정 설계.

Upbit 일봉은 KST 09:00에 마감된다(UTC 00:00 경계) — KST 자정이 아니다. "확정된 최신 일봉"이
무엇인지는 조회 시각이 오늘 09:00(KST)을 지났는지에 따라 달라진다(§7-1).

저장소는 chroma_db(벡터 검색)가 아니라 이 파일 같은 구조화된 로컬 캐시다 — 지난 날짜의
일봉은 확정되면 값이 바뀌지 않고, 지표 계산엔 "최근 200일" 같은 정확한 range 쿼리가 필요해
벡터 검색과 맞지 않기 때문이다(§7).

최초 1회 페이지네이션 백필 후, 이후에는 매일(또는 호출 시점마다) 최신 일봉만 이어붙인다.

**확정 캔들만 저장한다(2026-09-18 수정)**: Upbit API는 "최신 캔들"을 물으면 아직 마감 전인
진행 중 캔들도 함께 내려준다. 예전 코드는 이걸 그대로 캐시에 저장했는데, `check_freshness()`가
"저장된 최신 날짜 >= 기대 날짜"로만 판정하다 보니 — 그 날짜에 저장된 값이 실제로 마감 후 확정값인지
는 전혀 확인하지 않았다. 그 결과 미마감 상태에서 미리 저장된 값이 나중에 그 날짜가 실제로 마감된
뒤에도 재조회 없이 그대로 확정값처럼 쓰일 수 있었다(연산자를 `==`로만 바꿔도 저장 시점에 이미 오염된
값이면 똑같이 못 잡는다 — 그래서 저장 단계 자체에서 미마감 캔들을 걸러내는 게 진짜 수정이다).
그래서 이제 `backfill()`/`update_incremental()` 모두 저장 직전에 `_filter_confirmed()`로 마감
여부를 캔들 시작일+1일 09:00(KST)과 조회 기준 시각을 비교해 명시적으로 확인한다.
"""

from __future__ import annotations

import calendar
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

UPBIT_BASE = "https://api.upbit.com/v1"
MARKET = "KRW-BTC"

# BTC_AGENT_DATA_DIR가 설정돼 있으면 그 경로를 쓴다(수동 테스트용 격리 데이터 디렉터리 지정 —
# 미설정 시 기존과 동일하게 실제 data/ 디렉터리를 그대로 쓴다).
DATA_DIR = Path(os.environ.get("BTC_AGENT_DATA_DIR") or (Path(__file__).resolve().parents[1] / "data"))
HISTORY_PATH = DATA_DIR / "price_history.json"

CANDLE_BOUNDARY_HOUR_KST = 9  # Upbit 일봉 마감 시각(KST) — SERVICE.md §7-1
BACKTEST_MONTHS = 48  # SERVICE.md §6 — 직전 달까지 완료된 48개월
INDICATOR_BUFFER_DAYS = 260  # MA200(200) + RSI RMA 시드(14) + 여유
MAX_CANDLES_PER_CALL = 200  # Upbit API 1회 호출 상한

# 캐시 파일 포맷 버전. 버전 필드 없는 bare list(구버전 전부)나 CACHE_VERSION보다 낮은 버전은
# "오염 가능성이 있는 캐시"로 취급돼 `_full_recovery_migrate()`가 보유 구간 전체를 확정 일봉으로
# 다시 받아 검증 후 통째로 교체한다(아래 "기존 캐시 전환" 참고) — 부분 패치가 아니다. 이 버전을
# 새로 올릴 때는 반드시 `_full_recovery_migrate`가 실제로 다시 실행되게 할 것 — 그냥 숫자만 올리고
# 마이그레이션 로직을 안 건드리면 예전 캐시가 조용히 "이미 확정 캐시"로 취급된다.
# v2 -> v3(2026-09-18): v2 자체가 "최근 5일만 재수집"하는 불완전한 패치였다는 게 재검토 결과라
# v2 파일도 이번 버전에서 다시 전체 복구 대상이 된다.
CACHE_VERSION = 3

KST = timezone(timedelta(hours=9))

FetchFn = Callable[[str | None, int], list[dict]]


def _target_total_days() -> int:
    """48개월(달마다 최대 31일로 넉넉히 환산) + 지표 계산용 선행 버퍼."""
    return BACKTEST_MONTHS * 31 + INDICATOR_BUFFER_DAYS


def _fetch_candles_page(to: str | None, count: int) -> list[dict]:
    """Upbit 일봉 API 1페이지 조회. `to`는 그 시각(UTC) 이전 캔들을 최신순으로 반환한다.

    주의: `to`가 None(또는 지금에 가까운 시각)이면 API는 아직 마감되지 않은 진행 중 캔들도 같이
    내려준다 — 이 함수 자체는 그걸 걸러내지 않는다(순수 조회). 걸러내는 건 호출부(`_filter_confirmed`)
    의 책임이다.
    """
    params: dict[str, Any] = {"market": MARKET, "count": count}
    if to:
        params["to"] = to
    resp = requests.get(f"{UPBIT_BASE}/candles/days", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _to_candle_record(raw: dict) -> dict:
    return {
        "date_kst": raw["candle_date_time_kst"][:10],  # "YYYY-MM-DD" — 그 일봉이 시작하는 날짜
        "open": raw["opening_price"],
        "high": raw["high_price"],
        "low": raw["low_price"],
        "close": raw["trade_price"],
    }


# ══════════════════════════════════════════════════════════════════
# 확정 여부 판정 — 이 모듈 전체가 이 두 함수 하나로 통일해서 쓴다(§7-1 경계 로직 이원화 방지)
# ══════════════════════════════════════════════════════════════════

def _candle_close_boundary(date_kst: str) -> datetime:
    """date_kst로 시작하는 캔들이 마감되는 시각 — 시작일 09:00(KST) + 1일."""
    d = date.fromisoformat(date_kst)
    return datetime(d.year, d.month, d.day, CANDLE_BOUNDARY_HOUR_KST, tzinfo=KST) + timedelta(days=1)


def _is_confirmed(date_kst: str, now: datetime) -> bool:
    """date_kst 캔들이 now 시점 기준으로 이미 마감됐는지."""
    return now >= _candle_close_boundary(date_kst)


def _filter_confirmed(records: list[dict], now: datetime) -> list[dict]:
    """진행 중(미마감) 캔들을 제거한다 — backfill/update_incremental이 저장 직전에 반드시 거친다."""
    return [r for r in records if _is_confirmed(r["date_kst"], now)]


def last_confirmed_date_kst(now: datetime | None = None) -> str:
    """SERVICE.md §7-1: 지금 시점에 마감돼 있어야 할 가장 최근 일봉 날짜.

    캐시에 저장된 date_kst는 그 캔들이 "시작"한 날짜다(예: "2026-09-16"는 2026-09-16 09:00 KST ~
    2026-09-17 09:00 KST 구간). 하루 단위 캔들이라 후보는 "어제"와 "그저께" 둘뿐이다 —
    `_is_confirmed`(이 모듈의 유일한 확정 판정 기준)로 실제로 마감됐는지 확인해서 고른다.
    """
    now = now or datetime.now(KST)
    candidate = now.date() - timedelta(days=1)
    if _is_confirmed(candidate.isoformat(), now):
        return candidate.isoformat()
    return (candidate - timedelta(days=1)).isoformat()


# ══════════════════════════════════════════════════════════════════
# 저장소 — 버전 필드를 포함해 원자적으로 저장한다
# ══════════════════════════════════════════════════════════════════

def load_history_meta() -> tuple[list[dict], int]:
    """(캔들 목록, 캐시 포맷 버전)을 반환한다. 버전이 CACHE_VERSION보다 낮으면 마이그레이션 대상이다.

    파일이 아예 bare list(이 수정 이전 포맷)면 버전 0으로 취급한다 — 그 파일은 미마감 상태의
    값이 어딘가에 저장돼 있을 수 있다는 뜻이다(아래 `_full_recovery_migrate`).
    """
    if not HISTORY_PATH.exists():
        return [], CACHE_VERSION  # 빈 캐시는 오염될 데이터 자체가 없으니 마이그레이션 대상이 아니다
    raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw, 0
    return raw.get("candles", []), raw.get("version", 0)


def load_history() -> list[dict]:
    return load_history_meta()[0]


def save_history(records: list[dict], version: int = CACHE_VERSION) -> None:
    """캔들 목록을 버전 필드와 함께 원자적으로 저장한다.

    쓰다가 프로세스가 죽어도 기존 파일이 반쪽짜리로 남지 않도록, 임시 파일에 다 쓴 뒤
    `Path.replace()`(POSIX/Windows 둘 다 원자적 교체)로 덮어쓴다.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": version, "candles": records}, ensure_ascii=False)
    tmp_path = HISTORY_PATH.with_name(HISTORY_PATH.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(HISTORY_PATH)


# ══════════════════════════════════════════════════════════════════
# 조회 — 최신부터 거슬러 올라가며 필요한 만큼 모으는 공용 페이지네이션
# ══════════════════════════════════════════════════════════════════

def _fetch_recent_days(fetch: FetchFn, total_days: int) -> list[dict]:
    """API가 "지금" 기준으로 가진 최신 캔들부터 거슬러 올라가며 total_days개를 모은다.

    `backfill()`(최초 백필)과 `update_incremental()`(누락 구간 보충)이 이 알고리즘을 공유한다 —
    "얼마나 되돌아갈지"만 다르지, 둘 다 결국 "최신부터 필요한 만큼 페이지네이션"이기 때문이다.
    반환값에는 아직 미마감 캔들이 섞여 있을 수 있다(순수 조회) — 호출부가 `_filter_confirmed`로
    걸러야 한다.
    """
    collected: list[dict] = []
    to_param: str | None = None
    while len(collected) < total_days:
        remaining = total_days - len(collected)
        page = fetch(to_param, min(MAX_CANDLES_PER_CALL, remaining))
        if not page:
            break
        collected.extend(page)
        to_param = page[-1]["candle_date_time_utc"]  # 다음 페이지는 이 시각 이전부터(더 과거로)
    return collected


def backfill(total_days: int | None = None, fetch: FetchFn | None = None, now: datetime | None = None) -> list[dict]:
    """페이지네이션으로 과거 일봉을 최초 1회 수집한다(SERVICE.md §7 확정 방식). 확정 캔들만 남긴다."""
    fetch = fetch or _fetch_candles_page
    now = now or datetime.now(KST)  # 한 번의 백필 안에서는 이 now 하나만 쓴다(호출마다 새로 재지 않음)
    total_days = total_days or _target_total_days()

    collected = _fetch_recent_days(fetch, total_days)
    records = [_to_candle_record(r) for r in collected]
    records = _filter_confirmed(records, now)
    # 페이지 경계에서 겹칠 수 있어 날짜 기준으로 중복 제거 후 오래된 -> 최신 순 정렬
    dedup: dict[str, dict] = {r["date_kst"]: r for r in records}
    return sorted(dedup.values(), key=lambda r: r["date_kst"])


# ══════════════════════════════════════════════════════════════════
# 기존(이 수정 이전) 캐시 전환 — "지금 마감됐다"는 이유만으로 저장값을 확정값이라 믿지 않는다
# ══════════════════════════════════════════════════════════════════
#
# 2026-09-18 재검토: 처음엔 "최근 5일만 재수집하면 충분하다"고 판단했었다(이 서비스 코드는 매번
# 최신 데이터를 받아 저장하므로, 정상적인 사용 패턴에서는 어느 시점에도 "가장 최근 날짜 하나"만
# 미마감 상태로 캡처될 수 있다는 논리였다). 하지만 이 프로젝트 자체의 개발 과정에서 여러 스크립트가
# 서로 다른 시각(diff `now`)에 이 캐시를 직접 갱신한 이력이 있어, "가장 최근 날짜만 오염됐다"는
# 전제를 실제로 증명할 수 없다는 지적을 받았다 — 맞는 지적이다. 그래서 부분 패치를 폐기하고,
# **버전이 CACHE_VERSION보다 낮은 캐시는 보유 구간 전체를 확정 일봉으로 다시 받아 검증 후 통째로
# 교체**한다. 이미 한 번(v2로) 부분 패치된 파일도 CACHE_VERSION(v3)보다 낮으므로 이번 전체 복구
# 대상에서 빠지지 않는다 — "버전이 이미 있으니 건너뛴다"는 조건은 여기 없다.


def _validate_full_recovery(records: list[dict], required_start: str, now: datetime) -> tuple[bool, str]:
    """전체 재수집 결과를 디스크에 쓰기 전에 검증한다: 구간 커버리지·중복·필수 필드.

    `required_start`는 기존 파일이 갖고 있던 시작일 — 재수집 결과가 이보다 늦게 시작하면(과거
    구간을 잃어버렸다는 뜻이므로) 실패로 본다. 최신 쪽은 `now` 기준 "지금 확정돼 있어야 할 마지막
    일봉"까지 도달했는지 확인한다.
    """
    if not records:
        return False, "재수집 결과가 비어 있습니다."

    required_fields = ("date_kst", "open", "high", "low", "close")
    for r in records:
        if any(r.get(f) is None for f in required_fields):
            return False, f"필수 필드가 누락된 레코드가 있습니다: {r}"

    dates = [r["date_kst"] for r in records]
    if len(dates) != len(set(dates)):
        return False, "재수집 결과에 중복된 날짜가 있습니다."

    if dates[0] > required_start:
        return False, f"기존 시작일({required_start})보다 늦게 시작합니다({dates[0]}) — 과거 구간 손실 의심."

    expected_end = last_confirmed_date_kst(now)
    if dates[-1] != expected_end:
        return False, f"최신 확정 일봉({expected_end})까지 도달하지 못했습니다(재수집 결과 마지막: {dates[-1]})."

    missing = find_missing_dates(records, dates[0], dates[-1])
    if missing:
        return False, f"재수집 결과 구간 내 결측 {len(missing)}건: {missing[:5]}"

    return True, ""


def _full_recovery_migrate(records: list[dict], fetch: FetchFn, now: datetime) -> list[dict]:
    """버전이 낮은(오염 가능성이 있는) 캐시의 보유 구간 전체를 API에서 다시 받아 검증 후 교체한다.

    재수집 결과는 메모리에서 전부 검증한 뒤에만 `save_history()`(임시 파일 + 원자적 교체)로 실제
    파일을 바꾼다 — API 호출 실패든 검증 실패든, 실패하면 기존 파일을 그대로 두고 버전을 올리지
    않는다(복구 실패를 성공으로 표시하지 않는다. 다음 호출에서 다시 시도된다).
    """
    if not records:
        return records
    # 정상적으로 저장된 캐시는 항상 날짜순 정렬 상태지만, 레거시 파일(특히 손으로 만들었거나 아주
    # 예전 버전)이 그렇지 않을 가능성까지 방어적으로 대비해 여기서 다시 정렬해 최소/최대를 구한다.
    sorted_dates = sorted(r["date_kst"] for r in records)
    old_start, old_end = sorted_dates[0], sorted_dates[-1]
    span_days = (date.fromisoformat(old_end) - date.fromisoformat(old_start)).days + 1

    try:
        fetched_raw = _fetch_recent_days(fetch, span_days + 5)  # 여유 좀 더
    except requests.RequestException:
        return records

    confirmed = _filter_confirmed([_to_candle_record(r) for r in fetched_raw], now)
    # 날짜 기준 중복 제거 + 정렬 — _fetch_recent_days는 최신->과거 순으로 주고, 페이지 경계에서
    # 겹칠 수도 있다(backfill()과 같은 이유).
    dedup: dict[str, dict] = {r["date_kst"]: r for r in confirmed}
    refreshed = sorted(dedup.values(), key=lambda r: r["date_kst"])
    ok, _reason = _validate_full_recovery(refreshed, old_start, now)
    if not ok:
        return records

    save_history(refreshed, version=CACHE_VERSION)
    return refreshed


def _load_and_migrate(fetch: FetchFn, now: datetime) -> list[dict]:
    existing, version = load_history_meta()
    if existing and version < CACHE_VERSION:
        return _full_recovery_migrate(existing, fetch, now)
    return existing


def ensure_backfilled(fetch: FetchFn | None = None, now: datetime | None = None) -> list[dict]:
    """캐시가 비어 있으면 최초 백필한다. 이미 있으면(레거시면 마이그레이션한 뒤) 그대로 반환한다."""
    fetch = fetch or _fetch_candles_page
    now = now or datetime.now(KST)
    existing = _load_and_migrate(fetch, now)
    if existing:
        return existing
    records = backfill(fetch=fetch, now=now)
    save_history(records)
    return records


def update_incremental(fetch: FetchFn | None = None, now: datetime | None = None) -> list[dict]:
    """마지막 확정 일봉 이후로 비어 있는 구간만 이어붙인다.

    고정 30일 조회로 제한하지 않는다 — 오래 안 쓰였다면(누락 구간이 30일보다 길어도) 그만큼
    페이지네이션으로 전부 보충한다. API 호출이 실패해도 예외를 밖으로 던지지 않고 기존 정상 캐시를
    그대로 보존한다.
    """
    fetch = fetch or _fetch_candles_page
    now = now or datetime.now(KST)  # 이 호출 안에서는 이 now 하나만 쓴다
    existing = _load_and_migrate(fetch, now)
    if not existing:
        return ensure_backfilled(fetch=fetch, now=now)

    expected = last_confirmed_date_kst(now)
    last_available = existing[-1]["date_kst"]
    if last_available == expected:
        return existing  # 이미 최신 — 불필요한 재수집 안 함

    gap_days = (date.fromisoformat(expected) - date.fromisoformat(last_available)).days
    total_days = max(gap_days, 0) + 3  # 겹치게 몇 일 더(중복은 병합 과정에서 자연히 제거됨)

    try:
        fetched_raw = _fetch_recent_days(fetch, total_days)
    except requests.RequestException:
        return existing  # 갱신 실패 — 기존 정상 캐시 보존, 예외를 던지지 않는다

    new_records = _filter_confirmed([_to_candle_record(r) for r in fetched_raw], now)
    merged = {r["date_kst"]: r for r in existing}
    merged.update({r["date_kst"]: r for r in new_records})
    records = sorted(merged.values(), key=lambda r: r["date_kst"])
    save_history(records)
    return records


def check_freshness(now: datetime | None = None) -> dict:
    """캐시의 마지막 확정 일봉이 §7-1 기준과 **정확히** 일치하는지 확인한다.

    `>=`가 아니라 `==`다 — 저장 시점에 이미 미마감 캔들을 걸러내므로(`_filter_confirmed`)
    last_available이 expected를 앞서갈 일은 정상적으로 없지만, 혹시 어긋나면(시계 오차 등) "신선하다"
    고 조용히 넘기지 않고 다시 확인하게 만든다. 임의의 유예 기준을 두지 않는다.
    """
    now = now or datetime.now(KST)
    records = load_history()
    expected = last_confirmed_date_kst(now)
    if not records:
        return {"fresh": False, "expected": expected, "last_available": None}
    last_available = records[-1]["date_kst"]
    return {"fresh": last_available == expected, "expected": expected, "last_available": last_available}


def confirmed_records(records: list[dict] | None = None, now: datetime | None = None) -> list[dict]:
    """§7-1 확정성 원칙: 마감된 최신 일봉까지만 남기고 자른다.

    "지금 지표가 얼마인지"처럼 최신값을 직접 쓰는 계산(§3-1)은 반드시 이 함수를 거친 뒤의
    records를 써야 한다. 저장 단계(`_filter_confirmed`)에서 이미 미마감 캔들을 걸러내므로 이 필터는
    이제 대부분 no-op이지만, 명시적인 방어선으로 남겨둔다(예: `records`를 직접 넘겨 호출하는 다른
    경로가 생기더라도 안전).
    """
    records = records if records is not None else load_history()
    boundary = last_confirmed_date_kst(now)
    return [r for r in records if r["date_kst"] <= boundary]


def find_missing_dates(records: list[dict], start_date: str, end_date: str) -> list[str]:
    """start_date~end_date(둘 다 포함, YYYY-MM-DD) 구간에서 records에 없는 날짜를 전부 반환한다.

    "최신 확정 일봉이 있다"와 "필요한 구간 전체에 빠짐이 없다"는 서로 다른 질문이다 — 이 함수는
    후자를 확인한다(빈 리스트면 결측 없음).
    """
    have = {r["date_kst"] for r in records}
    d0, d1 = date.fromisoformat(start_date), date.fromisoformat(end_date)
    missing = []
    cur = d0
    while cur <= d1:
        iso = cur.isoformat()
        if iso not in have:
            missing.append(iso)
        cur += timedelta(days=1)
    return missing


def slice_recent_days(records: list[dict], days: int) -> list[dict]:
    """과거->최신 순 records에서 최근 N일만 자른다(지표별 계산 기간 분리 — §7)."""
    return records[-days:] if days > 0 else []


def month_key(date_str: str) -> tuple[int, int]:
    y, m, _ = date_str.split("-")
    return int(y), int(m)


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _month_has_full_data(days: list[dict], key: tuple[int, int]) -> bool:
    """그 달 안에 빠진 일봉이 없는지 — `find_missing_dates()`로 직접 확인한다(2026-09-18: 이전에는
    "보유 캔들 수 == 그 달의 달력상 일수"만 비교했는데, 이 방식은 이론적으로 중복 레코드가 결측을
    가려 카운트만 우연히 맞아떨어질 수 있었다 — find_missing_dates는 날짜 존재 여부를 직접 보므로
    그런 사각지대가 없다. 백테스트도 지표 계산과 같은 결측 판정 기준을 쓰도록 통일한다)."""
    y, m = key
    last_day = calendar.monthrange(y, m)[1]
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y:04d}-{m:02d}-{last_day:02d}"
    return not find_missing_dates(days, start, end)


def _previous_month_key(key: tuple[int, int]) -> tuple[int, int]:
    y, m = key
    return (y - 1, 12) if m == 1 else (y, m - 1)


def select_backtest_months(
    records: list[dict], now: datetime | None = None, months: int = BACKTEST_MONTHS
) -> list[tuple[int, int]]:
    """직전 달까지 **완료된** N개월(SPEC §6·§6-1 확정).

    "직전 달"은 항상 조회 시점 기준 달력상 바로 전 달을 가리킨다. 그 달이 아직 확정 전이면(예:
    매월 1일 09:00 KST 이전에는 직전 달의 마지막 일봉이 아직 마감되지 않는다 — §6-1) **더 오래된
    달로 조용히 대체하지 않고** 빈 리스트를 반환한다 — 그러면 뭘 "48개월"이라고 부르는지가 조회
    시각에 따라 달라지는 혼란을 피할 수 있다(호출부는 부족분을 "아직 준비 안 됨"으로 처리한다).
    """
    now = now or datetime.now(KST)
    boundary = date.fromisoformat(last_confirmed_date_kst(now))
    target_last = _previous_month_key((now.year, now.month))

    groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for r in records:
        groups[month_key(r["date_kst"])].append(r)

    target_days = groups.get(target_last, [])
    if _last_day_of_month(*target_last) > boundary or not _month_has_full_data(target_days, target_last):
        return []

    complete_keys = sorted(k for k in groups if _month_has_full_data(groups[k], k))
    if target_last not in complete_keys:
        return []
    idx = complete_keys.index(target_last)
    return complete_keys[max(0, idx - months + 1) : idx + 1]
