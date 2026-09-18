"""정책 변경(2026-09-20): 월중에도 이번 달부터 DCA 시작 지원.

이전 정책("월중 최초 이용자는 무조건 다음 달부터")을 바꿔, 최초 설정은 이번 달/다음 달 중 사용자가
확인한 달로 시작할 수 있게 했다. 이미 다음 달 시작으로 설정된(아직 시작 전) 계획을 이번 달로
앞당기는 것도 지원한다. 기존 "계획이 이미 시작된 뒤의 예산 변경은 항상 다음 달부터"라는 정책은
그대로 유지한다(month_state._budget_plan_snapshot의 "change" 분기).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import agent
import ledger
import month_state as ms

SEP17 = datetime(2026, 9, 17, 10, 0, tzinfo=ms.KST)  # 월중, 9월
SEP17_LATE = datetime(2026, 9, 17, 20, 0, tzinfo=ms.KST)


# ── 1. 월중 신규 사용자 → 이번 달 시작 (기본값) ──────────────────────────────


def test_new_user_mid_month_defaults_to_this_month():
    proposal = ms.propose_budget_change(2_000_000, now=SEP17)
    assert proposal["ok"]
    assert proposal["action"] == "initial"
    assert proposal["effective_month"] == "2026-09"
    assert proposal["month_mismatch"] is False

    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    state = ms.load_state()
    assert state["plan_start_month"] == "2026-09"
    assert ms.effective_budget_for(2026, 9, state) == 2_000_000

    status = ms.get_month_status(2026, 9, now=SEP17)
    assert status["plan_started"] is True
    assert status["monthly_budget_krw"] == 2_000_000


# ── 2. 명시적으로 다음 달 시작 선택 → 기존 흐름 유지 ─────────────────────────


def test_new_user_can_still_explicitly_choose_next_month():
    proposal = ms.propose_budget_change(2_000_000, requested_month="2026-10", now=SEP17)
    assert proposal["ok"]
    assert proposal["action"] == "initial"
    assert proposal["effective_month"] == "2026-10"
    assert proposal["month_mismatch"] is False

    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    state = ms.load_state()
    assert state["plan_start_month"] == "2026-10"
    status_this_month = ms.get_month_status(2026, 9, now=SEP17)
    assert status_this_month["plan_started"] is False


def test_new_user_cannot_backdate_start_month():
    """과거 달로 소급 시작하지 말 것 — 요청은 반영되지 않고 이번 달로 대체되며 그 사실이 남는다."""
    proposal = ms.propose_budget_change(2_000_000, requested_month="2026-08", now=SEP17)
    assert proposal["ok"]
    assert proposal["effective_month"] == "2026-09"  # 과거 달로 소급되지 않음
    assert proposal["month_mismatch"] is True
    assert proposal["requested_month"] == "2026-08"


def test_new_user_request_beyond_next_month_falls_back_with_mismatch():
    proposal = ms.propose_budget_change(2_000_000, requested_month="2026-12", now=SEP17)
    assert proposal["ok"]
    assert proposal["effective_month"] == "2026-09"  # 이번 달/다음 달 중 하나로만 시작 가능
    assert proposal["month_mismatch"] is True


def test_final_answer_explains_initial_start_month_mismatch_differently_from_change():
    """최초 시작 불일치 안내는 "변경" 불일치 안내(§20-3)와 문구가 달라야 한다(이번 달/다음 달 둘 중
    선택 가능하다는 맥락이 있어야 함)."""
    proposal = {
        "confirmation_token": "tok", "amount_krw": 2_000_000.0, "action": "initial",
        "is_initial": True, "effective_month": "2026-09", "requested_month": "2026-08",
        "month_mismatch": True,
    }
    answer = agent._build_final_answer({}, [], None, proposal)
    assert "시작할 수 없습니다" in answer
    assert "이번 달 또는 다음 달만 가능합니다" in answer


# ── 3. 다음 달 시작 계획 → 이번 달로 앞당김 ──────────────────────────────────


def _confirm_next_month_start(now: datetime, amount: float = 2_000_000.0) -> None:
    proposal = ms.propose_budget_change(amount, requested_month="2026-10", now=now)
    ms.confirm_budget_change(proposal["confirmation_token"], now=now)


def test_advance_pending_next_month_plan_to_this_month():
    _confirm_next_month_start(SEP17)
    assert ms.load_state()["plan_start_month"] == "2026-10"

    proposal = ms.propose_budget_change(1_500_000, requested_month="2026-09", now=SEP17)
    assert proposal["ok"]
    assert proposal["action"] == "advance"
    assert proposal["effective_month"] == "2026-09"
    assert proposal["month_mismatch"] is False
    assert proposal["previous_start_month"] == "2026-10"
    assert proposal["previous_start_amount"] == 2_000_000.0

    result = ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    assert result["ok"]
    state = ms.load_state()
    assert state["plan_start_month"] == "2026-09"


def test_advance_preserves_existing_future_budget_entry():
    _confirm_next_month_start(SEP17, amount=2_000_000.0)
    proposal = ms.propose_budget_change(1_500_000, requested_month="2026-09", now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)

    state = ms.load_state()
    # 이번 달(새 시작월) 예산과 기존 다음 달 예산이 각각 따로 남아 있어야 한다 — 삭제·덮어쓰기 없음.
    assert ms.effective_budget_for(2026, 9, state) == 1_500_000.0
    assert ms.effective_budget_for(2026, 10, state) == 2_000_000.0
    history = sorted(state["budget_history"], key=lambda h: h["effective_month"])
    assert history == [
        {"amount_krw": 1_500_000.0, "effective_month": "2026-09"},
        {"amount_krw": 2_000_000.0, "effective_month": "2026-10"},
    ]


def test_advance_cancel_leaves_plan_start_unchanged():
    _confirm_next_month_start(SEP17)
    proposal = ms.propose_budget_change(1_500_000, requested_month="2026-09", now=SEP17)
    cancel_result = ms.cancel_budget_change(proposal["confirmation_token"])
    assert cancel_result["ok"]

    state = ms.load_state()
    assert state["plan_start_month"] == "2026-10"  # 앞당겨지지 않음
    assert ms.effective_budget_for(2026, 9, state) is None


def test_advance_not_triggered_when_user_does_not_request_this_month():
    """다음 달 시작으로 이미 설정된 상태에서, 이번 달을 콕 집어 요청하지 않은 일반 재제안은
    "앞당기기"가 아니라 그 미래 시작월(다음 달) 자체를 갱신하는 "변경"으로 처리돼야 한다."""
    _confirm_next_month_start(SEP17, amount=2_000_000.0)
    proposal = ms.propose_budget_change(3_000_000.0, now=SEP17)  # 이번 달 콕 집지 않음
    assert proposal["action"] == "change"
    assert proposal["effective_month"] == "2026-10"


def test_advance_rejected_when_plan_already_active():
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))  # 8월부터 이미 시작된 계획
    result = ms.advance_plan_start(1_000_000, "2026-09", now=SEP17)
    assert not result["ok"]


def test_advance_stale_when_state_changes_between_propose_and_confirm():
    """제안 뒤 다른 변경으로 계획 상태(budget_history)가 바뀌면, 앞당기기 확인도 재확인을
    요구해야 한다(§4-2 낡은 제안 판정과 동일한 보호)."""
    _confirm_next_month_start(SEP17, amount=2_000_000.0)
    advance_proposal = ms.propose_budget_change(1_500_000, requested_month="2026-09", now=SEP17)

    other_proposal = ms.propose_budget_change(2_500_000, requested_month="2026-09", now=SEP17)
    ms.confirm_budget_change(other_proposal["confirmation_token"], now=SEP17)  # 먼저 확정됨

    result = ms.confirm_budget_change(advance_proposal["confirmation_token"], now=SEP17)
    assert not result["ok"]
    assert result.get("stale") is True

    state = ms.load_state()
    assert ms.effective_budget_for(2026, 9, state) == 2_500_000.0  # 낡은 제안(1,500,000)이 아님


def test_final_answer_shows_advance_and_preserves_existing_future_budget_text():
    proposal = {
        "confirmation_token": "tok", "amount_krw": 1_500_000.0, "action": "advance",
        "is_initial": False, "effective_month": "2026-09", "requested_month": "2026-09",
        "month_mismatch": False, "previous_start_month": "2026-10", "previous_start_amount": 2_000_000.0,
    }
    answer = agent._build_final_answer({}, [], None, proposal)
    assert "앞당기는 제안" in answer
    assert "2026-10" in answer and "2,000,000원" in answer  # 기존 다음 달 예산 구분 안내
    assert "1,500,000원" in answer  # 이번 달(새 시작월) 예산


# ── 이미 진행 중인 계획의 일반 예산 변경은 기존 다음 달 정책 유지 (회귀 방지) ──


def test_active_plan_budget_change_still_always_targets_next_month():
    ms.init_plan(2_000_000, now=datetime(2026, 8, 1, tzinfo=ms.KST))  # 8월부터 이미 활성
    proposal = ms.propose_budget_change(3_000_000, requested_month="2026-09", now=SEP17)
    assert proposal["ok"]
    assert proposal["action"] == "change"
    assert proposal["effective_month"] == "2026-10"
    assert proposal["month_mismatch"] is True  # 9월을 요청했지만 다음 달(10월)로만 적용 가능


# ── 4/5. 월중 시작 시 첫 매수 반영 (기존 ledger 집계 재사용 — 구조 확인) ──────


def test_existing_buy_this_month_is_reflected_in_remaining_budget_after_mid_month_start():
    proposal = ms.propose_budget_change(2_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    ledger.record_virtual_buy(
        amount_krw=800_000, price_krw=100_000_000, executed_date="2026-09-17",
    )

    status = ms.get_month_status(2026, 9, now=SEP17)
    assert status["spent_krw"] == 800_000
    assert status["remaining_krw"] == 1_200_000
    assert status["buy_count"] == 1


def test_no_buy_yet_after_mid_month_start_shows_full_remaining_budget():
    proposal = ms.propose_budget_change(2_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)

    status = ms.get_month_status(2026, 9, now=SEP17)
    assert status["spent_krw"] == 0
    assert status["remaining_krw"] == 2_000_000
    assert status["suggested_next_buy_krw"] == 2_000_000  # 예산 확인만으로 차감되지 않음


def test_over_budget_buy_preserved_and_suggested_next_buy_never_negative():
    proposal = ms.propose_budget_change(1_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    ledger.record_virtual_buy(amount_krw=1_500_000, price_krw=100_000_000, executed_date="2026-09-17")

    status = ms.get_month_status(2026, 9, now=SEP17)
    assert status["spent_krw"] == 1_500_000  # 실제 초과 매수 기록 보존
    assert status["over_budget"] is True
    assert status["remaining_krw"] == -500_000
    assert status["suggested_next_buy_krw"] == 0.0  # 음수로 권장하지 않음


def test_decline_day_condition_not_determined_without_first_buy_record():
    """§4: 하락일 조건은 첫 매수가가 없으면 충족/미충족을 단정하면 안 된다 — "판정 불가"로 안내."""
    proposal = ms.propose_budget_change(2_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    strat_proposal = ms.propose_strategy_change(2026, 9, "decline_day", now=SEP17)
    ms.confirm_strategy_change(strat_proposal["confirmation_token"], now=SEP17)

    days = [{"date_kst": f"2026-09-{d:02d}", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0} for d in range(1, 5)]
    result = ms.evaluate_current_condition(2026, 9, "decline_day", days, {}, first_buy_price=None)
    assert result["triggered"] is False
    assert "첫 매수 기록이 아직 없어" in result["reason"]


# ── 6. 15일 09:00 KST 전후 전략 선택 경계 (월중 시작 계획에서도 동일) ────────


def test_strategy_choices_available_before_biweekly_cutoff_for_mid_month_start():
    proposal = ms.propose_budget_change(2_000_000, now=SEP17)  # 9/17, 15일 09:00 이후
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    # 15일 09:00은 이미 지났으므로 정기 분할은 불가, 하락일/RSI는 가능해야 한다.
    ok_decline, _ = ms.can_select_strategy(2026, 9, "decline_day", now=SEP17)
    ok_rsi, _ = ms.can_select_strategy(2026, 9, "rsi", now=SEP17)
    ok_biweekly, reason = ms.can_select_strategy(2026, 9, "biweekly", now=SEP17)
    assert ok_decline is True
    assert ok_rsi is True
    assert ok_biweekly is False
    assert "15일 09:00" in reason


def test_biweekly_available_for_mid_month_start_before_cutoff():
    before_cutoff = datetime(2026, 9, 10, 10, 0, tzinfo=ms.KST)
    proposal = ms.propose_budget_change(2_000_000, now=before_cutoff)
    ms.confirm_budget_change(proposal["confirmation_token"], now=before_cutoff)
    ok_biweekly, _ = ms.can_select_strategy(2026, 9, "biweekly", now=before_cutoff)
    assert ok_biweekly is True


# ── 7. 월말 마지막 날 신규 시작 ──────────────────────────────────────────────


def test_starting_on_last_day_of_month_allows_plan_start_but_blocks_new_strategy():
    last_day = datetime(2026, 9, 30, 10, 0, tzinfo=ms.KST)
    proposal = ms.propose_budget_change(2_000_000, now=last_day)
    assert proposal["effective_month"] == "2026-09"  # 이번 달 시작 자체는 허용
    ms.confirm_budget_change(proposal["confirmation_token"], now=last_day)

    status = ms.get_month_status(2026, 9, now=last_day)
    assert status["plan_started"] is True
    assert status["strategy_change_allowed"] is False  # 월말 마감이 이미 지남

    ok, reason = ms.can_select_strategy(2026, 9, "rsi", now=last_day)
    assert ok is False
    assert "마지막 날 00:00" in reason


# ── 11. 백테스트는 정책 변경과 무관하게 그대로 ──────────────────────────────


def test_backtest_result_unaffected_by_plan_start_month_or_advance():
    import backtest as backtest_mod

    records = []
    d = date(2021, 1, 1)
    price = 50_000_000.0
    while d < date(2026, 9, 18):
        records.append({"date_kst": d.isoformat(), "open": price, "high": price * 1.01, "low": price * 0.99, "close": price})
        price += 1000
        d += timedelta(days=1)

    result_before = backtest_mod.run_backtest(1_000_000, records, now=SEP17)

    proposal = ms.propose_budget_change(2_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    ms.propose_budget_change(1_500_000, requested_month="2026-09", now=SEP17)  # advance 제안(미확인)

    result_after = backtest_mod.run_backtest(1_000_000, records, now=SEP17)

    assert result_before == result_after, "plan_state 변경이 백테스트 결과에 영향을 주면 안 됨"


# ── 첫 매수 안내 — 실사용 신고(2026-09-20, #4) ───────────────────────────────


def test_get_month_status_states_first_half_step_when_no_buy_record_exists():
    """예산 100만원·매수 기록 없음·RSI 선택 상태에서 "남은 예산 전부를 RSI 신호 대기금"으로
    안내하던 결함 — get_month_status 자체의 사실 텍스트에 첫 절반 매수 단계 안내를 포함시켜
    LLM이 이 프레이밍을 매번 정확히 재구성하지 않아도 되게 한다."""
    proposal = ms.propose_budget_change(1_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    strat = ms.propose_strategy_change(2026, 9, "rsi", now=SEP17)
    ms.confirm_strategy_change(strat["confirmation_token"], now=SEP17)

    text = agent.get_month_status.invoke({"year": 2026, "month": 9})
    assert "현재 기록 기준 사용액 0원" in text
    assert "500,000원 매수를 고려" in text  # 100만원의 절반
    assert "전략 조건 대기금으로 보지 마세요" in text


def test_get_month_status_reports_existing_buy_count_instead_of_first_step_guidance():
    """이미 매수 기록이 있으면 "첫 매수 단계" 안내 대신 반영된 기록 건수를 알려야 한다 — 첫 절반
    매수를 다시 하라고 반복 안내하면 안 된다."""
    proposal = ms.propose_budget_change(1_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    ledger.record_virtual_buy(amount_krw=500_000, price_krw=100_000_000, executed_date="2026-09-17")

    text = agent.get_month_status.invoke({"year": 2026, "month": 9})
    assert "매수 기록 1건이 이미 반영" in text
    assert "첫 매수 단계로 둡니다" not in text


def test_get_month_status_on_last_day_suggests_full_remaining_not_half():
    """월말 마지막 날에 신규 시작한 사용자에게는 절반이 아니라 남은 예산 전액을 안내해야 한다 —
    첫 절반·잔여 예산 매수를 중복 안내하지 않기 위함이다. get_month_status 도구는 항상 실제
    시각(datetime.now)을 쓰므로, 이 테스트에서만 agent.datetime을 고정해 "월말"을 재현한다."""
    from unittest.mock import patch

    last_day = datetime(2026, 9, 30, 10, 0, tzinfo=ms.KST)
    proposal = ms.propose_budget_change(1_000_000, now=last_day)
    ms.confirm_budget_change(proposal["confirmation_token"], now=last_day)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return last_day

    with patch("agent.datetime", _FixedDatetime):
        text = agent.get_month_status.invoke({"year": 2026, "month": 9})
    assert "남은 예산 1,000,000원 전액을 매수" in text
    assert "500,000원 매수를 고려" not in text  # 절반 안내와 중복되면 안 됨


def test_get_month_status_preserves_actual_over_half_buy_amount_in_remaining_calc():
    """실제 매수액이 절반과 달라도(더 많이 샀어도) 기록을 그대로 보존하고 실제 잔여액으로
    계산해야 한다."""
    proposal = ms.propose_budget_change(1_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    ledger.record_virtual_buy(amount_krw=700_000, price_krw=100_000_000, executed_date="2026-09-17")

    status = ms.get_month_status(2026, 9, now=SEP17)
    assert status["spent_krw"] == 700_000
    assert status["remaining_krw"] == 300_000  # 절반(50만원)이 아니라 실제 사용액 기준


# ── 전략 선택은 매수 조건 충족 여부와 무관하다 — 실사용 신고(2026-09-20, #1) ──


def test_can_select_strategy_does_not_depend_on_current_rsi_value():
    """전략 선택 가능 여부는 RSI 현재값과 무관해야 한다 — can_select_strategy는 애초에 RSI 값을
    파라미터로 받지 않는다(계획 시작 여부·마감 시각만 확인). "RSI가 30보다 높아도 선택 자체는
    가능해야 한다"는 요구사항이 이미 구조적으로 보장됨을 명시적으로 남긴다."""
    proposal = ms.propose_budget_change(1_000_000, now=SEP17)
    ms.confirm_budget_change(proposal["confirmation_token"], now=SEP17)
    ok, _ = ms.can_select_strategy(2026, 9, "rsi", now=SEP17)
    assert ok is True
