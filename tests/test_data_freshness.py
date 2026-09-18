"""최신 확정 일봉 누락 시 stale-data 확인 요구, 필수 과거 데이터 부족 시 계산 불가 — SPEC §6-2·§7-1.

네트워크 호출(Upbit API) 없이 검증한다 — check_freshness/compute_ma_deviation_pct는 로컬 캐시/입력만
본다. DD·MDD(compute_dd_mdd) 회귀 테스트는 tests/test_dd_mdd.py로 분리했다(2026-09-20 정책 변경 —
종가 기준 통일·MDD 추가).
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


def test_compute_ma_deviation_none_when_insufficient_history():
    closes = [100.0] * 50  # 200일 이동평균에 필요한 데이터보다 적음
    assert indicators.compute_ma_deviation_pct(closes, 200) is None
