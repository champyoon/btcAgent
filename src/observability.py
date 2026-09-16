"""BTC DCA 어시스턴트 — 계측과 누적 평가 (Day7 패턴)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

_VALID_EVENTS = {"llm_start", "llm_end", "tool_start", "tool_end"}


class TraceCollector:
    """실행 기록을 남깁니다. 한 줄에 이벤트 하나 — 중간에 죽어도 그때까지 기록은 남습니다."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: str, name: str, **fields: Any) -> None:
        if event not in _VALID_EVENTS:
            raise ValueError(f"알 수 없는 이벤트 종류입니다: {event}")
        self.events.append({"event": event, "name": name, "ts": time.time(), **fields})

    def dump(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for entry in self.events:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_eval(answer_fn: Callable[[str], Any], eval_set: list[dict], judge=None) -> dict:
    """평가셋을 돌려 유형별 통과율을 집계합니다.

    한 문항이 예외를 던져도 전체가 멈추지 않고 그 문항만 status="error"로 남깁니다.
    judge가 없으면 답이 비어 있지 않은지만 봅니다.
    """
    by_type: dict[str, dict] = {}
    failures: list[dict] = []
    passed_total = 0

    for item in eval_set:
        qtype = item.get("type", "unknown")
        bucket = by_type.setdefault(qtype, {"total": 0, "passed": 0})
        bucket["total"] += 1

        ok = False
        try:
            answer = answer_fn(item["question"])
            ok = judge(item, answer) if judge is not None else bool(str(answer or "").strip())
            if not ok:
                failures.append(
                    {"id": item.get("id"), "type": qtype, "status": "fail", "detail": str(answer)[:200]}
                )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "id": item.get("id"),
                    "type": qtype,
                    "status": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

        if ok:
            bucket["passed"] += 1
            passed_total += 1

    return {"total": len(eval_set), "passed": passed_total, "by_type": by_type, "failures": failures}


def compare_eval(before: dict, after: dict) -> dict:
    """두 run_eval 결과를 문항 단위로 비교해 개선/회귀를 분리합니다."""
    before_failed = {f["id"] for f in before.get("failures", []) if f.get("id")}
    after_failed = {f["id"] for f in after.get("failures", []) if f.get("id")}

    fixed = sorted(before_failed - after_failed)
    regressed = sorted(after_failed - before_failed)
    delta = after.get("passed", 0) - before.get("passed", 0)

    return {"fixed": fixed, "regressed": regressed, "delta": delta, "safe": len(regressed) == 0}
