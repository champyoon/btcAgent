"""가짜 API 응답으로 Streamlit UI 흐름을 검증한다 (요청 6번 항목).

streamlit.testing.v1.AppTest로 실제 브라우저 없이 스크립트를 구동하고, api_client.requests.post/
get만 몽키패치해 백엔드를 흉내 낸다 — app.py/agent.py는 전혀 뜨지 않으므로 실제 Bedrock 호출도,
실제 data/*.json 변경도 없다. 실제 백엔드 연동 검증은 이 파일이 아니라 UI_GUIDE.md에 기록된
수동/스크립트 절차(격리된 BTC_AGENT_DATA_DIR)로 별도 수행한다.

이 테스트들은 `tests/`(pytest, 143개 결정적 계산 테스트)와는 다른 범주라 별도 디렉터리에 뒀다 —
CLAUDE.md/REPORT.md가 추적하는 그 카운트에 섞이지 않도록 의도적으로 분리했다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import requests
from streamlit.testing.v1 import AppTest

UI_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(UI_DIR))

APP_PATH = str(UI_DIR / "streamlit_app.py")


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _run_with_query_response(body: dict, question: str = "테스트 질문") -> AppTest:
    with patch("api_client.requests.post", return_value=_FakeResponse(200, body)):
        at = AppTest.from_file(APP_PATH, default_timeout=10)
        at.run()
        at.chat_input[0].set_value(question).run()
    assert not at.exception, f"query 단계에서 예외 발생: {at.exception}"
    return at


def _last_message(at: AppTest) -> dict:
    return at.session_state["messages"][-1]


# ── 1. 일반 답변 ──────────────────────────────────────────────────────────


def test_plain_answer_renders_without_action_cards():
    body = {
        "answer": "현재가는 1억원입니다.",
        "contexts": [], "trace": [], "agents_used": ["price_agent"], "approvals_needed": [],
    }
    at = _run_with_query_response(body)
    assert any("현재가는 1억원입니다." in m.value for m in at.markdown)
    msg = _last_message(at)
    assert "budget" not in msg["actions"]
    assert "strategy" not in msg["actions"]
    assert msg["actions"]["approvals"] == {}


# ── 2. 예산 확인/취소 ─────────────────────────────────────────────────────


def _budget_query_body(token: str = "tok-budget") -> dict:
    return {
        "answer": "[예산 확인 필요] 200만원 제안입니다.",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": token, "amount_krw": 2000000.0,
            "effective_month": "2026-10", "is_initial": True,
        },
    }


def test_budget_confirm_success_shows_amount_and_hides_buttons():
    at = _run_with_query_response(_budget_query_body())
    msg = _last_message(at)
    confirm_btn = at.get_by_key(f"budget_confirm_{msg['id']}")

    confirm_response = {"ok": True, "plan_start_month": "2026-10", "monthly_budget_krw": 2000000.0}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, confirm_response)) as mock_post:
        confirm_btn.click().run()

    assert not at.exception
    mock_post.assert_called_once_with(
        "http://localhost:8000/confirm_budget_change",
        json={"confirmation_token": "tok-budget"},
        timeout=15.0,
    )
    assert msg["actions"]["budget"]["status"] == "confirmed"
    assert any("2,000,000원" in s.value for s in at.success)
    assert not any(b.key == confirm_btn.key for b in at.button), "확인 후에도 확인 버튼이 남아있음(중복 실행 위험)"


def test_budget_cancel_leaves_status_cancelled_and_does_not_call_confirm():
    at = _run_with_query_response(_budget_query_body())
    msg = _last_message(at)
    cancel_btn = at.get_by_key(f"budget_cancel_{msg['id']}")

    cancel_response = {"ok": True, "amount_krw": 2000000.0, "effective_month": "2026-10", "is_initial": True}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, cancel_response)) as mock_post:
        cancel_btn.click().run()

    assert mock_post.call_args[0][0] == "http://localhost:8000/cancel_budget_change"
    assert msg["actions"]["budget"]["status"] == "cancelled"
    assert any("취소했습니다" in s.value for s in at.success)


def test_budget_confirm_409_conflict_shows_warning_not_success():
    at = _run_with_query_response(_budget_query_body())
    msg = _last_message(at)
    confirm_btn = at.get_by_key(f"budget_confirm_{msg['id']}")

    with patch("api_client.requests.post", return_value=_FakeResponse(409, {"detail": "이미 처리된 확인 토큰입니다."})):
        confirm_btn.click().run()

    assert msg["actions"]["budget"]["status"] == "conflict"
    assert not at.success  # 성공 문구는 없어야 함
    assert any("이미 처리" in w.value for w in at.warning)


def test_budget_confirm_404_not_found_shows_error_and_no_retry_button_loop():
    at = _run_with_query_response(_budget_query_body())
    msg = _last_message(at)
    confirm_btn = at.get_by_key(f"budget_confirm_{msg['id']}")

    with patch("api_client.requests.post", return_value=_FakeResponse(404, {"detail": "존재하지 않는 확인 토큰입니다."})):
        confirm_btn.click().run()

    assert msg["actions"]["budget"]["status"] == "not_found"
    assert any("서버가 더 이상 알지 못합니다" in e.value for e in at.error)


def test_budget_confirm_timeout_is_unknown_not_error_and_offers_manual_retry():
    at = _run_with_query_response(_budget_query_body())
    msg = _last_message(at)
    confirm_btn = at.get_by_key(f"budget_confirm_{msg['id']}")

    with patch("api_client.requests.post", side_effect=requests.exceptions.Timeout("timed out")):
        confirm_btn.click().run()

    assert msg["actions"]["budget"]["status"] == "unknown_timeout"
    assert not at.success
    assert not at.error  # "실패 확정"이 아니라 warning이어야 함
    assert any("시간 초과" in w.value for w in at.warning)

    retry_btn = at.get_by_key(f"budget_{msg['id']}_retry")
    confirm_response = {"ok": True, "plan_start_month": "2026-10", "monthly_budget_krw": 2000000.0}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, confirm_response)) as mock_post:
        retry_btn.click().run()

    # 재시도는 이전에 시도했던 것과 동일한 액션(확인)이어야 한다 — 취소가 나가면 안 된다.
    assert mock_post.call_args[0][0] == "http://localhost:8000/confirm_budget_change"
    assert msg["actions"]["budget"]["status"] == "confirmed"


def test_budget_confirm_double_click_only_sends_one_request():
    """같은 런에서 버튼을 두 번 누르는 것을 직접 흉내낼 순 없지만(스트림릿 실행 모델상 클릭마다
    스크립트가 통째로 재실행됨), 첫 클릭 이후 같은 세션 상태로 다시 렌더링했을 때 버튼이 사라지고
    없다는 것 자체가 "두 번째 클릭이 물리적으로 불가능함"의 증거다."""
    at = _run_with_query_response(_budget_query_body())
    msg = _last_message(at)
    confirm_btn = at.get_by_key(f"budget_confirm_{msg['id']}")

    confirm_response = {"ok": True, "plan_start_month": "2026-10", "monthly_budget_krw": 2000000.0}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, confirm_response)) as mock_post:
        confirm_btn.click().run()
        at.run()  # 순수 재실행(rerun) — 사용자 조작 없음
        at.run()

    assert mock_post.call_count == 1, "재실행만으로 확인 API가 다시 불림 — 재호출 방지가 깨짐"


# ── 3. 전략 확인/취소 ─────────────────────────────────────────────────────


def test_strategy_confirm_success_shows_korean_label_not_raw_id():
    body = {
        "answer": "[전략 확인 필요]",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "strategy_change_needs_confirmation": {
            "confirmation_token": "tok-strat", "year": 2026, "month": 10, "strategy": "biweekly",
        },
    }
    at = _run_with_query_response(body)
    assert any("정기 분할" in m.value for m in at.markdown)

    msg = _last_message(at)
    confirm_btn = at.get_by_key(f"strategy_confirm_{msg['id']}")
    confirm_response = {"ok": True, "year": 2026, "month": 10, "strategy": "biweekly", "previous": None}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, confirm_response)) as mock_post:
        confirm_btn.click().run()

    assert mock_post.call_args[0][0] == "http://localhost:8000/confirm_strategy_change"
    assert msg["actions"]["strategy"]["status"] == "confirmed"
    assert any("정기 분할" in s.value for s in at.success)


# ── 4. 매수 기록 등 승인/거절 (여러 건) ────────────────────────────────────


def test_multiple_approvals_are_independent():
    body = {
        "answer": "[승인 필요] 매수 기록 2건",
        "contexts": [], "trace": [], "agents_used": ["ledger_agent"],
        "approvals_needed": [
            {"approval_id": "app-1", "tool": "record_virtual_buy", "args": {"amount_krw": 500000},
             "reason": "실제 매수 기록 생성"},
            {"approval_id": "app-2", "tool": "reset_ledger", "args": {}, "reason": "장부 초기화"},
        ],
    }
    at = _run_with_query_response(body)
    msg = _last_message(at)

    approve_btn_1 = at.get_by_key(f"approve_{msg['id']}_app-1")
    with patch("api_client.requests.post", return_value=_FakeResponse(200, {"approval_id": "app-1", "result": "기록됨"})) as mock_post:
        approve_btn_1.click().run()
    assert mock_post.call_args[0][0] == "http://localhost:8000/approve"
    assert msg["actions"]["approvals"]["app-1"]["status"] == "approved"
    assert msg["actions"]["approvals"]["app-2"]["status"] == "pending"  # 둘째 건은 영향 없음

    reject_btn_2 = at.get_by_key(f"reject_{msg['id']}_app-2")
    with patch("api_client.requests.post", return_value=_FakeResponse(200, {"approval_id": "app-2", "status": "rejected"})) as mock_post:
        reject_btn_2.click().run()
    assert mock_post.call_args[0][0] == "http://localhost:8000/reject"
    assert msg["actions"]["approvals"]["app-2"]["status"] == "rejected"
    assert msg["actions"]["approvals"]["app-1"]["status"] == "approved"  # 첫째 건 유지


# ── 5. 데이터 신선도 동의/취소 ─────────────────────────────────────────────


def test_data_gap_proceed_reissues_same_question_with_flag_and_appends_new_message():
    body = {
        "answer": "[데이터 확인 필요]",
        "contexts": [], "trace": [], "agents_used": ["price_agent"], "approvals_needed": [],
        "data_gap_needs_confirmation": {"fresh": False, "expected": "2026-09-18", "last_available": "2026-09-16"},
    }
    at = _run_with_query_response(body, question="현재 btc 지표 알려줘")
    msg = _last_message(at)
    proceed_btn = at.get_by_key(f"gap_proceed_{msg['id']}")

    followup_body = {"answer": "RSI는 50입니다.", "contexts": [], "trace": [], "agents_used": ["price_agent"], "approvals_needed": []}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, followup_body)) as mock_post:
        proceed_btn.click().run()

    assert mock_post.call_args == (
        ("http://localhost:8000/query",),
        {
            "json": {
                "question": "현재 btc 지표 알려줘", "proceed_with_stale_data": True,
                "awaiting_input_token": "",
            },
            "timeout": 60.0,
        },
    )
    assert len(at.session_state["messages"]) == 3  # user + 원래 assistant + 재요청 assistant
    assert at.session_state["messages"][-1]["question"] == "현재 btc 지표 알려줘"
    assert any("RSI는 50입니다." in m.value for m in at.markdown)


def test_data_gap_cancel_does_not_call_api_at_all():
    body = {
        "answer": "[데이터 확인 필요]",
        "contexts": [], "trace": [], "agents_used": ["price_agent"], "approvals_needed": [],
        "data_gap_needs_confirmation": {"fresh": False, "expected": "2026-09-18", "last_available": "2026-09-16"},
    }
    at = _run_with_query_response(body)
    msg = _last_message(at)
    cancel_btn = at.get_by_key(f"gap_cancel_{msg['id']}")

    with patch("api_client.requests.post") as mock_post:
        cancel_btn.click().run()

    mock_post.assert_not_called()
    assert msg["actions"]["data_gap"]["status"] == "dismissed"
    assert len(at.session_state["messages"]) == 2  # 새 메시지가 생기지 않아야 함


# ── 6. 연결 실패 자체가 채팅에 안내로 표시되는지 ────────────────────────────


def test_query_connection_error_shows_client_side_message_not_crash():
    with patch("api_client.requests.post", side_effect=requests.exceptions.ConnectionError("refused")):
        at = AppTest.from_file(APP_PATH, default_timeout=10)
        at.run()
        at.chat_input[0].set_value("아무 질문").run()

    assert not at.exception
    assert any("연결할 수 없습니다" in m.value for m in at.markdown)


# ── 7. 후속 입력 상태(awaiting_input) — 실사용 UI 신고 #1 ────────────────────


def test_awaiting_input_token_is_forwarded_to_the_very_next_query():
    """"자 뭐부터 시작하면 돼?" → (금액을 물어봄, awaiting_input 포함) → "200만원" 순서를
    그대로 재현한다. 두 번째 요청에 첫 응답의 토큰이 그대로 실려 나가야 한다."""
    first_body = {
        "answer": "[plan_agent] 월 예산으로 얼마를 쓰고 싶으신가요?",
        "narrative": "월 예산으로 얼마를 쓰고 싶으신가요?",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "awaiting_input": {"kind": "monthly_budget_amount", "awaiting_input_token": "tok-await-1"},
    }
    at = _run_with_query_response(first_body, question="자 뭐부터 시작하면 돼?")
    assert at.session_state["pending_awaiting_token"] == "tok-await-1"
    assert any("다음 메시지에 금액만 입력해도" in c.value for c in at.caption)

    second_body = {
        "answer": (
            "[예산 확인 필요]\n- 월 예산을 2,000,000원으로 시작하는 제안이며, 2026-10부터 "
            "적용될 예정입니다. 아직 저장되지 않았습니다.\n- 적용하려면 "
            "confirmation_token(\"tok-confirm-1\")을 POST /confirm_budget_change 로 보내세요."
        ),
        "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "tok-confirm-1", "amount_krw": 2_000_000.0,
            "effective_month": "2026-10", "is_initial": True,
            "requested_month": None, "month_mismatch": False,
        },
    }
    with patch("api_client.requests.post", return_value=_FakeResponse(200, second_body)) as mock_post:
        at.chat_input[0].set_value("200만원").run()

    assert mock_post.call_args[1]["json"]["awaiting_input_token"] == "tok-await-1"
    # 응답에 새 awaiting_input이 없으므로 토큰은 소진되고 남아있으면 안 된다.
    assert at.session_state["pending_awaiting_token"] is None
    msg = _last_message(at)
    assert msg["api_response"]["budget_change_needs_confirmation"]["amount_krw"] == 2_000_000.0


def test_no_pending_awaiting_token_means_next_query_sends_empty_token():
    """awaiting_input이 없는 평범한 대화에서는 다음 질문에 빈 토큰이 나가야 한다(기존 Swagger
    단독 질문과 동일한 요청 모양)."""
    body = {"answer": "현재가는 1억원입니다.", "narrative": "현재가는 1억원입니다.",
            "contexts": [], "trace": [], "agents_used": ["price_agent"], "approvals_needed": []}
    at = _run_with_query_response(body)
    assert at.session_state["pending_awaiting_token"] is None

    with patch("api_client.requests.post", return_value=_FakeResponse(200, body)) as mock_post:
        at.chat_input[0].set_value("또 질문").run()
    assert mock_post.call_args[1]["json"]["awaiting_input_token"] == ""


def test_data_gap_proceed_replay_does_not_consume_or_touch_pending_awaiting_token():
    """데이터 신선도 "진행" 재요청은 시스템이 원래 질문을 대신 보내는 것이지 사용자의 새 대답이
    아니다 — 대기 중인 후속 입력 토큰을 건드리면 안 된다."""
    body = {
        "answer": "[데이터 확인 필요]", "narrative": "[데이터 확인 필요]",
        "contexts": [], "trace": [], "agents_used": ["price_agent"], "approvals_needed": [],
        "data_gap_needs_confirmation": {"fresh": False, "expected": "2026-09-18", "last_available": "2026-09-16"},
    }
    at = _run_with_query_response(body, question="현재 btc 지표 알려줘")
    at.session_state["pending_awaiting_token"] = "tok-should-survive"
    msg = _last_message(at)
    proceed_btn = at.get_by_key(f"gap_proceed_{msg['id']}")

    followup = {"answer": "RSI는 50", "narrative": "RSI는 50", "contexts": [], "trace": [],
                "agents_used": ["price_agent"], "approvals_needed": []}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, followup)) as mock_post:
        proceed_btn.click().run()

    assert mock_post.call_args[1]["json"]["awaiting_input_token"] == ""
    assert at.session_state["pending_awaiting_token"] == "tok-should-survive"


# ── 8. 토큰·API 경로와 중복 안내 정리 — 실사용 UI 신고 #4 ────────────────────


def test_narrative_shown_but_raw_answer_with_token_hidden_from_main_view():
    body = {
        "answer": (
            "[예산 확인 필요]\n- 월 예산을 2,000,000원으로 시작하는 제안이며, 2026-10부터 "
            "적용될 예정입니다.\n- confirmation_token(\"secret-token-xyz\")을 "
            "POST /confirm_budget_change 로 보내세요."
        ),
        "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "secret-token-xyz", "amount_krw": 2_000_000.0,
            "effective_month": "2026-10", "is_initial": True,
            "requested_month": None, "month_mismatch": False,
        },
    }
    at = _run_with_query_response(body)

    assert not any("secret-token-xyz" in m.value for m in at.markdown), "토큰이 본문에 그대로 노출됨"
    assert not any("POST /confirm_budget_change" in m.value for m in at.markdown), "API 경로가 본문에 노출됨"
    # 카드 자체는 confirmation_token을 내부적으로 알아야 하므로(버튼 클릭 시 사용) 완전히
    # 사라지는 게 아니라 "안 보이는 화면 요소"로 존재 — 디버그 영역(st.text)에서는 확인 가능해야
    # 한다(완전히 숨기는 게 아니라 "접힌 디버그 영역으로 분리"가 요청사항).
    assert any("secret-token-xyz" in t.value for t in at.text), "디버그 영역에도 원문이 없음"


def test_service_intro_narrative_still_shown_when_budget_proposal_present():
    """예산 제안이 있어도 다른 Agent(예: research_agent)의 유효한 설명은 narrative에 남아 화면에
    보여야 한다 — narrative 자체가 아예 안 나오는 회귀를 방지."""
    body = {
        "answer": "[research_agent] 이 서비스는 매달 절반은 정액 매수합니다.\n\n[예산 확인 필요]\n- ...",
        "narrative": "[research_agent] 이 서비스는 매달 절반은 정액 매수합니다.",
        "contexts": [], "trace": [], "agents_used": ["plan_agent", "research_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "tok-x", "amount_krw": 1_000_000.0,
            "effective_month": "2026-10", "is_initial": True,
            "requested_month": None, "month_mismatch": False,
        },
    }
    at = _run_with_query_response(body)
    assert any("정액 매수합니다" in m.value for m in at.markdown)


# ── 9. 요청 월과 적용월 불일치 — 실사용 UI 신고 #3 ───────────────────────────


def test_budget_card_shows_month_mismatch_warning_with_both_months():
    body = {
        "answer": "[예산 확인 필요]",
        "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "tok-mismatch", "amount_krw": 2_000_000.0,
            "effective_month": "2026-10", "is_initial": False,
            "requested_month": "2026-09", "month_mismatch": True,
        },
    }
    at = _run_with_query_response(body, question="9월 예산은 200만원으로 할게")
    assert any(
        "2026-09" in w.value and "2026-10" in w.value and "적용할 수 없습니다" in w.value
        for w in at.warning
    )


def test_budget_card_has_no_mismatch_warning_when_months_match():
    body = {
        "answer": "[예산 확인 필요]",
        "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "tok-ok", "amount_krw": 2_000_000.0,
            "effective_month": "2026-10", "is_initial": False,
            "requested_month": "2026-10", "month_mismatch": False,
        },
    }
    at = _run_with_query_response(body)
    assert not any("적용할 수 없습니다" in w.value for w in at.warning)


# ── 10. 정책 변경(2026-09-20) — 월중 시작/앞당기기 카드 표시 ────────────────


def test_budget_card_initial_mismatch_uses_start_specific_wording_not_change_wording():
    """최초 시작 불일치(이번 달/다음 달 중 하나만 가능)는 "변경"(항상 다음 달) 문구와 달라야
    한다 — 서로 다른 정책이라 문구를 헷갈리게 섞으면 안 된다."""
    body = {
        "answer": "[예산 확인 필요]", "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "tok-init", "amount_krw": 2_000_000.0, "action": "initial",
            "effective_month": "2026-09", "is_initial": True,
            "requested_month": "2026-08", "month_mismatch": True,
        },
    }
    at = _run_with_query_response(body, question="8월부터 시작할게")
    assert any(
        "시작할 수 없습니다" in w.value and "이번 달 또는 다음 달만 가능" in w.value for w in at.warning
    )
    assert not any("정책상 항상" in w.value for w in at.warning)  # "변경" 전용 문구가 섞이지 않아야 함


def test_budget_card_shows_advance_info_with_previous_start_month_and_amount():
    body = {
        "answer": "[예산 확인 필요]", "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "tok-advance", "amount_krw": 1_500_000.0, "action": "advance",
            "effective_month": "2026-09", "is_initial": False, "requested_month": "2026-09",
            "month_mismatch": False, "previous_start_month": "2026-10", "previous_start_amount": 2_000_000.0,
        },
    }
    at = _run_with_query_response(body, question="이번 달부터 시작하고 싶어")
    assert any("2026-10" in i.value and "2026-09" in i.value and "2,000,000원" in i.value for i in at.info)
    assert any("앞당겨 시작" in m.value for m in at.markdown)


def test_budget_card_advance_confirm_calls_confirm_budget_change_like_any_other_proposal():
    body = {
        "answer": "[예산 확인 필요]", "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["plan_agent"], "approvals_needed": [],
        "budget_change_needs_confirmation": {
            "confirmation_token": "tok-advance", "amount_krw": 1_500_000.0, "action": "advance",
            "effective_month": "2026-09", "is_initial": False, "requested_month": "2026-09",
            "month_mismatch": False, "previous_start_month": "2026-10", "previous_start_amount": 2_000_000.0,
        },
    }
    at = _run_with_query_response(body)
    msg = _last_message(at)
    confirm_btn = at.get_by_key(f"budget_confirm_{msg['id']}")

    confirm_response = {"ok": True, "plan_start_month": "2026-09", "amount_krw": 1_500_000.0, "previous_start_month": "2026-10"}
    with patch("api_client.requests.post", return_value=_FakeResponse(200, confirm_response)) as mock_post:
        confirm_btn.click().run()

    assert mock_post.call_args[0][0] == "http://localhost:8000/confirm_budget_change"
    assert mock_post.call_args[1]["json"] == {"confirmation_token": "tok-advance"}
    assert msg["actions"]["budget"]["status"] == "confirmed"


# ── 11. 실제 매수 보고 후속 입력 — 실사용 신고 #2 ────────────────────────────


def test_buy_execution_detail_awaiting_hint_differs_from_budget_hint():
    body = {
        "answer": "[ledger_agent] 체결 가격이나 매수한 수량을 알려주시겠어요?",
        "narrative": "체결 가격이나 매수한 수량을 알려주시겠어요?",
        "contexts": [], "trace": [], "agents_used": ["ledger_agent"], "approvals_needed": [],
        "awaiting_input": {"kind": "buy_execution_detail", "awaiting_input_token": "tok-buy-1"},
    }
    at = _run_with_query_response(body, question="오늘 50만원어치 BTC 매수했어")
    assert at.session_state["pending_awaiting_token"] == "tok-buy-1"
    assert any("체결 가격 또는 수량만 입력해도" in c.value for c in at.caption)
    assert not any("금액만 입력해도 예산 제안" in c.value for c in at.caption)


def test_buy_execution_detail_token_forwarded_and_approval_card_rendered_on_next_message():
    first_body = {
        "answer": "[ledger_agent] 체결 가격이나 수량을 알려주세요.",
        "narrative": "체결 가격이나 수량을 알려주세요.",
        "contexts": [], "trace": [], "agents_used": ["ledger_agent"], "approvals_needed": [],
        "awaiting_input": {"kind": "buy_execution_detail", "awaiting_input_token": "tok-buy-2"},
    }
    at = _run_with_query_response(first_body, question="오늘 50만원어치 BTC 매수했어")

    second_body = {
        "answer": "[실행 전 확인 필요]\n- 실제 매수 기록 생성 — 사용자가 직접 보고한 매수 내역 (도구: record_virtual_buy, 승인 ID: app-buy-1)",
        "narrative": "",
        "contexts": [], "trace": [], "agents_used": ["ledger_agent"],
        "approvals_needed": [{
            "approval_id": "app-buy-1", "agent": "ledger_agent", "tool": "record_virtual_buy",
            "args": {"amount_krw": 500_000.0, "executed_date": "2026-09-20", "execution_time_precision": "date", "price_krw": 100_000_000.0},
            "reason": "실제 매수 기록 생성 — 사용자가 직접 보고한 매수 내역",
        }],
    }
    with patch("api_client.requests.post", return_value=_FakeResponse(200, second_body)) as mock_post:
        at.chat_input[0].set_value("1억원에 샀어").run()

    assert mock_post.call_args[1]["json"]["awaiting_input_token"] == "tok-buy-2"
    assert at.session_state["pending_awaiting_token"] is None  # 새 awaiting_input이 없으므로 소진
    msg = _last_message(at)
    approve_btn = at.get_by_key(f"approve_{msg['id']}_app-buy-1")
    assert approve_btn is not None  # 기존 승인 카드 렌더링 로직이 그대로 재사용됨
    assert any('"amount_krw": 500000.0' in j.value for j in at.json), (
        "승인 카드에 매수 기록 args(amount_krw 포함)가 표시되지 않음"
    )
