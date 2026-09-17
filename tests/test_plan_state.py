"""계획 시작월 vs 조회 대상 월 vs 전략 선택 여부 구분, 계획 시작 전 월말 매수 안내 제외 — SPEC §2·§4.

기획팀 지적(2026-09-17): "다음 달 전략을 선택했다고 실제 계획이 시작된 건 아니다" — 두 개념이
코드/응답에서 실제로 분리되는지 확인한다.
"""

from datetime import datetime

import month_state as ms


def test_mid_month_signup_starts_plan_next_month():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ms.KST)  # 17일(1일 아님) -> 다음 달부터 시작
    result = ms.init_plan(2_000_000, now=now)
    assert result["ok"] is True
    assert result["plan_start_month"] == "2026-10"


def test_day1_signup_starts_plan_same_month():
    now = datetime(2026, 10, 1, 10, 0, tzinfo=ms.KST)
    result = ms.init_plan(2_000_000, now=now)
    assert result["plan_start_month"] == "2026-10"


def test_month_before_plan_start_excluded_from_budget_guidance():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ms.KST)
    ms.init_plan(2_000_000, now=now)  # 계획 시작월: 2026-10

    status_this_month = ms.get_month_status(2026, 9, now=now)
    assert status_this_month["plan_started"] is False
    assert status_this_month["plan_start_month"] == "2026-10"
    assert status_this_month["monthly_budget_krw"] is None
    # §4: 계획 시작 전 달은 "예산/남은 예산/월말 매수 안내" 집계 대상에서 완전히 빠져야 한다
    assert "spent_krw" not in status_this_month
    assert "suggested_next_buy_krw" not in status_this_month

    status_next_month = ms.get_month_status(2026, 10, now=now)
    assert status_next_month["plan_started"] is True
    assert status_next_month["plan_start_month"] == "2026-10"
    assert status_next_month["monthly_budget_krw"] == 2_000_000
    assert "spent_krw" in status_next_month  # 계획 시작월부터는 정상 집계 대상


def test_selecting_next_month_strategy_does_not_start_this_months_plan():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ms.KST)
    ms.init_plan(2_000_000, now=now)  # 시작월 2026-10

    proposal = ms.propose_strategy_change(2026, 10, "rsi", now=now)
    assert proposal["ok"] is True
    confirmed = ms.confirm_strategy_change(proposal["confirmation_token"], now=now)
    assert confirmed["ok"] is True
    assert ms.get_selected_strategy(2026, 10) == "rsi"

    # 10월 전략을 선택했다고 해서 9월(계획 시작 전 달)의 상태가 바뀌면 안 된다.
    status_this_month = ms.get_month_status(2026, 9, now=now)
    assert status_this_month["plan_started"] is False
    assert status_this_month["strategy"] is None


def test_select_strategy_blocked_before_plan_start_month():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ms.KST)
    ms.init_plan(2_000_000, now=now)  # 시작월 2026-10
    result = ms.propose_strategy_change(2026, 9, "rsi", now=now)  # 아직 시작 안 된 9월
    assert result["ok"] is False


def test_propose_confirm_cancel_token_flow_end_to_end():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ms.KST)
    ms.init_plan(2_000_000, now=now)

    proposal = ms.propose_strategy_change(2026, 10, "rsi", now=now)
    token = proposal["confirmation_token"]

    cancelled = ms.cancel_strategy_change(token)
    assert cancelled["ok"] is True
    assert ms.get_selected_strategy(2026, 10) is None  # 취소했으니 적용되면 안 된다

    replay_cancel = ms.cancel_strategy_change(token)  # 이미 소진된 토큰 재사용
    assert replay_cancel["ok"] is False

    proposal2 = ms.propose_strategy_change(2026, 10, "decline_day", now=now)
    confirmed = ms.confirm_strategy_change(proposal2["confirmation_token"], now=now)
    assert confirmed["ok"] is True
    assert ms.get_selected_strategy(2026, 10) == "decline_day"

    replay_confirm = ms.confirm_strategy_change(proposal2["confirmation_token"], now=now)
    assert replay_confirm["ok"] is False  # 같은 토큰 재사용은 실패해야 한다(1회용)
