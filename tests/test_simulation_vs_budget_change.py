"""시뮬레이션 요청이 실제 예산 변경 제안으로 잘못 처리되는 문제 — 실사용 재현(2026-09-20, #8).

재현: "300만원으로 4년동안 전략별 시뮬레이션 부탁해"에서 plan_agent가 run_backtest 대신
set_monthly_budget을 호출해 예산 변경 제안 카드가 떴다. 프롬프트에 "시뮬레이션 요청이면
run_backtest를 호출하라"는 지시를 추가했는데도(§29) 다른 문구로 재현됐다 — 프롬프트 준수에만
기대지 않고, 서버가 직접 감지해 고친다.

이 테스트는 실제로 그 프롬프트 지시를 무시하는 가짜 LLM(모델이 set_monthly_budget을 잘못 호출하는
상황을 그대로 재현)으로 안전망이 실제로 개입하는지 증명한다 — "프롬프트를 더 강하게 썼다"만으로는
증명되지 않는, 이 세션에서 반복돼 온 검증 방식이다.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import agent
import ledger
import month_state as ms
import price_history as ph
from langchain_core.messages import AIMessage


def _full_history_ending(end: date, days: int = 1600) -> list[dict]:
    """run_backtest가 필요로 하는 만큼(48개월 + 선행 구간)의 연속 확정 일봉을 합성한다 — 실제
    Upbit 네트워크 호출 없이(테스트는 결정적·빠르게 유지) end까지 이어지는 데이터를 만든다.
    가격의 실제 의미는 중요하지 않다(tests/test_backtest.py와 같은 원칙)."""
    start = end - timedelta(days=days - 1)
    price = 100_000_000.0
    records = []
    d = start
    while d <= end:
        records.append(
            {"date_kst": d.isoformat(), "open": price, "high": price * 1.001, "low": price * 0.999, "close": price}
        )
        price *= 1.0001
        d += timedelta(days=1)
    return records


@pytest.fixture(autouse=True)
def _stub_price_history_for_backtest():
    """캐시를 §7-1 기준으로 "이미 최신"인 상태로 미리 채워둔다 — 그러면 update_incremental()이
    이미 신선하다고 보고 네트워크를 전혀 타지 않는다(price_history.py의 실제 단축 경로, 흉내가
    아니라 그대로 이용)."""
    expected_end = date.fromisoformat(ph.last_confirmed_date_kst())
    ph.save_history(_full_history_ending(expected_end))
    yield


class _BoundFake:
    """bind_tools()가 매 Agent(worker graph)마다 한 번씩 불리므로, 호출마다 독립된 객체를
    돌려줘야 한다 — 안 그러면 여러 Agent가 같은 mutable 상태(호출 횟수 등)를 공유해 서로
    영향을 준다."""

    def __init__(self, parent: "_CallsSetMonthlyBudgetThenStops", tool_names: list[str]):
        self._parent = parent
        self._tool_names = tool_names

    def invoke(self, messages, *args, **kwargs):
        if "set_monthly_budget" not in self._tool_names:
            # 이 Agent에는 애초에 set_monthly_budget이 없다(실제 LLM이 bind_tools로 제한된 도구
            # 밖의 함수를 절대 호출할 수 없는 것과 같은 제약) — 무관한 답으로 마친다.
            return AIMessage(content="이 요청은 제 담당 도구와 무관합니다.")
        self._parent.calls += 1
        if self._parent.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "set_monthly_budget", "args": {"amount_krw": 3_000_000.0}, "id": "call_1"}
                ],
            )
        return AIMessage(content="월 예산 300만원으로 설정 제안을 만들었습니다.")


class _CallsSetMonthlyBudgetThenStops:
    """실제 재현: plan_agent가 시뮬레이션 요청인데도 set_monthly_budget을 호출한다(모델의 실제
    도구 선택 실수를 그대로 흉내). 두 번째 호출(도구 실행 결과를 받은 뒤)에서는 도구 호출 없이
    평범한 텍스트로 답해 루프를 끝낸다."""

    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        return _BoundFake(self, [t.name for t in tools])


def _supervisor():
    return agent.build_supervisor(llm=_CallsSetMonthlyBudgetThenStops())


def test_simulation_request_that_wrongly_triggers_budget_change_gets_replaced_with_backtest():
    run = _supervisor()
    result = run("300만원으로 4년동안 전략별 시뮬레이션 부탁해")

    # 예산 변경 제안이 사용자에게 노출되면 안 된다.
    assert result.get("budget_change_needs_confirmation") is None
    assert "예산 변경 제안" not in result["answer"]

    # 대신 실제 run_backtest 결과(3전략 비교)가 담겨야 한다.
    assert "하락일" in result["answer"] and "정기 분할" in result["answer"] and "RSI" in result["answer"]

    # month_state.json/ledger.json에는 아무 것도 반영되지 않아야 한다(제안도 취소돼 대기 상태가
    # 안 남고, 백테스트는 원래도 가상 계산이라 실제 장부를 건드리지 않는다).
    assert ms.load_state().get("budget_history", []) == []
    assert ledger.load_ledger() == []


def test_stray_budget_proposal_token_is_actually_cancelled_not_just_hidden():
    """서버가 제안을 화면에서 숨기기만 하고 토큰은 그대로 살려두면, 나중에 알 수 없는 경로로
    그 토큰이 확인돼 실제 예산이 바뀌는 위험이 남는다 — 실제로 취소 처리됐는지까지 확인한다."""
    run = _supervisor()
    run("300만원으로 4년동안 전략별 시뮬레이션 부탁해")

    # agent._LAST_BUDGET_PROPOSAL이 None으로 리셋됐는지 확인 — 취소 처리(month_state.
    # cancel_budget_change)가 실제로 이뤄져 토큰이 소진됐다는 뜻이다(그냥 응답에서 숨기기만
    # 한 게 아니다).
    assert agent._LAST_BUDGET_PROPOSAL is None


def test_genuine_budget_decision_without_simulation_words_is_not_touched():
    """이 안전망이 "예산 변경 제안이 생기면 무조건 취소"로 번지면 안 된다 — 시뮬레이션 의도
    단어가 전혀 없는 진짜 예산 설정 요청은 그대로 제안이 유지돼야 한다."""
    run = _supervisor()
    result = run("300만원으로 시작할래")

    assert result.get("budget_change_needs_confirmation") is not None
    assert result["budget_change_needs_confirmation"]["amount_krw"] == 3_000_000.0
