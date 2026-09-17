"""run_eval.py / llm_as_judge.py가 공유하는 평가용 상태 시딩 — 격리된 _eval_scratch에만 쓴다.

일부 문항(#7 전략 변경, #13 15일 마감, #16 매수 기록 수정)은 "이미 시작된 계획"과 "지난달 매수
기록"이 있다는 전제로 쓰여 있다. 실제 data/*.json에는 그런 상태가 없어(계획 미시작) 그대로 두면
이 전제 자체가 성립하지 않아 실패한다(2026-09-17 발견) — 평가 전용 격리 상태에 미리 만들어 둔다.

#14(놓친 신호 재계산)는 이번 달에 전략이 선택돼 있고 첫 매수 기록이 있어야 실제로 재계산 로직을
거친다 — 안 그러면 "전략 미선택"으로 즉시 끝나 evaluate_current_condition의 실제 동작을 검증하지
못한다(2026-09-18 발견, LLM-as-Judge에서 드러남).
"""

from __future__ import annotations

from datetime import datetime


def seed_eval_state(ledger, ms) -> None:
    now = datetime.now(ms.KST)
    prev_y, prev_m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    ms.init_plan(2_000_000, now=datetime(prev_y, prev_m, 1, tzinfo=ms.KST))
    ledger.record_virtual_buy(
        amount_krw=1_000_000,
        price_krw=100_000_000,
        executed_date=f"{prev_y:04d}-{prev_m:02d}-15",
        execution_time_precision="date",
    )

    this_y, this_m = now.year, now.month
    prop = ms.propose_strategy_change(this_y, this_m, "rsi", now=now)
    if prop["ok"]:
        ms.confirm_strategy_change(prop["confirmation_token"], now=now)
    ledger.record_virtual_buy(
        amount_krw=1_000_000,
        price_krw=100_000_000,
        executed_date=f"{this_y:04d}-{this_m:02d}-01",
        execution_time_precision="date",
    )
