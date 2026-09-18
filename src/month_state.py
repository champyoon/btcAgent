"""월별 상태(예산·전략 선택·계획 시작월) 관리 + 전략 변경 마감·놓친 신호 재계산 — SPEC.md §2·§4·§5.

지금 코드에는 "이번 달 예산이 얼마고 어떤 전략을 골랐는지"를 추적하는 개념 자체가 없었다(SPEC §2
구현 현황 확인). 이 모듈이 그 상태 저장소다. 장부 자체(집계)는 `ledger.py`가 맡고, 이 모듈은 그
위에 "월 단위 계획 상태"만 얹는다.
"""

from __future__ import annotations

import calendar
import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ledger
import price_history
from strategy import biweekly_target_day, first_trigger

# BTC_AGENT_DATA_DIR가 설정돼 있으면 그 경로를 쓴다(수동 테스트용 격리 데이터 디렉터리 지정 —
# 미설정 시 기존과 동일하게 실제 data/ 디렉터리를 그대로 쓴다).
DATA_DIR = Path(os.environ.get("BTC_AGENT_DATA_DIR") or (Path(__file__).resolve().parents[1] / "data"))
STATE_PATH = DATA_DIR / "month_state.json"

KST = timezone(timedelta(hours=9))
BIWEEKLY_CUTOFF_HOUR = 9  # §5: 15일 09:00(KST) 이후 정기 분할 변경 불가
STRATEGIES = ("decline_day", "biweekly", "rsi")


def _month_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _next_month(key: tuple[int, int]) -> tuple[int, int]:
    y, m = key
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"plan_start_month": None, "budget_history": [], "months": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def init_plan(monthly_budget_krw: float, now: datetime | None = None, start_month: str | None = None) -> dict:
    """§2-0 최초 이용.

    2026-09-20 정책 변경: 이전에는 "오늘이 1일이면 이번 달부터, 아니면 다음 달부터"만 가능했다
    (월중 최초 이용자는 무조건 다음 달로 미뤄졌다). 이제 이번 달/다음 달 중 사용자가 확인한 달로
    직접 시작할 수 있다 — `start_month`("YYYY-MM")을 명시하면 그대로 쓰고(제안→확인 절차에서 이미
    이번 달/다음 달 중 하나로 검증된 값만 넘어온다, `_budget_plan_snapshot` 참고), 생략하면(직접
    호출하는 기존 테스트·호출부 호환용) 예전 규칙(오늘이 1일이면 이번 달, 아니면 다음 달)으로
    계산한다."""
    if monthly_budget_krw <= 0:
        return {"ok": False, "error": "monthly_budget_krw는 양수여야 합니다."}
    now = now or datetime.now(KST)
    state = load_state()
    if state.get("plan_start_month"):
        return {"ok": False, "error": "이미 초기화된 계획이 있습니다(월 예산 변경은 set_monthly_budget 사용)."}

    if start_month:
        start_str = start_month
    else:
        start_key = (now.year, now.month) if now.day == 1 else _next_month((now.year, now.month))
        start_str = _month_str(*start_key)
    state["plan_start_month"] = start_str
    state["budget_history"] = [{"amount_krw": monthly_budget_krw, "effective_month": start_str}]
    state.setdefault("months", {})
    save_state(state)
    return {"ok": True, "plan_start_month": start_str, "monthly_budget_krw": monthly_budget_krw}


def set_monthly_budget(amount_krw: float, now: datetime | None = None) -> dict:
    """§4: 월 예산 변경은 항상 다음 달부터 적용한다. 이번 달 예산은 그대로 유지된다."""
    if amount_krw <= 0:
        return {"ok": False, "error": "amount_krw는 양수여야 합니다."}
    now = now or datetime.now(KST)
    state = load_state()
    if not state.get("plan_start_month"):
        return {"ok": False, "error": "먼저 계획을 시작해야 합니다(init_plan)."}

    effective = _month_str(*_next_month((now.year, now.month)))
    history = state.setdefault("budget_history", [])
    history[:] = [h for h in history if h["effective_month"] != effective]  # 같은 달 재예약은 마지막 값으로 대체
    history.append({"amount_krw": amount_krw, "effective_month": effective})
    history.sort(key=lambda h: h["effective_month"])
    save_state(state)
    return {"ok": True, "amount_krw": amount_krw, "effective_month": effective}


