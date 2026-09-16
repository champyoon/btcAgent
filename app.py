"""BTC DCA Agent — FastAPI 진입점.

엔드포인트:
  GET  /health   상태 확인
  POST /query    §4-2 표준 규약 — {"question": str, "proceed_with_stale_data": bool} ->
                 {"answer", "contexts", "trace", ...}
  POST /approve  대기 중인 승인을 실행 {"approval_id": str} — SPEC §10-1
  POST /reject   대기 중인 승인을 거절 {"approval_id": str} — SPEC §10-1

실행:
  python app.py
  또는: uvicorn app:app --reload --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()  # 저장소 루트 .env 를 찾아 AWS 자격증명을 환경변수로 등록합니다.

import agent

# Day4의 실패 분류 패턴(tools.classify_failure — retryable/backoff/fatal)을 API 경계에도 그대로
# 적용합니다. Bedrock 호출이 실패하면(쿼터 초과 등) agent.run()이 예외를 그대로 던지는데, 이걸 안
# 잡으면 FastAPI가 밋밋한 500 "Internal Server Error"만 돌려줘서 클라이언트가 원인을 알 수 없습니다.
def _classify_llm_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"):
            return "backoff"
        if code in ("ModelTimeoutException", "ModelErrorException"):
            return "retryable"
        return "fatal"
    return "fatal"


_LLM_ERROR_MESSAGES: dict[str, str] = {
    "backoff": "지금 요청이 많아 일시적으로 응답할 수 없습니다. 잠시 후 다시 시도해주세요.",
    "retryable": "일시적인 오류로 응답하지 못했습니다. 다시 시도해주세요.",
    "fatal": "요청을 처리하는 중 오류가 발생했습니다.",
}

app = FastAPI(title="BTC DCA Agent", description="월 예산 기반 BTC DCA 전략 비교·기록 관리 에이전트")

_supervisor_fn = None


def _get_supervisor():
    """Supervisor(LLM 포함)를 최초 요청 시 1회만 만들어 재사용합니다."""
    global _supervisor_fn
    if _supervisor_fn is None:
        _supervisor_fn = agent.build_supervisor()
    return _supervisor_fn


class QueryRequest(BaseModel):
    question: str
    proceed_with_stale_data: bool = False


class ApproveRequest(BaseModel):
    approval_id: str


class RejectRequest(BaseModel):
    approval_id: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/query")
def query(req: QueryRequest) -> dict:
    """질문 하나를 받아 Supervisor를 거쳐 답을 반환합니다.

    §4-2 표준 규약: {"answer": str, "contexts": [{"doc_id","text"}], "trace": [{"step","input","output"}]}.
    approvals_needed가 채워져 있으면, 그 항목의 approval_id를 그대로 /approve(또는 /reject)에
    보내야 실제로 실행(또는 거절)됩니다 — 서버가 저장해둔 tool/args만 실행하므로 클라이언트가
    tool/args를 다시 보낼 필요도, 그럴 방법도 없습니다(SPEC §10-1).

    data_gap_needs_confirmation이 채워져 있으면(SPEC §7-1), 최신 확정 일봉이 아직 없어 계산을
    멈춘 상태입니다. 그래도 진행하려면 같은 question으로 proceed_with_stale_data=true를 실어
    재요청하세요.

    Bedrock 호출이 실패해도(쿼터 초과 등) 500을 그대로 노출하지 않고, §4-2 계약 형태를 유지한 채
    사유를 안내합니다 (`error` 필드는 계약 밖 부가 정보).
    """
    try:
        return _get_supervisor()(req.question, proceed_with_stale_data=req.proceed_with_stale_data)
    except Exception as exc:  # noqa: BLE001 — API 경계에서는 어떤 예외든 계약 형태로 감싸 반환합니다.
        kind = _classify_llm_error(exc)
        return {
            "answer": _LLM_ERROR_MESSAGES[kind],
            "contexts": [],
            "trace": [{"step": "error", "input": req.question, "output": {"kind": kind, "detail": str(exc)}}],
            "error": kind,
        }


@app.post("/approve")
def approve(req: ApproveRequest) -> dict:
    """대기 중인 승인을 approval_id만으로 실행합니다(SPEC §10-1). LLM을 다시 거치지 않습니다."""
    result = agent.execute_approved_action(req.approval_id)
    status = result.pop("http_status")
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/reject")
def reject(req: RejectRequest) -> dict:
    """대기 중인 승인을 approval_id만으로 거절합니다(SPEC §10-1). 도메인 도구는 실행되지 않습니다."""
    result = agent.reject_approved_action(req.approval_id)
    status = result.pop("http_status")
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
