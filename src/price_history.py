"""BTC 일봉 히스토리 로컬 캐시 — SERVICE.md §7·§7-1 확정 설계.

Upbit 일봉은 KST 09:00에 마감된다(UTC 00:00 경계) — KST 자정이 아니다. "확정된 최신 일봉"이
무엇인지는 조회 시각이 오늘 09:00(KST)을 지났는지에 따라 달라진다(§7-1).

저장소는 chroma_db(벡터 검색)가 아니라 이 파일 같은 구조화된 로컬 캐시다 — 지난 날짜의
일봉은 확정되면 값이 바뀌지 않고, 지표 계산엔 "최근 200일" 같은 정확한 range 쿼리가 필요해
벡터 검색과 맞지 않기 때문이다(§7).

최초 1회 페이지네이션 백필 후, 이후에는 매일(또는 호출 시점마다) 최신 일봉만 이어붙인다.
"""

from __future__ import annotations

import calendar
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

UPBIT_BASE = "https://api.upbit.com/v1"
MARKET = "KRW-BTC"

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
HISTORY_PATH = DATA_DIR / "price_history.json"

CANDLE_BOUNDARY_HOUR_KST = 9  # Upbit 일봉 마감 시각(KST) — SERVICE.md §7-1
BACKTEST_MONTHS = 48  # SERVICE.md §6 — 직전 달까지 완료된 48개월
INDICATOR_BUFFER_DAYS = 260  # MA200(200) + RSI RMA 시드(14) + 여유
MAX_CANDLES_PER_CALL = 200  # Upbit API 1회 호출 상한

KST = timezone(timedelta(hours=9))

FetchFn = Callable[[str | None, int], list[dict]]


def _target_total_days() -> int:
    """48개월(달마다 최대 31일로 넉넉히 환산) + 지표 계산용 선행 버퍼."""
    return BACKTEST_MONTHS * 31 + INDICATOR_BUFFER_DAYS


def _fetch_candles_page(to: str | None, count: int) -> list[dict]:
    """Upbit 일봉 API 1페이지 조회. `to`는 그 시각(UTC) 이전 캔들을 최신순으로 반환한다."""
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


def backfill(total_days: int | None = None, fetch: FetchFn | None = None) -> list[dict]:
    """페이지네이션으로 과거 일봉을 최초 1회 수집한다(SERVICE.md §7 확정 방식).

    Upbit 일봉 API가 1회 최대 200개까지만 주므로, `to` 파라미터로 구간을 나눠 여러 번 호출한다.
    """
    fetch = fetch or _fetch_candles_page
    total_days = total_days or _target_total_days()

    collected: list[dict] = []
    to_param: str | None = None
    while len(collected) < total_days:
        remaining = total_days - len(collected)
        page = fetch(to_param, min(MAX_CANDLES_PER_CALL, remaining))
        if not page:
            break
        collected.extend(page)
        to_param = page[-1]["candle_date_time_utc"]  # 다음 페이지는 이 시각 이전부터(더 과거로)

    records = [_to_candle_record(r) for r in collected]
    # 페이지 경계에서 겹칠 수 있어 날짜 기준으로 중복 제거 후 오래된 -> 최신 순 정렬
    dedup: dict[str, dict] = {r["date_kst"]: r for r in records}
    return sorted(dedup.values(), key=lambda r: r["date_kst"])


def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))


def save_history(records: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def ensure_backfilled(fetch: FetchFn | None = None) -> list[dict]:
    """캐시가 비어 있으면 최초 백필한다. 이미 있으면 그대로 반환한다(재수집하지 않음 — §7)."""
    existing = load_history()
    if existing:
        return existing
    records = backfill(fetch=fetch)
    save_history(records)
    return records


def last_confirmed_date_kst(now: datetime | None = None) -> str:
    """SERVICE.md §7-1: KST 09:00 경계 기준, 지금 시점에 마감돼 있어야 할 가장 최근 일봉 날짜.

    캐시에 저장된 date_kst는 그 캔들이 "시작"한 날짜다(예: "2026-09-16"는 2026-09-16 09:00 KST ~
    2026-09-17 09:00 KST 구간). 조회 시각이 오늘 09:00(KST) 이전이면 어제 시작한 캔들이 아직
    마감 전이라 그저께 시작 캔들까지만 확정이고, 09:00 이후면 어제 시작 캔들까지 확정이다.
    """
    now = now or datetime.now(KST)
    if now.hour < CANDLE_BOUNDARY_HOUR_KST:
        confirmed = now.date() - timedelta(days=2)
    else:
        confirmed = now.date() - timedelta(days=1)
    return confirmed.isoformat()


def check_freshness(now: datetime | None = None) -> dict:
    """캐시의 마지막 확정 일봉이 §7-1 기준과 일치하는지 확인한다. 임의의 유예 기준을 두지 않는다."""
    records = load_history()
    expected = last_confirmed_date_kst(now)
    if not records:
        return {"fresh": False, "expected": expected, "last_available": None}
    last_available = records[-1]["date_kst"]
    return {"fresh": last_available >= expected, "expected": expected, "last_available": last_available}


def update_incremental(fetch: FetchFn | None = None) -> list[dict]:
    """마지막 확정 일봉 이후로 비어 있는 구간만 이어붙인다(매 요청마다 전체를 다시 모으지 않음)."""
    fetch = fetch or _fetch_candles_page
    existing = load_history()
    if not existing:
        return ensure_backfilled(fetch=fetch)

    if check_freshness()["fresh"]:
        return existing

    recent = fetch(None, 30)  # 경계 근처 오차 대비 여유 있게 최근 30일 재조회
    new_records = [_to_candle_record(r) for r in recent]
    merged = {r["date_kst"]: r for r in existing}
    merged.update({r["date_kst"]: r for r in new_records})
    records = sorted(merged.values(), key=lambda r: r["date_kst"])
    save_history(records)
    return records


def confirmed_records(records: list[dict] | None = None, now: datetime | None = None) -> list[dict]:
    """§7-1 확정성 원칙: 마감된 최신 일봉까지만 남기고 자른다.

    "지금 지표가 얼마인지"처럼 최신값을 직접 쓰는 계산(§3-1)은 반드시 이 함수를 거친 뒤의
    records를 써야 한다 — 원본 캐시에는 아직 마감되지 않은 당일 진행 중 캔들이 섞여 있을 수
    있다(Upbit API가 실시간으로 그날 캔들을 함께 내려주기 때문). 반면 백테스트(§6)는 완결된
    달만 골라 쓰므로(월의 마지막 날짜도 항상 이미 지난 날) 이 필터가 없어도 안전하다.
    """
    records = records if records is not None else load_history()
    boundary = last_confirmed_date_kst(now)
    return [r for r in records if r["date_kst"] <= boundary]


def slice_recent_days(records: list[dict], days: int) -> list[dict]:
    """과거->최신 순 records에서 최근 N일만 자른다(지표별 계산 기간 분리 — §7)."""
    return records[-days:] if days > 0 else []


def month_key(date_str: str) -> tuple[int, int]:
    y, m, _ = date_str.split("-")
    return int(y), int(m)


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _month_has_full_data(days: list[dict], key: tuple[int, int]) -> bool:
    """그 달 안에 빠진 일봉이 없는지(캐시 누락 여부) — 달력상 일수와 실제 보유 캔들 수 비교."""
    y, m = key
    return len(days) == calendar.monthrange(y, m)[1]


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
