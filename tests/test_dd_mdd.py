"""DD(현재 하락률)·MDD(최대 낙폭) 확장 — SPEC §6-2(2026-09-20 정책 변경).

정책 변경 요약: 기존에는 드로다운을 기간 내 최고 고가(high) 기준으로만 계산했다. 이번 변경으로
DD·MDD 둘 다 확정 종가(close) 기준으로 통일하고, MDD(고점 이후 발생한 저점만 반영하는 최대
낙폭)를 새로 추가한다. RSI(14)는 계산 방식이 바뀌지 않았다(회귀 없음은 tests/test_data_freshness.py
등 기존 테스트가 계속 확인한다).
"""

from __future__ import annotations

from datetime import date, timedelta

import indicators


def _candles(closes: list[float], *, start: date = date(2026, 1, 1), high_offset: float = 0.0) -> list[dict]:
    """closes만으로 candle 목록을 만든다. high_offset을 주면 high가 close와 달라지는데(§ 요구사항
    "high만 바꾸고 close는 유지하면 결과 불변"), 이 함수의 기본값(0)은 high==close인 단순 픽스처다."""
    return [
        {
            "date_kst": (start + timedelta(days=i)).isoformat(),
            "open": c,
            "high": c + high_offset,
            "low": c,
            "close": c,
        }
        for i, c in enumerate(closes)
    ]


# ══════════════════════════════════════════════════════════════════
# §7 필수 테스트 — 요청사항에 명시된 예시들
# ══════════════════════════════════════════════════════════════════


def test_100_to_60_to_90_gives_dd_minus10_mdd_minus40():
    candles = _candles([100.0, 60.0, 90.0])
    result = indicators.compute_dd_mdd(candles, days=3)
    assert result is not None
    assert abs(result["dd_pct"] - (-10.0)) < 1e-9
    assert abs(result["mdd_pct"] - (-40.0)) < 1e-9


def test_60_to_100_to_90_gives_dd_minus10_mdd_minus10():
    """최저가가 최고가보다 먼저 나온 경우 — 고점 이후 저점만 MDD에 반영되므로 초반의 60은
    아직 고점이 없을 때라 유의미한 낙폭에 포함되지 않는다."""
    candles = _candles([60.0, 100.0, 90.0])
    result = indicators.compute_dd_mdd(candles, days=3)
    assert result is not None
    assert abs(result["dd_pct"] - (-10.0)) < 1e-9
    assert abs(result["mdd_pct"] - (-10.0)) < 1e-9


def test_rising_only_window_has_zero_dd_and_mdd():
    candles = _candles([80.0, 90.0, 100.0, 110.0])
    result = indicators.compute_dd_mdd(candles, days=4)
    assert result is not None
    assert result["dd_pct"] == 0.0
    assert result["mdd_pct"] == 0.0


def test_flat_window_has_zero_dd_and_mdd():
    candles = _candles([100.0] * 10)
    result = indicators.compute_dd_mdd(candles, days=10)
    assert result is not None
    assert result["dd_pct"] == 0.0
    assert result["mdd_pct"] == 0.0


def test_multiple_peaks_and_troughs_picks_the_deepest_post_peak_drop():
    # 100 -> 80(-20%, peak 100) -> 120(신고점) -> 60(-50%, peak 120) -> 110(-8.33%, peak 120)
    candles = _candles([100.0, 80.0, 120.0, 60.0, 110.0])
    result = indicators.compute_dd_mdd(candles, days=5)
    assert result is not None
    assert result["mdd_pct"] == -50.0
    assert result["mdd_peak_close"] == 120.0
    assert result["mdd_trough_close"] == 60.0
    # DD는 마지막 날(110) 기준 — 구간 전체 최고 종가(120) 대비
    assert abs(result["dd_pct"] - ((110.0 / 120.0 - 1) * 100)) < 1e-9


def test_changing_high_while_keeping_close_does_not_change_result():
    base = _candles([100.0, 60.0, 90.0])
    changed = _candles([100.0, 60.0, 90.0], high_offset=500.0)  # high만 다르게
    assert indicators.compute_dd_mdd(base, days=3) == indicators.compute_dd_mdd(changed, days=3)


