"""후속 입력 상태(awaiting_input) — 실사용 UI 신고(2026-09-20, #1) 회귀 테스트.

재현: "자 뭐부터 시작하면 돼?" → plan_agent가 월 예산 금액을 물음 → 사용자가 "200만원"이라고만
답함 → 이 API는 무상태라 "200만원"은 어떤 라우팅 키워드에도 안 걸려 범위 밖으로 거절됐다.

이 파일은 LLM 호출 없이(_NoInvokeLLM) `run()`의 awaiting_input 직접 처리 경로만 검증한다 —
request_monthly_budget_amount가 토큰을 발급하고, 그 토큰이 함께 온 다음 요청의 "200만원"을
route_question을 거치지 않고 곧장 예산 제안으로 연결하는지, 그리고 다른 주제로 넘어가면 상태가
해제되는지를 확인한다. 실제 LLM이 이 두 도구(request_monthly_budget_amount/set_monthly_budget)를
올바른 순서로 호출하는지는 실 /query 검증(REPORT.md 참고)으로 별도 확인한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent
import month_state as ms


class _NoInvokeLLM:
    """이 경로가 실수로라도 LLM을 호출하면 즉시 실패하게 만드는 가짜 LLM. bind_tools는 그래프
    구성 시점에 호출되므로 자기 자신을 돌려주기만 하면 된다."""

    def bind_tools(self, tools):
        return self

    def invoke(self, *args, **kwargs):  # pragma: no cover - 호출되면 안 됨
        raise AssertionError("이 경로는 LLM을 호출하면 안 됩니다 — awaiting_input 직접 처리 경로")


def _supervisor():
    return agent.build_supervisor(llm=_NoInvokeLLM())


# ── _parse_krw_amount ────────────────────────────────────────────────────


def test_parse_krw_amount_handles_all_three_notations():
    assert agent._parse_krw_amount("200만원") == 2_000_000.0
    assert agent._parse_krw_amount("200만 원") == 2_000_000.0
    assert agent._parse_krw_amount("2,000,000원") == 2_000_000.0
    assert agent._parse_krw_amount("1만원") == 10_000.0


def test_parse_krw_amount_returns_none_when_no_amount_present():
    assert agent._parse_krw_amount("오늘 날씨 어때?") is None
    assert agent._parse_krw_amount("") is None


# ── 전체 흐름: request_monthly_budget_amount → "200만원" ──────────────────


def test_bare_amount_with_valid_awaiting_token_creates_budget_proposal_without_llm():
    # request_monthly_budget_amount는 문자열을 반환하므로 토큰은 _LAST_AWAITING_INPUT에서 읽는다.
    agent.request_monthly_budget_amount.invoke({})
    token = agent._LAST_AWAITING_INPUT["awaiting_input_token"]
    assert token in agent._PENDING_AWAITING_INPUT

    run = _supervisor()
    result = run("200만원", awaiting_input_token=token)

    assert result["agents_used"] == ["plan_agent"]
    proposal = result.get("budget_change_needs_confirmation")
    assert proposal is not None, "amount가 파싱됐는데 budget_change_needs_confirmation이 없음"
    assert proposal["amount_krw"] == 2_000_000.0
    assert "confirmation_token" in proposal
    # 확인 전이므로 아직 저장되지 않아야 한다.
    assert not ms.STATE_PATH.exists() or ms.load_state().get("plan_start_month") is None
    # 토큰은 1회용 — 소진 후에는 남아있으면 안 된다.
    assert token not in agent._PENDING_AWAITING_INPUT
    awaiting_step = next(t for t in result["trace"] if t["step"] == "awaiting_input")
    assert awaiting_step["output"]["amount_parsed"] == 2_000_000.0


def test_bare_amount_notation_variants_all_resolve_via_awaiting_token():
    for text, expected in [("200만 원", 2_000_000.0), ("2,000,000원", 2_000_000.0)]:
        agent.request_monthly_budget_amount.invoke({})
        token = agent._LAST_AWAITING_INPUT["awaiting_input_token"]
        run = _supervisor()
        result = run(text, awaiting_input_token=token)
        proposal = result["budget_change_needs_confirmation"]
        assert proposal["amount_krw"] == expected, f"{text!r} -> {proposal['amount_krw']}"


def test_confirming_the_proposal_actually_saves_it():
    agent.request_monthly_budget_amount.invoke({})
    token = agent._LAST_AWAITING_INPUT["awaiting_input_token"]
    run = _supervisor()
    result = run("200만원", awaiting_input_token=token)
    confirmation_token = result["budget_change_needs_confirmation"]["confirmation_token"]

    confirm_result = agent.confirm_budget_change_action(confirmation_token)
    assert confirm_result["http_status"] == 200
    assert ms.load_state()["budget_history"][0]["amount_krw"] == 2_000_000.0


def test_cancelling_the_proposal_leaves_budget_unchanged():
    agent.request_monthly_budget_amount.invoke({})
    token = agent._LAST_AWAITING_INPUT["awaiting_input_token"]
    run = _supervisor()
    result = run("200만원", awaiting_input_token=token)
    confirmation_token = result["budget_change_needs_confirmation"]["confirmation_token"]

    cancel_result = agent.cancel_budget_change_action(confirmation_token)
    assert cancel_result["http_status"] == 200
    assert not ms.STATE_PATH.exists()


# ── 맥락 없는 금액 입력은 여전히 무시된다 ───────────────────────────────────


def test_bare_amount_without_awaiting_token_is_not_treated_as_budget_setting():
    """awaiting_input_token 없이 "200만원"만 보내면(새 세션에서 곧바로 입력한 경우) 임의로 예산
    설정으로 해석하면 안 된다 — 요청사항: "맥락 없이 입력한 금액을 무조건 예산 설정으로 해석하지
    마세요."""
    run = _supervisor()
    result = run("200만원")
    assert "budget_change_needs_confirmation" not in result
    assert result["agents_used"] == []


def test_reusing_an_already_consumed_awaiting_token_does_not_create_second_proposal():
    """같은 토큰을 두 번 보내도(UI 재실행/중복 전송) 제안이 두 번 생기면 안 된다 — 첫 사용으로
    이미 소진됐으므로 두 번째는 토큰이 없는 것과 동일하게 처리된다."""
    agent.request_monthly_budget_amount.invoke({})
    token = agent._LAST_AWAITING_INPUT["awaiting_input_token"]
    run = _supervisor()
    first = run("200만원", awaiting_input_token=token)
    assert "budget_change_needs_confirmation" in first

    second = run("200만원", awaiting_input_token=token)
    assert "budget_change_needs_confirmation" not in second, "이미 소진된 토큰인데 제안이 다시 생성됨"


# ── 다른 주제로 전환하면 상태가 해제된다 ────────────────────────────────────


def test_switching_topic_while_awaiting_amount_clears_the_state_and_routes_normally():
    """예산 금액을 기다리는 중에 금액이 아닌 다른 말(예: 매수 시기 질문)을 하면, 그 토큰은
    소진되고 정상적으로 다른 라우팅을 타야 한다 — 요청사항: "다른 주제·취소·입력 완료 시 후속
    입력 상태를 적절히 해제해주세요.\""""
    agent.request_monthly_budget_amount.invoke({})
    token = agent._LAST_AWAITING_INPUT["awaiting_input_token"]
    run = _supervisor()
    # "지금이 매수 타이밍이야?"는 route_question만으로 price_agent에 매칭되는 문장이지만,
    # 실제 도구 호출까지 확인하려면 LLM이 필요하다 — 여기서는 "이 경로가 LLM을 부르지 않았다"는
    # 확인만 한다(_NoInvokeLLM). LLM을 실제로 부르는 경로인지 자체를 확인하려면 route에만 걸리는
    # 문장이 아니라, 라우팅 결과가 빈 목록인 문장으로 "LLM 자체가 안 불림"까지 함께 확인한다.
    result = run("오늘 날씨 어때?", awaiting_input_token=token)
    assert "budget_change_needs_confirmation" not in result
    assert result["agents_used"] == []  # 진짜 범위 밖 질문이라 정상적으로 거절됨
    assert token not in agent._PENDING_AWAITING_INPUT, "다른 주제로 넘어갔는데 토큰이 그대로 남음"


def test_unknown_or_expired_awaiting_token_is_silently_ignored():
    """서버 재시작 등으로 사라진 토큰이 오면(알려진 한계, 다른 confirmation_token들과 동일) 에러를
    내지 않고 그냥 토큰이 없는 것처럼 정상 라우팅으로 진행해야 한다."""
    run = _supervisor()
    result = run("200만원", awaiting_input_token="no-such-token")
    assert "budget_change_needs_confirmation" not in result
    assert result["agents_used"] == []


# ── 월 불일치(요청 월 vs 적용월) — 실사용 UI 신고 #3 ─────────────────────────


def test_propose_budget_change_flags_month_mismatch_when_requested_month_differs():
    ms.init_plan(1_000_000.0, now=__import__("datetime").datetime(2026, 8, 15, tzinfo=ms.KST))
    proposal = ms.propose_budget_change(
        2_000_000.0, requested_month="2026-09",
        now=__import__("datetime").datetime(2026, 9, 5, tzinfo=ms.KST),
    )
    assert proposal["ok"]
    assert proposal["effective_month"] == "2026-10"  # 정책: 항상 다음 달부터
    assert proposal["requested_month"] == "2026-09"
    assert proposal["month_mismatch"] is True


def test_propose_budget_change_no_mismatch_when_requested_month_matches_policy():
    ms.init_plan(1_000_000.0, now=__import__("datetime").datetime(2026, 8, 15, tzinfo=ms.KST))
    proposal = ms.propose_budget_change(
        2_000_000.0, requested_month="2026-10",
        now=__import__("datetime").datetime(2026, 9, 5, tzinfo=ms.KST),
    )
    assert proposal["month_mismatch"] is False


def test_propose_budget_change_no_mismatch_field_set_when_requested_month_omitted():
    proposal = ms.propose_budget_change(2_000_000.0)  # requested_month 생략 — 기존 Swagger 호출과 동일
    assert proposal["requested_month"] is None
    assert proposal["month_mismatch"] is False


def test_final_answer_explains_month_mismatch_reason_and_alternative():
    budget_proposal = {
        "confirmation_token": "tok-1", "amount_krw": 2_000_000.0, "effective_month": "2026-10",
        "is_initial": False, "requested_month": "2026-09", "month_mismatch": True,
    }
    answer = agent._build_final_answer({}, [], None, budget_proposal)
    assert "2026-09" in answer and "적용할 수 없습니다" in answer
    assert "2026-10" in answer


def test_final_answer_omits_mismatch_line_when_months_match():
    budget_proposal = {
        "confirmation_token": "tok-1", "amount_krw": 2_000_000.0, "effective_month": "2026-10",
        "is_initial": False, "requested_month": "2026-10", "month_mismatch": False,
    }
    answer = agent._build_final_answer({}, [], None, budget_proposal)
    assert "적용할 수 없습니다" not in answer


def test_set_monthly_budget_tool_passes_requested_month_through_to_proposal():
    ms.init_plan(1_000_000.0, now=__import__("datetime").datetime(2026, 8, 15, tzinfo=ms.KST))
    result_text = agent.set_monthly_budget.invoke(
        {"amount_krw": 2_000_000.0, "requested_year": 2026, "requested_month": 9}
    )
    assert "제안 실패" not in result_text
    assert agent._LAST_BUDGET_PROPOSAL["requested_month"] is not None
    assert agent._LAST_BUDGET_PROPOSAL["month_mismatch"] in (True, False)


def test_set_monthly_budget_tool_without_requested_month_is_unaffected():
    """기존 Swagger 단독 질문 호환 — requested_year/requested_month를 안 주면 이전과 동일하게
    동작해야 한다(요청사항)."""
    result_text = agent.set_monthly_budget.invoke({"amount_krw": 1_500_000.0})
    assert "제안 실패" not in result_text
    assert agent._LAST_BUDGET_PROPOSAL["requested_month"] is None
    assert agent._LAST_BUDGET_PROPOSAL["month_mismatch"] is False
