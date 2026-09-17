"""백테스트 미래 데이터 참조 금지, 월말/조건매수 중복 방지, 동일 입력 재현성 — SPEC §6·§6-1.

전부 합성 캔들로 검증한다(실제 Upbit 데이터/네트워크 불필요) — 계산 규칙 자체를 확인하는 것이
목적이라 가격의 실제 의미는 중요하지 않다.
"""

import calendar
from datetime import date, datetime, timedelta

import backtest
from price_history import select_backtest_months


def _add_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _make_full_months(start_year: int, start_month: int, num_months: int, start_price: float = 100_000_000.0):
    """start_year-start_month부터 num_months개월치 "완결된"(그 달의 실제 일수만큼 꽉 찬) 캔들을 만든다.

    가격은 완만하게 우상향(조건 미충족 -> 월말 대체매수 유도)시켜 조건매수/월말대체가 둘 다
    자연스럽게 섞이지 않게 한다. 반환값의 last_date는 마지막으로 생성한 캔들의 날짜다.
    """
    candles = []
    price = start_price
    y, m = start_year, start_month
    last_date = None
    for _ in range(num_months):
        days_in_month = calendar.monthrange(y, m)[1]
        for day in range(1, days_in_month + 1):
            d = date(y, m, day)
            candles.append(
                {"date_kst": d.isoformat(), "open": price, "high": price * 1.001, "low": price * 0.999, "close": price}
            )
            price *= 1.0001
            last_date = d
        y, m = _add_month(y, m)
    return candles, last_date


def test_select_backtest_months_excludes_in_progress_current_month():
    # 1월은 완결(31일 전부), 2월은 15일치만(진행 중, 미완료) — "지금"은 2월 20일.
    jan_candles, _ = _make_full_months(2026, 1, 1)
    feb_partial = [
        {"date_kst": date(2026, 2, d).isoformat(), "open": 1, "high": 1, "low": 1, "close": 1}
        for d in range(1, 16)
    ]
    candles = jan_candles + feb_partial
    now = datetime(2026, 2, 20, 12, 0, tzinfo=backtest.KST)

    keys = select_backtest_months(candles, now=now, months=1)
    # "직전 달"인 1월만 완결됐으므로 1월만 반환돼야 한다 — 미완료인 2월(진행 중인 달)을
    # 절대 끌어와 채우면 안 된다.
    assert keys == [(2026, 1)]


def test_run_backtest_not_ready_when_fewer_than_required_months():
    candles, last_date = _make_full_months(2020, 1, 40)  # 48개월에 못 미치는 40개월치만 완비
    now = datetime(last_date.year, last_date.month, last_date.day, 12, 0, tzinfo=backtest.KST) + timedelta(days=2)
    result = backtest.run_backtest(1_000_000, candles, now=now, months=48)
    assert result["ready"] is False
    assert "reason" in result


def test_run_backtest_no_duplicate_condition_and_month_end_buy_same_month():
    candles, last_date = _make_full_months(2020, 1, 55)  # 48개월 + 여유
    now = datetime(last_date.year, last_date.month, last_date.day, 12, 0, tzinfo=backtest.KST) + timedelta(days=2)
    result = backtest.run_backtest(1_000_000, candles, now=now, months=48)
    assert result["ready"] is True

    for strategy_name in ("decline_day", "biweekly", "rsi"):
        buys = result["strategies"][strategy_name]["buys"]
        by_month: dict[str, list[str]] = {}
        for b in buys:
            by_month.setdefault(b["date"][:7], []).append(b["kind"])
        for month_key, kinds in by_month.items():
            non_first_half = [k for k in kinds if k != "first_half"]
            # 조건매수(condition)와 월말대체(month_end_fallback)가 같은 달에 동시에 나오면 안 된다
            assert len(non_first_half) == 1, f"{month_key}: {kinds}"


def test_run_backtest_covers_exactly_requested_month_count():
    candles, last_date = _make_full_months(2020, 1, 55)
    now = datetime(last_date.year, last_date.month, last_date.day, 12, 0, tzinfo=backtest.KST) + timedelta(days=2)
    result = backtest.run_backtest(1_000_000, candles, now=now, months=48)
    assert len(result["months_covered"]) == 48
    # 마지막으로 포함된 달은 "직전 달"(now 기준)이어야지, 데이터에 남은 미래의 달까지 끌어오면 안 된다.
    expected_last_month = f"{last_date.year:04d}-{last_date.month:02d}"
    assert result["months_covered"][-1] == expected_last_month


def test_run_backtest_is_reproducible_for_identical_input():
    candles, last_date = _make_full_months(2020, 1, 55)
    now = datetime(last_date.year, last_date.month, last_date.day, 12, 0, tzinfo=backtest.KST) + timedelta(days=2)
    r1 = backtest.run_backtest(1_000_000, candles, now=now, months=48)
    r2 = backtest.run_backtest(1_000_000, candles, now=now, months=48)
    assert r1 == r2


def test_run_backtest_final_valuation_uses_last_included_close_not_a_later_price():
    candles, last_date = _make_full_months(2020, 1, 55)
    # last_date 이후에도 데이터가 있다면(예: 아직 미완료인 다음 달 일부) 그 이후 가격이 최종
    # 평가에 절대 쓰이면 안 된다 — "오늘 가격"이 아니라 마지막 포함 달의 확정 종가여야 한다(§6-1).
    future_leak = [{"date_kst": (last_date + timedelta(days=1)).isoformat(), "open": 999_999_999_999.0,
                     "high": 999_999_999_999.0, "low": 999_999_999_999.0, "close": 999_999_999_999.0}]
    now = datetime(last_date.year, last_date.month, last_date.day, 12, 0, tzinfo=backtest.KST) + timedelta(days=2)
    result_without_leak = backtest.run_backtest(1_000_000, candles, now=now, months=48)
    result_with_leak = backtest.run_backtest(1_000_000, candles + future_leak, now=now, months=48)
    assert result_without_leak["final_evaluation_date"] == result_with_leak["final_evaluation_date"]
    for strategy_name in ("decline_day", "biweekly", "rsi"):
        assert (
            result_without_leak["strategies"][strategy_name]["valuation_krw"]
            == result_with_leak["strategies"][strategy_name]["valuation_krw"]
        )