# ══════════════════════════════════════════════════════════════════
# 월 예산 설정/변경 — 서버 검증 확인(propose -> confirm) — SPEC §4-2(2026-09-18 기획 변경): 원래
# init_plan/set_monthly_budget을 도구에서 직접 즉시 반영했으나, "설명 요청"과 "실제 설정 지시"를
# LLM이 잘못 구분하면 확인 절차 없이 바로 반영돼버리는 위험이 있어 select_strategy와 같은
# propose→confirm 구조로 통일했다. 다만 select_strategy의 _PENDING_STRATEGY_CHANGES에는 명시적
# Lock이 없었던 반면(그 경로는 실제 경쟁 조건이 신고된 적 없음), 이 경로는 명시적으로 요청받아
# threading.Lock을 쓴다 — approvals.py가 "GIL만 믿어도 된다"고 여겼다가 실제 스레드 경합 검증에서
# 문제가 될 뻔했던 사례를 반복하지 않는다.
# ══════════════════════════════════════════════════════════════════

_BUDGET_LOCK = threading.Lock()
_PENDING_BUDGET_CHANGES: dict[str, dict] = {}
# 확인/취소로 이미 소진된 토큰 — 재사용 시도를 "존재한 적 없음"(404)과 구분해 "이미 처리됨"(409)으로
# 답하기 위해 별도로 기억해둔다. 서버가 재시작되면 이것도 함께 사라진다(§10-1과 같은 이유의 한계).
_CONSUMED_BUDGET_TOKENS: dict[str, str] = {}


def _budget_plan_snapshot(now: datetime, state: dict, requested_month: str | None = None) -> dict:
    """예산 제안이 세 가지 중 어느 동작인지, 적용월이 언제인지를 계산한다. propose 때와 confirm
    때 각각 다시 호출해서 그 사이 시점이 달라졌는지(예: 월 경계를 넘김) 비교하는 데 쓴다.

    2026-09-20 정책 변경(월중에도 이번 달부터 DCA 시작 지원) — 세 가지 동작:
    - **"initial"**: 계획이 아예 없다(`plan_start_month`가 None). 이번 달 또는 다음 달 중
      선택할 수 있다 — `requested_month`가 그 둘 중 하나면 그대로, 없으면 **이번 달을 기본으로
      제안한다**(이전에는 "오늘이 1일이 아니면 무조건 다음 달"이었다 — 이번 정책 변경으로 폐기).
      그 둘이 아닌 달을 요청했으면(과거 달 포함) 이번 달로 대체하고 `month_mismatch`를 True로
      남긴다 — 조용히 다른 달로 바꾸지 않고 그 사실을 구조화된 필드로 알린다.
    - **"advance"**: 계획은 있지만 아직 시작되지 않았고(`plan_start_month`가 이번 달보다 미래),
      사용자가 정확히 "이번 달"을 요청했다 — 아직 시작 안 한 계획의 시작월을 이번 달로 앞당기는
      경우다(§2). 기존에 설정된 미래 시작월의 예산 항목은 이 계산만으로는 건드리지 않는다
      (`advance_plan_start`가 실제로 반영할 때 보존한다).
    - **"change"**: 그 외 전부(계획이 이미 시작됐거나, 아직 시작 전이지만 이번 달로 당기라는
      요청이 아닌 경우) — 기존 §4 규칙 그대로 **항상 다음 달부터** 적용한다.
    """
    plan_start = state.get("plan_start_month")
    this_month = _month_str(now.year, now.month)
    next_month = _month_str(*_next_month((now.year, now.month)))

    if plan_start is None:
        allowed = {this_month, next_month}
        if requested_month and requested_month in allowed:
            effective_month = requested_month
            mismatch = False
        else:
            effective_month = this_month
            mismatch = bool(requested_month)  # 뭔가 요청했는데 허용 범위 밖이었다
        return {"action": "initial", "is_initial": True, "effective_month": effective_month, "month_mismatch": mismatch}

    if plan_start > this_month and requested_month == this_month:
        prev_amount = effective_budget_for(*(int(x) for x in plan_start.split("-")), state)
        return {
            "action": "advance",
            "is_initial": False,
            "effective_month": this_month,
            "month_mismatch": False,
            "previous_start_month": plan_start,
            "previous_start_amount": prev_amount,
        }

    mismatch = bool(requested_month) and requested_month != next_month
    return {"action": "change", "is_initial": False, "effective_month": next_month, "month_mismatch": mismatch}


