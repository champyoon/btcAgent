"""승인 ID 기반 실행 게이트 — SPEC.md §10-1 확정 설계.

핵심: `/approve`는 클라이언트가 보낸 tool/args를 절대 신뢰하지 않는다. `/query`가 승인이 필요한
도구 호출을 발견했을 때만 이 모듈이 approval_id를 발급해 tool/args를 서버에 저장해두고, `/approve`는
그 id로 조회한 값을 그대로 실행한다 — `/query`를 거치지 않은 approval_id는 애초에 존재하지 않으므로
뒷문 호출 자체가 불가능하다.

저장 위치는 서버 프로세스 메모리(dict)다 — 단일 프로세스·개인 1인·저빈도 사용이라 파일/DB
영속화는 불필요하다(재시작하면 대기 중 승인이 사라지는 건 알려진 한계). TTL 없음, 1회용.

동시성: pending → executing/rejected 전환은 명시적 `threading.Lock`으로 보호한다(2026-09-18 —
이전에는 "check-then-set 사이에 await가 없어 CPython GIL만으로 원자적"이라고 문서화했었는데,
이건 표준 CPython의 구현 세부사항에 기댄 것이지 언어 차원의 보장이 아니다(free-threaded 빌드처럼
GIL이 없는 인터프리터에선 성립하지 않는다). 게다가 FastAPI/Starlette가 동기(`def`, `async def`
아님) 라우트 핸들러를 스레드풀에서 실행하므로, `/approve`/`/reject`에 대한 동시 요청은 실제로
서로 다른 OS 스레드에서 실행될 수 있다 — "우연히 안전했다"가 아니라 실제로 보장되게 락을 건다.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

_STORE: dict[str, dict] = {}
_LOCK = threading.Lock()

# 상태 전이: pending -> executing -> executed
#            pending -> rejected
_TERMINAL_STATUSES = ("executed", "rejected")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(tool: str, args: dict[str, Any], reason: str = "") -> str:
    """승인이 필요한 도구 호출을 등록하고 approval_id를 발급한다."""
    approval_id = uuid.uuid4().hex
    entry = {
        "approval_id": approval_id,
        "tool": tool,
        "args": args,
        "reason": reason,
        "status": "pending",
        "created_at": _now_iso(),
    }
    with _LOCK:
        _STORE[approval_id] = entry
    return approval_id


def get(approval_id: str) -> dict | None:
    with _LOCK:
        entry = _STORE.get(approval_id)
        return dict(entry) if entry is not None else None


def begin_execution(approval_id: str) -> tuple[bool, str]:
    """pending -> executing. 이미 다른 상태면 실패(404/409 판단은 호출부의 몫).

    check(status가 pending인지)와 set(executing으로 전환)을 같은 락 구간 안에서 하므로, 여러
    스레드가 동시에 같은 approval_id로 이 함수를 불러도 정확히 하나만 성공한다(실제 OS 스레드
    50개 × 50회 경합으로 검증 — tests/test_approvals.py).

    반환: (성공 여부, 실패 사유 또는 "executing"). 실패 사유는 "not_found" 또는 현재 status.
    """
    with _LOCK:
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
    with _LOCK:
        entry = _STORE.get(approval_id)
        if entry is not None:
            entry["status"] = "executed"
            entry["result"] = result
            entry["executed_at"] = _now_iso()


def reject(approval_id: str) -> tuple[bool, str]:
    """pending -> rejected. 이미 실행 중/완료/거절된 건 실패.

    begin_execution과 같은 락을 공유하므로, 한쪽이 승인 실행을 시작하는 순간과 다른 쪽이 거절하는
    순간이 동시에 들어와도 둘 중 하나만 이긴다(먼저 락을 잡은 쪽이 pending을 소비해버려, 나중
    쪽은 "이미 처리됨"으로 실패한다).

    반환: (성공 여부, 실패 사유 또는 "rejected").
    """
    with _LOCK:
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
