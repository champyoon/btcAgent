"""BTC DCA Agent — 실사용 테스트용 Streamlit 채팅 UI.

Swagger에서 토큰·승인 ID를 손으로 복사하며 테스트하던 것을, 채팅으로 질문하고 버튼으로
확인/취소/승인/거절하는 흐름으로 대체한다. 기존 FastAPI(app.py)를 HTTP로만 호출한다 — 백엔드
정책(승인 게이트, 제안→확인 절차, 데이터 신선도 게이트)은 전혀 재구현하지 않고 그대로 따른다.

실행: ui/UI_GUIDE.md 참고 (백엔드와 이 UI를 각각 별도 프로세스로 켠다).

핵심 설계:
- 모든 백엔드 호출은 api_client.py를 거친다 — 이 모듈은 절대 raise하지 않고 {"ok", "status_code",
  "body", "error_kind", "detail"} 형태만 돌려준다(ok=False가 "실패 확정"과 "확인 안 됨"을 모두
  포함하므로, 상태 판정은 error_kind/status_code로 갈라서 한다 — 아래 _run_action 참고).
- 매 질문·매 확인/승인 결과는 st.session_state.messages에 저장한다. 버튼 클릭 시에만 API를
  부르고, 그 외의 모든 재실행(rerun)은 이미 저장된 state를 다시 그리기만 한다 — st.rerun()이
  스크립트를 처음부터 다시 돌려도 이미 "confirmed"/"approved" 등 종결 상태인 항목은 버튼 자체를
  다시 그리지 않으므로 중복 실행이 구조적으로 불가능하다(버튼이 없으면 누를 수 없다).
- 타임아웃/연결 실패는 "실패"가 아니라 "확인 안 됨" 상태로 남긴다 — 자동으로 같은 요청을
  재전송하거나 새 제안을 만들지 않는다. 사용자가 "다시 확인 시도" 버튼을 눌러야만 동일한 액션을
  다시 시도한다(자동 재시도 금지 vs 사용자가 명시적으로 다시 누르는 것은 다른 문제다).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_client  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:8000"

# select_strategy 도구 docstring(src/agent.py)에 문서화된 뜻 그대로 — 여기서 새로 지어내지 않았다.
# "biweekly"는 2주 간격이 아니라 매월 1일·15일 두 날짜를 가리키는 내부 식별자다(CLAUDE.md 참고).
_STRATEGY_LABELS = {
    "decline_day": "하락일 매수",
    "biweekly": "정기 분할 (매월 1일·15일)",
    "rsi": "RSI 매수",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _init_state() -> None:
    if "base_url" not in st.session_state:
        st.session_state.base_url = DEFAULT_BASE_URL
    if "messages" not in st.session_state:
        st.session_state.messages = []
    # 후속 입력 상태(awaiting_input, 2026-09-20 추가) — 서버가 직전 응답에서 "다음 메시지는 방금
    # 물어본 질문의 답이다"라고 발급한 토큰을 다음 질문에 실어 보내기 전까지 들고 있는다. 이
    # Streamlit 세션(브라우저 세션)마다 독립된 session_state이므로 다른 브라우저 세션과 섞일 일이
    # 없다(요청사항).
    if "pending_awaiting_token" not in st.session_state:
        st.session_state.pending_awaiting_token = None


def _client_error_response(client_result: dict) -> dict:
    """api_client 결과(ok=False)를 /query 응답과 같은 모양으로 감싼다 — 렌더링 쪽이 항상 같은
    구조(answer/contexts/trace/agents_used/approvals_needed)만 다루면 되게 하기 위함이다."""
    kind = client_result.get("error_kind")
    if kind == "timeout":
        answer = (
            "⚠ 요청이 시간 초과됐습니다. 서버가 실제로 처리를 마쳤는지 이 화면에서는 확인되지 "
            "않았습니다. 잠시 후 상태를 확인하는 질문을 새로 보내보세요."
        )
    elif kind == "connection_error":
        answer = f"⚠ 백엔드에 연결할 수 없습니다: {client_result.get('detail')}"
    else:
        answer = f"⚠ 요청이 실패했습니다: {client_result.get('detail')}"
    return {
        "answer": answer,
        "narrative": answer,
        "contexts": [],
        "trace": [],
        "agents_used": [],
        "approvals_needed": [],
        "_client_error_kind": kind,
    }


def _query_and_append(question: str, proceed_with_stale_data: bool = False, note: str = "",
                       consume_awaiting: bool = True) -> None:
    """/query를 호출해 결과를 새 assistant 메시지로 추가한다.

    consume_awaiting(2026-09-20 추가, 실사용 UI 신고 #1): 세션에 대기 중인 후속 입력 토큰이
    있으면 이번 요청에 실어 보내고 즉시 비운다(단발성 — 서버도 토큰을 1회용으로 소진한다). 응답에
    새 awaiting_input이 오면 그걸로 다시 채운다. 데이터 신선도 "진행" 재요청처럼 사용자가 직접 친
    새 메시지가 아니라 시스템이 원래 질문을 대신 재전송하는 경우는 False로 호출해, 실제 사용자의
    다음 대답을 위해 대기 토큰을 그대로 남겨둔다."""
    awaiting_token = ""
    if consume_awaiting:
        awaiting_token = st.session_state.pending_awaiting_token or ""
        st.session_state.pending_awaiting_token = None

    with st.spinner("답변을 기다리는 중입니다..."):
        res = api_client.query(
            st.session_state.base_url, question,
            proceed_with_stale_data=proceed_with_stale_data,
            awaiting_input_token=awaiting_token,
        )
    api_response = res["body"] if (res["ok"] and isinstance(res["body"], dict)) else _client_error_response(res)

    if consume_awaiting:
        new_awaiting = api_response.get("awaiting_input")
        st.session_state.pending_awaiting_token = new_awaiting["awaiting_input_token"] if new_awaiting else None

    st.session_state.messages.append({
        "id": _new_id(),
        "role": "assistant",
        "question": question,
        "note": note,
        "api_response": api_response,
        "actions": {"approvals": {}},
    })


def _run_action(state: dict, api_fn, success_status: str, *args) -> None:
    """확인/취소/승인/거절 공용 처리. 호출 전 인자를 state에 남겨둬서, 타임아웃 뒤 사용자가
    "다시 확인 시도"를 누르면 정확히 같은 액션(같은 함수·같은 토큰)만 재실행하게 한다 — 확인을
    시도했는데 취소가 나가는 일이 없도록."""
    state["last_action"] = (api_fn, args, success_status)
    with st.spinner("처리 중입니다..."):
        res = api_fn(st.session_state.base_url, *args)
    state["result"] = res.get("body")
    state["detail"] = res.get("detail", "")

    if res["ok"]:
        state["status"] = success_status
    elif res["error_kind"] == "timeout":
        state["status"] = "unknown_timeout"
    elif res["error_kind"] == "connection_error":
        state["status"] = "connection_error"
    elif res["status_code"] == 404:
        state["status"] = "not_found"
    elif res["status_code"] == 409:
        state["status"] = "conflict"
    else:
        state["status"] = "error"


def _render_action_status(state: dict, success_labels: dict[str, str], key_prefix: str) -> None:
    """_run_action이 남긴 state["status"]에 따라 결과 문구/재시도 버튼을 그린다.

    success_labels: {success_status_이름: 사용자에게 보여줄 문구}. pending이 아닌 모든 케이스를
    여기서 공통으로 처리한다(budget/strategy/approvals 카드가 이 함수를 공유한다).
    """
    status = state["status"]
    if status in success_labels:
        st.success(f"✅ {success_labels[status]}")
        return
    if status in ("unknown_timeout", "connection_error"):
        st.warning(f"⚠ {state['detail']}")
        st.caption("자동으로 다시 보내지 않습니다 — 실제로 처리됐는지 확인되지 않은 상태입니다.")
        if st.button("다시 확인 시도", key=f"{key_prefix}_retry"):
            fn, args, success_status = state["last_action"]
            _run_action(state, fn, success_status, *args)
            st.rerun()
        return
    if status == "not_found":
        st.error(
            "이 제안/승인을 서버가 더 이상 알지 못합니다(서버 재시작 등으로 사라졌을 수 있습니다). "
            f"{state['detail']}"
        )
        st.caption("새로 질문해서 다시 제안을 받아주세요.")
        return
    if status == "conflict":
        st.warning(f"이미 처리되었거나 더 이상 유효하지 않습니다: {state['detail']}")
        st.caption("현재 상태가 궁금하면 상태 조회 질문을 새로 보내보세요.")
        return
    st.error(f"처리 중 오류가 발생했습니다: {state['detail']}")


def _render_data_gap_card(msg: dict) -> None:
    gap = msg["api_response"].get("data_gap_needs_confirmation")
    if not gap:
        return
    state = msg["actions"].setdefault("data_gap", {"status": "pending"})
    with st.container(border=True):
        if state["status"] == "pending":
            st.info(
                "📅 **최신 데이터 확인 필요** — 확보된 데이터는 "
                f"{gap.get('last_available') or '없음'}까지이고, 마감된 최신 일봉"
                f"({gap['expected']})이 아직 없어 계산을 멈췄습니다. 지금 있는 데이터로 진행할까요?"
            )
            c1, c2 = st.columns(2)
            with c1:
                if st.button("진행 (현재 데이터로 계산)", key=f"gap_proceed_{msg['id']}", type="primary"):
                    state["status"] = "proceeded"
                    _query_and_append(
                        msg["question"], proceed_with_stale_data=True,
                        note="(신선도 확인 후 재요청한 결과)", consume_awaiting=False,
                    )
                    st.rerun()
            with c2:
                if st.button("취소 (진행 안 함)", key=f"gap_cancel_{msg['id']}"):
                    state["status"] = "dismissed"
                    st.rerun()
        elif state["status"] == "proceeded":
            st.success("✅ 현재 데이터로 재요청했습니다 — 아래 새 메시지를 확인하세요.")
        else:
            st.info("계산을 진행하지 않는 것으로 처리했습니다.")


def _render_budget_card(msg: dict) -> None:
    proposal = msg["api_response"].get("budget_change_needs_confirmation")
    if not proposal:
        return
    state = msg["actions"].setdefault("budget", {"status": "pending"})
    token = proposal["confirmation_token"]
    # "action"이 없는 이전 백엔드 응답과도 호환 — is_initial만으로 시작/변경을 구분한다.
    action = proposal.get("action") or ("initial" if proposal.get("is_initial") else "change")
    verb = {"initial": "시작", "advance": "앞당겨 시작", "change": "변경"}.get(action, "변경")
    amount = proposal["amount_krw"]
    month = proposal["effective_month"]
    with st.container(border=True):
        st.markdown(f"💰 **예산 {verb} 제안** — {month}부터 월 예산을 **{amount:,.0f}원**으로 {verb}할까요?")
        # 실사용 신고(2026-09-20, #3 / 정책 변경): 요청한 월과 실제 적용월이 다른데 이유 설명 없이
        # 제안만 나가는 문제 — 카드에도 요청월과 실제 적용월을 나란히 명확히 표시한다(설명은
        # narrative에도 있지만, 여기서도 놓치지 않게 한다). 최초 시작(이번 달/다음 달 중 선택
        # 가능)과 변경(항상 다음 달만)은 허용 범위가 달라 문구도 다르게 보여준다.
        if proposal.get("month_mismatch"):
            if action == "initial":
                st.warning(
                    f"⚠ 요청하신 **{proposal['requested_month']}**에는 시작할 수 없습니다(최초 시작은 "
                    f"이번 달 또는 다음 달만 가능) — 대신 **{month}**부터 시작하는 제안입니다."
                )
            else:
                st.warning(
                    f"⚠ 요청하신 **{proposal['requested_month']}**에는 적용할 수 없습니다(정책상 항상 "
                    f"다음 달부터 적용) — 대신 **{month}**부터 적용하는 제안입니다."
                )
        elif action == "advance" and proposal.get("previous_start_month"):
            # 정책 변경(2026-09-20) §2: 아직 시작하지 않은 계획의 시작월을 이번 달로 앞당기는
            # 경우 — 기존에 설정된 미래 시작월의 예산은 삭제되지 않고 그대로 유지된다는 것을
            # 변경 전/후 시작월과 함께 명확히 표시한다(요청사항: "변경 전후 시작월" 표시).
            prev_amount = proposal.get("previous_start_amount")
            prev_amount_text = f"{prev_amount:,.0f}원" if prev_amount is not None else "미설정"
            st.info(
                f"ℹ 시작월을 **{proposal['previous_start_month']}** → **{month}**(이번 달)로 "
                f"앞당깁니다. 기존에 설정된 {proposal['previous_start_month']} 예산"
                f"({prev_amount_text})은 삭제되지 않고 그대로 유지됩니다 — 이번 달분 예산만 새로 "
                "추가됩니다."
            )
        st.caption("아직 저장되지 않았습니다 — 아래에서 확인해야 실제로 반영됩니다.")
        if state["status"] == "pending":
            c1, c2 = st.columns(2)
            with c1:
                if st.button("확인", key=f"budget_confirm_{msg['id']}", type="primary"):
                    _run_action(state, api_client.confirm_budget_change, "confirmed", token)
                    st.rerun()
            with c2:
                if st.button("취소", key=f"budget_cancel_{msg['id']}"):
                    _run_action(state, api_client.cancel_budget_change, "cancelled", token)
                    st.rerun()
        else:
            _render_action_status(
                state,
                {
                    "confirmed": f"{verb} 내용이 저장됐습니다 ({month}부터 {amount:,.0f}원).",
                    "cancelled": "예산 제안을 취소했습니다.",
                },
                key_prefix=f"budget_{msg['id']}",
            )


def _render_strategy_card(msg: dict) -> None:
    proposal = msg["api_response"].get("strategy_change_needs_confirmation")
    if not proposal:
        return
    state = msg["actions"].setdefault("strategy", {"status": "pending"})
    token = proposal["confirmation_token"]
    label = _STRATEGY_LABELS.get(proposal["strategy"], proposal["strategy"])
    year, month = proposal["year"], proposal["month"]
    with st.container(border=True):
        st.markdown(f"📈 **전략 변경 제안** — {year}-{month:02d} 전략을 **{label}**(으)로 변경할까요?")
        st.caption("아직 저장되지 않았습니다 — 아래에서 확인해야 실제로 반영됩니다.")
        if state["status"] == "pending":
            c1, c2 = st.columns(2)
            with c1:
                if st.button("확인", key=f"strategy_confirm_{msg['id']}", type="primary"):
                    _run_action(state, api_client.confirm_strategy_change, "confirmed", token)
                    st.rerun()
            with c2:
                if st.button("취소", key=f"strategy_cancel_{msg['id']}"):
                    _run_action(state, api_client.cancel_strategy_change, "cancelled", token)
                    st.rerun()
        else:
            _render_action_status(
                state,
                {
                    "confirmed": f"전략이 '{label}'(으)로 변경됐습니다.",
                    "cancelled": "전략 변경 제안을 취소했습니다.",
                },
                key_prefix=f"strategy_{msg['id']}",
            )


def _render_approvals(msg: dict) -> None:
    for a in msg["api_response"].get("approvals_needed") or []:
        aid = a["approval_id"]
        state = msg["actions"]["approvals"].setdefault(aid, {"status": "pending"})
        with st.container(border=True):
            st.markdown(f"🔒 **승인 필요** — {a.get('reason', '')}")
            st.caption(f"도구: `{a.get('tool', '')}` · 승인 ID: `{aid}`")
            if a.get("args"):
                st.json(a["args"])
            if state["status"] == "pending":
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("승인", key=f"approve_{msg['id']}_{aid}", type="primary"):
                        _run_action(state, api_client.approve, "approved", aid)
                        st.rerun()
                with c2:
                    if st.button("거절", key=f"reject_{msg['id']}_{aid}"):
                        _run_action(state, api_client.reject, "rejected", aid)
                        st.rerun()
            else:
                result_text = ""
                if state["status"] == "approved" and isinstance(state.get("result"), dict):
                    result_text = f" 결과: {state['result'].get('result', '')}"
                _render_action_status(
                    state,
                    {
                        "approved": f"승인 완료.{result_text}",
                        "rejected": "거절했습니다 — 실행되지 않았습니다.",
                    },
                    key_prefix=f"approval_{msg['id']}_{aid}",
                )


def _render_debug_expander(msg: dict) -> None:
    """실사용 UI 신고(2026-09-20, #4): answer를 그대로 보여주면 confirmation_token·API 경로
    문구가 카드 안내와 중복 노출된다. answer(토큰·경로가 그대로 담긴 원문 — 백엔드 §4-2 계약의
    하위 호환 필드)와 구조화 필드는 여기 접힌 영역으로 옮기고, 화면에는 narrative(설명 전용
    텍스트)만 보여준다 — answer 문자열을 정규식으로 잘라내는 게 아니라, 백엔드가 애초에 두 필드를
    따로 내려준다(agent.py의 _build_narrative_text)."""
    resp = msg["api_response"]
    with st.expander("디버그 정보 (answer 원문 / 구조화 필드 / trace)", expanded=False):
        st.markdown("**answer (원문 — confirmation_token·API 경로 포함)**")
        st.text(resp.get("answer", ""))
        st.markdown("**agents_used**")
        st.write(resp.get("agents_used", []))
        for field in (
            "budget_change_needs_confirmation", "strategy_change_needs_confirmation",
            "approvals_needed", "data_gap_needs_confirmation", "awaiting_input",
        ):
            if resp.get(field):
                st.markdown(f"**{field}**")
                st.json(resp[field])
        st.markdown("**contexts**")
        st.write(resp.get("contexts", []))
        st.markdown("**trace**")
        st.json(resp.get("trace", []))


def _render_assistant_message(msg: dict) -> None:
    resp = msg["api_response"]
    if msg.get("note"):
        st.caption(msg["note"])
    # narrative가 없는 백엔드(과거 버전 등)와도 호환되도록 answer로 대체한다.
    narrative = resp.get("narrative", resp.get("answer", ""))
    if narrative:
        st.markdown(narrative)
    if resp.get("error"):
        st.caption(f"⚠ 서버가 오류를 안내 문구로 감싸 반환했습니다(error={resp['error']}) — 위 문구를 참고하세요.")
    awaiting = resp.get("awaiting_input")
    if awaiting:
        # 2026-09-20 추가(실사용 신고 #2): 후속 입력 상태가 "예산 금액" 말고 "매수 체결 가격/수량"
        # 일 수도 있어졌다 — 종류(kind)에 맞는 힌트를 보여준다.
        hint = {
            "monthly_budget_amount": "💬 바로 다음 메시지에 금액만 입력해도 예산 제안으로 이어집니다.",
            "buy_execution_detail": "💬 바로 다음 메시지에 체결 가격 또는 수량만 입력해도 매수 기록 승인 요청으로 이어집니다.",
        }.get(awaiting.get("kind"), "💬 바로 다음 메시지가 방금 물어본 질문의 답으로 이어집니다.")
        st.caption(hint)
    _render_data_gap_card(msg)
    _render_budget_card(msg)
    _render_strategy_card(msg)
    _render_approvals(msg)
    _render_debug_expander(msg)


def main() -> None:
    st.set_page_config(page_title="BTC DCA Agent 채팅 UI", page_icon="💬", layout="centered")
    _init_state()

    with st.sidebar:
        st.header("백엔드 연결")
        st.session_state.base_url = st.text_input("백엔드 주소", value=st.session_state.base_url)
        if st.button("연결 확인"):
            h = api_client.health(st.session_state.base_url)
            if h["ok"]:
                st.success("연결됨")
            else:
                st.error(f"연결 실패: {h.get('detail') or h.get('status_code')}")

        st.divider()
        st.warning(
            "⚠ **테스트 환경 확인 안 됨**\n\n"
            "이 UI는 백엔드가 실제 데이터(`data/`)로 떴는지, 격리된 테스트 데이터로 떴는지 "
            "API로 알아낼 방법이 없습니다(`/health`에 그 정보가 없습니다). 실제 장부를 보호하려면 "
            "UI_GUIDE.md 안내대로 `BTC_AGENT_DATA_DIR`을 설정한 백엔드에 연결한 뒤 사용하세요 — "
            "확인 없이 테스트 환경이라고 가정하지 마세요."
        )
        st.caption(
            "대화 내역은 화면에 표시되지만, 현재 API는 이전 대화를 자동으로 기억하지 않습니다. "
            "각 질문은 서로 독립적으로 처리됩니다."
        )

    st.title("BTC DCA Agent 채팅 (실사용 테스트용)")

    for msg in st.session_state.messages:
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["content"])
        else:
            with st.chat_message("assistant"):
                _render_assistant_message(msg)

    prompt = st.chat_input("질문을 입력하세요 (예: 이번 달 예산 얼마 남았어?)")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt, "id": _new_id()})
        _query_and_append(prompt)
        st.rerun()


main()
