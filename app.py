"""BTC DCA Agent — FastAPI 진입점.

엔드포인트:
  GET  /health   상태 확인
  POST /query    §4-2 표준 규약 — {"question": str, "proceed_with_stale_data": bool} ->
                 {"answer", "contexts", "trace", ...}
  POST /approve  대기 중인 승인을 실행 {"approval_id": str} — SPEC §10-1
  POST /reject   대기 중인 승인을 거절 {"approval_id": str} — SPEC §10-1
  POST /confirm_strategy_change  select_strategy가 제안한 전략 변경을 적용 {"confirmation_token": str}
  POST /cancel_strategy_change   select_strategy가 제안한 전략 변경을 취소 {"confirmation_token": str}
  POST /confirm_budget_change    set_monthly_budget이 제안한 예산 설정/변경을 적용 {"confirmation_token": str}
  POST /cancel_budget_change     set_monthly_budget이 제안한 예산 설정/변경을 취소 {"confirmation_token": str}

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
    # 후속 입력 상태(awaiting_input, SPEC §2-2, 2026-09-20 추가) — 직전 응답의
    # awaiting_input.awaiting_input_token을 그대로 실어 보내면, 이번 질문이 그 후속 질문(예: 방금
    # 물어본 예산 금액)에 대한 답임을 서버가 구조적으로 인식한다. 생략하면(기존 Swagger 단독 질문과
    # 100% 동일하게) 항상 일반 라우팅만 탄다.
    awaiting_input_token: str = ""


class ApproveRequest(BaseModel):
    approval_id: str


class RejectRequest(BaseModel):
    approval_id: str


class StrategyConfirmationRequest(BaseModel):
    confirmation_token: str


class BudgetConfirmationRequest(BaseModel):
    confirmation_token: str


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

    strategy_change_needs_confirmation이 채워져 있으면 select_strategy가 전략 변경을 제안한
    상태입니다. 이 API는 대화 기록이 없는 무상태라 "네 확인했어요" 같은 자연어 재질문은 라우팅조차
    안 될 수 있습니다 — 반드시 그 안의 confirmation_token을 /confirm_strategy_change(적용) 또는
    /cancel_strategy_change(취소)로 구조화해서 보내세요.

    budget_change_needs_confirmation이 채워져 있으면 set_monthly_budget이 예산 설정/변경을 제안한
    상태입니다(SPEC §4-2, 2026-09-18 확정 — 이전엔 즉시 반영이었습니다). 같은 이유로 반드시 그 안의
    confirmation_token을 /confirm_budget_change(적용) 또는 /cancel_budget_change(취소)로
    구조화해서 보내세요 — 이 요청들은 금액·적용월을 함께 보내지 않습니다(서버가 제안 시점에 저장해둔
    값만 씁니다).

    awaiting_input이 채워져 있으면(SPEC §2-2, 2026-09-20 추가) 서버가 사용자에게 후속 정보(현재는
    월 예산 금액)를 물어본 상태입니다. 그 안의 awaiting_input_token을 바로 다음 /query 요청의
    awaiting_input_token 필드에 그대로 실어 보내면, "200만원"처럼 그 자체로는 어떤 주제 키워드도
    없는 짧은 답변을 서버가 정확히 그 질문에 대한 답으로 연결합니다. 생략해도 무방합니다(기존
    단독 질문과 동일하게 동작 — 다만 그 경우 짧은 답변은 일반 라우팅만 타서 범위 밖으로 거절될
    수 있습니다).

    narrative는 answer와 같은 내용을 담되, 확인/승인이 필요한 항목의 confirmation_token·API 경로
    안내 문구는 뺀 설명 전용 텍스트입니다(2026-09-20 추가) — UI가 그 안내를 구조화된 필드
    (budget_change_needs_confirmation 등)로 직접 만든 카드로 대체할 때, answer를 그대로 보여주면
    카드 내용과 중복 노출되는 문제를 위해 추가했습니다. answer는 하위 호환을 위해 그대로 유지되며,
    Swagger 등 카드가 없는 클라이언트는 계속 answer만 보고도 전부 확인할 수 있습니다.

    Bedrock 호출이 실패해도(쿼터 초과 등) 500을 그대로 노출하지 않고, §4-2 계약 형태를 유지한 채
    사유를 안내합니다 (`error` 필드는 계약 밖 부가 정보).
    """
    try:
        return _get_supervisor()(
            req.question,
            proceed_with_stale_data=req.proceed_with_stale_data,
            awaiting_input_token=req.awaiting_input_token,
        )
    except Exception as exc:  # noqa: BLE001 — API 경계에서는 어떤 예외든 계약 형태로 감싸 반환합니다.
        kind = _classify_llm_error(exc)
        return {
            "answer": _LLM_ERROR_MESSAGES[kind],
            "narrative": _LLM_ERROR_MESSAGES[kind],
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


@app.post("/confirm_strategy_change")
def confirm_strategy_change(req: StrategyConfirmationRequest) -> dict:
    """select_strategy가 제안한 전략 변경을 confirmation_token만으로 적용합니다. LLM/라우팅을
    거치지 않습니다 — approvals_needed/{/approve}와 같은 이유로, 이 API는 대화 기록이 없어 자연어
    확인만으로는 이 토큰을 다시 실어 보낼 방법이 없기 때문입니다."""
    result = agent.confirm_strategy_change_action(req.confirmation_token)
    status = result.pop("http_status")
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/cancel_strategy_change")
def cancel_strategy_change(req: StrategyConfirmationRequest) -> dict:
    """select_strategy가 제안한 전략 변경을 confirmation_token만으로 취소합니다. 아무것도
    적용되지 않습니다."""
    result = agent.cancel_strategy_change_action(req.confirmation_token)
    status = result.pop("http_status")
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/confirm_budget_change")
def confirm_budget_change(req: BudgetConfirmationRequest) -> dict:
    """set_monthly_budget이 제안한 예산 설정/변경을 confirmation_token만으로 적용합니다(SPEC §4-2,
    2026-09-18 확정). 클라이언트가 금액·적용월을 보내도 받지 않습니다 — 서버가 제안 시점에 저장해둔
    값만 씁니다. 존재하지 않는 토큰은 404, 이미 확인·취소로 소진됐거나 제안 이후 상태·시점이 달라져
    낡은 제안이 된 경우는 409입니다(다시 제안해야 합니다)."""
    result = agent.confirm_budget_change_action(req.confirmation_token)
    status = result.pop("http_status")
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/cancel_budget_change")
def cancel_budget_change(req: BudgetConfirmationRequest) -> dict:
    """set_monthly_budget이 제안한 예산 설정/변경을 confirmation_token만으로 취소합니다. 아무것도
    적용되지 않습니다. 존재하지 않는 토큰은 404, 이미 처리된 토큰은 409입니다."""
    result = agent.cancel_budget_change_action(req.confirmation_token)
    status = result.pop("http_status")
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
