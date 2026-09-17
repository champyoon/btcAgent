"""agent._build_final_answer() — 순수 함수, LLM 호출 없음.

수동 테스트 중 발견(2026-09-18): "처음 쓰는데 어떤 서비스야? 매달 200만원씩 투자하고 싶어."에서
`set_monthly_budget` 호출·제안 생성까지는 정상이었는데, 최종 answer에서 도구가 반환한 적용월이
빠지고, 실제로는 없는 "확인 버튼"을 안내하고, 전략 선택을 예산 확인의 선행 조건처럼 요구하는 등
LLM이 최종 문구를 잘못 조립하는 문제가 있었다 — judge_output의 keep=true는 "내용이 있고 근거 없는
단정이 없다"만 볼 뿐 이런 정확성 문제를 전혀 잡지 못한다. 금액·적용월·미저장 상태·API 경로를
`_build_final_answer`가 도구 결과에서 직접 조립해 LLM의 문구와 무관하게 항상 정확하도록 고쳤다.

**2026-09-19 1차 수정과 그 한계**: 처음엔 "요약 블록을 덧붙였다"만으로 안전하다고 여겼는데,
`plan_agent`의 자연어 답에 "설정 완료했습니다"/"2주마다" 같은 문구가 남아있으면 요약 블록과 나란히
모순으로 노출된다는 지적을 받았다. 1차 대응은 `plan_agent` 프롬프트에서 이 정보를 서술하지 말라고
지시하는 것이었다 — 하지만 이건 **프롬프트 준수에 의존**하는 방식이라, 모델이 지시를 어기면(또는
그 지시를 놓치면) 여전히 모순이 노출될 수 있었다.

**2026-09-19 2차 수정(최종) — 프롬프트에 의존하지 않는 강제**: `_build_final_answer`가 이제
`answers_by_agent: dict[str, str]`를 직접 받아서, **예산 제안(`budget_proposal`)이 있으면
`plan_agent` 항목 자체를 결과에서 제외**한다 — plan_agent가 프롬프트를 완전히 무시하고 아무 문구를
써도(이 파일의 `test_plan_agent_text_is_replaced_even_when_it_violates_instructions`가 일부러
지시를 어긴 입력으로 이를 증명한다), 그 텍스트 자체가 애초에 최종 답변에 합쳐지지 않으므로 노출될
수 없다. 함께 요청된 서비스 소개·전략 설명(예: research_agent)은 그대로 유지된다 — 제외되는 건
plan_agent의 서술뿐이다.

이 파일은 이 **결과 계약**(plan_agent 텍스트 제외, 다른 Agent 텍스트 유지, 각 사실이 정확히 한 번만
나타남)을 pure 함수 수준에서 검증한다 — "Agent가 새 프롬프트를 실제로 따르는지"는 이제 이 결과에
영향을 주지 않으므로(따르든 안 따르든 plan_agent 텍스트는 어차피 제외됨) 별도 검증이 필요 없어졌다.
다만 이 코드 경로 자체가 실제 LLM 응답과 결합됐을 때 문제없이 동작하는지는 모델별 실 `/query`
스모크 테스트로 별도 확인한다(REPORT.md §15 — 2026-09-19 기준 Nova-pro는 확인, Haiku 4.5는 쿼터
소진으로 대기).
"""

from __future__ import annotations

import agent


def test_budget_summary_always_includes_amount_month_unsaved_and_api_paths():
    budget_proposal = {
        "confirmation_token": "tok-123",
        "amount_krw": 2_000_000,
        "effective_month": "2026-10",
        "is_initial": True,
    }
    # answers_by_agent가 비어 있어도(최악의 경우) 구조화 요약은 항상 붙어야 함
    answer = agent._build_final_answer({}, [], None, budget_proposal)

    assert "2,000,000원" in answer
    assert "2026-10" in answer
    assert "아직 저장되지 않았습니다" in answer
    assert "tok-123" in answer
    assert "POST /confirm_budget_change" in answer
    assert "POST /cancel_budget_change" in answer
    assert "확인 버튼" not in answer


