"""월 예산 설정/변경 확인(propose→confirm/cancel) — SPEC §4-2(2026-09-18 기획 변경).

`set_monthly_budget`을 승인 없이 즉시 반영하던 이전 방식에서, select_strategy와 같은
"제안 → 사용자 확인 → 저장" 구조로 바꿨다. 여기서는 그 서버 쪽 강제(제안만으로는 절대 반영되지
않음, 토큰 1회용, 확인·취소 경합 방지, 변조 인자 무시, 낡은 제안 재확인)를 확인한다 — 실제
`/query`를 통한 의도 판별(설명 요청 vs 실제 설정 지시, BTC 전용 안내)은
`evaluation/manual_test_guide.md`의 실 서버 재현 절차로 별도 확인한다.
"""

from __future__ import annotations

import threading
from datetime import datetime

import month_state as ms


def test_propose_alone_does_not_start_plan():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    proposal = ms.propose_budget_change(2_000_000, now=now)
    assert proposal["ok"]
    assert proposal["is_initial"] is True
    assert proposal["effective_month"] == "2026-10"

    state = ms.load_state()
    assert state.get("plan_start_month") is None, "제안만으로 계획이 시작되면 안 됨"
    assert state.get("budget_history", []) == []


def test_confirm_applies_correct_amount_and_month():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    proposal = ms.propose_budget_change(2_000_000, now=now)
    result = ms.confirm_budget_change(proposal["confirmation_token"], now=now)
    assert result["ok"]

    state = ms.load_state()
    assert state["plan_start_month"] == "2026-10"
    assert state["budget_history"] == [{"amount_krw": 2_000_000, "effective_month": "2026-10"}]


def test_cancel_leaves_state_unchanged():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    proposal = ms.propose_budget_change(3_000_000, now=now)
    result = ms.cancel_budget_change(proposal["confirmation_token"])
    assert result["ok"]

    state = ms.load_state()
    assert state.get("plan_start_month") is None
    assert state.get("budget_history", []) == []


def test_existing_plan_budget_change_applies_next_month_and_keeps_current_and_history():
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)

    proposal = ms.propose_budget_change(3_000_000, now=now)
    assert proposal["is_initial"] is False
    assert proposal["effective_month"] == "2026-10"

    ms.confirm_budget_change(proposal["confirmation_token"], now=now)
    state = ms.load_state()
    assert state["plan_start_month"] == "2026-08"  # 최초 시작월 그대로
    assert ms.effective_budget_for(2026, 9, state) == 2_000_000  # 이번 달 예산 유지
    assert ms.effective_budget_for(2026, 10, state) == 3_000_000  # 다음 달부터 새 금액


def test_double_confirm_second_attempt_rejected_as_already_processed():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]
    first = ms.confirm_budget_change(token, now=now)
    assert first["ok"]
    second = ms.confirm_budget_change(token, now=now)
    assert not second["ok"]
    assert second.get("already_processed") is True


def test_confirm_after_cancel_rejected():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]
    ms.cancel_budget_change(token)
    result = ms.confirm_budget_change(token, now=now)
    assert not result["ok"]
    assert result.get("already_processed") is True


def test_cancel_after_confirm_rejected():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]
    ms.confirm_budget_change(token, now=now)
    result = ms.cancel_budget_change(token)
    assert not result["ok"]
    assert result.get("already_processed") is True


def test_unknown_token_error_distinct_from_already_processed_token():
    unknown = ms.confirm_budget_change("no-such-token-ever")
    assert not unknown["ok"]
    assert not unknown.get("already_processed")
    assert not unknown.get("stale")

    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]
    ms.confirm_budget_change(token, now=now)
    processed = ms.confirm_budget_change(token, now=now)
    # agent.confirm_budget_change_action이 이 둘을 각각 404/409로 매핑할 수 있어야 하므로,
    # 도메인 계층에서부터 구분되는 신호(already_processed 유무)가 있어야 한다.
    assert unknown.get("already_processed") != processed.get("already_processed")


def test_stale_proposal_due_to_month_boundary_requires_reconfirmation():
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))
    propose_time = datetime(2026, 8, 31, 10, 0, tzinfo=ms.KST)
    proposal = ms.propose_budget_change(3_000_000, now=propose_time)
    assert proposal["effective_month"] == "2026-09"

    confirm_time = datetime(2026, 9, 1, 10, 0, tzinfo=ms.KST)  # 월 경계를 넘겨서 확인
    result = ms.confirm_budget_change(proposal["confirmation_token"], now=confirm_time)
    assert not result["ok"]
    assert result.get("stale") is True

    # 낡은 제안이 실제로 적용되지 않았어야 한다
    state = ms.load_state()
    assert ms.effective_budget_for(2026, 9, state) == 2_000_000


