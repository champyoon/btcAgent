"""승인 ID 기반 실행 게이트 — SPEC.md §10-1 확정 설계.

핵심: `/approve`는 클라이언트가 보낸 tool/args를 절대 신뢰하지 않는다. `/query`가 승인이 필요한
도구 호출을 발견했을 때만 이 모듈이 approval_id를 발급해 tool/args를 서버에 저장해두고, `/approve`는
그 id로 조회한 값을 그대로 실행한다 — `/query`를 거치지 않은 approval_id는 애초에 존재하지 않으므로
뒷문 호출 자체가 불가능하다.

저장 위치는 서버 프로세스 메모리(dict)다 — 단일 프로세스·개인 1인·저빈도 사용이라 파일/DB
영속화는 불필요하다(재시작하면 대기 중 승인이 사라지는 건 알려진 한계). TTL 없음, 1회용.

동시성: pending → executing 전환은 dict 조회 후 바로 갱신하며 그 사이에 `await`가 없다 — CPython의
GIL 덕에 이 짧은 구간은 다른 코루틴이 끼어들 수 없어, 별도의 락 없이도 "동시 승인 실행 중 하나만
성립"이 보장된다(§10-1 "원자적 전환").
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

_STORE: dict[str, dict] = {}

# 상태 전이: pending -> executing -> executed
#            pending -> rejected
_TERMINAL_STATUSES = ("executed", "rejected")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(tool: str, args: dict[str, Any], reason: str = "") -> str:
    """승인이 필요한 도구 호출을 등록하고 approval_id를 발급한다."""
    approval_id = uuid.uuid4().hex
    _STORE[approval_id] = {
        "approval_id": approval_id,
        "tool": tool,
        "args": args,
        "reason": reason,
        "status": "pending",
        "created_at": _now_iso(),
    }
    return approval_id


def get(approval_id: str) -> dict | None:
    entry = _STORE.get(approval_id)
    return dict(entry) if entry is not None else None


def begin_execution(approval_id: str) -> tuple[bool, str]:
    """pending -> executing. 이미 다른 상태면 실패(404/409 판단은 호출부의 몫).

    반환: (성공 여부, 실패 사유 또는 "executing"). 실패 사유는 "not_found" 또는 현재 status.
    """
    entry = _STORE.get(approval_id)
    if entry is None:
        return False, "not_found"
    if entry["status"] != "pending":
        return False, entry["status"]
    entry["status"] = "executing"
    entry["execution_started_at"] = _now_iso()
    return True, "executing"


def finish_execution(approval_id: str, result: str) -> None:
    """실행 완료 후 executed로 확정한다. 실행 결과가 불명확해도(§10-1) 자동 재실행 대상에서 빼기
    위해 executing 상태를 executed로 확정하는 것이 이 함수의 역할이다 — 호출부가 실행 성공/실패를
    가리지 않고 반드시 한 번 불러 상태를 종결시켜야 한다.
    """
    entry = _STORE.get(approval_id)
    if entry is not None:
        entry["status"] = "executed"
        entry["result"] = result
        entry["executed_at"] = _now_iso()


def reject(approval_id: str) -> tuple[bool, str]:
    """pending -> rejected. 이미 실행 중/완료/거절된 건 실패.

    반환: (성공 여부, 실패 사유 또는 "rejected").
    """
    entry = _STORE.get(approval_id)
    if entry is None:
        return False, "not_found"
    if entry["status"] != "pending":
        return False, entry["status"]
    entry["status"] = "rejected"
    entry["rejected_at"] = _now_iso()
    return True, "rejected"


def is_terminal(status: str) -> bool:
    return status in _TERMINAL_STATUSES
