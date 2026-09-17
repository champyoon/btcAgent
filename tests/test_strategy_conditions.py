"""RSI 30 / 일봉 -5% 경계값, 첫 매수가 대비 엄격한 비교, 반올림 전 값 판정 — SPEC §3·§6-3·§6-5.

각 값은 "직전(트리거 안 됨) · 동일(경계값, 트리거 됨) · 직후(트리거 안 됨)" 세 지점을 모두 확인한다.
"""

import indicators
from strategy import check_decline_day, check_rsi


# ══════════════════════════════════════════════════════════════════
# RSI <= 30 경계
# ══════════════════════════════════════════════════════════════════

def test_rsi_exactly_30_triggers():
    assert check_rsi("2026-01-01", {"2026-01-01": 30.0}) is True


def test_rsi_just_above_30_does_not_trigger():
    assert check_rsi("2026-01-01", {"2026-01-01": 30.0000001}) is False


def test_rsi_just_below_30_triggers():
    assert check_rsi("2026-01-01", {"2026-01-01": 29.9999999}) is True


def test_rsi_uses_raw_value_not_display_rounded():
    # 30.004는 표시용으로 반올림하면 30.0이 되지만, §6-5에 따라 판정은 반올림 전 원시값을 써야
    # 하므로 30.004(>30)는 트리거되면 안 된다.
    assert check_rsi("2026-01-01", {"2026-01-01": 30.004}) is False


def test_rsi_none_value_never_triggers():
    assert check_rsi("2026-01-01", {}) is False
    assert check_rsi("2026-01-01", {"2026-01-01": None}) is False


# ══════════════════════════════════════════════════════════════════
# 일봉 -5% (당일 종가/시가) 경계 + 첫 매수가 대비 엄격한 비교
# ══════════════════════════════════════════════════════════════════

def test_decline_day_exact_minus_5pct_triggers():
    day = {"open": 100_000_000, "close": 95_000_000}  # (95m/100m - 1)*100 = -5.0 정확히
    assert check_decline_day(day, first_buy_price=96_000_000) is True


def test_decline_day_minus_4_99pct_does_not_trigger():
    day = {"open": 100_000_000, "close": 95_010_000}  # -4.99%
    assert check_decline_day(day, first_buy_price=96_000_000) is False


def test_decline_day_minus_5_01pct_triggers():
    day = {"open": 100_000_000, "close": 94_990_000}  # -5.01%
    assert check_decline_day(day, first_buy_price=96_000_000) is True


def test_decline_day_requires_strictly_lower_than_first_buy_price():
    # 종가가 -5%를 만족해도 "첫 매수가와 정확히 같으면"(더 낮지 않으면) 조건 불충족 — 엄격한 비교(<).
    day = {"open": 100_000_000, "close": 95_000_000}
    assert check_decline_day(day, first_buy_price=95_000_000) is False


def test_decline_day_close_just_above_first_buy_price_does_not_trigger():
    day = {"open": 100_000_000, "close": 95_000_000}
    assert check_decline_day(day, first_buy_price=94_999_999) is False  # close(95m) > first_buy_price


def test_decline_day_no_open_price_returns_false_not_error():
    assert check_decline_day({"open": 0, "close": 95_000_000}, first_buy_price=96_000_000) is False


# ══════════════════════════════════════════════════════════════════
# RSI 시리즈(Wilder's RMA) — §6-3 특수값
# ══════════════════════════════════════════════════════════════════

def test_rsi_series_all_gain_no_loss_is_100():
    closes = [100.0 + i for i in range(20)]  # 계속 상승만
    series = indicators.compute_rsi_series(closes, period=14)
    assert series[14] == 100.0


def test_rsi_series_all_loss_no_gain_is_0():
    closes = [200.0 - i for i in range(20)]  # 계속 하락만
    series = indicators.compute_rsi_series(closes, period=14)
    assert series[14] == 0.0


def test_rsi_series_no_change_is_50():
    closes = [100.0] * 20
    series = indicators.compute_rsi_series(closes, period=14)
    assert series[14] == 50.0


def test_rsi_series_insufficient_data_is_none():
    closes = [100.0] * 10  # period(14)보다 적음
    series = indicators.compute_rsi_series(closes, period=14)
    assert all(v is None for v in series)