def test_budget_summary_says_change_not_start_for_existing_plan():
    budget_proposal = {
        "confirmation_token": "tok-456",
        "amount_krw": 3_000_000,
        "effective_month": "2026-10",
        "is_initial": False,
    }
    answer = agent._build_final_answer({}, [], None, budget_proposal)
    assert "변경하는 제안" in answer
    assert "시작하는 제안" not in answer


def test_strategy_summary_includes_month_and_token_and_correct_label():
    strategy_proposal = {
        "confirmation_token": "tok-789",
        "year": 2026,
        "month": 10,
        "strategy": "biweekly",
    }
    answer = agent._build_final_answer({}, [], strategy_proposal, None)
    assert "2026년 10월" in answer
    assert "정기 분할" in answer
    assert "1일·15일" in answer  # "2주마다" 오해를 방지하는 라벨
    assert "tok-789" in answer
    assert "POST /confirm_strategy_change" in answer
    assert "POST /cancel_strategy_change" in answer


def test_plan_agent_text_is_dropped_but_other_agents_preserved_when_budget_proposal_exists():
    """핵심 계약: 예산 제안이 있으면 plan_agent 항목은 결과에서 완전히 제외되고, 다른 Agent(예:
    research_agent — 서비스 소개·전략 설명)는 그대로 유지된다."""
    answers_by_agent = {
        "plan_agent": "예산 제안을 만들었습니다.",
        "research_agent": "이 서비스는 BTC DCA 서비스입니다.",
    }
    budget_proposal = {
        "confirmation_token": "tok-999",
        "amount_krw": 2_000_000,
        "effective_month": "2026-10",
        "is_initial": True,
    }
    answer = agent._build_final_answer(answers_by_agent, [], None, budget_proposal)
    assert "[plan_agent]" not in answer
    assert "예산 제안을 만들었습니다." not in answer
    assert "[research_agent]" in answer
    assert "이 서비스는 BTC DCA 서비스입니다." in answer
    assert "[예산 확인 필요]" in answer


def test_plan_agent_text_is_preserved_when_no_budget_proposal():
    """budget_proposal이 없으면(예산과 무관한 질문) plan_agent 텍스트를 제외할 이유가 없다 —
    제외 로직이 예산 제안이 있을 때만 작동하는지 확인."""
    answers_by_agent = {"plan_agent": "이번 달 예산은 200만원입니다."}
    answer = agent._build_final_answer(answers_by_agent, [], None, None)
    assert "[plan_agent]" in answer
    assert "이번 달 예산은 200만원입니다." in answer


def test_no_proposals_or_approvals_and_no_answers_falls_back_to_generic_message():
    answer = agent._build_final_answer({}, [], None, None)
    assert answer == "쓸 만한 답을 만들지 못했습니다. 질문을 조금 더 구체적으로 해주세요."


def test_budget_proposal_alone_is_not_swallowed_by_empty_answers_fallback():
    """이전엔 `not answers and not approvals_needed`일 때 무조건 일반 메시지로 대체됐다 — 이제는
    예산/전략 제안이 있으면(agent 쪽 텍스트가 judge_output에 전부 걸러졌거나 애초에 제외됐더라도)
    그 제안 요약만은 반드시 살아남아야 한다."""
    budget_proposal = {
        "confirmation_token": "tok-alone",
        "amount_krw": 1_000_000,
        "effective_month": "2026-10",
        "is_initial": True,
    }
    answer = agent._build_final_answer({}, [], None, budget_proposal)
    assert "쓸 만한 답을 만들지 못했습니다" not in answer
    assert "tok-alone" in answer


def test_approval_strategy_and_budget_summaries_can_coexist():
    approvals_needed = [{"reason": "매수 기록 생성", "tool": "record_virtual_buy", "approval_id": "a1"}]
    strategy_proposal = {"confirmation_token": "s1", "year": 2026, "month": 10, "strategy": "rsi"}
    budget_proposal = {
        "confirmation_token": "b1",
        "amount_krw": 500_000,
        "effective_month": "2026-10",
        "is_initial": False,
    }
    answer = agent._build_final_answer(
        {"ledger_agent": "기록 제안"}, approvals_needed, strategy_proposal, budget_proposal
    )
    assert "[실행 전 확인 필요]" in answer
    assert "[전략 변경 확인 필요]" in answer
    assert "[예산 확인 필요]" in answer
    assert "[ledger_agent]" in answer  # 예산 제안이 있어도 plan_agent가 아닌 다른 Agent는 유지됨