def test_peak_outside_the_period_is_excluded():
    """기간 이전의 고점을 그 기간의 DD·MDD 계산에 끌어오지 않는다 — 구간의 시작점부터 새로
    최고 종가를 계산해야 한다."""
    # 기간 밖(과거)에 아주 높은 고점(1000)을 두고, 실제 30일 창은 그보다 훨씬 낮은 값들로 구성.
    pre_period = _candles([1000.0], start=date(2025, 12, 1))
    in_period = _candles([100.0, 90.0, 95.0], start=date(2026, 1, 1))
    candles = pre_period + in_period
    result = indicators.compute_dd_mdd(candles, days=3)  # 최근 3일 = in_period만
    assert result is not None
    assert result["dd_peak_close"] == 100.0  # 1000이 아니라 구간 내 최고 종가
    assert result["mdd_peak_close"] == 100.0


# ══════════════════════════════════════════════════════════════════
# 기간 경계·윤년·48개월 보정
# ══════════════════════════════════════════════════════════════════


def test_30_day_window_is_d_minus_29_to_d_inclusive():
    candles = _candles([100.0] * 40, start=date(2026, 1, 1))
    result = indicators.compute_dd_mdd(candles, days=30)
    assert result is not None
    assert result["start_date"] == "2026-01-11"  # D(2026-02-09)-29일
    assert result["end_date"] == "2026-02-09"


def test_365_day_window_spans_leap_year_correctly():
    # 2028은 윤년 — 365일 구간이 2/29를 포함해도 날짜 수 계산이 깨지지 않아야 한다.
    start = date(2027, 3, 1)
    candles = _candles([100.0 + i * 0.01 for i in range(400)], start=start)
    result = indicators.compute_dd_mdd(candles, days=365)
    assert result is not None
    end_date = start + timedelta(days=399)
    expected_start = end_date - timedelta(days=364)
    assert result["start_date"] == expected_start.isoformat()
    assert result["end_date"] == end_date.isoformat()


def test_48_month_window_start_on_ordinary_leap_day_needs_no_correction():
    """4년(48개월) 전이 정확히 2월 29일인 일반적인 조합(둘 다 윤년) — 보정 없이 그대로 쓰인다."""
    end_date = date(2028, 2, 29)  # 2028은 윤년
    # 48개월 전은 2024-02-29(2024도 윤년이라 2/29가 실제로 존재 — 보정 불필요한 경계 케이스)
    candles = _candles([100.0] * (365 * 5), start=end_date - timedelta(days=365 * 5 - 1))
    result = indicators.compute_dd_mdd(candles, months=48)
    assert result is not None
    assert result["start_date"] == "2024-03-01"  # 2024-02-29 다음날부터 포함(§6-2 "그 다음날 ~ D")
    assert result["end_date"] == end_date.isoformat()


def test_48_month_window_corrects_missing_leap_day_across_century_boundary():
    """§6-2: 대응 날짜가 없으면 그 달 말일로 보정한다 — 2104-02-29(윤년)의 48개월 전은
    2100-02-29인데, 2100은 100의 배수이면서 400의 배수는 아니라 윤년이 아니므로 2/29가 없다.
    이 경우 2100-02-28로 보정돼야 한다(4년 간격이라도 항상 둘 다 윤년인 건 아니다 — 세기 경계에서
    어긋난다)."""
    end_date = date(2104, 2, 29)
    candles = _candles([100.0] * (365 * 5), start=end_date - timedelta(days=365 * 5 - 1))
    result = indicators.compute_dd_mdd(candles, months=48)
    assert result is not None
    assert result["start_date"] == "2100-03-01"  # 2100-02-28(보정된 말일) 다음날부터 포함
    assert result["end_date"] == end_date.isoformat()


# ══════════════════════════════════════════════════════════════════
# 결측·데이터 부족·유효하지 않은 값
# ══════════════════════════════════════════════════════════════════


def test_none_when_a_day_is_missing_inside_the_window():
    base = date(2026, 1, 1)
    candles = [
        {"date_kst": (base + timedelta(days=i)).isoformat(), "close": 100.0}
        for i in range(30)
        if i != 15  # 중간 하루 결측 — 임의로 채우거나 기간을 늘리면 안 된다
    ]
    assert indicators.compute_dd_mdd(candles, days=30) is None


