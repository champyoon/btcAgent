"""실제 매수·관망 기록 + 월 예산 계산 — SPEC.md §4·§4-1 확정 스키마·규칙 그대로 구현.

전부 예외를 던지지 않고 `{"ok": bool, ...}` 형태로 결과/오류를 돌려준다(기존 `src/tools.py` 도구들의
관례와 동일). 승인(§10-1)은 이 모듈이 아니라 `src/approvals.py`가 맡는다 — 이 모듈은 "승인된 뒤
실제로 무엇을 어떻게 저장하는가"만 다룬다.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
LEDGER_PATH = DATA_DIR / "ledger.json"

KST = timezone(timedelta(hours=9))

_ID_PATTERN = re.compile(r"^(b|w)_(\d{4,})$")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(entry: dict) -> dict:
    """옛 스키마(§4-1 이전, record_id 등이 없는 항목)를 읽을 때 기본값을 채운다.

    실제로 새로 쓸 때는 항상 전체 필드를 채운다 — 여기서는 "읽었을 때 죽지 않게" 하는 역할만 한다.
    """
    if "record_id" in entry:
        return entry
    executed_date = entry.get("recorded_at", _now_utc_iso())[:10]
    return {
        "record_id": None,  # 마이그레이션 전 — 조회는 되지만 amend/cancel 대상으로 지목은 못 한다.
        "type": entry.get("type", "buy"),
        "amount_krw": entry["amount_krw"],
        "price_krw": entry.get("price_krw"),
        "quantity_btc": entry.get("quantity_btc"),
        "note": entry.get("note", ""),
        "executed_at": None,
        "executed_date": executed_date,
        "execution_time_precision": "date",
        "recorded_at": entry.get("recorded_at", _now_utc_iso()),
        "status": entry.get("status", "active"),
        "history": entry.get("history", []),
    }


def load_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return [_normalize(e) for e in raw]


def save_ledger(entries: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_record_id(entries: list[dict], prefix: str) -> str:
    max_seq = 0
    for e in entries:
        rid = e.get("record_id") or ""
        m = _ID_PATTERN.match(rid)
        if m and m.group(1) == prefix:
            max_seq = max(max_seq, int(m.group(2)))
    return f"{prefix}_{max_seq + 1:04d}"


def _validate_amount_price_quantity(
    amount_krw: float, price_krw: float | None, quantity_btc: float | None
) -> tuple[bool, str]:
    """§4-1: 금액·가격·수량은 유한한 양수. 수량과 가격을 함께 받으면 서로 불일치하지 않아야 한다."""
    for name, v in (("amount_krw", amount_krw), ("price_krw", price_krw), ("quantity_btc", quantity_btc)):
        if v is None:
            continue
        if not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")) or v <= 0:
            return False, f"{name}은(는) 유한한 양수여야 합니다."
    if price_krw is None and quantity_btc is None:
        return False, "price_krw 또는 quantity_btc 중 하나는 있어야 합니다."
    if price_krw is not None and quantity_btc is not None:
        implied_qty = amount_krw / price_krw
        if implied_qty == 0:
            return False, "price_krw 기준으로 역산한 수량이 0입니다."
        diff_ratio = abs(implied_qty - quantity_btc) / implied_qty
        if diff_ratio > 0.01:  # 1% 초과 차이면 입력 불일치로 본다(수수료·반올림 정도는 허용)
            return False, (
                f"amount_krw/price_krw로 역산한 수량({implied_qty:.8f})과 입력한 quantity_btc"
                f"({quantity_btc:.8f})의 차이가 1%를 넘습니다 — 값을 확인해주세요."
            )
    return True, ""


def _validate_execution_time(
    executed_date: str | None, executed_at: str | None, precision: str
) -> tuple[bool, str]:
    if precision not in ("date", "datetime"):
        return False, "execution_time_precision은 'date' 또는 'datetime'이어야 합니다."
    if not executed_date:
        return False, "executed_date는 필수입니다."
    if precision == "date":
        if executed_at is not None:
            return False, "precision이 'date'면 executed_at은 비워야 합니다(임의 시각을 만들지 않음)."
        return True, ""
    # precision == "datetime"
    if not executed_at:
        return False, "precision이 'datetime'이면 executed_at이 필요합니다."
    try:
        dt = datetime.fromisoformat(executed_at)
    except ValueError:
        return False, f"executed_at 형식을 해석할 수 없습니다: {executed_at}"
    if dt.tzinfo is None:
        return False, "executed_at은 시간대 정보를 포함해야 합니다."
    kst_date = dt.astimezone(KST).date().isoformat()
    if kst_date != executed_date:
        return False, f"executed_at을 KST로 환산한 날짜({kst_date})가 executed_date({executed_date})와 다릅니다."
    return True, ""


def _effective_qty(entry: dict) -> float:
    if entry.get("quantity_btc") is not None:
        return entry["quantity_btc"]
    return entry["amount_krw"] / entry["price_krw"]


def record_virtual_buy(
    amount_krw: float,
    price_krw: float | None = None,
    quantity_btc: float | None = None,
    executed_date: str | None = None,
    executed_at: str | None = None,
    execution_time_precision: str = "date",
    note: str = "",
) -> dict:
    """실제 매수 기록을 한 건 추가한다(§4·§4-1). 승인 이후 호출되는 것을 전제로 한다."""
    ok, err = _validate_amount_price_quantity(amount_krw, price_krw, quantity_btc)
    if not ok:
        return {"ok": False, "error": err}
    ok, err = _validate_execution_time(executed_date, executed_at, execution_time_precision)
    if not ok:
        return {"ok": False, "error": err}

    entries = load_ledger()
    record = {
        "record_id": _next_record_id(entries, "b"),
        "type": "buy",
        "amount_krw": amount_krw,
        "price_krw": price_krw,
        "quantity_btc": quantity_btc,
        "note": note,
        "executed_at": executed_at,
        "executed_date": executed_date,
        "execution_time_precision": execution_time_precision,
        "recorded_at": _now_utc_iso(),
        "status": "active",
        "history": [],
    }
    entries.append(record)
    save_ledger(entries)
    return {"ok": True, "record": record}


def record_watch_decision(
    note: str,
    indicators_snapshot: dict | None = None,
    strategy_condition_status: dict | None = None,
) -> dict:
    """관망 기록 — §2-2: 승인 불필요, 매수 기록과 같은 장부를 공유하고 type으로 구분한다."""
    entries = load_ledger()
    record = {
        "record_id": _next_record_id(entries, "w"),
        "type": "watch",
        "note": note,
        "indicators_snapshot": indicators_snapshot or {},
        "strategy_condition_status": strategy_condition_status or {},
        "recorded_at": _now_utc_iso(),
        "status": "active",
        "history": [],
    }
    entries.append(record)
    save_ledger(entries)
    return {"ok": True, "record": record}


_UNSET = object()


def amend_virtual_buy(
    record_id: str,
    amount_krw: float | None = _UNSET,
    price_krw: float | None = _UNSET,
    quantity_btc: float | None = _UNSET,
    executed_date: str | None = _UNSET,
    executed_at: str | None = _UNSET,
    execution_time_precision: str | None = _UNSET,
    note: str | None = _UNSET,
    reason: str = "",
) -> dict:
    """매수 기록 수정 — §4-1: 지정한 필드만 갱신, 미전달 필드는 유지, 변경 전 상태를 history에 보존.

    각 인자의 기본값은 "전달 안 함"(_UNSET)이다. 명시적으로 None을 넘기는 것과 아예 안 넘기는 것을
    구분해야 하므로(§4-1 "필드 누락과 명시적 null을 구분"), 파이썬 기본값으로 None을 쓰지 않는다.
    """
    entries = load_ledger()
    idx = next((i for i, e in enumerate(entries) if e.get("record_id") == record_id), None)
    if idx is None:
        return {"ok": False, "error": f"record_id를 찾을 수 없습니다: {record_id}"}
    target = entries[idx]
    if target["type"] != "buy":
        return {"ok": False, "error": "매수 기록만 수정할 수 있습니다."}
    if target["status"] != "active":
        return {"ok": False, "error": f"'{target['status']}' 상태인 기록은 수정할 수 없습니다."}

    updated = dict(target)
    for field, value in (
        ("amount_krw", amount_krw),
        ("price_krw", price_krw),
        ("quantity_btc", quantity_btc),
        ("executed_date", executed_date),
        ("executed_at", executed_at),
        ("execution_time_precision", execution_time_precision),
        ("note", note),
    ):
        if value is not _UNSET:
            updated[field] = value

    ok, err = _validate_amount_price_quantity(updated["amount_krw"], updated["price_krw"], updated["quantity_btc"])
    if not ok:
        return {"ok": False, "error": err}
    ok, err = _validate_execution_time(
        updated["executed_date"], updated["executed_at"], updated["execution_time_precision"]
    )
    if not ok:
        return {"ok": False, "error": err}

    history_entry = {k: v for k, v in target.items() if k != "history"}
    history_entry["amended_at"] = _now_utc_iso()
    history_entry["reason"] = reason
    updated["history"] = target.get("history", []) + [history_entry]

    entries[idx] = updated
    save_ledger(entries)
    return {"ok": True, "record": updated}


def cancel_virtual_buy(record_id: str, reason: str = "") -> dict:
    """매수 기록 취소 — §4-1: 삭제가 아니라 status를 cancelled로 전환, 이전 상태를 history에 보존."""
    entries = load_ledger()
    idx = next((i for i, e in enumerate(entries) if e.get("record_id") == record_id), None)
    if idx is None:
        return {"ok": False, "error": f"record_id를 찾을 수 없습니다: {record_id}"}
    target = entries[idx]
    if target["type"] != "buy":
        return {"ok": False, "error": "매수 기록만 취소할 수 있습니다."}
    if target["status"] != "active":
        return {"ok": False, "error": f"이미 '{target['status']}' 상태입니다."}

    history_entry = {k: v for k, v in target.items() if k != "history"}
    history_entry["cancelled_at"] = _now_utc_iso()
    history_entry["reason"] = reason

    updated = dict(target)
    updated["status"] = "cancelled"
    updated["history"] = target.get("history", []) + [history_entry]

    entries[idx] = updated
    save_ledger(entries)
    return {"ok": True, "record": updated, "note": "장부 정정입니다 — 실제 거래 취소가 아닙니다."}


def search_ledger() -> list[dict]:
    return load_ledger()


def reset_ledger() -> dict:
    count = len(load_ledger())
    save_ledger([])
    return {"ok": True, "cleared_count": count}


def month_budget_status(monthly_budget: float, year: int, month: int, entries: list[dict] | None = None) -> dict:
    """§4: 남은 예산 = 월 예산 - 해당 월 유효(active) 매수금액 합. 관망 기록은 집계에서 제외."""
    entries = entries if entries is not None else load_ledger()
    prefix = f"{year:04d}-{month:02d}"
    active_buys = [
        e for e in entries if e["type"] == "buy" and e["status"] == "active" and (e.get("executed_date") or "").startswith(prefix)
    ]
    spent = sum(e["amount_krw"] for e in active_buys)
    remaining = monthly_budget - spent
    return {
        "year": year,
        "month": month,
        "monthly_budget_krw": monthly_budget,
        "spent_krw": spent,
        "remaining_krw": remaining,
        "suggested_next_buy_krw": max(remaining, 0.0),
        "over_budget": remaining < 0,
        "buy_count": len(active_buys),
    }


def average_buy_price(entries: list[dict] | None = None) -> float | None:
    """§4: 평균 매수가 = 총 유효 매수금액 / 총 유효 매수 BTC 수량. 유효 매수가 없으면 계산 불가(None)."""
    entries = entries if entries is not None else load_ledger()
    active_buys = [e for e in entries if e["type"] == "buy" and e["status"] == "active"]
    total_amount = sum(e["amount_krw"] for e in active_buys)
    total_qty = sum(_effective_qty(e) for e in active_buys)
    if total_qty <= 0:
        return None
    return total_amount / total_qty