def advance_plan_start(amount_krw: float, new_start_month: str, now: datetime | None = None) -> dict:
    """§2: 아직 시작하지 않은(미래 시작월로 예약된) 계획의 시작월을 이번 달로 앞당긴다.

    기존에 설정돼 있던 미래 시작월의 예산 항목은 **그대로 보존**한다(삭제·이번 달로 복사 둘 다
    하지 않는다) — 새 시작월(이번 달)에 대한 예산 항목만 새로 추가한다. 실제 매수 기록·전략
    선택에는 손대지 않는다(이 함수가 건드리는 건 plan_start_month와 budget_history뿐이다)."""
    if amount_krw <= 0:
        return {"ok": False, "error": "amount_krw는 양수여야 합니다."}
    now = now or datetime.now(KST)
    state = load_state()
    plan_start = state.get("plan_start_month")
    this_month = _month_str(now.year, now.month)
    if not plan_start:
        return {"ok": False, "error": "아직 시작된 계획이 없습니다(init_plan이 먼저 필요합니다)."}
    if plan_start <= this_month:
        return {"ok": False, "error": "이미 시작된 계획입니다 — 앞당길 대상인 미래 시작월이 없습니다."}
    if new_start_month != this_month:
        return {"ok": False, "error": "시작월은 이번 달로만 앞당길 수 있습니다."}

    previous_start_month = plan_start
    state["plan_start_month"] = new_start_month
    history = state.setdefault("budget_history", [])
    history[:] = [h for h in history if h["effective_month"] != new_start_month]  # 재확인 대비 — 이번 달 항목만 교체
    history.append({"amount_krw": amount_krw, "effective_month": new_start_month})
    history.sort(key=lambda h: h["effective_month"])
    save_state(state)
    return {
        "ok": True,
        "plan_start_month": new_start_month,
        "amount_krw": amount_krw,
        "previous_start_month": previous_start_month,
    }


def propose_budget_change(amount_krw: float, requested_month: str | None = None, now: datetime | None = None) -> dict:
    """1단계: 실제로 반영하지 않고 제안만 만든다. 제안 시점의 계획 상태(스냅샷)를 함께 저장해
    confirm 시점에 "그 사이 다른 변경으로 낡은 제안이 됐는지" 비교할 수 있게 한다.

    requested_month("YYYY-MM"): 사용자가 "9월부터 시작할게"/"9월 예산은 200만원으로 할게"처럼
    특정 월을 명시했을 때 그 달을 받는다. 실제 동작(초기 설정/앞당기기/변경)과 적용월은
    `_budget_plan_snapshot`이 결정한다 — 요청 월이 그대로 받아들여지지 않으면(과거 달 등)
    `month_mismatch`가 True로 돌아온다. 이 사실을 자유 서술에만 맡기면 예산 제안이 있을 때
    plan_agent의 텍스트 자체가 최종 답변에서 빠지므로(_build_final_answer의 구조적 제외,
    agent.py 참고) 이유가 통째로 사라진다 — 그래서 반환값에 구조화된 필드로 담아 agent.py가
    최종 답변 요약 블록에 직접 반영하게 한다."""
    if amount_krw <= 0:
        return {"ok": False, "error": "amount_krw는 양수여야 합니다."}
    now = now or datetime.now(KST)
    state = load_state()
    calc = _budget_plan_snapshot(now, state, requested_month=requested_month)
    token = uuid.uuid4().hex
    pending = {
        "amount_krw": amount_krw,
        "action": calc["action"],
        "effective_month": calc["effective_month"],
        "is_initial": calc["is_initial"],
        "requested_month": requested_month,
        "month_mismatch": calc["month_mismatch"],
        "previous_start_month": calc.get("previous_start_month"),
        "previous_start_amount": calc.get("previous_start_amount"),
        "snapshot_plan_start_month": state.get("plan_start_month"),
        "snapshot_budget_history": list(state.get("budget_history", [])),
    }
    with _BUDGET_LOCK:
        _PENDING_BUDGET_CHANGES[token] = pending
    return {"ok": True, "confirmation_token": token, **{k: v for k, v in pending.items() if not k.startswith("snapshot_")}}