def test_none_when_not_enough_history_at_all():
    """365일 창을 요구했는데 전체 캔들이 100개뿐이면(결측이 아니라 애초에 데이터가 그만큼 없음)
    짧은 구간으로 몰래 대체하지 않고 계산 불가여야 한다."""
    candles = _candles([100.0] * 100)
    assert indicators.compute_dd_mdd(candles, days=365) is None


def test_none_when_a_close_price_is_zero_or_negative():
    candles = _candles([100.0, 0.0, 90.0])
    assert indicators.compute_dd_mdd(candles, days=3) is None
    candles2 = _candles([100.0, -5.0, 90.0])
    assert indicators.compute_dd_mdd(candles2, days=3) is None


def test_one_period_short_of_data_does_not_block_a_different_period():
    """30일 구간은 계산 가능한데 365일 구간은 데이터가 부족한 경우, 30일 결과는 그대로 나와야
    한다 — 한 기간의 부족이 다른 기간까지 일괄 차단하면 안 된다는 원칙(agent._indicators_summary와
    같은 원칙을 compute_dd_mdd 레벨에서도 확인)."""
    candles = _candles([100.0 - i * 0.1 for i in range(40)])
    assert indicators.compute_dd_mdd(candles, days=30) is not None
    assert indicators.compute_dd_mdd(candles, days=365) is None


# ══════════════════════════════════════════════════════════════════
# 부등식·불변량
# ══════════════════════════════════════════════════════════════════


def test_mdd_le_dd_le_zero_holds_across_varied_windows():
    fixtures = [
        [100.0, 60.0, 90.0],
        [60.0, 100.0, 90.0],
        [100.0] * 10,
        [80.0, 90.0, 100.0, 110.0],
        [100.0, 80.0, 120.0, 60.0, 110.0],
    ]
    for closes in fixtures:
        result = indicators.compute_dd_mdd(_candles(closes), days=len(closes))
        assert result is not None
        assert result["mdd_pct"] <= result["dd_pct"] <= 0.0


# ══════════════════════════════════════════════════════════════════
# 최신 미마감 캔들 제외·데이터 신선도(§7-1)는 price_history.confirmed_records()의 책임이지
# compute_dd_mdd 자체의 책임이 아니다 — compute_dd_mdd는 이미 필터링된 확정 데이터만 받는다는
# 계약을 그대로 유지한다(변경 없음). 이 계약 자체의 회귀는 tests/test_data_freshness.py가 커버.
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# agent.py 통합 — _indicators_summary/get_indicators/_dd_mdd_desc
# ══════════════════════════════════════════════════════════════════


def test_dd_mdd_desc_shows_both_values_and_close_basis_language():
    import agent

    d = {
        "dd_pct": -10.0,
        "dd_peak_close": 100.0,
        "dd_peak_date": "2026-01-02",
        "mdd_pct": -40.0,
        "mdd_peak_close": 100.0,
        "mdd_peak_date": "2026-01-01",
        "mdd_trough_close": 60.0,
        "mdd_trough_date": "2026-01-02",
        "start_date": "2026-01-01",
        "end_date": "2026-01-03",
    }
    desc = agent._dd_mdd_desc("최근 30일", d, required_days_desc="최근 30일")
    assert "DD -10.00%" in desc
    assert "MDD -40.00%" in desc
    assert "최고 종가" in desc  # "최고가"가 아니라 "최고 종가"로 정확히 표시(요청사항)
    assert "2026-01-01" in desc and "2026-01-02" in desc


def test_dd_mdd_desc_states_calculation_impossible_with_reason_when_none():
    import agent

    desc = agent._dd_mdd_desc("최근 365일", None, required_days_desc="최근 365일")
    assert "계산 불가" in desc
    assert "최근 365일" in desc


def test_indicators_summary_reports_dd_mdd_independently_of_rsi_shortfall():
    """확정 일봉이 100일뿐이면(RSI/MA200엔 200일 필요) RSI는 계산 불가지만, 30일 DD·MDD는
    데이터가 충분하므로 그대로 계산돼야 한다 — 한 지표의 부족이 다른 지표까지 막지 않는다."""
    import agent

    base = date(2026, 1, 1)
    records = [
        {"date_kst": (base + timedelta(days=i)).isoformat(), "open": 100.0, "high": 100.0,
         "low": 100.0, "close": 100.0 + i}
        for i in range(100)
    ]
    summary = agent._indicators_summary(records)
    assert "RSI(14)·200일 이동평균 괴리율: 계산 불가" in summary
    assert "최근 30일: DD" in summary  # 30일 DD·MDD는 그대로 계산됨
    assert "최근 365일: 계산 불가" in summary  # 100일뿐이라 365일 구간은 확보 안 됨
    assert "MDD" in summary


