"""승인 중복/동시 실행 방지, 거절 후 실행 차단 — SPEC §10-1.

기획팀 지적(2026-09-17): "동시 승인 테스트가 단순 재호출이 아니라 실제 경합도 검증하는지 확인"
— 아래 `test_duplicate_execution_prevented_by_atomic_transition`은 같은 스레드에서 순차 호출이라
진짜 동시성 검증이 아니다. `test_concurrent_execution_real_thread_race`가 실제 OS 스레드로 같은
approval_id를 동시에 두드려 검증한다.

2026-09-18: `approvals.py`가 GIL에만 기대던 것에서 명시적 `threading.Lock`으로 바뀌었다(FastAPI가
동기 라우트를 스레드풀에서 돌리므로 `/approve`/`/reject` 동시 요청이 실제 다른 스레드에서 실행될
수 있다 — GIL 기반 주장은 표준 CPython 구현 세부사항일 뿐 보장이 아니었다). 아래 경합 테스트도
`begin_execution` 단독 경합뿐 아니라 `begin_execution` vs `reject` 교차 경합까지 추가했다.
"""

import threading

import approvals


def test_duplicate_execution_prevented_by_atomic_transition():
    aid = approvals.create("record_virtual_buy", {"amount_krw": 1}, reason="test")
    ok1, status1 = approvals.begin_execution(aid)
    assert ok1 is True
    assert status1 == "executing"

    # 이미 executing인 승인을 다시 실행하려는 시도(동시 실행 방지) — 실패해야 한다
    ok2, status2 = approvals.begin_execution(aid)
    assert ok2 is False
    assert status2 == "executing"


def test_executed_approval_cannot_be_re_executed():
    aid = approvals.create("record_virtual_buy", {"amount_krw": 1}, reason="test")
    approvals.begin_execution(aid)
    approvals.finish_execution(aid, result="ok")

    ok, status = approvals.begin_execution(aid)
    assert ok is False
    assert status == "executed"


def test_reject_then_execution_blocked():
    aid = approvals.create("reset_ledger", {}, reason="test")
    ok, status = approvals.reject(aid)
    assert ok is True
    assert status == "rejected"

    ok2, status2 = approvals.begin_execution(aid)
    assert ok2 is False
    assert status2 == "rejected"


def test_rejected_approval_cannot_be_rejected_again():
    aid = approvals.create("reset_ledger", {}, reason="test")
    approvals.reject(aid)
    ok, status = approvals.reject(aid)
    assert ok is False
    assert status == "rejected"


def test_concurrent_execution_real_thread_race():
    """`begin_execution`의 pending->executing 전환이 실제 OS 스레드 경합에서도 1승만 나오는지 확인한다.

    앞의 `test_duplicate_execution_prevented_by_atomic_transition`은 같은 스레드에서 순차 호출이라
    "동시 실행"을 실제로 재현하지 않는다 — approvals.py 주석의 "GIL 덕에 원자적"이라는 주장은
    CPython 표준 빌드(GIL 활성)에서만 성립하고, 언어 차원의 보장(명시적 락)이 아니다. 여기서는
    threading.Barrier로 여러 스레드가 정말로 동시에 begin_execution을 두드리게 만들어, 승자가
    정확히 1명만 나오는지를 여러 번 반복해 통계적으로 확인한다.
    """
    n_threads = 50
    n_trials = 50
    bad_trials = []

    for trial in range(n_trials):
        approvals._STORE.clear()
        aid = approvals.create("record_virtual_buy", {"amount_krw": 1}, reason="race")
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()  # 모든 스레드가 여기서 대기했다가 한 번에 풀려나가 진짜 동시 호출을 유도한다
            ok, _status = approvals.begin_execution(aid)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if results.count(True) != 1:
            bad_trials.append((trial, results.count(True)))

    assert not bad_trials, f"동시 실행 경합에서 승자가 1명이 아닌 시행 발견: {bad_trials}"


def test_concurrent_execution_vs_reject_cross_race():
    """`begin_execution`과 `reject`가 같은 approval_id를 놓고 동시에 경합해도 정확히 하나만
    이겨야 한다(둘 다 pending에서 시작해 서로 다른 종단 상태로 가려는 경쟁 — 같은 락을 공유하는지
    확인하는 테스트. 각 함수 내부에서만 락을 걸고 "함수 간" 경합은 안 걸었다면 여기서 잡힌다).
    """
    n_pairs = 25  # 스레드 절반은 begin_execution, 절반은 reject
    n_trials = 50
    bad_trials = []

    for trial in range(n_trials):
        approvals._STORE.clear()
        aid = approvals.create("reset_ledger", {}, reason="cross-race")
        results: list[tuple[str, bool]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n_pairs * 2)

        def exec_worker():
            barrier.wait()
            ok, _status = approvals.begin_execution(aid)
            with lock:
                results.append(("execute", ok))

        def reject_worker():
            barrier.wait()
            ok, _status = approvals.reject(aid)
            with lock:
                results.append(("reject", ok))

        threads = [threading.Thread(target=exec_worker) for _ in range(n_pairs)]
        threads += [threading.Thread(target=reject_worker) for _ in range(n_pairs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = [r for r in results if r[1]]
        if len(wins) != 1:
            bad_trials.append((trial, wins))

    assert not bad_trials, f"execute/reject 교차 경합에서 승자가 1명이 아닌 시행 발견: {bad_trials}"


def test_unknown_approval_id_is_not_found():
    ok, status = approvals.begin_execution("does-not-exist")
    assert ok is False
    assert status == "not_found"

    ok2, status2 = approvals.reject("does-not-exist")
    assert ok2 is False
    assert status2 == "not_found"
