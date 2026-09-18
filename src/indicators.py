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


#: DD·MDD 계산 기준 버전 — 정책이 바뀌면 이 문자열도 새로 발급한다(§6-2, 2026-09-20 정책
#: 변경으로 "close_v1" 도입). 과거 관망 스냅샷(indicators_snapshot)에는 이 키 자체가 없으므로
#: "키가 없으면 구 기준(고가 기준) 또는 DD·MDD 미포함"이라는 뜻으로 자연스럽게 구분된다.
DD_MDD_CALC_BASIS = "close_v1"


def _window_for_period(
    candles: list[dict], *, days: int | None = None, months: int | None = None
) -> tuple[list[dict], date, date] | None:
    """DD·MDD가 공유하는 구간 추출 — candles는 과거 -> 최신 [{date_kst, close}, ...], **확정
    데이터만**(당일 진행 중 캔들은 호출 전에 걸러야 한다 — price_history.confirmed_records).

    `days` 또는 `months` 중 정확히 하나를 지정한다:
      - 1개월 = days=30 (D-29 ~ D, 양끝 포함)
      - 1년   = days=365 (D-364 ~ D, 양끝 포함)
      - 4년   = months=48 (D에서 달력상 48개월 뺀 날짜의 다음날 ~ D, 양끝 포함)

    구간 내 단 하루라도 일봉이 없으면(캐시 누락이든, 애초에 그만큼의 과거 데이터가 없든) None을
    반환한다 — 임의로 기간을 늘리거나 짧은 구간으로 대체하지 않는다.
    """
    if not candles:
        return None
    if (days is None) == (months is None):
        raise ValueError("_window_for_period: days와 months 중 정확히 하나만 지정해야 한다")

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
            return None  # 구간 내 누락(또는 그만큼의 과거 데이터 자체가 없음) — 계산 불가(§6-2)
        window.append(c)
        cursor += timedelta(days=1)
    return window, expected_start, end_date


def compute_dd_mdd(
    candles: list[dict], *, days: int | None = None, months: int | None = None
) -> dict | None:
    """§6-2(2026-09-20 정책 변경) 확정 DD·MDD — **둘 다 확정 종가(close) 기준으로 통일**한다.
    기존에는 DD를 최고 고가(high) 기준으로 계산했다 — 그 기준은 폐기됐다. `high` 필드는 이제 이
    함수에서 전혀 읽지 않는다.

    DD(현재 하락률, %) = (최신 확정 종가 / 기간 내 최고 종가 - 1) × 100
    MDD(최대 낙폭, %) = 날짜순으로 그 시점까지의 최고 종가 대비 각 시점 낙폭 중 최솟값 — 반드시
    고점 이후에 발생한 저점만 반영한다(단순 기간 전체 최저가/최고가 비교가 아니다). 상승만 한
    구간·횡보 구간의 MDD는 0%. 둘 다 0 이하의 부호 있는 백분율로, 항상 MDD ≤ DD ≤ 0이다(DD도
    같은 러닝 최고 종가로 계산되는 마지막 시점의 낙폭이므로).

    구간 내 결측 일봉이 있거나(그만큼의 과거 데이터 자체가 없어도 마찬가지), 구간 내 종가가 0
    이하인 값이 있으면 None — 임의로 기간을 줄이거나 값을 추정해 채우지 않는다.
    """
    extracted = _window_for_period(candles, days=days, months=months)
    if extracted is None:
        return None
    window, start_date, end_date = extracted

    peak_close: float | None = None
    peak_date: str | None = None
    mdd_pct: float | None = None
    mdd_peak_close: float | None = None
    mdd_peak_date: str | None = None
    mdd_trough_close: float | None = None
    mdd_trough_date: str | None = None

    for c in window:
        close = c["close"]
        if close <= 0:
            return None  # 유효하지 않은 가격 — 계산 불가
        if peak_close is None or close > peak_close:
            peak_close = close
            peak_date = c["date_kst"]
        dd_at_t = (close / peak_close - 1) * 100
        if mdd_pct is None or dd_at_t < mdd_pct:
            mdd_pct = dd_at_t
            mdd_peak_close = peak_close
            mdd_peak_date = peak_date
            mdd_trough_close = close
            mdd_trough_date = c["date_kst"]

    end_close = window[-1]["close"]
    dd_pct = (end_close / peak_close - 1) * 100

    return {
        "calc_basis": DD_MDD_CALC_BASIS,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "end_close": end_close,
        "dd_pct": dd_pct,
        "dd_peak_close": peak_close,
        "dd_peak_date": peak_date,
        "mdd_pct": mdd_pct,
        "mdd_peak_close": mdd_peak_close,
        "mdd_peak_date": mdd_peak_date,
        "mdd_trough_close": mdd_trough_close,
        "mdd_trough_date": mdd_trough_date,
    }
