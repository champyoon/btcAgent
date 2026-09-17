"""KST 09:00 캔들 경계 / 15일 09:00 정기분할 마감 / 월말 00:00 전략변경 마감 — SPEC §5·§7-1.

각 경계를 "직전(마감 전) · 정각(마감) · 직후(마감 후)" 세 지점으로 확인한다.
"""

from datetime import datetime

import month_state as ms
import price_history as ph


# ══════════════════════════════════════════════════════════════════
# §7-1: KST 09:00 캔들 확정 경계
# ══════════════════════════════════════════════════════════════════

def test_last_confirmed_date_before_0900():
    now = datetime(2026, 9, 17, 8, 59, 59, tzinfo=ph.KST)
    assert ph.last_confirmed_date_kst(now) == "2026-09-15"


def test_last_confirmed_date_exactly_0900():
    now = datetime(2026, 9, 17, 9, 0, 0, tzinfo=ph.KST)
    assert ph.last_confirmed_date_kst(now) == "2026-09-16"


def test_last_confirmed_date_just_after_0900():
    now = datetime(2026, 9, 17, 9, 0, 1, tzinfo=ph.KST)
    assert ph.last_confirmed_date_kst(now) == "2026-09-16"


def test_confirmed_records_excludes_unclosed_candle():
    records = [
        {"date_kst": "2026-09-15", "open": 1, "high": 1, "low": 1, "close": 1},
        {"date_kst": "2026-09-16", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    now = datetime(2026, 9, 17, 8, 59, 59, tzinfo=ph.KST)  # 09:00 이전 -> 9/16은 아직 미확정
    confirmed = ph.confirmed_records(records, now=now)
    assert [r["date_kst"] for r in confirmed] == ["2026-09-15"]

    now2 = datetime(2026, 9, 17, 9, 0, 0, tzinfo=ph.KST)
    confirmed2 = ph.confirmed_records(records, now=now2)
    assert [r["date_kst"] for r in confirmed2] == ["2026-09-15", "2026-09-16"]


# ══════════════════════════════════════════════════════════════════
# §5: 15일 09:00(KST) 정기 분할 변경 마감
# ══════════════════════════════════════════════════════════════════

def test_biweekly_cutoff_before_0900_on_15th():
    now = datetime(2026, 9, 15, 8, 59, 59, tzinfo=ms.KST)
    assert ms.biweekly_cutoff_passed(2026, 9, now=now) is False


def test_biweekly_cutoff_exactly_0900_on_15th():
    now = datetime(2026, 9, 15, 9, 0, 0, tzinfo=ms.KST)
    assert ms.biweekly_cutoff_passed(2026, 9, now=now) is True


def test_biweekly_cutoff_just_after_0900_on_15th():
    now = datetime(2026, 9, 15, 9, 0, 1, tzinfo=ms.KST)
    assert ms.biweekly_cutoff_passed(2026, 9, now=now) is True


# ══════════════════════════════════════════════════════════════════
# §5: 월 마지막 날 00:00(KST) 전략 변경 전체 마감
# ══════════════════════════════════════════════════════════════════

def test_month_end_cutoff_before_last_day_midnight():
    now = datetime(2026, 9, 29, 23, 59, 59, tzinfo=ms.KST)  # 9월은 30일까지
    assert ms.month_end_cutoff_passed(2026, 9, now=now) is False


def test_month_end_cutoff_exactly_at_last_day_midnight():
    now = datetime(2026, 9, 30, 0, 0, 0, tzinfo=ms.KST)
    assert ms.month_end_cutoff_passed(2026, 9, now=now) is True


def test_month_end_cutoff_just_after_last_day_midnight():
    now = datetime(2026, 9, 30, 0, 0, 1, tzinfo=ms.KST)
    assert ms.month_end_cutoff_passed(2026, 9, now=now) is True


def test_can_select_strategy_biweekly_blocked_after_cutoff_but_others_allowed():
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))  # 8/1 가입 -> 8월부터 시작
    now = datetime(2026, 9, 16, tzinfo=ms.KST)  # 15일 09:00 마감 이후
    ok_biweekly, _ = ms.can_select_strategy(2026, 9, "biweekly", now=now)
    ok_rsi, _ = ms.can_select_strategy(2026, 9, "rsi", now=now)
    assert ok_biweekly is False
    assert ok_rsi is True
