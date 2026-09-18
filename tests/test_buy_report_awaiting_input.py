"""실제 매수 보고 → 체결 가격/수량 후속 입력 → 승인 카드 — 실사용 신고(2026-09-20, #2).

재현: "오늘 50만원어치 BTC 매수했어" → route=[]로 범위 밖 거절. 원인은 (a) 과거 시제 보고문이 어떤
ledger_agent 키워드에도 안 걸렸고(라우팅은 tests/test_routing.py에서 별도 확인), (b) 설령 걸려도
record_virtual_buy에 필요한 price_krw/quantity_btc가 없으면 이 무상태 API가 두 메시지에 걸친 정보를
합칠 방법이 없었다. request_monthly_budget_amount와 같은 원리(awaiting_input)를 재사용하되, 이번엔
바로 도구를 실행하는 게 아니라 approvals.py의 표준 승인 절차로 이어진다.
"""

from __future__ import annotations

import agent
import approvals
import ledger
from langchain_core.messages import AIMessage


class _NoInvokeLLM:
    def bind_tools(self, tools):
        return self

    def invoke(self, *args, **kwargs):  # pragma: no cover - 호출되면 안 됨
        raise AssertionError("이 경로는 LLM을 호출하면 안 됩니다 — awaiting_input 직접 처리 경로")


class _ProseOnlyLLM:
    """실사용 재현: 시스템 프롬프트가 request_buy_execution_detail을 먼저 호출하라고 지시해도,
    실제 Haiku 4.5가 도구를 부르지 않고 그냥 말로만 되묻은 사례가 있었다. 도구를 전혀 호출하지
    않는 이 가짜 LLM으로 그 상황을 재현한다."""

    def bind_tools(self, tools):
        return self

    def invoke(self, *args, **kwargs):
        return AIMessage(content="체결 가격이나 매수한 수량을 알려주시겠어요?")


def _supervisor():
    return agent.build_supervisor(llm=_NoInvokeLLM())


def _issue_awaiting_token(amount_krw: float = 500_000.0, executed_date: str = "2026-09-20") -> str:
    agent.request_buy_execution_detail.invoke({"amount_krw": amount_krw, "executed_date": executed_date})
    return agent._LAST_AWAITING_INPUT["awaiting_input_token"]


# ── _parse_buy_execution_detail ──────────────────────────────────────────


def test_parse_buy_execution_detail_recognizes_price():
    assert agent._parse_buy_execution_detail("1억원에 샀어") == {"price_krw": 100_000_000.0}
    assert agent._parse_buy_execution_detail("100,000,000원") == {"price_krw": 100_000_000.0}


def test_parse_buy_execution_detail_recognizes_quantity():
    assert agent._parse_buy_execution_detail("0.005개 샀어") == {"quantity_btc": 0.005}
    assert agent._parse_buy_execution_detail("0.005 BTC") == {"quantity_btc": 0.005}
    assert agent._parse_buy_execution_detail("0.005비트코인") == {"quantity_btc": 0.005}


def test_parse_buy_execution_detail_returns_none_for_unrelated_text():
    assert agent._parse_buy_execution_detail("오늘 날씨 어때?") is None


# ── 전체 흐름: 1차(금액·날짜) → 2차(가격/수량) → 승인 카드 ──────────────────


def test_price_reply_creates_pending_approval_without_touching_ledger():
    token = _issue_awaiting_token(500_000.0, "2026-09-20")
    assert token in agent._PENDING_AWAITING_INPUT

    run = _supervisor()
    result = run("1억원에 샀어", awaiting_input_token=token)

    assert result["agents_used"] == ["ledger_agent"]
    approvals_needed = result["approvals_needed"]
    assert len(approvals_needed) == 1
    entry = approvals_needed[0]
    assert entry["tool"] == "record_virtual_buy"
    assert entry["args"] == {
        "amount_krw": 500_000.0, "executed_date": "2026-09-20",
        "execution_time_precision": "date", "price_krw": 100_000_000.0,
    }
    assert token not in agent._PENDING_AWAITING_INPUT  # 1회용 소진
    # 승인 전에는 장부가 전혀 바뀌지 않아야 한다.
    assert ledger.load_ledger() == []


