"""전략별 매수 조건 판정 — SPEC.md §3·§6-1 확정 조건.

`backtest.py`(과거 시뮬레이션)와 `month_state.py`(실사용 — 놓친 신호 재계산, §5)가 **같은 함수**를
공유한다. SPEC이 "현재 지표 조회와 백테스트는 같은 초기화 기준·계산 함수·가격 이력을 사용한다"고
못박은 원칙(§6-4, RSI에 한정된 문장이지만 취지상 전략 조건 판정에도 그대로 적용)을 지키기 위해서다
— 둘을 따로 구현하면 같은 조건인데 판정이 갈리는 회귀를 조용히 만들 수 있다.
"""

from __future__ import annotations


def check_decline_day(day: dict, first_buy_price: float) -> bool:
    """§3 전략①: 확정 종가 < 해당 월 첫 매수가 AND 당일 (종가/시가-1)*100 <= -5. 둘 다 참이어야 함."""
    if not day.get("open"):
        return False
    day_change_pct = (day["close"] / day["open"] - 1) * 100
    return day["close"] < first_buy_price and day_change_pct <= -5


def check_rsi(date_kst: str, rsi_by_date: dict[str, float | None]) -> bool:
    """§3 전략③: 확정 일봉 기준 RSI(14) <= 30. 반올림 전 값으로 판정한다(§6-5) — 호출부가
    rsi_by_date에 반올림하지 않은 원시값을 넣어야 한다."""
    rsi = rsi_by_date.get(date_kst)
    return rsi is not None and rsi <= 30


def first_trigger(
    strategy: str,
    days: list[dict],
    first_buy_price: float,
    rsi_by_date: dict[str, float | None],
    *,
    include_last_day: bool = True,
) -> dict | None:
    """1일 일봉부터(포함) 조건을 평가해 최초로 충족된 날을 찾는다(§6-1) — 없으면 None.

    `days`는 한 달치(과거->최신) 캔들. `include_last_day=False`면 마지막 날은 트리거 후보에서
    제외한다(백테스트에서 "월 마지막 날은 다음 날이 없어 트리거로 안 본다"는 규칙에 씀). 실사용
    놓친 신호 재계산에서는 마지막 확정 일봉 자체도 트리거일 수 있으므로 기본값은 True다.

    반환: {"trigger_date": "YYYY-MM-DD", "buy_date": "YYYY-MM-DD" | None(다음날 데이터가 아직 없음)}
    """
    if strategy not in ("decline_day", "rsi"):
        raise ValueError(f"first_trigger는 decline_day/rsi 전략에만 쓴다: {strategy}")

    limit = len(days) if include_last_day else len(days) - 1
    for i in range(0, limit):
        d = days[i]
        hit = check_decline_day(d, first_buy_price) if strategy == "decline_day" else check_rsi(d["date_kst"], rsi_by_date)
        if hit:
            buy_date = days[i + 1]["date_kst"] if i + 1 < len(days) else None
            return {"trigger_date": d["date_kst"], "buy_date": buy_date}
    return None


def biweekly_target_day(days: list[dict]) -> dict | None:
    """§3 전략②: 매월 15일 일봉. 그 달에 15일 데이터가 없으면(있을 수 없지만 방어적으로) None."""
    return next((d for d in days if d["date_kst"].endswith("-15")), None)
