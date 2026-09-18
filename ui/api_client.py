"""BTC DCA Agent 백엔드(app.py)에 대한 얇은 HTTP 클라이언트.

Streamlit UI(streamlit_app.py)가 요청/응답 처리를 이 모듈에만 맡기고, 화면 쪽은 이 모듈이
돌려주는 결과 dict만 보고 렌더링하도록 분리했다 — UI 흐름을 가짜 API 응답으로 검증할 때
`requests.post`/`requests.get`만 몽키패치하면 되게 하기 위함이다(ui/tests/test_api_client.py,
ui/tests/test_ui_flow.py 참고).

app.py의 실제 엔드포인트/필드명(POST /query의 answer·approvals_needed·
data_gap_needs_confirmation·strategy_change_needs_confirmation·budget_change_needs_confirmation,
POST /confirm_budget_change 등의 {"confirmation_token"} 요청 바디, 404/409 매핑)을 직접 읽고
그대로 따랐다 — 이름이나 형식을 추정하지 않았다.

모든 함수는 예외를 던지지 않고 아래 형태의 dict를 반환한다(`src/`의 다른 도메인 모듈들이
지키는 "절대 raise하지 않는다" 관례와 동일하게 맞췄다 — CLAUDE.md 참고):

    {
        "ok": bool,
        "status_code": int | None,       # 실제 받은 HTTP 상태 코드(네트워크 자체가 실패하면 None)
        "body": dict | None,             # 응답 JSON(파싱 실패 시 None)
        "error_kind": None | "timeout" | "connection_error" | "http_error",
        "detail": str,                   # 사람이 읽을 오류 설명(성공이면 빈 문자열)
    }

"error_kind"가 "http_error"일 때 "status_code"에 실제 404/409 등이 담기고, "detail"은 FastAPI의
HTTPException이 돌려주는 {"detail": "..."}에서 뽑아온다. 타임아웃/연결 실패는 "이게 실제로
서버에서 처리됐는지 알 수 없다"는 의미이므로 호출부가 "실패 확정"과 구분해서 다뤄야 한다
(UI_GUIDE.md, streamlit_app.py의 처리 참고).
"""

from __future__ import annotations

import requests

# /query는 Bedrock 호출(도구 여러 번 호출 가능)을 포함해 다른 엔드포인트보다 훨씬 오래 걸릴 수
# 있어 별도로 긴 기본값을 둔다. 나머지(승인/확인/취소)는 LLM을 다시 거치지 않는 순수 상태
# 변경이라 짧게 잡아도 된다.
QUERY_TIMEOUT = 60.0
ACTION_TIMEOUT = 15.0
HEALTH_TIMEOUT = 5.0


def _post(base_url: str, path: str, payload: dict, timeout: float) -> dict:
    url = base_url.rstrip("/") + path
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        return {
            "ok": False,
            "status_code": None,
            "body": None,
            "error_kind": "timeout",
            "detail": "요청이 시간 초과됐습니다. 서버가 실제로 처리했는지 확인되지 않았습니다.",
        }
    except requests.exceptions.RequestException as exc:
        return {
            "ok": False,
            "status_code": None,
            "body": None,
            "error_kind": "connection_error",
            "detail": f"백엔드({base_url})에 연결할 수 없습니다: {exc}",
        }

    try:
        body = resp.json()
    except ValueError:
        body = None

    if resp.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        return {
            "ok": False,
            "status_code": resp.status_code,
            "body": body,
            "error_kind": "http_error",
            "detail": detail or f"HTTP {resp.status_code}",
        }

    return {"ok": True, "status_code": resp.status_code, "body": body, "error_kind": None, "detail": ""}


def query(base_url: str, question: str, proceed_with_stale_data: bool = False,
          awaiting_input_token: str = "", timeout: float = QUERY_TIMEOUT) -> dict:
    """POST /query. 성공(ok=True)이어도 body에 "error" 필드가 있을 수 있다 — app.py가 Bedrock
    실패(쿼터 초과 등)를 500이 아니라 §4-2 계약 형태로 감싸 200으로 돌려주기 때문이다.

    awaiting_input_token(2026-09-20 추가, 실사용 UI 신고 #1): 직전 응답의
    awaiting_input.awaiting_input_token을 그대로 넘기면, "200만원"처럼 그 자체로는 주제를 알 수
    없는 짧은 답변도 방금 서버가 물어본 질문(현재는 월 예산 금액)에 대한 답으로 연결된다. 생략하면
    (기본값) 기존과 동일하게 일반 라우팅만 탄다."""
    return _post(
        base_url, "/query",
        {
            "question": question,
            "proceed_with_stale_data": proceed_with_stale_data,
            "awaiting_input_token": awaiting_input_token,
        },
        timeout,
    )


def approve(base_url: str, approval_id: str) -> dict:
    return _post(base_url, "/approve", {"approval_id": approval_id}, ACTION_TIMEOUT)


def reject(base_url: str, approval_id: str) -> dict:
    return _post(base_url, "/reject", {"approval_id": approval_id}, ACTION_TIMEOUT)


def confirm_strategy_change(base_url: str, confirmation_token: str) -> dict:
    return _post(base_url, "/confirm_strategy_change", {"confirmation_token": confirmation_token}, ACTION_TIMEOUT)


def cancel_strategy_change(base_url: str, confirmation_token: str) -> dict:
    return _post(base_url, "/cancel_strategy_change", {"confirmation_token": confirmation_token}, ACTION_TIMEOUT)


def confirm_budget_change(base_url: str, confirmation_token: str) -> dict:
    return _post(base_url, "/confirm_budget_change", {"confirmation_token": confirmation_token}, ACTION_TIMEOUT)


def cancel_budget_change(base_url: str, confirmation_token: str) -> dict:
    return _post(base_url, "/cancel_budget_change", {"confirmation_token": confirmation_token}, ACTION_TIMEOUT)


def health(base_url: str, timeout: float = HEALTH_TIMEOUT) -> dict:
    """GET /health. UI의 "연결 확인" 버튼 전용 — 데이터 디렉터리 격리 여부는 알려주지 않는다
    (health 엔드포인트에 그 정보가 없다 — UI_GUIDE.md의 "테스트 환경" 항목 참고)."""
    url = base_url.rstrip("/") + "/health"
    try:
        resp = requests.get(url, timeout=timeout)
        return {"ok": resp.status_code == 200, "status_code": resp.status_code, "detail": ""}
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "status_code": None, "detail": str(exc)}
