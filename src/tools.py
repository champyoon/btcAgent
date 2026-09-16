"""BTC DCA 어시스턴트 — 도구 모음.

Day3(도구 정의) · Day4(예외 없는 도구 + 실패 분류) · Day1(결정적 규칙) 패턴을 이 도메인에 적용합니다.

원칙:
  - 모든 도구는 예외를 밖으로 던지지 않고 문자열/딕셔너리를 반환합니다.
  - 네트워크 도구는 실패 종류(retryable/backoff/fatal)에 따라 다르게 대응하고,
    끝내 실패하면 백업 경로로 넘어가되 그 사실을 반환값에 남깁니다.
  - 가상 매수 기록은 실제 거래소 주문이 아닙니다. data/ledger.json에만 남습니다.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

UPBIT_BASE = "https://api.upbit.com/v1"
MARKET = "KRW-BTC"
MAX_RETRIES = 3

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
LEDGER_PATH = DATA_DIR / "ledger.json"
PRICE_CACHE_PATH = DATA_DIR / "price_cache.json"


# ══════════════════════════════════════════════════════════════════
# 실패 분류 (Day4 패턴)
# ══════════════════════════════════════════════════════════════════

def classify_failure(error: Exception) -> str:
    """오류를 retryable / backoff / fatal 세 갈래로 분류합니다.

    다시 물었을 때 답이 달라질 수 있는가를 기준으로 가릅니다.
    """
    if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return "retryable"
    if isinstance(error, requests.exceptions.HTTPError):
        status = getattr(error.response, "status_code", None)
        if status == 429:
            return "backoff"
        return "fatal"
    return "fatal"


def _default_price_fetch() -> dict:
    resp = requests.get(f"{UPBIT_BASE}/ticker", params={"markets": MARKET}, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("빈 응답")
    return data[0]


def _default_candles_fetch(days: int) -> list[dict]:
    resp = requests.get(
        f"{UPBIT_BASE}/candles/days", params={"market": MARKET, "count": days}, timeout=5
    )
    resp.raise_for_status()
    return resp.json()


def _load_price_cache() -> dict:
    if PRICE_CACHE_PATH.exists():
        return json.loads(PRICE_CACHE_PATH.read_text(encoding="utf-8"))
    return {"trade_price": 0, "cached_at": "없음"}


def _save_price_cache(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "trade_price": data.get("trade_price", 0),
        "signed_change_rate": data.get("signed_change_rate", 0),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    PRICE_CACHE_PATH.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def get_btc_price_backup() -> str:
    """실시간 조회가 계속 실패했을 때 최근 캐시된 가격 스냅샷을 반환합니다."""
    cached = _load_price_cache()
    if cached.get("cached_at") == "없음":
        return "[백업 경로: get_btc_price_backup] 캐시된 값이 없어 가격을 알 수 없습니다."
    return (
        f"[백업 경로: get_btc_price_backup] 캐시된 가격 {cached['trade_price']:,.0f}원 "
        f"({cached['cached_at']} 기준, 실시간 아님)"
    )


def get_btc_price(fetch: Callable[[], dict] | None = None) -> str:
    """비트코인(KRW-BTC) 현재가와 전일 대비 변동률을 조회합니다. '지금 가격 얼마?' 류 질문에 씁니다."""
    fetch = fetch or _default_price_fetch
    last_err: Exception | None = None
    for _ in range(MAX_RETRIES):
        try:
            data = fetch()
            _save_price_cache(data)
            return (
                f"현재가 {data['trade_price']:,.0f}원 "
                f"(전일 대비 {data['signed_change_rate'] * 100:+.2f}%)"
            )
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if classify_failure(exc) == "fatal":
                break
    reason = classify_failure(last_err) if last_err else "fatal"
    return f"실시간 시세 조회 실패({reason}). " + get_btc_price_backup()


def get_price_history(days: int = 200, fetch: Callable[[int], list[dict]] | None = None) -> str:
    """최근 N일(기본 200일) 일봉 종가 히스토리를 조회합니다. 이동평균·추세 계산의 재료로 씁니다."""
    days = max(1, min(days, 200))
    fetch = fetch or _default_candles_fetch
    try:
        candles = fetch(days)
        closes = [c["trade_price"] for c in candles]
        return f"{len(closes)}일치 종가 히스토리를 가져왔습니다 (최신 종가 {closes[0]:,.0f}원)."
    except Exception as exc:  # noqa: BLE001
        reason = classify_failure(exc)
        return f"가격 히스토리 조회 실패({reason}). 지표 계산을 건너뜁니다."


def _fetch_indicator_inputs(fetch: Callable[[int], list[dict]] | None = None) -> list[dict] | None:
    fetch = fetch or _default_candles_fetch
    try:
        return fetch(200)
    except Exception:  # noqa: BLE001
        return None


def get_indicators(fetch: Callable[[int], list[dict]] | None = None) -> dict:
    """200일 이동평균 괴리율, 52주 드로다운, 14일 RSI를 계산해 dict로 반환합니다.

    네트워크 실패 시 빈 값 대신 usable=False가 포함된 dict를 반환합니다 (예외를 던지지 않습니다).
    """
    candles = _fetch_indicator_inputs(fetch)
    if not candles:
        return {"usable": False, "reason": "가격 히스토리를 가져오지 못했습니다."}

    closes = [c["trade_price"] for c in candles]  # candles[0]이 최신
    current = closes[0]

    ma200 = sum(closes) / len(closes)
    ma200_deviation_pct = (current - ma200) / ma200 * 100

    high_1y = max(c["high_price"] for c in candles)
    drawdown_from_high_pct = (current - high_1y) / high_1y * 100

    rsi14 = _compute_rsi(closes[:15]) if len(closes) >= 15 else None

    return {
        "usable": True,
        "current_price": current,
        "ma200": ma200,
        "ma200_deviation_pct": round(ma200_deviation_pct, 2),
        "drawdown_from_high_pct": round(drawdown_from_high_pct, 2),
        "rsi14": round(rsi14, 1) if rsi14 is not None else None,
    }


def _compute_rsi(recent_closes_desc: list[float]) -> float:
    """최신순으로 정렬된 종가 리스트(15개, 최신이 [0])로 14일 RSI를 계산합니다."""
    closes = list(reversed(recent_closes_desc))  # 과거 -> 최신
    gains, losses = [], []
    for prev, curr in zip(closes, closes[1:]):
        change = curr - prev
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ══════════════════════════════════════════════════════════════════
# DCA 판정 (Day1 패턴 — LLM 없이 결정적 규칙)
# ══════════════════════════════════════════════════════════════════

def assess_dca_signal(indicators: dict) -> dict:
    """지표를 근거로 DCA 매수 시그널을 결정적으로 판정합니다. LLM을 호출하지 않습니다."""
    if not indicators.get("usable"):
        return {"signal": "판정 불가", "reason": "지표를 계산할 수 없습니다.", "rule_applied": None}

    dev = indicators["ma200_deviation_pct"]
    dd = indicators["drawdown_from_high_pct"]
    rsi = indicators.get("rsi14")

    if dev <= -15 and rsi is not None and rsi <= 35:
        return {
            "signal": "적극 매수 검토",
            "reason": f"200일 이평 대비 {dev:.1f}%, RSI {rsi:.0f} — 저점 신호 중첩",
            "rule_applied": "strong_buy",
        }
    if dev <= -8 or dd <= -25:
        return {
            "signal": "분할 매수 검토",
            "reason": f"200일 이평 대비 {dev:.1f}% 또는 고점 대비 {dd:.1f}% 하락",
            "rule_applied": "buy_watch",
        }
    if rsi is not None and rsi >= 70:
        return {
            "signal": "매수 자제",
            "reason": f"RSI {rsi:.0f} 과열 구간",
            "rule_applied": "overheated",
        }
    return {"signal": "관망", "reason": "뚜렷한 저점·과열 신호가 없습니다.", "rule_applied": None}


# ══════════════════════════════════════════════════════════════════
# 가상 매수 기록 (Day5의 read/write/destructive 대상이 되는 도구들)
# ══════════════════════════════════════════════════════════════════

def _load_ledger() -> list[dict]:
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return []


def _save_ledger(entries: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def search_ledger(question: str = "") -> str:
    """지금까지 남긴 가상 매수 기록을 조회합니다. (read — 승인 불필요)"""
    entries = _load_ledger()
    if not entries:
        return "가상 매수 기록이 없습니다."
    total_krw = sum(e["amount_krw"] for e in entries)
    lines = [f"- {e['recorded_at']} {e['amount_krw']:,.0f}원 (기준가 {e['price_krw']:,.0f}원)" for e in entries]
    return f"총 {len(entries)}건, 누적 {total_krw:,.0f}원 매수 기록:\n" + "\n".join(lines)


def record_virtual_buy(amount_krw: float, price_krw: float, note: str = "") -> str:
    """가상 매수 기록을 한 건 추가합니다. 실제 거래소 주문이 아닙니다. (write — 승인 필요)"""
    entries = _load_ledger()
    entries.append(
        {
            "amount_krw": amount_krw,
            "price_krw": price_krw,
            "note": note,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _save_ledger(entries)
    return f"가상 매수 기록 추가: {amount_krw:,.0f}원 (기준가 {price_krw:,.0f}원). 실제 주문은 발생하지 않았습니다."


def reset_ledger() -> str:
    """가상 매수 기록을 전부 삭제합니다. 되돌릴 수 없습니다. (destructive — 승인+이중확인 필요)"""
    count = len(_load_ledger())
    _save_ledger([])
    return f"가상 매수 기록 {count}건을 모두 삭제했습니다. 이 작업은 되돌릴 수 없습니다."
