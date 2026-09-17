"""예산·평균 매수가 계산 + 수정/취소 후 재집계 — 고정 입력에 대해 독립적으로 계산한 기대값과 비교.

SPEC §4: 남은 예산 = 월 예산 - 해당 월 active 매수 합. 평균 매수가 = 총 active 매수금액 / 총 수량.
"""

import ledger


def test_month_budget_status_sums_only_this_month_active_buys():
    entries = [
        {"type": "buy", "status": "active", "executed_date": "2026-09-05", "amount_krw": 300_000,
         "price_krw": 100_000_000, "quantity_btc": None},
        {"type": "buy", "status": "active", "executed_date": "2026-09-20", "amount_krw": 200_000,
         "price_krw": 100_000_000, "quantity_btc": None},
        # 다른 달 — 이번 달 집계에서 제외돼야 한다
        {"type": "buy", "status": "active", "executed_date": "2026-08-31", "amount_krw": 999_999,
         "price_krw": 1, "quantity_btc": None},
        # 관망 기록 — 매수 집계에서 제외돼야 한다
        {"type": "watch", "status": "active", "recorded_at": "2026-09-10T00:00:00+00:00"},
        # 취소된 매수 — 집계에서 제외돼야 한다
        {"type": "buy", "status": "cancelled", "executed_date": "2026-09-15", "amount_krw": 500_000,
         "price_krw": 100_000_000, "quantity_btc": None},
    ]
    status = ledger.month_budget_status(1_000_000, 2026, 9, entries=entries)
    # 독립 계산: 300,000 + 200,000 = 500,000 (다른 항목은 전부 제외 대상)
    assert status["spent_krw"] == 500_000
    assert status["remaining_krw"] == 500_000
    assert status["buy_count"] == 2
    assert status["over_budget"] is False


def test_month_budget_status_over_budget_flag():
    entries = [
        {"type": "buy", "status": "active", "executed_date": "2026-09-01", "amount_krw": 1_200_000,
         "price_krw": 100_000_000, "quantity_btc": None},
    ]
    status = ledger.month_budget_status(1_000_000, 2026, 9, entries=entries)
    assert status["remaining_krw"] == -200_000
    assert status["over_budget"] is True
    assert status["suggested_next_buy_krw"] == 0.0  # 음수를 그대로 제안하지 않는다


def test_average_buy_price_weighted_by_quantity_and_excludes_cancelled():
    entries = [
        {"type": "buy", "status": "active", "amount_krw": 1_000_000, "price_krw": 100_000_000, "quantity_btc": None},
        {"type": "buy", "status": "active", "amount_krw": 2_000_000, "price_krw": 50_000_000, "quantity_btc": None},
        {"type": "buy", "status": "cancelled", "amount_krw": 5_000_000, "price_krw": 1, "quantity_btc": None},
    ]
    # 독립 계산: qty1 = 1,000,000/100,000,000 = 0.01 BTC, qty2 = 2,000,000/50,000,000 = 0.04 BTC
    # 총액 3,000,000 / 총수량 0.05 = 60,000,000
    avg = ledger.average_buy_price(entries=entries)
    assert avg is not None
    assert abs(avg - 60_000_000) < 1e-6


def test_average_buy_price_none_when_no_active_buys():
    entries = [{"type": "watch", "status": "active", "recorded_at": "2026-09-10T00:00:00+00:00"}]
    assert ledger.average_buy_price(entries=entries) is None


def test_amend_then_cancel_reaggregates_correctly():
    r = ledger.record_virtual_buy(amount_krw=1_000_000, price_krw=100_000_000, executed_date="2026-09-05")
    assert r["ok"] is True
    rid = r["record"]["record_id"]

    before = ledger.month_budget_status(2_000_000, 2026, 9)
    assert before["spent_krw"] == 1_000_000

    amend = ledger.amend_virtual_buy(rid, amount_krw=1_500_000, reason="가격 정정")
    assert amend["ok"] is True
    after_amend = ledger.month_budget_status(2_000_000, 2026, 9)
    assert after_amend["spent_krw"] == 1_500_000  # 재집계 확인
    assert len(amend["record"]["history"]) == 1
    assert amend["record"]["history"][0]["amount_krw"] == 1_000_000  # 변경 전 스냅샷 보존

    cancel = ledger.cancel_virtual_buy(rid, reason="실수로 등록")
    assert cancel["ok"] is True
    after_cancel = ledger.month_budget_status(2_000_000, 2026, 9)
    assert after_cancel["spent_krw"] == 0  # 취소된 기록은 재집계에서 완전히 빠져야 한다
    assert ledger.average_buy_price() is None


def test_amend_only_changes_specified_fields():
    r = ledger.record_virtual_buy(
        amount_krw=1_000_000, price_krw=100_000_000, executed_date="2026-09-05", note="원본"
    )
    rid = r["record"]["record_id"]
    amend = ledger.amend_virtual_buy(rid, amount_krw=1_200_000)  # note는 안 건드림
    assert amend["record"]["amount_krw"] == 1_200_000
    assert amend["record"]["note"] == "원본"  # 미전달 필드는 유지돼야 한다
    assert amend["record"]["executed_date"] == "2026-09-05"


def test_cancel_does_not_delete_record_only_flips_status():
    r = ledger.record_virtual_buy(amount_krw=1_000_000, price_krw=100_000_000, executed_date="2026-09-05")
    rid = r["record"]["record_id"]
    ledger.cancel_virtual_buy(rid, reason="취소")
    all_entries = ledger.search_ledger()
    assert len(all_entries) == 1  # 삭제되지 않고 그대로 남아있어야 한다
    assert all_entries[0]["status"] == "cancelled"


def test_search_ledger_filters_buys_by_month_when_specified():
    ledger.record_virtual_buy(amount_krw=100_000, price_krw=100_000_000, executed_date="2026-08-20")
    ledger.record_virtual_buy(amount_krw=200_000, price_krw=100_000_000, executed_date="2026-09-05")
    all_entries = ledger.search_ledger()
    assert len(all_entries) == 2  # 필터 없으면 전체

    sept_only = ledger.search_ledger(year=2026, month=9)
    assert len(sept_only) == 1
    assert sept_only[0]["executed_date"] == "2026-09-05"

    aug_only = ledger.search_ledger(year=2026, month=8)
    assert len(aug_only) == 1
    assert aug_only[0]["executed_date"] == "2026-08-20"

    empty_month = ledger.search_ledger(year=2026, month=1)
    assert empty_month == []


def test_search_ledger_filters_watch_by_recorded_month():
    from datetime import datetime, timezone

    ledger.record_watch_decision(note="오늘 관망")
    this_month = datetime.now(timezone.utc)
    same_month = ledger.search_ledger(year=this_month.year, month=this_month.month)
    assert len(same_month) == 1
    other_month = ledger.search_ledger(year=this_month.year - 1, month=1)
    assert other_month == []


def test_amend_or_cancel_on_cancelled_record_rejected():
    r = ledger.record_virtual_buy(amount_krw=1_000_000, price_krw=100_000_000, executed_date="2026-09-05")
    rid = r["record"]["record_id"]
    ledger.cancel_virtual_buy(rid)
    assert ledger.amend_virtual_buy(rid, amount_krw=999)["ok"] is False
    assert ledger.cancel_virtual_buy(rid)["ok"] is False  # 이미 취소된 걸 다시 취소 불가