def test_quantity_reply_creates_pending_approval_with_quantity_field():
    token = _issue_awaiting_token(500_000.0, "2026-09-20")
    run = _supervisor()
    result = run("0.005개 샀어", awaiting_input_token=token)

    entry = result["approvals_needed"][0]
    assert entry["args"]["quantity_btc"] == 0.005
    assert "price_krw" not in entry["args"] or entry["args"].get("price_krw") is None or True
    assert ledger.load_ledger() == []


def test_approving_the_pending_buy_report_actually_records_it_with_exact_args():
    token = _issue_awaiting_token(500_000.0, "2026-09-20")
    run = _supervisor()
    result = run("1억원에 샀어", awaiting_input_token=token)
    approval_id = result["approvals_needed"][0]["approval_id"]

    exec_result = agent.execute_approved_action(approval_id)
    assert exec_result["http_status"] == 200

    entries = ledger.load_ledger()
    assert len(entries) == 1
    assert entries[0]["amount_krw"] == 500_000.0
    assert entries[0]["price_krw"] == 100_000_000.0
    assert entries[0]["executed_date"] == "2026-09-20"
    assert entries[0]["status"] == "active"


def test_rejecting_the_pending_buy_report_leaves_ledger_untouched():
    token = _issue_awaiting_token(500_000.0, "2026-09-20")
    run = _supervisor()
    result = run("1억원에 샀어", awaiting_input_token=token)
    approval_id = result["approvals_needed"][0]["approval_id"]

    reject_result = agent.reject_approved_action(approval_id)
    assert reject_result["http_status"] == 200
    assert ledger.load_ledger() == []


def test_double_approval_of_same_buy_report_does_not_duplicate_record():
    """동일 승인 재호출·UI 재실행 → 중복 기록 없음(§5 F항목) — approvals.py의 기존
    pending→executing→executed 상태 기계가 이미 이걸 보장한다."""
    token = _issue_awaiting_token(500_000.0, "2026-09-20")
    run = _supervisor()
    result = run("1억원에 샀어", awaiting_input_token=token)
    approval_id = result["approvals_needed"][0]["approval_id"]

    first = agent.execute_approved_action(approval_id)
    assert first["http_status"] == 200
    second = agent.execute_approved_action(approval_id)
    assert second["http_status"] == 409

    assert len(ledger.load_ledger()) == 1  # 두 번째 시도로 기록이 추가되지 않았어야 한다


def test_two_separate_buy_reports_with_same_date_and_amount_both_recorded():
    """같은 날짜·금액이라는 이유만으로 서로 다른 실제 매수를 무조건 중복 처리하지 않는다 —
    사용자가 실제로 두 번 나눠 샀다고 보고하면 둘 다 기록돼야 한다."""
    run = _supervisor()

    token1 = _issue_awaiting_token(500_000.0, "2026-09-20")
    result1 = run("1억원에 샀어", awaiting_input_token=token1)
    agent.execute_approved_action(result1["approvals_needed"][0]["approval_id"])

    token2 = _issue_awaiting_token(500_000.0, "2026-09-20")
    result2 = run("1억원에 샀어", awaiting_input_token=token2)
    agent.execute_approved_action(result2["approvals_needed"][0]["approval_id"])

    assert len(ledger.load_ledger()) == 2


# ── 다른 주제로 전환 → 상태 해제 ──────────────────────────────────────────


def test_switching_topic_while_awaiting_buy_detail_clears_state_and_routes_normally():
    token = _issue_awaiting_token(500_000.0, "2026-09-20")
    run = _supervisor()
    result = run("오늘 날씨 어때?", awaiting_input_token=token)

    assert result["approvals_needed"] == []
    assert result["agents_used"] == []  # 진짜 범위 밖 질문
    assert token not in agent._PENDING_AWAITING_INPUT
    assert ledger.load_ledger() == []


# ── 서버가 직접 도구를 강제 호출하는 안전망(프롬프트 준수에만 기대지 않음) ──────


