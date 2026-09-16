"""3전략 백테스트 엔진 — SPEC.md §3(전략 조건)·§6·§6-1(백테스트 규칙) 확정 사항 그대로 구현.

확정 규칙 요약:
  - 구간: 직전 달까지 "완료된" 48개월(진행 중인 달은 제외, 달력만으로 완료 판단하지 않음) — §6·§6-1
  - 매 월 1일: 월 예산의 절반을 그 날 시가로 매수
  - 나머지 절반: 1일 일봉부터 조건을 평가해 최초 충족 신호 다음 날 시가로 매수(정기 분할은 15일 당일
    시가). 이전 달 신호가 다음 달 1일 매수에 영향을 주지 않는다(월별로 완전히 독립 시뮬레이션).
  - 조건부 전략이 월중 충족 없이 월말에 도달하면 월 마지막 날 시가로 매수(중복 매수 없음)
  - 최종 평가는 마지막 대상 일봉의 확정 종가(오늘 시점 가격이 아니다)
  - 수수료·슬리피지 0 가정 — §6
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from indicators import compute_rsi_series
from price_history import BACKTEST_MONTHS, KST, month_key, select_backtest_months
from strategy import biweekly_target_day, first_trigger

STRATEGIES = ("decline_day", "biweekly", "rsi")


def _group_by_month(candles: list[dict]) -> dict[tuple[int, int], list[dict]]:
    groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for c in candles:
        groups[month_key(c["date_kst"])].append(c)
    for k in groups:
        groups[k].sort(key=lambda c: c["date_kst"])
    return groups


def _simulate_month(
    strategy: str,
    days: list[dict],
    monthly_budget: float,
    rsi_by_date: dict[str, float | None],
) -> dict:
    """한 달치 캔들로 한 전략을 시뮬레이션해 그 달의 매수 내역을 반환한다."""
    half = monthly_budget / 2
    remaining = monthly_budget - half

    first_day = days[0]
    buys = [{"date": first_day["date_kst"], "amount_krw": half, "price": first_day["open"], "kind": "first_half"}]

    if strategy == "biweekly":
        target = biweekly_target_day(days)
        kind = "scheduled" if target is not None else "month_end_fallback"
        target = target or days[-1]
        buys.append({"date": target["date_kst"], "amount_krw": remaining, "price": target["open"], "kind": kind})
        return {"buys": buys}

    first_buy_price = first_day["open"]
    # §6-1: "해당 월 1일 일봉부터" 조건을 평가한다(1일 자신도 트리거 대상) — 마지막 날은 "다음 날"이
    # 없어 트리거 대상에서 제외하고 대신 월말 대체로 처리한다(include_last_day=False). 이전 달의
    # 신호가 이번 달로 넘어와 1일에 중복 매수를 만들지 않는다(매달 완전히 독립적으로 새로 판정).
    trigger = first_trigger(strategy, days, first_buy_price, rsi_by_date, include_last_day=False)

    if trigger is not None:
        buy_day = next(d for d in days if d["date_kst"] == trigger["buy_date"])
        buys.append(
            {"date": buy_day["date_kst"], "amount_krw": remaining, "price": buy_day["open"], "kind": "condition"}
        )
    else:
        last_day = days[-1]
        buys.append(
            {"date": last_day["date_kst"], "amount_krw": remaining, "price": last_day["open"], "kind": "month_end_fallback"}
        )

    return {"buys": buys}


def run_backtest(
    monthly_budget: float,
    candles: list[dict],
    now: datetime | None = None,
    months: int = BACKTEST_MONTHS,
) -> dict:
    """§6·§6-1 확정 규칙대로 세 전략을 48개월(직전 달까지 "완료된") 백테스트한다.

    candles: 과거 -> 최신 전체 OHLC(지표 선행 버퍼 포함, 로컬 캐시 그대로 — 당일 진행 중 캔들이
    섞여 있어도 무방하다. 월 완결 판정(select_backtest_months)이 이를 걸러낸다).

    반환:
      - 아직 48개월분이 확정되지 않았으면 {"ready": False, "reason": "..."} (§6-1: 월초 09:00 이전
        재조회 안내).
      - 준비됐으면 {"ready": True, "months_covered": [...], "final_evaluation_date": "...",
        "strategies": {strategy: {...}}}.
    """
    month_keys = select_backtest_months(candles, now=now, months=months)
    if len(month_keys) < months:
        return {
            "ready": False,
            "reason": (
                f"직전 달까지 완료된 {months}개월 데이터가 아직 갖춰지지 않았습니다"
                f"(현재 확정된 달: {len(month_keys)}개월). 마감 이후 다시 조회해주세요."
            ),
        }

    groups = _group_by_month(candles)
    all_closes = [c["close"] for c in candles]
    rsi_series = compute_rsi_series(all_closes)
    rsi_by_date = {candles[i]["date_kst"]: rsi_series[i] for i in range(len(candles))}

    last_month_days = groups[month_keys[-1]]
    final_close = last_month_days[-1]["close"]  # §6-1: 최종 평가는 마지막 대상 일봉의 확정 종가

    results = {}
    for strategy in STRATEGIES:
        total_invested = 0.0
        total_qty = 0.0
        condition_buys = 0
        fallback_buys = 0
        all_buys: list[dict] = []

        for mk in month_keys:
            sim = _simulate_month(strategy, groups[mk], monthly_budget, rsi_by_date)
            for buy in sim["buys"]:
                total_invested += buy["amount_krw"]
                total_qty += buy["amount_krw"] / buy["price"] if buy["price"] else 0.0
                if buy["kind"] == "condition":
                    condition_buys += 1
                elif buy["kind"] == "month_end_fallback":
                    fallback_buys += 1
            all_buys.extend(sim["buys"])

        avg_price = (total_invested / total_qty) if total_qty else 0.0
        valuation = total_qty * final_close
        profit = valuation - total_invested
        results[strategy] = {
            "total_invested_krw": total_invested,
            "total_qty_btc": total_qty,
            "avg_price_krw": avg_price,
            "valuation_krw": valuation,
            "profit_krw": profit,
            "return_pct": (profit / total_invested * 100) if total_invested else 0.0,
            "condition_buys": condition_buys,
            "month_end_fallback_buys": fallback_buys,
            "buys": all_buys,
        }

    return {
        "ready": True,
        "months_covered": [f"{y}-{m:02d}" for y, m in month_keys],
        "final_evaluation_date": last_month_days[-1]["date_kst"],
        "strategies": results,
    }