def test_indicators_summary_states_close_basis_and_no_prediction_caveat():
    import agent

    base = date(2026, 1, 1)
    records = [
        {"date_kst": (base + timedelta(days=i)).isoformat(), "open": 100.0, "high": 999.0,
         "low": 50.0, "close": 100.0 + i * 0.01}
        for i in range(250)
    ]
    summary = agent._indicators_summary(records)
    assert "최고 종가" in summary
    assert "최고가" not in summary.replace("최고 종가", "")  # "최고가"만 단독으로 쓰이지 않음
    assert "예측하지" in summary  # 미래 예측이 아니라는 안내가 포함됨


def test_get_indicators_tool_output_matches_summary_shape_without_network():
    """도구 경계(get_indicators)까지 실제 네트워크 호출 없이 확인 — 신선도 게이트만
    proceed_with_stale_data로 넘기고, 결과 텍스트가 _indicators_summary와 같은 구조인지 본다."""
    import agent

    base = date(2026, 1, 1)
    records = [
        {"date_kst": (base + timedelta(days=i)).isoformat(), "open": 100.0, "high": 100.0,
         "low": 100.0, "close": 100.0 + i * 0.01}
        for i in range(250)
    ]
    original_update = agent.price_history.update_incremental
    original_confirmed = agent.price_history.confirmed_records
    agent.price_history.update_incremental = lambda *a, **kw: records
    agent.price_history.confirmed_records = lambda *a, **kw: records
    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = True
    try:
        result = agent.get_indicators.invoke({})
    finally:
        agent.price_history.update_incremental = original_update
        agent.price_history.confirmed_records = original_confirmed
        agent._REQUEST_CONTEXT["proceed_with_stale_data"] = False
    assert "RSI(14" in result
    assert "최근 30일: DD" in result
    assert "MDD" in result


# ══════════════════════════════════════════════════════════════════
# 관망 스냅샷 — 계산 기준/버전 식별
# ══════════════════════════════════════════════════════════════════


def test_watch_snapshot_carries_dd_mdd_calc_basis_version():
    import agent

    base = date(2026, 1, 1)
    records = [
        {"date_kst": (base + timedelta(days=i)).isoformat(), "open": 100.0, "high": 100.0,
         "low": 100.0, "close": 100.0 + i * 0.01}
        for i in range(250)
    ]
    original_update = agent.price_history.update_incremental
    original_confirmed = agent.price_history.confirmed_records
    agent.price_history.update_incremental = lambda *a, **kw: records
    agent.price_history.confirmed_records = lambda *a, **kw: records
    try:
        agent.record_watch_decision.invoke({"note": "DD·MDD 스냅샷 확인"})
    finally:
        agent.price_history.update_incremental = original_update
        agent.price_history.confirmed_records = original_confirmed

    entries = agent.ledger.search_ledger()
    watch = next(e for e in entries if e["type"] == "watch" and e["note"] == "DD·MDD 스냅샷 확인")
    snapshot = watch["indicators_snapshot"]
    assert snapshot["dd_mdd_calc_basis"] == indicators.DD_MDD_CALC_BASIS
    assert snapshot["dd_mdd"]["30d"] is not None
    assert snapshot["dd_mdd"]["365d"] is None  # 250일뿐이라 365일 창은 확보 안 됨


def test_old_watch_snapshot_without_calc_basis_key_is_distinguishable():
    """이번 정책 변경 이전 스냅샷(또는 DD·MDD 자체가 없는 스냅샷)은 `dd_mdd_calc_basis` 키가
    아예 없다 — 신규 스냅샷과 혼동하지 않고 구분할 수 있어야 한다(요청사항: "기존 기록과 신규
    기록의 기준 차이를 식별할 수 있게")."""
    old_snapshot = {"rsi14": 42.0, "ma200_deviation_pct": 1.5, "as_of": "2026-01-01"}
    assert "dd_mdd_calc_basis" not in old_snapshot
    assert "dd_mdd" not in old_snapshot