def test_plan_agent_text_is_replaced_even_when_it_violates_instructions():
    """요청받은 핵심 회귀 테스트(2026-09-19): plan_agent가 프롬프트를 대놓고 어기고 "설정
    완료했습니다. 2주마다 자동으로 매수합니다." 같은 틀린 문구를 냈다고 가정해도, 그 문구는
    최종 답변에 전혀 남지 않고 올바른 구조화 예산 안내만 노출돼야 한다 — 이건 plan_agent가
    "우연히 프롬프트를 잘 따라서"가 아니라 코드가 그 텍스트 자체를 애초에 제외하기 때문이다."""
    answers_by_agent = {
        "plan_agent": "설정 완료했습니다. 2주마다 자동으로 매수합니다.",
        "research_agent": (
            "이 서비스는 BTC를 매달 정한 예산으로 모으도록 돕는 서비스입니다. 매달 1일에 월 예산의 "
            "절반을 조건 없이 정액 매수하고, 나머지 절반은 하락일·정기 분할(매월 1일·15일)·RSI 매수 "
            "중 사용자가 고른 방식으로 매수합니다. 실제 매수는 사용자가 직접 신고합니다."
        ),
    }
    budget_proposal = {
        "confirmation_token": "tok-violation",
        "amount_krw": 2_000_000,
        "effective_month": "2026-10",
        "is_initial": True,
    }
    answer = agent._build_final_answer(answers_by_agent, [], None, budget_proposal)

    # 지시를 어긴 plan_agent 문구는 전혀 남지 않는다
    for forbidden in ("설정 완료", "2주마다", "격주", "자동으로 매수", "자동 주문", "확인 버튼"):
        assert forbidden not in answer, f"금지된 표현이 노출됨(plan_agent 텍스트가 제외되지 않음): {forbidden!r}"
    assert "[plan_agent]" not in answer

    # 함께 요청된 서비스 소개·전략 설명(확정 규칙 근거)은 그대로 유지된다
    assert "[research_agent]" in answer
    assert "매월 1일·15일" in answer

    # 올바른 구조화 예산 안내만 정확히 한 번씩 노출된다
    assert answer.count("2,000,000원") == 1
    assert answer.count("2026-10") == 1
    assert answer.count("아직 저장되지 않았습니다") == 1
    assert answer.count("POST /confirm_budget_change") == 1
    assert answer.count("POST /cancel_budget_change") == 1


def test_normal_well_formed_input_still_has_no_duplication():
    """정상적인 입력(plan_agent가 프롬프트대로 짧게만 답한 경우)에서도 중복이 없는지 유지 확인 —
    plan_agent 텍스트가 애초에 제외되므로, 중복 여지 자체가 구조적으로 없다."""
    answers_by_agent = {
        "plan_agent": "예산 설정 제안을 만들었습니다.",
        "research_agent": (
            "이 서비스는 BTC를 매달 정한 예산으로 모으도록 돕는 서비스입니다. 매달 1일에 월 예산의 "
            "절반을 조건 없이 정액 매수하고, 나머지 절반은 하락일·정기 분할(매월 1일·15일)·RSI 매수 "
            "중 사용자가 고른 방식으로 매수합니다. 실제 매수는 사용자가 직접 신고합니다."
        ),
    }
    budget_proposal = {
        "confirmation_token": "tok-once",
        "amount_krw": 2_000_000,
        "effective_month": "2026-10",
        "is_initial": True,
    }
    answer = agent._build_final_answer(answers_by_agent, [], None, budget_proposal)

    assert answer.count("2,000,000원") == 1
    assert answer.count("2026-10") == 1
    assert answer.count("아직 저장되지 않았습니다") == 1
    assert answer.count("POST /confirm_budget_change") == 1
    assert answer.count("POST /cancel_budget_change") == 1

    for forbidden in ("설정 완료", "2주마다", "격주", "자동으로 매수", "자동 주문", "확인 버튼"):
        assert forbidden not in answer, f"금지된 표현이 노출됨: {forbidden!r}"

    assert "[research_agent]" in answer
    assert "매월 1일·15일" in answer
