"""인사·기능 소개 요청에 대한 고정 소개 응답 — 실사용 UI 신고(2026-09-20, #5).

재현: "안녕"/"넌 무슨일을 할수있니"가 어느 Agent 키워드에도 안 걸려, 다른 진짜 범위 밖 질문과
똑같이 "이 어시스턴트는 BTC 예산·전략 계획·시세·지표·매수 기록에 대해서만 답할 수 있습니다"로
거절됐다 — 처음 쓰는 사용자가 이 서비스로 뭘 할 수 있는지 전혀 감을 못 잡았다.

어느 Agent도 이 메타 질문(서비스 자체에 대한 질문)을 소유하지 않으므로, LLM/그래프를 전혀
거치지 않고 서버가 고정된 소개 문구를 직접 반환한다 — `_NoInvokeLLM`으로 LLM이 절대 호출되지
않음을 직접 증명한다.
"""

from __future__ import annotations

import agent


class _NoInvokeLLM:
    def bind_tools(self, tools):
        return self

    def invoke(self, *args, **kwargs):  # pragma: no cover - 호출되면 안 됨
        raise AssertionError("인사/기능 소개 경로는 LLM을 호출하면 안 됩니다")


def _supervisor():
    return agent.build_supervisor(llm=_NoInvokeLLM())


def test_greeting_and_capability_questions_get_fixed_intro_without_llm_call():
    run = _supervisor()
    phrasings = [
        "안녕",
        "안녕하세요",
        "넌 무슨일을 할수있니",
        "너는 무슨 일을 할 수 있어?",
        "뭘 할 수 있어?",
        "어떤 도움을 줄 수 있어?",
    ]
    for q in phrasings:
        result = run(q)
        assert result["agents_used"] == [], f"인사/기능 소개 질문인데 Agent가 매칭됨: {q!r}"
        assert result["approvals_needed"] == []
        assert result["answer"] == agent._CAPABILITY_INTRO_TEXT, f"고정 소개 문구가 아님: {q!r}"


def test_capability_intro_mentions_all_four_domains():
    intro = agent._CAPABILITY_INTRO_TEXT
    for keyword in ("예산", "전략", "시세", "지표", "매수 기록"):
        assert keyword in intro, f"소개 문구에 {keyword!r}가 빠짐"


def test_genuinely_out_of_scope_questions_still_get_the_generic_rejection():
    """이 수정이 "범위 밖 질문 전부"를 소개 문구로 바꾸는 쪽으로 번지면 안 된다 — 진짜 범위 밖
    질문은 기존처럼 일반 거절 문구를 받아야 한다."""
    run = _supervisor()
    result = run("오늘 날씨 어때?")
    assert result["agents_used"] == []
    assert result["answer"] != agent._CAPABILITY_INTRO_TEXT
    assert "BTC 예산·전략 계획·시세·지표·매수 기록" in result["answer"]


def test_question_that_matches_a_real_agent_is_not_hijacked_by_greeting_check():
    """"안녕하세요, 이번 달 예산 얼마 남았어?"처럼 인사말 뒤에 진짜 질문이 붙어 있으면, 실제로
    매칭되는 Agent(plan_agent)가 있으니 고정 소개 문구로 대체되면 안 된다 — 소개 문구 분기는
    agents가 완전히 비었을 때만 확인한다."""
    matched = agent.route_question("안녕하세요, 이번 달 예산 얼마 남았어?")
    assert "plan_agent" in matched
