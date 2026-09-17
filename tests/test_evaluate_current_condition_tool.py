"""§5 놓친 신호 재계산을 실제 도구(evaluate_current_condition)로 연결 — 2026-09-18.

전에는 month_state.evaluate_current_condition()이 구현은 됐지만 어떤 @tool에도 연결돼 있지
않았고, 관련 질문 자체가 route_question에도 안 걸렸다(REPORT.md에 미구현으로 기록됐던 항목).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import agent
import ledger
import month_state as ms


def _continuous_records(start: date, num_days: int, price_fn) -> list[dict]:
    records = []
    for i in range(num_days):
        d = start + timedelta(days=i)
        price = price_fn(i)
        records.append(
            {"date_kst": d.isoformat(), "open": price, "high": price * 1.01, "low": price * 0.99, "close": price}
        )
    return records


def _patch_price_history(records):
    original_update = agent.price_history.update_incremental
    original_confirmed = agent.price_history.confirmed_records
    agent.price_history.update_incremental = lambda *a, **kw: records
    agent.price_history.confirmed_records = lambda *a, **kw: records
    return original_update, original_confirmed


def _unpatch_price_history(originals):
    agent.price_history.update_incremental, agent.price_history.confirmed_records = originals


def test_no_strategy_selected_refuses_to_evaluate():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ms.KST)
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))  # 계획은 8월부터 시작, 전략 미선택
    result = agent.evaluate_current_condition.invoke({})
    assert "선택된 전략이 없어" in result


def test_decline_day_triggered_signal_is_reported_without_implying_still_buyable():
    now = datetime.now(ms.KST)
    year, month = now.year, now.month
    ms.init_plan(2_000_000, now=datetime(year, month, 1, tzinfo=ms.KST))
    prop = ms.propose_strategy_change(year, month, "decline_day", now=now)
    ms.confirm_strategy_change(prop["confirmation_token"], now=now)

    ledger.record_virtual_buy(
        amount_krw=1_000_000, price_krw=100_000_000,
        executed_date=f"{year:04d}-{month:02d}-01", execution_time_precision="date",
    )

    # 1일 매수가(1억원)보다 낮고 당일 -5% 이상 하락한 날을 하루 만든다.
    start = date(year, month, 1)
    def price_fn(i):
        if i == 4:  # 5일차 — 트리거
            return 94_000_000.0
        return 100_000_000.0 + i * 10_000
    records = _continuous_records(start, 10, price_fn)
    # check_decline_day는 open/close로 당일 변동률을 보므로 open도 맞춰준다.
    for i, r in enumerate(records):
        if i == 4:
            r["open"] = 100_000_000.0
            r["close"] = 94_000_000.0

    originals = _patch_price_history(records)
    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = True
    try:
        result = agent.evaluate_current_condition.invoke({})
    finally:
        _unpatch_price_history(originals)
        agent._REQUEST_CONTEXT["proceed_with_stale_data"] = False

    assert "충족된 것으로 계산됩니다" in result
    assert "자동으로 매수 조건이 이어지는 건" in result  # 지금도 매수 가능하다고 단정하지 않음


def test_no_signal_yet_reports_reason():
    now = datetime.now(ms.KST)
    year, month = now.year, now.month
    ms.init_plan(2_000_000, now=datetime(year, month, 1, tzinfo=ms.KST))
    prop = ms.propose_strategy_change(year, month, "rsi", now=now)
    ms.confirm_strategy_change(prop["confirmation_token"], now=now)
    ledger.record_virtual_buy(
        amount_krw=1_000_000, price_krw=100_000_000,
        executed_date=f"{year:04d}-{month:02d}-01", execution_time_precision="date",
    )

    start = date(year, month, 1)
    records = _continuous_records(start, 5, lambda i: 100_000_000.0)  # RSI 중립, 트리거 없음

    originals = _patch_price_history(records)
    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = True
    try:
        result = agent.evaluate_current_condition.invoke({})
    finally:
        _unpatch_price_history(originals)
        agent._REQUEST_CONTEXT["proceed_with_stale_data"] = False

    assert "충족된 적이 없습니다" in result
