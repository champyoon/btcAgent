"""최신 확정 일봉 누락 시 stale-data 확인 요구, 필수 과거 데이터 부족 시 계산 불가 — SPEC §6-2·§7-1.

네트워크 호출(Upbit API) 없이 검증한다 — check_freshness/compute_drawdown은 로컬 캐시/입력만 본다.
"""

from datetime import date, datetime, timedelta

import indicators
import price_history as ph


def test_check_freshness_detects_stale_cache():
    ph.save_history([{"date_kst": "2026-09-10", "open": 1, "high": 1, "low": 1, "close": 1}])
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ph.KST)  # 09:00 이후 -> 기대 확정일 2026-09-16
    result = ph.check_freshness(now=now)
    assert result["fresh"] is False
    assert result["expected"] == "2026-09-16"
    assert result["last_available"] == "2026-09-10"


def test_check_freshness_fresh_when_cache_matches_expected():
    ph.save_history([{"date_kst": "2026-09-16", "open": 1, "high": 1, "low": 1, "close": 1}])
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ph.KST)
    result = ph.check_freshness(now=now)
    assert result["fresh"] is True


def test_agent_freshness_gate_blocks_then_allows_with_override():
    import agent

    ph.save_history([{"date_kst": "2020-01-01", "open": 1, "high": 1, "low": 1, "close": 1}])
    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = False
    msg = agent._freshness_gate_message()
    assert msg is not None
    assert "확인" in msg
    assert agent._LAST_DATA_GAP is not None

    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = True
    msg2 = agent._freshness_gate_message()
    assert msg2 is None
    agent._REQUEST_CONTEXT["proceed_with_stale_data"] = False  # 다른 테스트에 영향 안 주도록 복원


def test_compute_drawdown_none_when_a_day_missing_in_window():
    base = date(2026, 1, 1)
    candles = []
    for i in range(30):
        if i == 15:
            continue  # 중간 하루 결측 — 임의로 채우거나 기간을 늘리면 안 된다
        d = base + timedelta(days=i)
        candles.append({"date_kst": d.isoformat(), "high": 100.0, "close": 100.0})
    assert indicators.compute_drawdown(candles, days=30) is None


def test_compute_drawdown_computes_when_window_is_complete():
    base = date(2026, 1, 1)
    candles = [
        {"date_kst": (base + timedelta(days=i)).isoformat(), "high": 100.0 + i, "close": 90.0}
        for i in range(30)
    ]
    result = indicators.compute_drawdown(candles, days=30)
    assert result is not None
    expected_high = max(c["high"] for c in candles)
    assert result["high"] == expected_high
    assert abs(result["pct"] - ((90.0 / expected_high - 1) * 100)) < 1e-9
    # 이 픽스처는 최고가가 구간 마지막 날(day 29)에 찍히도록 만들어져 있다 — 아래
    # test_compute_drawdown_high_date_is_the_actual_peak_day_not_window_start가 "구간 시작일과
    # 실제 고점일이 다른" 더 일반적인 경우를 확인한다.
    assert result["high_date"] == candles[-1]["date_kst"]


def test_compute_drawdown_high_date_is_the_actual_peak_day_not_window_start():
    """실사용 중 발견(2026-09-19, Haiku 4.5 실제 응답): "1년 드로다운 -41.71%(작년 9월 17일
    고점 179,869,000원 대비)"처럼 구간 시작일을 고점 발생일로 잘못 서술했다 — 실제 고점은 그
    구간 안의 다른 날짜(3주 뒤)였다. 이 테스트는 고점이 구간 시작일도 종료일도 아닌 중간 날짜에
    찍히도록 만들어, `high_date`가 그 실제 날짜를 정확히 가리키는지 확인한다."""
    base = date(2026, 1, 1)
    candles = []
    for i in range(30):
        high = 100.0
        if i == 10:  # 구간 시작(0)도 끝(29)도 아닌 중간에 고점을 둔다
            high = 999.0
        candles.append({"date_kst": (base + timedelta(days=i)).isoformat(), "high": high, "close": 90.0})

    result = indicators.compute_drawdown(candles, days=30)
    assert result is not None
    expected_peak_date = (base + timedelta(days=10)).isoformat()
    assert result["high_date"] == expected_peak_date
    assert result["high_date"] != result["start_date"], "고점일이 구간 시작일과 우연히도 같으면 이 테스트가 무의미해짐"
    assert result["high"] == 999.0


def test_dd_desc_states_peak_date_separately_from_window_range():
    """`agent._dd_desc()`가 만드는 문구 자체가 "구간 범위"와 "고점 발생일"을 구분해서 보여주는지
    확인 — 이전 문구("(시작일~종료일 고점 X원 대비)")는 이 둘을 구분하지 않아 답변을 만드는 쪽이
    구간 시작일을 고점 발생일로 오해하게 만들었다."""
    import agent

    d = {
        "pct": -41.71,
        "start_date": "2025-09-17",
        "end_date": "2026-09-16",
        "high": 179_869_000.0,
        "high_date": "2025-10-09",
        "close": 104_852_000.0,
    }
    desc = agent._dd_desc(d)
    assert "2025-10-09" in desc  # 실제 고점 발생일이 명시돼야 함
    assert "2025-09-17" in desc and "2026-09-16" in desc  # 조회 구간 범위도 유지
    assert "179,869,000원" in desc


def test_compute_ma_deviation_none_when_insufficient_history():
    closes = [100.0] * 50  # 200일 이동평균에 필요한 데이터보다 적음
    assert indicators.compute_ma_deviation_pct(closes, 200) is None