def confirm_budget_change(confirmation_token: str, now: datetime | None = None) -> dict:
    """2단계: 사용자가 동의한 뒤에만 호출된다. Lock 안에서 토큰 조회·낡은 제안 판정·실제 반영까지
    한 번에 끝내 confirm/cancel 경합, confirm 두 번 경합 모두 정확히 하나만 성공하게 한다."""
    now = now or datetime.now(KST)
    with _BUDGET_LOCK:
        if confirmation_token in _CONSUMED_BUDGET_TOKENS:
            return {"ok": False, "error": "이미 처리된 확인 토큰입니다.", "already_processed": True}
        pending = _PENDING_BUDGET_CHANGES.get(confirmation_token)
        if pending is None:
            return {"ok": False, "error": "존재하지 않는 확인 토큰입니다."}

        state = load_state()
        if (
            state.get("plan_start_month") != pending["snapshot_plan_start_month"]
            or list(state.get("budget_history", [])) != pending["snapshot_budget_history"]
        ):
            del _PENDING_BUDGET_CHANGES[confirmation_token]
            _CONSUMED_BUDGET_TOKENS[confirmation_token] = "stale"
            return {
                "ok": False,
                "error": "제안 이후 다른 변경으로 계획 상태가 바뀌어 이 제안은 더 이상 유효하지 않습니다. 다시 제안해주세요.",
                "stale": True,
            }

        calc_now = _budget_plan_snapshot(now, state, requested_month=pending.get("requested_month"))
        if calc_now["action"] != pending["action"] or calc_now["effective_month"] != pending["effective_month"]:
            del _PENDING_BUDGET_CHANGES[confirmation_token]
            _CONSUMED_BUDGET_TOKENS[confirmation_token] = "stale"
            return {
                "ok": False,
                "error": "제안 이후 월 경계를 지나 적용월 계산이 바뀌었습니다. 다시 제안해주세요.",
                "stale": True,
            }

        del _PENDING_BUDGET_CHANGES[confirmation_token]
        _CONSUMED_BUDGET_TOKENS[confirmation_token] = "confirmed"
        if pending["action"] == "initial":
            return init_plan(pending["amount_krw"], now=now, start_month=pending["effective_month"])
        if pending["action"] == "advance":
            return advance_plan_start(pending["amount_krw"], pending["effective_month"], now=now)
        return set_monthly_budget(pending["amount_krw"], now=now)


def cancel_budget_change(confirmation_token: str) -> dict:
    """제안을 취소한다 — 아무 것도 반영하지 않고 토큰만 소진 처리한다(§10-1 /reject과 같은 역할)."""
    with _BUDGET_LOCK:
        if confirmation_token in _CONSUMED_BUDGET_TOKENS:
            return {"ok": False, "error": "이미 처리된 확인 토큰입니다.", "already_processed": True}
        pending = _PENDING_BUDGET_CHANGES.pop(confirmation_token, None)
        if pending is None:
            return {"ok": False, "error": "존재하지 않는 확인 토큰입니다."}
        _CONSUMED_BUDGET_TOKENS[confirmation_token] = "cancelled"
    return {"ok": True, **pending}


def effective_budget_for(year: int, month: int, state: dict | None = None) -> float | None:
    state = state if state is not None else load_state()
    target = _month_str(year, month)
    applicable = [h for h in state.get("budget_history", []) if h["effective_month"] <= target]
    if not applicable:
        return None
    return applicable[-1]["amount_krw"]


def is_plan_started(year: int, month: int, state: dict | None = None) -> bool:
    state = state if state is not None else load_state()
    start = state.get("plan_start_month")
    if not start:
        return False
    return _month_str(year, month) >= start


def biweekly_cutoff_passed(year: int, month: int, now: datetime | None = None) -> bool:
    """§5: 15일 09:00(KST) 마감."""
    now = now or datetime.now(KST)
    cutoff = datetime(year, month, 15, BIWEEKLY_CUTOFF_HOUR, 0, tzinfo=KST)
    return now >= cutoff


def month_end_cutoff_passed(year: int, month: int, now: datetime | None = None) -> bool:
    """§5: 해당 월 마지막 날 00:00(KST) 마감."""
    now = now or datetime.now(KST)
    last_day = _last_day_of_month(year, month)
    cutoff = datetime(last_day.year, last_day.month, last_day.day, 0, 0, tzinfo=KST)
    return now >= cutoff