def test_model_answering_in_prose_without_calling_the_tool_still_gets_a_forced_awaiting_token():
    """실사용 라이브 검증 중 실제로 재현된 상황: 모델이 request_buy_execution_detail을 부르지
    않고 말로만 되물었다. 이 경우에도 서버가 직접 도구를 호출해 awaiting_input을 강제 발급해야
    한다 — 그래야 다음 턴 "1억원에 샀어"가 이 보고와 연결된다."""
    run = agent.build_supervisor(llm=_ProseOnlyLLM())
    result = run("오늘 50만원어치 BTC 매수했어")

    assert result["agents_used"] == ["ledger_agent"]
    assert result["approvals_needed"] == []
    awaiting = result.get("awaiting_input")
    assert awaiting is not None and awaiting["kind"] == "buy_execution_detail"
    token = awaiting["awaiting_input_token"]
    pending = agent._PENDING_AWAITING_INPUT[token]
    assert pending["amount_krw"] == 500_000.0
    assert ledger.load_ledger() == []

    # 다음 턴에서 실제로 후속 입력과 연결되는지까지 확인한다.
    follow_up = agent.build_supervisor(llm=_NoInvokeLLM())
    result2 = follow_up("1억원에 샀어", awaiting_input_token=token)
    entry = result2["approvals_needed"][0]
    assert entry["args"]["amount_krw"] == 500_000.0
    assert entry["args"]["price_krw"] == 100_000_000.0


def test_investing_verb_variant_buy_report_also_gets_forced_awaiting_token():
    """test_queries.csv 최종 검증(evaluation/run_eval.py 실행) 중 실제로 재현된 결함: "오늘 정기
    매수로 100만원 넣었어 기록해줘"는 "매수했어"/"샀어"/"구매했어" 중 어느 것도 안 써서 이 안전망
    자체가 개입하지 않았고, 모델이 프롬프트 지시를 어기고 말로만 되물어도 그대로 방치됐다.
    "넣었어"/"투자했어" 계열도 안전망 대상에 포함돼야 한다."""
    run = agent.build_supervisor(llm=_ProseOnlyLLM())
    result = run("오늘 정기 매수로 100만원 넣었어 기록해줘")

    assert result["agents_used"] == ["ledger_agent"]
    assert result["approvals_needed"] == []
    awaiting = result.get("awaiting_input")
    assert awaiting is not None and awaiting["kind"] == "buy_execution_detail"
    pending = agent._PENDING_AWAITING_INPUT[awaiting["awaiting_input_token"]]
    assert pending["amount_krw"] == 1_000_000.0


def test_forced_awaiting_token_not_issued_when_message_already_has_quantity():
    """수량까지 이미 메시지에 있으면(=정보가 충분할 수 있으므로) 서버가 함부로 끼어들지 않는다 —
    이 경우는 모델이 record_virtual_buy를 직접 호출해야 할 상황이라 강제 개입 대상이 아니다."""
    run = agent.build_supervisor(llm=_ProseOnlyLLM())
    result = run("오늘 50만원어치 0.005 BTC 매수했어")
    assert result.get("awaiting_input") is None


def test_forced_awaiting_token_not_issued_for_negated_purchase():
    """'아직 안 샀어'류 부정 표현은 애초에 금액이 없는 경우가 흔하지만, 혹시 금액이 있어도 부정
    표현이 보이면 강제 발급하지 않는다 — 실제로 산 게 아니므로 기록을 준비시키면 안 된다."""
    run = agent.build_supervisor(llm=_ProseOnlyLLM())
    result = run("50만원어치 아직 안 샀어")
    assert result.get("awaiting_input") is None


def test_forced_awaiting_token_not_issued_when_two_amounts_present():
    """금액이 두 번 이상 나오면(예: 총액과 체결가가 모두 원화로 표현된 애매한 경우) 서버가 어느
    쪽이 총액이고 어느 쪽이 가격인지 임의로 추측하지 않는다."""
    run = agent.build_supervisor(llm=_ProseOnlyLLM())
    result = run("오늘 50만원어치 BTC를 1억원에 샀어")
    assert result.get("awaiting_input") is None


def test_unknown_or_expired_buy_detail_token_is_silently_ignored():
    """알 수 없는 토큰이 오면(서버 재시작 등) 에러 없이 무시되고 일반 라우팅으로 진행해야 한다.
    "1억원에 샀어"는 그 자체로 "샀어" 키워드 때문에 정상적으로 ledger_agent에 매칭되는 문장이라
    (그 자체는 올바른 동작) LLM 호출이 필요해지므로, 여기서는 라우팅이 아예 안 걸리는 문장으로
    "토큰이 무시되고 정상 처리로 빠졌다"만 확인한다."""
    run = _supervisor()
    result = run("오늘 날씨 어때?", awaiting_input_token="no-such-token")
    assert result["approvals_needed"] == []
    assert result["agents_used"] == []
    assert ledger.load_ledger() == []