def test_stale_proposal_due_to_concurrent_state_change_requires_reconfirmation():
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)

    stale_proposal = ms.propose_budget_change(3_000_000, now=now)
    other_proposal = ms.propose_budget_change(4_000_000, now=now)
    ms.confirm_budget_change(other_proposal["confirmation_token"], now=now)  # 먼저 다른 제안이 확정됨

    result = ms.confirm_budget_change(stale_proposal["confirmation_token"], now=now)
    assert not result["ok"]
    assert result.get("stale") is True

    state = ms.load_state()
    assert ms.effective_budget_for(2026, 10, state) == 4_000_000  # 낡은 제안의 3,000,000이 아님


def test_strategy_token_cannot_confirm_budget_change_and_vice_versa():
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)

    strategy_token = ms.propose_strategy_change(2026, 10, "rsi", now=now)["confirmation_token"]
    budget_token = ms.propose_budget_change(3_000_000, now=now)["confirmation_token"]

    r1 = ms.confirm_budget_change(strategy_token, now=now)
    assert not r1["ok"], "전략 변경용 토큰으로 예산 변경이 확인되면 안 됨"

    r2 = ms.confirm_strategy_change(budget_token, now=now)
    assert not r2["ok"], "예산 변경용 토큰으로 전략 변경이 확인되면 안 됨"


def test_tampered_amount_and_effective_month_are_ignored_at_http_boundary():
    """클라이언트가 /confirm_budget_change에 amount_krw/effective_month를 함께 실어 보내도
    무시되고, 서버가 제안 시점에 저장해둔 값만 반영돼야 한다."""
    import app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]

    resp = client.post(
        "/confirm_budget_change",
        json={
            "confirmation_token": token,
            "amount_krw": 999_000_000,
            "effective_month": "2099-01",
        },
    )
    assert resp.status_code == 200

    state = ms.load_state()
    assert state["budget_history"] == [{"amount_krw": 2_000_000, "effective_month": "2026-10"}]
    assert state["plan_start_month"] == "2026-10"


def test_confirm_unknown_token_returns_404_confirm_stale_or_processed_returns_409():
    import app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)

    resp = client.post("/confirm_budget_change", json={"confirmation_token": "no-such-token"})
    assert resp.status_code == 404

    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)
    token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]
    client.post("/confirm_budget_change", json={"confirmation_token": token})  # 1차 확인(성공)
    resp2 = client.post("/confirm_budget_change", json={"confirmation_token": token})  # 재사용
    assert resp2.status_code == 409


def test_concurrent_confirm_attempts_exactly_one_winner():
    n_threads = 50
    n_trials = 50
    bad_trials = []
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)

    for trial in range(n_trials):
        ms._PENDING_BUDGET_CHANGES.clear()
        ms._CONSUMED_BUDGET_TOKENS.clear()
        token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            r = ms.confirm_budget_change(token, now=now)
            with lock:
                results.append(r["ok"])

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if results.count(True) != 1:
            bad_trials.append((trial, results.count(True)))

    assert not bad_trials, f"승자가 정확히 1명이 아닌 시행: {bad_trials}"


def test_concurrent_confirm_vs_cancel_exactly_one_winner():
    n_pairs = 25
    n_trials = 50
    bad_trials = []
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)

    for trial in range(n_trials):
        ms._PENDING_BUDGET_CHANGES.clear()
        ms._CONSUMED_BUDGET_TOKENS.clear()
        token = ms.propose_budget_change(2_000_000, now=now)["confirmation_token"]
        results: list[tuple[str, bool]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n_pairs * 2)

        def confirm_worker():
            barrier.wait()
            r = ms.confirm_budget_change(token, now=now)
            with lock:
                results.append(("confirm", r["ok"]))

        def cancel_worker():
            barrier.wait()
            r = ms.cancel_budget_change(token)
            with lock:
                results.append(("cancel", r["ok"]))

        threads = [threading.Thread(target=confirm_worker) for _ in range(n_pairs)]
        threads += [threading.Thread(target=cancel_worker) for _ in range(n_pairs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = [r for r in results if r[1]]
        if len(wins) != 1:
            bad_trials.append((trial, wins))

    assert not bad_trials, f"confirm/cancel 경합에서 승자가 정확히 1명이 아닌 시행: {bad_trials}"
