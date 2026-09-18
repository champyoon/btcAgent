"""전체 테스트 공통 설정 — 실제 data/*.json을 절대 건드리지 않도록 모든 테스트를 격리한다.

기획팀 요청(2026-09-17): "실제 데이터나 개인 장부를 훼손하지 않도록 테스트 데이터를 격리해주세요."
ledger.LEDGER_PATH / month_state.STATE_PATH / price_history.HISTORY_PATH를 매 테스트마다
tmp_path 아래 새 파일로 바꿔치기하고, approvals._STORE / month_state._PENDING_STRATEGY_CHANGES
같은 프로세스 메모리 상태도 초기화한다(autouse라 모든 테스트에 자동 적용).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    import agent
    import approvals
    import ledger
    import month_state as ms
    import price_history as ph

    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(ms, "STATE_PATH", tmp_path / "month_state.json")
    monkeypatch.setattr(ph, "HISTORY_PATH", tmp_path / "price_history.json")

    approvals._STORE.clear()
    ms._PENDING_STRATEGY_CHANGES.clear()
    ms._PENDING_BUDGET_CHANGES.clear()
    ms._CONSUMED_BUDGET_TOKENS.clear()
    # 후속 입력 상태(awaiting_input, 2026-09-20 추가) — request_monthly_budget_amount를 도구
    # 함수로 직접 호출하는 테스트가 토큰을 남기면 다음 테스트에 새어나갈 수 있다.
    agent._PENDING_AWAITING_INPUT.clear()
    agent._LAST_AWAITING_INPUT = None
    agent._LAST_BUDGET_PROPOSAL = None
    agent._LAST_STRATEGY_PROPOSAL = None
    agent._LAST_DATA_GAP = None
    yield
