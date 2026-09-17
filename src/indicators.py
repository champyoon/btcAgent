"""RSI(RMA)·이동평균·드로다운 계산 — SPEC.md §6-2·§6-3·§6-4·§6-5 확정 계산법.

전부 예외를 던지지 않고, 계산에 필요한 데이터(또는 구간 내 누락 없는 데이터)가 부족하면 None을
반환한다(추정값으로 채우지 않는다 — SPEC §10 정책 1). 반올림은 여기서 하지 않는다 — §6-5 확정:
"화면 표시를 위한 반올림 값을 후속 계산에 다시 사용하지 않는다." 표시용 반올림은 이 값을 쓰는
상위 계층(도구/응답 포맷터)의 몫이다.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def compute_rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RMA 방식 RSI 시리즈(§6-4). closes는 과거 -> 최신 순.

    최초 `period`개 구간은 시드 데이터가 부족해 None이다. 최초 평균 이후로는 이전 평균에 새 값을
    반영하는 방식으로 갱신한다(매번 최근 14일만 잘라 재초기화하지 않는다 — §6-4).
    """
    n = len(closes)
    rsi: list[float | None] = [None] * n
    if n <= period:
        return rsi

    changes = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = _rsi_from_avg(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i + 1] = _rsi_from_avg(avg_gain, avg_loss)

    return rsi


def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    """§6-3 RSI 특수값 처리 — G(평균 상승폭)·L(평균 하락폭) 기준."""
    if avg_gain == 0 and avg_loss == 0:
        return 50.0  # "가격 변화 없음" — 우리 서비스의 명시적 규칙(외부 차트와 완전히 일치하지 않을 수 있음)
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_ma(closes: list[float], window: int) -> float | None:
    """최근 `window`개 종가의 단순 이동평균. 데이터가 부족하면 None."""
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def compute_ma_deviation_pct(closes: list[float], window: int) -> float | None:
    """최신 확정 종가의 이동평균 대비 괴리율(%, 반올림 전 값). 데이터가 부족하면 None."""
    ma = compute_ma(closes, window)
    if ma is None or ma == 0:
        return None
    current = closes[-1]
    return (current - ma) / ma * 100


def _shift_months(d: date, months: int) -> date:
    """d에서 `months`개월을 이동한 날짜. 대응 날짜가 없으면 그 달 말일로 보정(§6-2)."""
    total = d.year * 12 + (d.month - 1) + months
    y, m = divmod(total, 12)
    m += 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def compute_drawdown(
    candles: list[dict], *, days: int | None = None, months: int | None = None
) -> dict | None:
    """§6-2 확정 드로다운. candles는 과거 -> 최신 [{date_kst, high, close}, ...], **확정 데이터만**
    (당일 진행 중 캔들은 호출 전에 걸러야 한다 — price_history.confirmed_records).

    `days` 또는 `months` 중 정확히 하나를 지정한다:
      - 1개월 = days=30 (D-29 ~ D, 양끝 포함)
      - 1년   = days=365 (D-364 ~ D, 양끝 포함)
      - 4년   = months=48 (D에서 달력상 48개월 뺀 날짜의 다음날 ~ D, 양끝 포함)

    구간 내 단 하루라도 일봉이 없으면(캐시 누락) None을 반환한다 — 임의로 기간을 늘리거나 채우지
    않는다. 최고 고가(high) 기준·최신 확정 종가(close) 기준으로
    `(최신 확정 종가 / 기간 내 최고 고가 - 1) × 100`을 계산한다(기간 전체 MDD와는 다른 개념).
    """
    if not candles:
        return None
    if (days is None) == (months is None):
        raise ValueError("compute_drawdown: days와 months 중 정확히 하나만 지정해야 한다")

    end = candles[-1]
    end_date = date.fromisoformat(end["date_kst"])
    if days is not None:
        expected_start = end_date - timedelta(days=days - 1)
    else:
        expected_start = _shift_months(end_date, -months) + timedelta(days=1)

    by_date = {c["date_kst"]: c for c in candles}
    total_days = (end_date - expected_start).days + 1
    if total_days <= 0:
        return None

    window: list[dict] = []
    cursor = expected_start
    for _ in range(total_days):
        c = by_date.get(cursor.isoformat())
        if c is None:
            return None  # 구간 내 누락 일봉 — 늘리거나 채우지 않고 계산 불가로 처리(§6-2)
        window.append(c)
        cursor += timedelta(days=1)

    high_candle = max(window, key=lambda c: c["high"])
    high = high_candle["high"]
    if high == 0:
        return None
    return {
        "pct": (end["close"] / high - 1) * 100,
        "start_date": expected_start.isoformat(),
        "end_date": end_date.isoformat(),
        "high": high,
        # 2026-09-19 추가: 실사용 중 발견 — 이전에는 구간 내 최고가(high)만 반환하고 그 최고가가
        # "언제" 찍혔는지는 버리고 있었다. 그러자 이 값을 답변 문구로 옮기는 쪽(agent.py:_dd_desc)이
        # "(구간 시작일~종료일 고점 X원 대비)"처럼 구간 범위만 보여줬는데, 실제 Haiku 응답에서
        # "1년: 179,869,000원 대비(작년 9월 17일 고점)"처럼 구간 시작일을 고점 발생일로 잘못
        # 서술하는 결과가 나왔다 — 실제 고점은 그 약 3주 뒤(2025-10-09)였다. 최고가가 찍힌 실제
        # 날짜를 도구 결과 자체에 포함시켜, 답변을 만드는 쪽이 추측하지 않고 이 값을 그대로 쓰게 한다.
        "high_date": high_candle["date_kst"],
        "close": end["close"],
    }