def can_select_strategy(
    year: int, month: int, strategy: str, now: datetime | None = None, state: dict | None = None
) -> tuple[bool, str]:
    """§2·§5 확정 규칙을 전부 확인한다: 계획 시작 여부, 월말 마감, 정기분할 15일 마감."""
    if strategy not in STRATEGIES:
        return False, f"알 수 없는 전략입니다: {strategy}"
    now = now or datetime.now(KST)
    state = state if state is not None else load_state()
    if not is_plan_started(year, month, state):
        return False, f"{_month_str(year, month)}은(는) 아직 계획이 시작되지 않은 달입니다(§2-0)."
    if month_end_cutoff_passed(year, month, now):
        return False, f"{_month_str(year, month)} 마지막 날 00:00(KST)이 지나 신규 선택·변경이 마감됐습니다."
    if strategy == "biweekly" and biweekly_cutoff_passed(year, month, now):
        return False, "15일 09:00(KST)이 지나 이번 달은 정기 분할 방식으로 변경할 수 없습니다(하락일·RSI만 가능)."
    return True, ""


def get_selected_strategy(year: int, month: int, state: dict | None = None) -> str | None:
    state = state if state is not None else load_state()
    entry = state.get("months", {}).get(_month_str(year, month))
    return entry.get("strategy") if entry else None


def select_strategy(
    year: int,
    month: int,
    strategy: str,
    now: datetime | None = None,
    data_as_of: str | None = None,
) -> dict:
    """§2-1·§5: 전략 선택/변경. 확정 이후부터 적용되며, 과거 거래 기록을 소급 변경하지 않는다
    (이 함수는 상태만 바꾸고 과거 매수 기록에는 손대지 않는다 — 소급 없음을 코드 구조로 보장)."""
    now = now or datetime.now(KST)
    state = load_state()
    ok, reason = can_select_strategy(year, month, strategy, now, state)
    if not ok:
        return {"ok": False, "error": reason}

    key = _month_str(year, month)
    months = state.setdefault("months", {})
    entry = months.setdefault(key, {"strategy": None, "strategy_history": []})
    previous = entry["strategy"]
    entry["strategy_history"].append(
        {"from": previous, "to": strategy, "changed_at": now.isoformat(), "data_as_of": data_as_of}
    )
    entry["strategy"] = strategy
    save_state(state)
    return {"ok": True, "year": year, "month": month, "strategy": strategy, "previous": previous}


# ══════════════════════════════════════════════════════════════════
# 전략 변경 — 서버 검증 확인(propose -> confirm) — SPEC §14-0: select_strategy는 "서버에서
# 검증"해야 하며 일반 write 승인(§10-1 approvals.py) 대상과는 구분한다. 그래서 별도의 가벼운
# 토큰 저장소를 쓴다 — approvals.py와 구조는 비슷하지만 그 대상 목록에는 포함되지 않는다.
# ══════════════════════════════════════════════════════════════════

_PENDING_STRATEGY_CHANGES: dict[str, dict] = {}


def propose_strategy_change(year: int, month: int, strategy: str, now: datetime | None = None) -> dict:
    """1단계: 변경 가능 여부만 확인하고, 실제 적용은 하지 않는다. confirmation_token을 발급한다."""
    now = now or datetime.now(KST)
    ok, reason = can_select_strategy(year, month, strategy, now)
    if not ok:
        return {"ok": False, "error": reason}
    token = uuid.uuid4().hex
    _PENDING_STRATEGY_CHANGES[token] = {"year": year, "month": month, "strategy": strategy}
    return {"ok": True, "confirmation_token": token, "year": year, "month": month, "strategy": strategy}


def confirm_strategy_change(confirmation_token: str, now: datetime | None = None) -> dict:
    """2단계: 사용자가 확인한 뒤에만 호출된다. 토큰을 소진하고 실제로 적용한다."""
    pending = _PENDING_STRATEGY_CHANGES.pop(confirmation_token, None)
    if pending is None:
        return {"ok": False, "error": "유효하지 않거나 이미 처리된 확인 토큰입니다."}
    return select_strategy(pending["year"], pending["month"], pending["strategy"], now=now)


