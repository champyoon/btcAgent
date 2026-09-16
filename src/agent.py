"""BTC DCA Agent — Supervisor + 전문 Agent 4개 (Day6 패턴, Day3의 순환 그래프 포함).

구성:
  price_agent    — 실시간 시세 + 지표 값·의미 설명 (read 전용, 종합 매수 등급 없음 — SPEC §3-1)
  plan_agent     — 이번 달 예산·전략 상태 조회, 전략 선택/변경(자체 토큰 확인), 48개월 백테스트
  research_agent — 전략/리스크/용어 문서 검색 (RAG, read 전용)
  ledger_agent   — 실제 매수·관망 기록 조회/기록/정정/초기화 (write·destructive 포함 → 승인 게이트)

route_question은 LLM을 부르지 않는 결정적 규칙입니다 (Day6).
승인이 필요한 도구 호출은 그래프가 실행하지 않고 "await_approval" 상태로 멈춥니다 (Day5의 게이트를
프롬프트가 아니라 그래프 구조로 강제하는 지점) — SPEC §10-1: 이때 approvals.py가 approval_id를
발급해 tool/args를 서버 메모리에 저장하고, 사용자가 /approve(또는 /reject)로 그 id를 보내야만
실제 실행(또는 거절)됩니다. select_strategy는 이 표준 게이트 대상이 아니라 자체 토큰 확인
(propose/confirm)으로 서버가 검증합니다(SPEC §14-0) — guardrails.RISK_LEVELS에 "read"로 등록된
이유입니다.

데이터 확정성 게이트(SPEC §7-1): 지표·백테스트 계산 전 로컬 가격 캐시가 최신 확정 일봉까지
갖춰졌는지 확인합니다. 갖춰지지 않았고 사용자가 아직 진행을 허락하지 않았으면 계산을 멈추고
확인을 요청합니다 — `build_supervisor()`가 반환하는 콜러블의 `proceed_with_stale_data` 인자가
그 허락 신호입니다(API가 무상태라 매 요청에 다시 실어 보내는 방식).

API 계약: build_supervisor(llm)의 반환 콜러블은 질문 문자열을 받아
    {"answer": str, "contexts": [{"doc_id","text"}], "trace": [{"step","input","output"}], ...}
를 반환합니다 (§4-2 표준 규약). agents_used/approvals_needed/data_gap_needs_confirmation은
계약 밖 부가 정보입니다.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

import approvals
import backtest as backtest_mod
import guardrails
import indicators
import ledger
import month_state
import price_history
import retriever
import tools as domain_tools
from price_history import KST
from datetime import datetime

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
REGION = "us-east-1"
MAX_STEPS = 4  # Day3의 MAX_TOOL_CALLS와 같은 취지 — 순환이 무한히 돌지 않게

AGENT_NAMES: list[str] = ["price_agent", "plan_agent", "research_agent", "ledger_agent"]


def _default_llm():
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(model=MODEL_ID, region_name=REGION, temperature=0)


# ══════════════════════════════════════════════════════════════════
# 요청 스코프 상태 — 단일 요청 기준으로만 안전(README "알려진 한계"와 동일한 스코프)
# ══════════════════════════════════════════════════════════════════

_REQUEST_CONTEXT: dict[str, Any] = {"proceed_with_stale_data": False}
_LAST_DATA_GAP: dict | None = None
_LAST_RETRIEVED_DOCS: list = []


def _freshness_gate_message() -> str | None:
    """SPEC §7-1 확인 순서. 계산을 막아야 하면 안내 메시지를 반환하고, 통과하면 None을 반환한다."""
    global _LAST_DATA_GAP
    freshness = price_history.check_freshness()
    if freshness["fresh"]:
        return None
    if _REQUEST_CONTEXT.get("proceed_with_stale_data"):
        return None
    _LAST_DATA_GAP = freshness
    return (
        f"[데이터 확인 필요] 최근 확보된 데이터가 {freshness['last_available']}까지입니다. "
        f"마감된 최신 일봉({freshness['expected']})이 아직 없어 계산을 진행하지 않았습니다. "
        "그래도 지금 있는 데이터로 진행하려면 proceed_with_stale_data를 true로 다시 요청해주세요."
    )


def _staleness_caveat() -> str:
    freshness = price_history.check_freshness()
    if freshness["fresh"]:
        return ""
    return f" ⚠ {freshness['last_available']} 기준(최신 데이터 지연 — 이후 다시 확인 권장)"


# ══════════════════════════════════════════════════════════════════
# 도구 — Agent별로 격리 (같은 도구가 두 Agent에 걸치지 않음)
# ══════════════════════════════════════════════════════════════════

@tool
def get_btc_price() -> str:
    """비트코인(KRW-BTC) 현재가와 전일 대비 변동률을 실시간으로 조회합니다."""
    return domain_tools.get_btc_price()


@tool
def get_indicators() -> str:
    """RSI(14, RMA)·200일 이동평균 괴리율·기간별(1개월/1년/4년) 드로다운의 값과 의미를 설명합니다.
    종합 매수 등급은 만들지 않습니다 — 매수 조건 판정은 select_strategy로 고른 전략이 따로 봅니다."""
    price_history.update_incremental()
    gate_msg = _freshness_gate_message()
    if gate_msg:
        return gate_msg

    records = price_history.confirmed_records()
    if len(records) < 200:
        return "지표 계산에 필요한 데이터가 아직 부족합니다(최소 200일 확정 일봉 필요)."

    closes = [r["close"] for r in records]
    rsi_series = indicators.compute_rsi_series(closes)
    rsi = rsi_series[-1]
    ma_dev = indicators.compute_ma_deviation_pct(closes, 200)
    dd_1m = indicators.compute_drawdown(records, days=30)
    dd_1y = indicators.compute_drawdown(records, days=365)
    dd_4y = indicators.compute_drawdown(records, months=48)

    def _rsi_desc(v: float | None) -> str:
        if v is None:
            return "계산 불가"
        if v > 70:
            return f"{v:.2f} (과매수 구간)"
        if v < 30:
            return f"{v:.2f} (과매도 구간)"
        return f"{v:.2f} (중립 구간)"

    def _dd_desc(d: dict | None) -> str:
        if d is None:
            return "계산 불가(구간 내 데이터 부족)"
        return f"{d['pct']:.2f}%({d['start_date']}~{d['end_date']} 고점 {d['high']:,.0f}원 대비)"

    ma_desc = "계산 불가" if ma_dev is None else f"{ma_dev:.2f}%({'저평가 방향' if ma_dev < 0 else '고평가 방향'})"

    return (
        f"기준일 {records[-1]['date_kst']}(확정 종가 {records[-1]['close']:,.0f}원). "
        f"RSI(14, RMA) {_rsi_desc(rsi)}. 200일 이동평균 괴리율 {ma_desc}. "
        f"드로다운 — 1개월 {_dd_desc(dd_1m)}, 1년 {_dd_desc(dd_1y)}, 4년 {_dd_desc(dd_4y)}."
        f"{_staleness_caveat()}"
    )


@tool
def get_month_status(year: int = 0, month: int = 0) -> str:
    """이번 달(연·월을 지정하지 않으면 오늘 기준)의 예산·선택 전략·남은 예산·전략 변경 가능 여부를
    조회합니다."""
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    status = month_state.get_month_status(year, month, now=now)

    if not status["plan_started"]:
        return f"{year}-{month:02d}은(는) 아직 계획이 시작되지 않았습니다(월중 가입 시 다음 달부터 시작)."

    lines = [f"{year}-{month:02d} 계획 상태 — 월 예산 {status['monthly_budget_krw']:,.0f}원"]
    if "spent_krw" in status:
        over = " (예산 초과 — 추가 매수는 권하지 않습니다)" if status["over_budget"] else ""
        lines.append(f"사용액 {status['spent_krw']:,.0f}원, 남은 예산 {status['remaining_krw']:,.0f}원{over}")
    lines.append(f"선택 전략: {status['strategy'] or '미선택'}")
    lines.append(
        "정기 분할 변경: " + ("가능" if status["biweekly_change_allowed"] else "불가(15일 09:00 KST 마감)")
    )
    lines.append(
        "전략 변경 전체: " + ("가능" if status["strategy_change_allowed"] else "불가(월말 00:00 KST 마감)")
    )
    return "\n".join(lines)


@tool
def set_monthly_budget(amount_krw: float) -> str:
    """월 투자 예산을 설정합니다. 아직 계획을 시작한 적이 없다면 이 호출로 계획이 시작됩니다
    (오늘이 1일이면 이번 달부터, 아니면 다음 달부터 — SPEC §2-0). 이미 계획이 있다면 예산 변경으로
    처리되며, 항상 다음 달부터 적용됩니다(이번 달 예산은 그대로 유지)."""
    state = month_state.load_state()
    if not state.get("plan_start_month"):
        result = month_state.init_plan(amount_krw)
        if not result["ok"]:
            return f"설정 실패: {result['error']}"
        return f"월 예산 {amount_krw:,.0f}원으로 계획을 시작합니다 — {result['plan_start_month']}부터 적용됩니다."

    result = month_state.set_monthly_budget(amount_krw)
    if not result["ok"]:
        return f"설정 실패: {result['error']}"
    return (
        f"월 예산을 {amount_krw:,.0f}원으로 변경합니다 — {result['effective_month']}부터 적용됩니다"
        "(이번 달은 기존 예산이 그대로 유지됩니다)."
    )


@tool
def select_strategy(strategy: str, year: int = 0, month: int = 0, confirmation_token: str = "") -> str:
    """이번 달 남은 예산에 적용할 전략을 선택/변경합니다. strategy는 decline_day(하락일)/
    biweekly(정기 분할)/rsi(RSI 매수) 중 하나입니다. 처음 호출하면 적용되지 않고 확인 절차만
    시작됩니다 — 사용자에게 변경 내용을 확인받은 뒤, 같은 인자에 confirmation_token을 채워
    다시 호출해야 실제로 적용됩니다(서버가 토큰으로 검증합니다)."""
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month

    if confirmation_token:
        result = month_state.confirm_strategy_change(confirmation_token, now=now)
        if not result["ok"]:
            return f"적용 실패: {result['error']}"
        prev = result["previous"] or "미선택"
        return f"{result['year']}-{result['month']:02d} 전략을 '{result['strategy']}'(으)로 변경했습니다(이전: {prev})."

    proposal = month_state.propose_strategy_change(year, month, strategy, now=now)
    if not proposal["ok"]:
        return f"변경 불가: {proposal['error']}"
    return (
        f"{year}-{month:02d} 전략을 '{strategy}'(으)로 변경하는 안입니다. 사용자에게 확인받은 뒤, "
        f"confirmation_token='{proposal['confirmation_token']}'로 이 도구를 다시 호출해 적용하세요."
    )


@tool
def run_backtest(monthly_budget_krw: float) -> str:
    """직전 달까지 완료된 48개월 동안 세 전략(하락일/정기 분할/RSI)을 같은 월 예산으로 비교
    시뮬레이션합니다. 수수료·슬리피지는 0으로 가정한 가상 결과입니다."""
    records = price_history.update_incremental()
    gate_msg = _freshness_gate_message()
    if gate_msg:
        return gate_msg

    result = backtest_mod.run_backtest(monthly_budget_krw, records)
    if not result["ready"]:
        return result["reason"]

    labels = {"decline_day": "하락일 매수", "biweekly": "정기 분할", "rsi": "RSI 매수"}
    lines = [
        f"직전 달까지 완료된 48개월({result['months_covered'][0]} ~ {result['months_covered'][-1]}) 기준, "
        f"가상 결과이며 수수료·슬리피지는 0으로 가정합니다."
    ]
    for name, label in labels.items():
        r = result["strategies"][name]
        lines.append(
            f"- {label}: 총 투자 {r['total_invested_krw']:,.0f}원 / 누적 {r['total_qty_btc']:.8f} BTC / "
            f"평균매수가 {r['avg_price_krw']:,.0f}원 / 평가금액 {r['valuation_krw']:,.0f}원 / "
            f"수익률 {r['return_pct']:.2f}% (조건매수 {r['condition_buys']}회, 월말대체 {r['month_end_fallback_buys']}회)"
        )
    return "\n".join(lines)


@tool
def retrieve_docs(query: str) -> str:
    """BTC·DCA·지표·서비스 규칙 문서에서 질문과 관련된 내용을 검색합니다."""
    docs = retriever.search_docs(query)
    _LAST_RETRIEVED_DOCS.extend(docs)
    if not docs:
        return "관련 문서를 찾지 못했습니다."
    return retriever.format_docs(docs)


@tool
def search_ledger() -> str:
    """지금까지 남긴 실제 매수·관망 기록을 조회합니다."""
    entries = ledger.search_ledger()
    if not entries:
        return "기록이 없습니다."
    lines = []
    for e in entries:
        if e["type"] == "buy":
            lines.append(
                f"[{e['record_id']}] 매수 {e.get('executed_date')} {e['amount_krw']:,.0f}원 "
                f"(상태: {e['status']})"
            )
        else:
            lines.append(f"[{e['record_id']}] 관망 {e['recorded_at'][:10]} — {e.get('note', '')}")
    return "\n".join(lines)


@tool
def record_virtual_buy(
    amount_krw: float,
    price_krw: float | None = None,
    quantity_btc: float | None = None,
    executed_date: str = "",
    note: str = "",
) -> str:
    """실제 매수 내역을 기록합니다. 실제 거래소 주문이 아니라 사용자가 신고한 내역입니다.
    executed_date는 실제 매수한 날짜(YYYY-MM-DD, KST)입니다. (승인 필요)"""
    if not executed_date:
        return "executed_date(실제 매수 날짜)가 필요합니다."
    result = ledger.record_virtual_buy(
        amount_krw=amount_krw,
        price_krw=price_krw,
        quantity_btc=quantity_btc,
        executed_date=executed_date,
        execution_time_precision="date",
        note=note,
    )
    if not result["ok"]:
        return f"기록 실패: {result['error']}"
    return f"매수 기록 추가: {result['record']['record_id']} ({amount_krw:,.0f}원). 실제 주문은 발생하지 않았습니다."


@tool
def amend_virtual_buy(
    record_id: str,
    amount_krw: float | None = None,
    price_krw: float | None = None,
    quantity_btc: float | None = None,
    executed_date: str | None = None,
    note: str | None = None,
    reason: str = "",
) -> str:
    """기존 매수 기록을 정정합니다 — 장부 정정이며 실제 거래 취소가 아닙니다. 지정한 값만
    바뀌고 나머지는 그대로 유지됩니다. (승인 필요)"""
    kwargs: dict[str, Any] = {}
    if amount_krw is not None:
        kwargs["amount_krw"] = amount_krw
    if price_krw is not None:
        kwargs["price_krw"] = price_krw
    if quantity_btc is not None:
        kwargs["quantity_btc"] = quantity_btc
    if executed_date is not None:
        kwargs["executed_date"] = executed_date
    if note is not None:
        kwargs["note"] = note
    result = ledger.amend_virtual_buy(record_id, reason=reason, **kwargs)
    if not result["ok"]:
        return f"수정 실패: {result['error']}"
    return f"매수 기록 {record_id} 정정 완료. 장부 정정이며 실제 거래 취소가 아닙니다."


@tool
def cancel_virtual_buy(record_id: str, reason: str = "") -> str:
    """매수 기록을 취소합니다 — 레코드를 삭제하는 게 아니라 상태를 전환하는 장부 정정입니다.
    실제 거래 취소가 아닙니다. (승인 필요)"""
    result = ledger.cancel_virtual_buy(record_id, reason=reason)
    if not result["ok"]:
        return f"취소 실패: {result['error']}"
    return f"매수 기록 {record_id} 취소 완료. {result['note']}"


@tool
def record_watch_decision(note: str) -> str:
    """이번엔 매수하지 않고 지켜보기로 한 결정을 그 순간의 지표 스냅샷과 함께 기록합니다.
    실제 자금 이동이 없는 결정이라 승인이 필요 없습니다."""
    price_history.update_incremental()
    records = price_history.confirmed_records()
    snapshot: dict[str, Any] = {}
    if len(records) >= 200:
        closes = [r["close"] for r in records]
        rsi_series = indicators.compute_rsi_series(closes)
        snapshot = {
            "rsi14": rsi_series[-1],
            "ma200_deviation_pct": indicators.compute_ma_deviation_pct(closes, 200),
            "as_of": records[-1]["date_kst"],
        }
    result = ledger.record_watch_decision(note=note, indicators_snapshot=snapshot)
    return f"관망 기록 추가: {result['record']['record_id']}"


@tool
def reset_ledger() -> str:
    """실제 매수·관망 기록을 전부 삭제합니다. 되돌릴 수 없습니다. (승인 + 이중확인 필요)"""
    result = ledger.reset_ledger()
    return f"기록 {result['cleared_count']}건을 모두 삭제했습니다. 이 작업은 되돌릴 수 없습니다."


AGENT_TOOLS: dict[str, list[str]] = {
    "price_agent": ["get_btc_price", "get_indicators"],
    "plan_agent": ["get_month_status", "set_monthly_budget", "select_strategy", "run_backtest"],
    "research_agent": ["retrieve_docs"],
    "ledger_agent": [
        "search_ledger",
        "record_virtual_buy",
        "amend_virtual_buy",
        "cancel_virtual_buy",
        "record_watch_decision",
        "reset_ledger",
    ],
}

_ALL_TOOLS = [
    get_btc_price,
    get_indicators,
    get_month_status,
    set_monthly_budget,
    select_strategy,
    run_backtest,
    retrieve_docs,
    search_ledger,
    record_virtual_buy,
    amend_virtual_buy,
    cancel_virtual_buy,
    record_watch_decision,
    reset_ledger,
]
_TOOL_REGISTRY = {t.name: t for t in _ALL_TOOLS}

_AGENT_SYSTEM_PROMPTS = {
    "price_agent": (
        "당신은 비트코인 시세·지표 담당 Agent입니다. 시세와 지표 조회 도구만 사용해 사실을 전달하세요. "
        "지표는 값과 의미만 설명하고, 종합 매수 등급이나 확정적인 투자 조언(무조건 오른다 등)은 "
        "하지 마세요."
    ),
    "plan_agent": (
        "당신은 이번 달 예산·전략 계획 담당 Agent입니다. get_month_status로 현재 상태를 확인하고, "
        "run_backtest로 과거 48개월 비교를 보여주고, select_strategy로 전략을 선택/변경합니다. "
        "select_strategy는 반드시 confirmation_token 없이 먼저 호출해 제안 내용을 사용자에게 보여주고, "
        "사용자가 명시적으로 동의한 뒤에만 confirmation_token을 채워 다시 호출하세요 — 동의 없이 "
        "바로 적용하지 마세요. 과거 성과가 가장 좋았던 전략을 대신 골라주지 마세요, 선택은 항상 "
        "사용자가 합니다."
    ),
    "research_agent": (
        "당신은 BTC·DCA·지표·서비스 규칙 문서 검색 담당 Agent입니다. retrieve_docs 도구로 찾은 내용만 "
        "근거로 답하세요. 문서에 없는 내용은 답하지 마세요."
    ),
    "ledger_agent": (
        "당신은 실제 매수·관망 기록 담당 Agent입니다. 실제 거래소 주문은 발생하지 않는다는 점을 항상 "
        "분명히 하세요. 매수 기록 생성·수정·취소·초기화는 도구가 승인 절차를 거칩니다. 매수 기록 "
        "수정·취소는 장부 정정일 뿐 실제 거래 취소가 아니라는 점도 함께 안내하세요."
    ),
}


# ══════════════════════════════════════════════════════════════════
# 라우팅 (Day6 패턴 — LLM 없이 결정적 규칙)
# ══════════════════════════════════════════════════════════════════

_AGENT_KEYWORDS: dict[str, list[str]] = {
    "price_agent": [
        "가격", "시세", "현재가", "지표", "이동평균", "이평", "rsi", "드로다운",
        "얼마", "급등", "급락", "price", "indicator",
    ],
    "plan_agent": [
        "예산", "남은 예산", "이번 달", "이번달", "전략", "백테스트", "시뮬레이션", "비교",
        "선택", "변경", "budget", "backtest", "strategy", "정기 분할", "정기분할", "하락일",
    ],
    "research_agent": [
        "전략", "원칙", "정의", "용어", "리스크", "관리", "란", "무엇", "dca",
        "strategy", "glossary", "위험",
        # RSI/이동평균/드로다운은 문서(data/docs)에 정확한 기준값이 정의돼 있어, price_agent의
        # 실시간 조회만으로는 답이 grounding 없이 LLM의 일반 지식(부정확할 수 있음)에 의존하게
        # 됩니다. research_agent도 함께 매칭시켜 retrieve_docs로 문서 기준값을 근거로 답하게 합니다.
        "이동평균", "이평", "rsi", "드로다운", "과매도", "과매수",
    ],
    "ledger_agent": [
        # 매수해도/매도해도 같은 질문형("~해도 괜찮아?")까지 명령으로 오인하지 않도록,
        # 실행을 요청하는 명령형 종결(줘/라)만 매칭합니다.
        "기록", "매수해줘", "매수해라", "매도해줘", "매도해라", "청산해줘", "청산해라",
        "초기화", "리셋", "관망", "buy", "ledger", "장부",
    ],
}


def route_question(question: str) -> list[str]:
    """질문을 읽고 어느 전문 Agent로 보낼지 LLM 없이 결정적으로 고릅니다.

    여러 주제가 섞이면 해당 Agent를 모두, 어디에도 안 걸리면 빈 목록을 반환합니다.
    """
    q = (question or "").lower()
    matched = [name for name, kws in _AGENT_KEYWORDS.items() if any(kw.lower() in q for kw in kws)]
    return matched


# ══════════════════════════════════════════════════════════════════
# 워커 출력 품질 게이트 (Day6 심화)
# ══════════════════════════════════════════════════════════════════

_UNSUPPORTED_CLAIMS = ["확실합니다", "무조건", "반드시 오릅니다", "보장합니다", "틀림없습니다", "100% "]
_HAS_DIGIT = re.compile(r"\d")


def judge_output(agent_name: str, output: str) -> dict:
    """워커가 내놓은 답을 그대로 내보내지 않고 한 번 거릅니다. LLM을 호출하지 않습니다."""
    text = (output or "").strip()
    if len(text) < 20:
        return {"keep": False, "reason": "저가치"}

    has_unsupported_claim = any(c in text for c in _UNSUPPORTED_CLAIMS)
    has_evidence = bool(_HAS_DIGIT.search(text)) or "출처" in text
    if has_unsupported_claim and not has_evidence:
        return {"keep": False, "reason": "근거 없는 단정"}

    return {"keep": True, "reason": "정상"}


# ══════════════════════════════════════════════════════════════════
# 전문 Agent 그래프 (Day3 패턴 — agent↔tools 순환 + 승인 게이트로 조기 종료)
# ══════════════════════════════════════════════════════════════════

class WorkerState(TypedDict):
    messages: Annotated[list, add_messages]
    steps: int
    pending_approval: dict | None


def _build_worker_graph(agent_name: str, llm):
    tool_names = AGENT_TOOLS[agent_name]
    worker_tools = [_TOOL_REGISTRY[n] for n in tool_names]
    llm_with_tools = llm.bind_tools(worker_tools)
    system_prompt = _AGENT_SYSTEM_PROMPTS[agent_name]

    def agent_node(state: WorkerState) -> dict:
        messages = [SystemMessage(content=system_prompt)] + state["messages"]
        response = llm_with_tools.invoke(messages)
        return {"messages": [response], "steps": state["steps"] + 1}

    def route_after_agent(state: WorkerState) -> str:
        last = state["messages"][-1]
        tool_calls = getattr(last, "tool_calls", None) or []
        if not tool_calls:
            return "end"
        if state["steps"] >= MAX_STEPS:
            return "end"
        needs_gate = any(guardrails.needs_approval(tc["name"], tc.get("args", {}))[0] for tc in tool_calls)
        return "await_approval" if needs_gate else "tools"

    def tools_node(state: WorkerState) -> dict:
        last = state["messages"][-1]
        results = []
        for tc in last.tool_calls:
            fn = _TOOL_REGISTRY[tc["name"]]
            output = fn.invoke(tc.get("args", {}))
            results.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))
        return {"messages": results}

    def await_approval_node(state: WorkerState) -> dict:
        last = state["messages"][-1]
        # tool_calls[0]이 아니라, 실제로 승인이 필요한 호출을 찾습니다 — 같은 턴에 read 도구와
        # 승인 필요 도구가 섞여 나오면 [0]번이 read일 수 있고, 그러면 알림 메시지가 엉뚱한
        # 도구/사유를 가리키게 됩니다.
        tc = next(t for t in last.tool_calls if guardrails.needs_approval(t["name"], t.get("args", {}))[0])
        _needed, reason = guardrails.needs_approval(tc["name"], tc.get("args", {}))
        # SPEC §10-1: approval_id를 발급해 서버 메모리에 tool/args를 저장한다 — 클라이언트가
        # 나중에 보내는 tool/args는 신뢰하지 않고, 이 id로 저장해둔 값만 실행한다.
        approval_id = approvals.create(tc["name"], tc.get("args", {}), reason=reason)
        pending = {
            "approval_id": approval_id,
            "agent": agent_name,
            "tool": tc["name"],
            "args": tc.get("args", {}),
            "reason": reason,
        }
        notice = (
            f"[승인 필요] {reason} 실행 전 사용자 확인이 필요해 멈췄습니다. "
            f"(승인 ID: {approval_id}, 도구: {tc['name']})"
        )
        return {"messages": [AIMessage(content=notice)], "pending_approval": pending}

    graph = StateGraph(WorkerState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("await_approval", await_approval_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent", route_after_agent, {"tools": "tools", "await_approval": "await_approval", "end": END}
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("await_approval", END)
    return graph.compile()


def _extract_tool_trace(messages: list) -> list[dict]:
    """메시지 이력에서 (도구명, 인자) -> 결과 쌍을 뽑아 API trace 형식으로 만듭니다."""
    calls_by_id: dict[str, dict] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in getattr(m, "tool_calls", None) or []:
                calls_by_id[tc["id"]] = {"name": tc["name"], "args": tc.get("args", {})}

    entries = []
    for m in messages:
        if isinstance(m, ToolMessage):
            call = calls_by_id.get(m.tool_call_id, {"name": "unknown", "args": {}})
            entries.append({"step": f"tool:{call['name']}", "input": call["args"], "output": str(m.content)})
    return entries


# ══════════════════════════════════════════════════════════════════
# Supervisor
# ══════════════════════════════════════════════════════════════════

def build_supervisor(llm=None):
    """질문을 받아 관련 전문 Agent(들)을 호출하고 결과를 합쳐 dict로 반환하는 콜러블을 만듭니다.

    반환값은 §4-2 표준 규약을 따릅니다: {"answer", "contexts", "trace"} (+ 부가 필드
    agents_used/approvals_needed/data_gap_needs_confirmation).
    """
    llm = llm or _default_llm()
    worker_graphs = {name: _build_worker_graph(name, llm) for name in AGENT_NAMES}

    def run(question: str, proceed_with_stale_data: bool = False) -> dict:
        global _LAST_DATA_GAP
        _REQUEST_CONTEXT["proceed_with_stale_data"] = proceed_with_stale_data
        _LAST_DATA_GAP = None
        trace: list[dict] = []

        blocked, guard_reason = guardrails.input_guard(question)
        trace.append({"step": "guard", "input": question, "output": {"blocked": blocked, "reason": guard_reason}})
        if blocked:
            return {
                "answer": f"요청을 처리할 수 없습니다: {guard_reason}",
                "contexts": [],
                "trace": trace,
                "agents_used": [],
                "approvals_needed": [],
            }

        safe_question = guardrails.mask_pii(question)
        agents = route_question(safe_question)
        trace.append({"step": "route", "input": safe_question, "output": agents})
        if not agents:
            return {
                "answer": "이 어시스턴트는 BTC 예산·전략 계획·시세·지표·매수 기록에 대해서만 답할 수 있습니다.",
                "contexts": [],
                "trace": trace,
                "agents_used": [],
                "approvals_needed": [],
            }

        answers: list[str] = []
        approvals_needed: list[dict] = []
        _LAST_RETRIEVED_DOCS.clear()

        for name in agents:
            result = worker_graphs[name].invoke(
                {"messages": [HumanMessage(content=safe_question)], "steps": 0, "pending_approval": None}
            )
            trace.extend(_extract_tool_trace(result["messages"]))

            if result.get("pending_approval"):
                approvals_needed.append(result["pending_approval"])
                trace.append({"step": "approval_required", "input": result["pending_approval"], "output": "실행 보류"})
                continue

            final_text = result["messages"][-1].content
            verdict = judge_output(name, final_text)
            trace.append({"step": f"judge:{name}", "input": final_text, "output": verdict})
            if verdict["keep"]:
                answers.append(f"[{name}] {final_text}")

        contexts = [
            {"doc_id": d.metadata.get("source", "unknown"), "text": d.page_content} for d in _LAST_RETRIEVED_DOCS
        ]

        if not answers and not approvals_needed:
            answer = "쓸 만한 답을 만들지 못했습니다. 질문을 조금 더 구체적으로 해주세요."
        else:
            answer = "\n\n".join(answers) if answers else "승인이 필요한 작업이 있어 답변을 만들지 못했습니다."

        response = {
            "answer": answer,
            "contexts": contexts,
            "trace": trace,
            "agents_used": agents,
            "approvals_needed": approvals_needed,
        }
        if _LAST_DATA_GAP is not None:
            response["data_gap_needs_confirmation"] = _LAST_DATA_GAP
        return response

    return run


def execute_approved_action(approval_id: str) -> dict:
    """SPEC §10-1: approval_id로 서버가 저장해둔 tool/args를 그대로 실행합니다(클라이언트가
    보낸 값은 신뢰하지 않습니다). pending -> executing 원자적 전환 후 실행하고 executed로 종결합니다.

    반환: {"http_status": int, "result": str} 또는 {"http_status": int, "error": str}.
    """
    entry = approvals.get(approval_id)
    if entry is None:
        return {"http_status": 404, "error": "존재하지 않는 approval_id입니다."}
    ok, status = approvals.begin_execution(approval_id)
    if not ok:
        return {"http_status": 409, "error": f"이미 처리 중이거나 처리된 승인입니다(상태: {status})."}

    fn = _TOOL_REGISTRY.get(entry["tool"])
    result = f"알 수 없는 도구입니다: {entry['tool']}" if fn is None else str(fn.invoke(entry["args"]))
    approvals.finish_execution(approval_id, result)
    return {"http_status": 200, "approval_id": approval_id, "result": result}


def reject_approved_action(approval_id: str) -> dict:
    """SPEC §10-1 거절 API: pending -> rejected 전환. 도메인 도구는 실행하지 않습니다."""
    entry = approvals.get(approval_id)
    if entry is None:
        return {"http_status": 404, "error": "존재하지 않는 approval_id입니다."}
    ok, status = approvals.reject(approval_id)
    if not ok:
        return {"http_status": 409, "error": f"이미 처리 중이거나 처리된 승인입니다(상태: {status})."}
    return {"http_status": 200, "approval_id": approval_id, "status": "rejected"}
