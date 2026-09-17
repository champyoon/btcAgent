"""가격 캐시의 09:00(KST) 확정 경계·미마감 캔들 배제·기존 캐시 전환 — 2026-09-18 기획팀 요청.

핵심 회귀: check_freshness()가 "저장된 최신 날짜 >= 기대 날짜"로만 판정하면, 미마감 상태에서
미리 저장된 값이 나중에 그 날짜가 실제로 마감된 뒤에도 재조회 없이 확정값처럼 쓰일 수 있었다.
이 파일은 (1) 저장 단계에서 미마감 캔들 자체를 걸러내는지, (2) 그 필터링이 없던 예전 캐시를
안전하게 전환하는지를 함께 검증한다 — 비교 연산자만 바꿔서는 안 잡히는 문제이기 때문이다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import requests

import price_history as ph

UTC = timezone.utc


def _raw_candle(d: date, price: float = 100_000_000.0) -> dict:
    """Upbit API가 실제로 주는 형태(day 캔들)를 흉내낸다 — date_kst는 앞 10글자만 쓰이므로
    "T00:00:00"을 붙인 형태로 충분하다. candle_date_time_utc는 그 캔들의 시작 시각(UTC) —
    date_kst 09:00(KST) = 00:00(UTC)이므로 같은 날짜의 00:00이다.
    """
    return {
        "candle_date_time_kst": f"{d.isoformat()}T00:00:00",
        "candle_date_time_utc": f"{d.isoformat()}T00:00:00",
        "opening_price": price,
        "high_price": price * 1.001,
        "low_price": price * 0.999,
        "trade_price": price,
    }


class FakeUpbit:
    """`_fetch_recent_days`가 기대하는 (to, count) -> newest-first 페이지 계약을 흉내낸다."""

    def __init__(self, days: list[date], price_fn=None):
        price_fn = price_fn or (lambda d: 100_000_000.0 + (d - days[0]).days * 1000)
        self.by_date = {d: _raw_candle(d, price_fn(d)) for d in days}
        self.call_log: list[tuple[str | None, int]] = []

    def fetch(self, to: str | None, count: int) -> list[dict]:
        self.call_log.append((to, count))
        pool = sorted(self.by_date.values(), key=lambda c: c["candle_date_time_utc"])
        if to is not None:
            pool = [c for c in pool if c["candle_date_time_utc"] < to]
        newest_first = list(reversed(pool))
        return newest_first[:count]


class FailingFetch:
    def __call__(self, to, count):
        raise requests.RequestException("네트워크 오류(테스트)")


def _write_legacy_cache(records: list[dict]) -> None:
    """이 수정 이전 포맷(버전 필드 없는 bare list)으로 직접 파일을 만든다."""
    ph.HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ph.HISTORY_PATH.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
# 1~2. 09:00(KST) 확정 경계
# ══════════════════════════════════════════════════════════════════

def test_last_confirmed_date_before_0900_is_two_days_back():
    now = datetime(2026, 9, 18, 8, 59, 59, tzinfo=ph.KST)
    assert ph.last_confirmed_date_kst(now) == "2026-09-16"


def test_last_confirmed_date_at_0900_advances_to_yesterday():
    now = datetime(2026, 9, 18, 9, 0, 0, tzinfo=ph.KST)
    assert ph.last_confirmed_date_kst(now) == "2026-09-17"


# ══════════════════════════════════════════════════════════════════
# 3. 진행 중(미마감) 캔들은 저장·계산에서 제외
# ══════════════════════════════════════════════════════════════════

def test_backfill_excludes_in_progress_candle():
    today = date(2026, 9, 18)
    days = [today - timedelta(days=i) for i in range(5, -1, -1)]  # 6일치, 오늘(today) 포함
    fake = FakeUpbit(days)
    now = datetime(2026, 9, 18, 12, 0, tzinfo=ph.KST)  # 오늘 캔들은 아직 마감 전(내일 09:00 마감)

    records = ph.backfill(total_days=6, fetch=fake.fetch, now=now)
    dates = [r["date_kst"] for r in records]
    assert today.isoformat() not in dates  # 오늘(미마감)은 빠져야 한다
    assert (today - timedelta(days=1)).isoformat() in dates  # 어제(마감됨)는 있어야 한다


def test_update_incremental_excludes_in_progress_candle():
    yesterday = date(2026, 9, 17)
    today = date(2026, 9, 18)
    ph.save_history([_confirmed_record(yesterday)])  # 이미 확정 포맷(버전 최신)으로 어제까지 저장됨
    fake = FakeUpbit([yesterday, today])
    now = datetime(2026, 9, 18, 12, 0, tzinfo=ph.KST)

    records = ph.update_incremental(fetch=fake.fetch, now=now)
    dates = [r["date_kst"] for r in records]
    assert today.isoformat() not in dates


def _confirmed_record(d: date, price: float = 100_000_000.0) -> dict:
    return {"date_kst": d.isoformat(), "open": price, "high": price * 1.001, "low": price * 0.999, "close": price}


# ══════════════════════════════════════════════════════════════════
# 4. 기존(레거시) 캐시의 미마감 저장값 → 마감 후 확정값으로 교체
# ══════════════════════════════════════════════════════════════════

def test_legacy_cache_stale_recent_value_gets_replaced_on_full_recovery():
    the_17th = date(2026, 9, 17)
    # 레거시 캐시: 17일이 "미마감 상태에서 찍힌" 잘못된(낮은) 값으로 저장돼 있다고 가정. 부분 패치가
    # 아니라 "보유 구간 전체"를 다시 받는지 확인하기 위해, 훨씬 오래된 날짜도 하나 섞어 둔다(오염
    # 여부를 증명할 수 없으니 그 구간도 포함해서 다시 받아야 한다).
    old_far_back = the_17th - timedelta(days=20)
    legacy = [_confirmed_record(old_far_back, price=50_000_000.0)]
    legacy += [_confirmed_record(the_17th - timedelta(days=i), price=100_000_000.0) for i in range(4, 0, -1)]
    legacy += [_confirmed_record(the_17th, price=90_000_000.0)]  # 미마감 상태에서 찍힌 스냅샷
    _write_legacy_cache(legacy)

    # 실제 API(FakeUpbit)는 그 구간 전체의 "진짜" 마감 확정값을 다르게(정확하게) 갖고 있다.
    correct_days = [old_far_back + timedelta(days=i) for i in range((the_17th - old_far_back).days + 1)]
    fake = FakeUpbit(correct_days, price_fn=lambda d: 123_000_000.0)

    now = datetime(2026, 9, 18, 10, 0, tzinfo=ph.KST)  # 17일은 이미(18일 09:00 이후) 마감된 시점
    records = ph.update_incremental(fetch=fake.fetch, now=now)

    fixed = next(r for r in records if r["date_kst"] == the_17th.isoformat())
    assert fixed["close"] == 123_000_000.0  # 레거시 스냅샷(9천만) 대신 재수집한 확정값으로 교체됨
    old_fixed = next(r for r in records if r["date_kst"] == old_far_back.isoformat())
    assert old_fixed["close"] == 123_000_000.0  # 훨씬 오래된 날짜도 "이미 확정이니 안전"이라고 안 넘어가고 재수집됨
    assert ph.find_missing_dates(records, old_far_back.isoformat(), the_17th.isoformat()) == []

    # 마이그레이션 후 파일 버전이 올라갔는지 확인
    _, version = ph.load_history_meta()
    assert version == ph.CACHE_VERSION


def test_already_v2_file_is_still_fully_recovered_not_skipped():
    """기획팀 지적: "버전만 보고 복구를 건너뛰지 마세요" — v2(과거의 부분 패치 버전)로 이미
    저장된 파일도 CACHE_VERSION(3)보다 낮으므로 전체 복구 대상이어야 한다."""
    the_17th = date(2026, 9, 17)
    v2_records = [_confirmed_record(the_17th - timedelta(days=i), price=77_000_000.0) for i in range(4, -1, -1)]
    ph.save_history(v2_records, version=2)

    correct_days = [the_17th - timedelta(days=i) for i in range(4, -1, -1)]
    fake = FakeUpbit(correct_days, price_fn=lambda d: 200_000_000.0)
    now = datetime(2026, 9, 18, 10, 0, tzinfo=ph.KST)

    records = ph.update_incremental(fetch=fake.fetch, now=now)
    assert all(r["close"] == 200_000_000.0 for r in records)  # v2였다는 이유로 건너뛰지 않고 재수집됨
    _, version = ph.load_history_meta()
    assert version == ph.CACHE_VERSION


def test_legacy_migration_failure_keeps_old_data_and_does_not_bump_version():
    the_17th = date(2026, 9, 17)
    _write_legacy_cache([_confirmed_record(the_17th, price=90_000_000.0)])
    now = datetime(2026, 9, 18, 10, 0, tzinfo=ph.KST)

    records = ph.update_incremental(fetch=FailingFetch(), now=now)
    # 실패했으니 기존 값을 그대로 보존(크래시하지 않음)
    assert any(r["date_kst"] == the_17th.isoformat() and r["close"] == 90_000_000.0 for r in records)
    _, version = ph.load_history_meta()
    assert version == 0  # 실패했으므로 마이그레이션 완료로 표시하지 않는다


# ══════════════════════════════════════════════════════════════════
# 5. 30일 이상 미사용해도 누락 구간 전체 보충
# ══════════════════════════════════════════════════════════════════

def test_update_incremental_backfills_gap_longer_than_30_days():
    old_last = date(2026, 7, 1)
    now = datetime(2026, 9, 18, 10, 0, tzinfo=ph.KST)  # old_last로부터 79일 지남(>>30일)
    ph.save_history([_confirmed_record(old_last)])

    all_days = [old_last + timedelta(days=i) for i in range((date(2026, 9, 17) - old_last).days + 1)]
    fake = FakeUpbit(all_days)

    records = ph.update_incremental(fetch=fake.fetch, now=now)
    dates = {r["date_kst"] for r in records}
    expected_dates = {d.isoformat() for d in all_days}
    assert expected_dates.issubset(dates)  # 30일보다 훨씬 긴 구간이 전부 채워졌는지
    assert ph.find_missing_dates(records, old_last.isoformat(), "2026-09-17") == []


# ══════════════════════════════════════════════════════════════════
# 6. API 페이지 경계 중복 제거 + 날짜 연속성
# ══════════════════════════════════════════════════════════════════

def test_backfill_dedups_across_page_boundaries_and_stays_continuous():
    start = date(2026, 1, 1)
    total_days = ph.MAX_CANDLES_PER_CALL + 50  # 여러 페이지에 걸치도록
    days = [start + timedelta(days=i) for i in range(total_days)]
    now = datetime(days[-1].year, days[-1].month, days[-1].day, 12, 0, tzinfo=ph.KST) + timedelta(days=2)
    fake = FakeUpbit(days)

    records = ph.backfill(total_days=total_days, fetch=fake.fetch, now=now)
    dates = [r["date_kst"] for r in records]
    assert len(dates) == len(set(dates))  # 중복 없음
    assert dates == sorted(dates)  # 시간순 정렬
    assert ph.find_missing_dates(records, start.isoformat(), days[-1].isoformat()) == []


# ══════════════════════════════════════════════════════════════════
# 7. 중간 결측 감지
# ══════════════════════════════════════════════════════════════════

def test_find_missing_dates_detects_gap_in_middle():
    records = [_confirmed_record(date(2026, 9, 1) + timedelta(days=i)) for i in range(10) if i != 4]
    missing = ph.find_missing_dates(records, "2026-09-01", "2026-09-10")
    assert missing == ["2026-09-05"]


def test_find_missing_dates_empty_when_continuous():
    records = [_confirmed_record(date(2026, 9, 1) + timedelta(days=i)) for i in range(10)]
    assert ph.find_missing_dates(records, "2026-09-01", "2026-09-10") == []


# ══════════════════════════════════════════════════════════════════
# 8. 갱신 실패 시 기존 캐시 보존
# ══════════════════════════════════════════════════════════════════

def test_update_incremental_preserves_cache_on_fetch_failure():
    existing = [_confirmed_record(date(2026, 9, 10))]
    ph.save_history(existing)
    now = datetime(2026, 9, 18, 10, 0, tzinfo=ph.KST)

    records = ph.update_incremental(fetch=FailingFetch(), now=now)
    assert records == existing  # 크래시하지 않고 기존 값 그대로

    freshness = ph.check_freshness(now=now)
    assert freshness["fresh"] is False  # 여전히 부족하다고 정확히 안내됨


# ══════════════════════════════════════════════════════════════════
# 9. stale-data 기준일 표시(구조 확인 — 실제 문구 조립은 agent.py 쪽)
# ══════════════════════════════════════════════════════════════════

def test_check_freshness_reports_last_available_for_caveat_display():
    ph.save_history([_confirmed_record(date(2026, 8, 17))])
    now = datetime(2026, 9, 18, 10, 0, tzinfo=ph.KST)
    freshness = ph.check_freshness(now=now)
    assert freshness["fresh"] is False
    assert freshness["last_available"] == "2026-08-17"  # 사용자에게 보여줄 기준일
    assert freshness["expected"] == "2026-09-17"


# ══════════════════════════════════════════════════════════════════
# 10. 월초·연도 변경 경계
# ══════════════════════════════════════════════════════════════════

def test_last_confirmed_date_across_year_boundary_before_0900():
    now = datetime(2027, 1, 1, 5, 0, tzinfo=ph.KST)
    assert ph.last_confirmed_date_kst(now) == "2026-12-30"


def test_last_confirmed_date_across_year_boundary_after_0900():
    now = datetime(2027, 1, 1, 10, 0, tzinfo=ph.KST)
    assert ph.last_confirmed_date_kst(now) == "2026-12-31"


# ══════════════════════════════════════════════════════════════════
# 11. 이미 최신(+마이그레이션 완료)인 캐시는 불필요하게 재수집하지 않음
# ══════════════════════════════════════════════════════════════════

def test_update_incremental_skips_fetch_when_already_fresh_and_migrated():
    now = datetime(2026, 9, 18, 10, 0, tzinfo=ph.KST)
    expected = ph.last_confirmed_date_kst(now)
    ph.save_history([_confirmed_record(date.fromisoformat(expected))])  # CACHE_VERSION으로 저장됨

    fake = FakeUpbit([date.fromisoformat(expected)])
    records = ph.update_incremental(fetch=fake.fetch, now=now)

    assert fake.call_log == []  # fetch가 한 번도 안 불렸는지
    assert records[-1]["date_kst"] == expected