def cancel_strategy_change(confirmation_token: str) -> dict:
    """제안을 취소한다 — 토큰만 소진하고 아무것도 적용하지 않는다(§10-1 /reject과 같은 역할)."""
    pending = _PENDING_STRATEGY_CHANGES.pop(confirmation_token, None)
    if pending is None:
        return {"ok": False, "error": "유효하지 않거나 이미 처리된 확인 토큰입니다."}
    return {"ok": True, **pending}


def get_month_status(year: int, month: int, now: datetime | None = None) -> dict:
    """§2·§4 종합 조회: 계획 시작 여부, 예산·남은 예산, 선택 전략, 변경 가능 여부."""
    now = now or datetime.now(KST)
    state = load_state()
    plan_started = is_plan_started(year, month, state)
    budget = effective_budget_for(year, month, state)
    strategy = get_selected_strategy(year, month, state)

    result: dict[str, Any] = {
        "year": year,
        "month": month,
        "plan_start_month": state.get("plan_start_month"),
        "plan_started": plan_started,
        "monthly_budget_krw": budget,
        "strategy": strategy,
        "biweekly_change_allowed": plan_started and not biweekly_cutoff_passed(year, month, now),
        "strategy_change_allowed": plan_started and not month_end_cutoff_passed(year, month, now),
    }
    if plan_started and budget is not None:
        result.update(ledger.month_budget_status(budget, year, month))
    return result


def evaluate_current_condition(
    year: int,
    month: int,
    strategy: str,
    days_this_month: list[dict],
    rsi_by_date: dict[str, float | None],
    first_buy_price: float | None,
) -> dict:
    """§5 놓친 신호 재계산: 이번 달 "지금까지 확정된" 일봉(`days_this_month`)만으로 조건이 이미
    충족된 적 있는지 다시 계산한다. 과거 신호(예: 10일 충족)가 있었다는 사실은 그대로 안내하되,
    "그러니 지금도 충족 상태"라고 자동으로 잇지 않는다 — 이 함수의 판정 자체가 "지금까지 확정된
    데이터"만 보고 새로 계산한 결과이므로, 그 결과를 그대로 쓰면 된다.

    `days_this_month`는 이번 달 1일부터, 호출 시점 기준 §7-1 확정 경계까지의 캔들이어야 한다
    (더 최근 것을 넣으면 안 마감된 캔들을 조건 판정에 쓰는 게 되어 §7-1 원칙이 깨진다).
    """
    if not days_this_month:
        return {"triggered": False, "reason": "이번 달 확정된 일봉이 아직 없습니다."}

    # get_indicators와 같은 기준(2026-09-18) — "일봉이 몇 개 있다"가 아니라 "이번 달 1일부터
    # 공급받은 마지막 날까지 빠짐없이 있는가"를 직접 확인한다. 이 구간 안에 결측이 있으면 그
    # 결측일 앞뒤 날짜를 실제로는 연속이 아닌데 연속인 것처럼 이어붙여 조건을 잘못 판정할 수 있다.
    month_start = f"{year:04d}-{month:02d}-01"
    last_supplied = days_this_month[-1]["date_kst"]
    missing = price_history.find_missing_dates(days_this_month, month_start, last_supplied)
    if missing:
        sample = ", ".join(missing[:3]) + (" 등" if len(missing) > 3 else "")
        return {
            "triggered": False,
            "reason": f"이번 달 확정 일봉 구간에 결측이 있어 판정할 수 없습니다({sample}).",
        }

    if strategy == "biweekly":
        target = biweekly_target_day(days_this_month)
        if target is None:
            return {"triggered": False, "reason": "아직 15일 일봉이 확정되지 않았습니다."}
        return {"triggered": True, "trigger_date": target["date_kst"], "buy_date": target["date_kst"]}

    if first_buy_price is None:
        return {"triggered": False, "reason": "이번 달 첫 매수 기록이 아직 없어 하락일 조건을 판정할 수 없습니다."}

    trigger = first_trigger(strategy, days_this_month, first_buy_price, rsi_by_date, include_last_day=True)
    if trigger is None:
        return {"triggered": False, "reason": "지금까지 확정된 데이터로는 조건이 충족된 적이 없습니다."}
    return {"triggered": True, **trigger}
