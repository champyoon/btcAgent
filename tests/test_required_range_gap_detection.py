"""필수 계산 구간의 결측 검사를 실제 계산 경로(get_indicators/record_watch_decision/전략 조건 확인)에
연결 — 2026-09-18 기획팀 지적: "단순 레코드 개수만으로 계산 가능 여부를 판단하지 마세요."

레코드 개수가 충분해도(>=200) 중간에 결측이 있으면 RSI/이동평균 계산이 인접하지 않은 두 날을
인접한 것처럼 다루게 된다 — 이 결측 검사는 stale-data 동의(`proceed_with_stale_data`)로도 우회되지
않아야 한다(최신 쪽 지연을 눈감아주는 것과 과거 구간 중간의 결측은 서로 다른 문제이기 때문).
"""

from __future__ import annotations

from datetime import date, timedelta

import agent
import month_state as ms


def _continuous_records(start: date, num_days: int, price: float = 100_000_000.0) -> list[dict]:
    return [
        {
            "date_kst": (start + timedelta(days=i)).isoformat(),
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price + i * 1000,
        }
        for i in range(num_days)
    ]


def _remove_middle_day(records: list[dict]) -> list[dict]:
    mid = len(records) // 2
    return records[:mid] + records[mid + 1 :]


# ══════════════════════════════════════════════════════════════════
# get_indicators — 지표 조회 경로
# ══════════════════════════════════════════════════════════════════

def test_indicators_summary_blocks_on_gap_despite_enough_count():
    records = _remove_middle_day(_continuous_records(date(2025, 1, 1), 250))
    assert len(records) >= 200  # 개수 조건 자체는 통과하는 상태
    summary = agent._indicators_summary(records)
    assert "결측" in summary
    assert "RSI" not in summary  # 계산 결과를 내놓지 않는다


def test_indicators_summary_computes_normally_when_continuous():
    records = _continuous_records(date(2025, 1, 1), 250)
    summary = agent._indicators_summary(records)
    assert "RSI" in summary
    assert "결측" not in summary


def test_indicators_summary_still_blocks_gap_even_with_stale_data_consent():
    """proceed_with_stale_data는 '최신 일봉이 아직 없다'는 것만 눈감아주는 플래그다 — 과거 구간
    중간의 결측까지 우회시키면 안 된다. _indicators_summary는 _REQUEST_CONTEXT를 아예 참조하지
    않으므로, 이 플래그를 True로 켜도 결측 차단 결과는 바뀌지 않아야 한다."""
    records = _remove_middle_day(_continuous_records(date(2025, 1, 1), 250))
    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = True
    try:
        summary = agent._indicators_summary(records)
    finally:
        agent._REQUEST_CONTEXT["proceed_with_stale_data"] = False
    assert "결측" in summary


def test_get_indicators_tool_blocks_on_gap_without_hitting_network():
    """도구 경계(get_indicators)까지 포함해 확인하되, 실제 네트워크 호출 없이 검증한다.

    isolated 테스트 캐시가 비어 있어 §7-1 신선도 게이트가 먼저 걸리므로, proceed_with_stale_data로
    그것만 넘긴다 — 그래도 결측 구간 검사는 별개로 여전히 막아야 한다는 게 이 테스트의 핵심이다.
    """
    records = _remove_middle_day(_continuous_records(date(2025, 1, 1), 250))
    original_update = agent.price_history.update_incremental
    original_confirmed = agent.price_history.confirmed_records
    agent.price_history.update_incremental = lambda *a, **kw: records
    agent.price_history.confirmed_records = lambda *a, **kw: records
    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = True
    try:
        result = agent.get_indicators.invoke({})
    finally:
        agent.price_history.update_incremental = original_update
        agent.price_history.confirmed_records = original_confirmed
        agent._REQUEST_CONTEXT["proceed_with_stale_data"] = False
    assert "결측" in result


# ══════════════════════════════════════════════════════════════════
# record_watch_decision — 관망 스냅샷 경로
# ══════════════════════════════════════════════════════════════════

def test_watch_decision_snapshot_empty_when_gap_present_but_note_still_recorded():
    records = _remove_middle_day(_continuous_records(date(2025, 1, 1), 250))
    original_update = agent.price_history.update_incremental
    original_confirmed = agent.price_history.confirmed_records
    agent.price_history.update_incremental = lambda *a, **kw: records
    agent.price_history.confirmed_records = lambda *a, **kw: records
    try:
        result = agent.record_watch_decision.invoke({"note": "테스트 관망"})
    finally:
        agent.price_history.update_incremental = original_update
        agent.price_history.confirmed_records = original_confirmed

    assert "관망 기록 추가" in result  # 관망 결정 자체는 그대로 기록됨(§2-2, 승인 불필요)
    entries = agent.ledger.search_ledger()
    watch = next(e for e in entries if e["type"] == "watch")
    assert watch["indicators_snapshot"] == {}  # 결측 구간이라 스냅샷은 비워짐(잘못된 RSI를 남기지 않음)


def test_watch_decision_snapshot_populated_when_continuous():
    records = _continuous_records(date(2025, 1, 1), 250)
    original_update = agent.price_history.update_incremental
    original_confirmed = agent.price_history.confirmed_records
    agent.price_history.update_incremental = lambda *a, **kw: records
    agent.price_history.confirmed_records = lambda *a, **kw: records
    try:
        agent.record_watch_decision.invoke({"note": "테스트 관망2"})
    finally:
        agent.price_history.update_incremental = original_update
        agent.price_history.confirmed_records = original_confirmed

    entries = agent.ledger.search_ledger()
    watch = next(e for e in entries if e["type"] == "watch" and e["note"] == "테스트 관망2")
    assert "rsi14" in watch["indicators_snapshot"]


# ══════════════════════════════════════════════════════════════════
# evaluate_current_condition — 전략 조건 확인 경로(§5)
# ══════════════════════════════════════════════════════════════════

def test_evaluate_current_condition_blocks_on_gap_in_this_month():
    days = _remove_middle_day(_continuous_records(date(2026, 9, 1), 20))  # 9/1~9/20 중 하루 결측
    result = ms.evaluate_current_condition(
        2026, 9, "decline_day", days, rsi_by_date={}, first_buy_price=100_500_000.0
    )
    assert result["triggered"] is False
    assert "결측" in result["reason"]


def test_evaluate_current_condition_normal_when_continuous():
    days = _continuous_records(date(2026, 9, 1), 20)
    rsi_by_date = {d["date_kst"]: 50.0 for d in days}  # 조건 미충족(RSI 50 > 30)이라 트리거 없음이 기대값
    result = ms.evaluate_current_condition(
        2026, 9, "rsi", days, rsi_by_date=rsi_by_date, first_buy_price=100_500_000.0
    )
    assert "결측" not in result["reason"]
