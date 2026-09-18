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
import uuid
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

MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"  # 2026-09-17: 4개 후보(haiku-4-5/sonnet-4-6/nova-pro/nova-lite) 동일 질문 비교 후 확정 채택(모델 자체는 그대로). 근거는 evaluation/model_comparison_report.md, CLAUDE.md "모델 선정" 참고 — 임시 대체가 아니라 최종 선택. 2026-09-19: "us." 리전 프로파일이 ServiceUnavailableException으로 막혀 같은 모델의 "global." 리전 프로파일로 전환(모델 교체 아님 — 버전 문자열 claude-haiku-4-5-20251001-v1:0은 동일, 라우팅 프리픽스만 변경).
# 이 계정에서 실제로 쓸 수 있는 다른 모델 목록·바꾸는 이유는 CLAUDE.md "모델 교체" 절 참고.
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
# select_strategy의 제안(confirmation_token 발급)을 API 응답에 실어 보내기 위한 요청 스코프 상태.
# 실사용 중 발견: 이 API는 대화 기록이 없는 완전 무상태라, 사용자가 "응 확인했어"처럼 키워드 없는
# 말로 답하면 route_question이 아무 Agent에도 안 걸려 그 토큰을 다시 실어 보낼 방법이 없었다
# (approvals_needed/approval_id는 /approve라는 별도의, 대화·라우팅을 안 거치는 구조화 경로가 있어
# 이 문제가 없다 — 그래서 select_strategy 확인도 같은 모양의 전용 엔드포인트를 추가한다).
_LAST_STRATEGY_PROPOSAL: dict | None = None
# set_monthly_budget의 제안(confirmation_token 발급)을 API 응답에 실어 보내기 위한 요청 스코프
# 상태 — _LAST_STRATEGY_PROPOSAL과 완전히 같은 이유(SPEC §4-2, 2026-09-18 기획 변경)로 존재한다.
_LAST_BUDGET_PROPOSAL: dict | None = None
_LAST_RETRIEVED_DOCS: list = []

# ══════════════════════════════════════════════════════════════════
# 후속 입력 상태(awaiting_input) — 실사용 UI 신고(2026-09-20, #1): "뭐부터 시작하면 돼?" →
# (plan_agent가 얼마로 시작할지 되물음) → "200만원"이 범위 밖으로 거절됨. 원인은 이 API가
# 무상태라 "200만원"이라는 문장 자체에는 예산 관련 신호(반복 주기 단어도, 결정 어미도, "예산"
# 키워드도)가 전혀 없어 route_question이 아무 데도 못 건 것 — UI에는 직전 대화가 보이지만 서버는
# 그걸 모른다. "answer 문자열에 '얼마'가 있는지 찾아서 추정"하는 방식은 명시적으로 배제하고
# (요청사항), select_strategy/set_monthly_budget의 confirmation_token과 같은 구조 — 서버가 발급한
# 토큰을 클라이언트가 다음 요청에 그대로 실어 보내는 방식 — 을 재사용한다. 다만 이건 "제안을
# 확정 짓는" 토큰이 아니라 "다음 메시지가 방금 물어본 질문의 답이다"라는 신호일 뿐이라 별도
# 이름(awaiting_input)과 별도 저장소를 쓴다 — confirmation_token과 섞이면 "제안 확인"과 "짧은
# 답변 잇기"라는 서로 다른 개념이 뒤섞인다.
_PENDING_AWAITING_INPUT: dict[str, dict] = {}
_LAST_AWAITING_INPUT: dict | None = None


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


def _required_range_gap_message(records: list[dict]) -> str | None:
    """확정 레코드가 실제로 쓰이는 전체 구간(records[0]~records[-1])에 결측이 없는지 확인한다.

    실사용/평가 중 발견(2026-09-18): "레코드 개수 >= 200"만 확인하면, 그 200개(또는 그 이상)가
    실제로는 연속된 날짜가 아니라 중간에 구멍이 뚫린 채로도 개수 조건만 맞으면 통과해버린다. RSI는
    누적 RMA라 어느 지점에 결측이 있든 그 뒤의 모든 계산이 "인접하지 않은 두 날"을 인접한 것처럼
    다루게 되고, MA200도 최근 200개 슬라이스가 실제로 연속 200일인지는 보지 않는다. 이 검사는
    `proceed_with_stale_data`(§7-1, "최신 일봉이 아직 없다")와는 완전히 별개다 — 그 플래그는 최신
    쪽 지연만 눈감아주는 것이지, 과거 구간 중간의 결측까지 우회하게 해서는 안 된다(그래서 이 함수는
    _REQUEST_CONTEXT를 전혀 참조하지 않는다).
    """
    if not records:
        return None
    missing = price_history.find_missing_dates(records, records[0]["date_kst"], records[-1]["date_kst"])
    if not missing:
        return None
    sample = ", ".join(missing[:3]) + (" 등" if len(missing) > 3 else "")
    return (
        f"계산에 필요한 구간({records[0]['date_kst']}~{records[-1]['date_kst']})에 결측 일봉이 "
        f"{len(missing)}건 있어 계산할 수 없습니다({sample}). proceed_with_stale_data로도 우회되지 "
        "않습니다 — 데이터 정합성을 먼저 복구해야 합니다."
    )


def _rsi_desc(v: float | None) -> str:
    if v is None:
        return "계산 불가"
    if v > 70:
        return f"{v:.2f} (과매수 구간)"
    if v < 30:
        return f"{v:.2f} (과매도 구간)"
    return f"{v:.2f} (중립 구간)"


def _dd_mdd_desc(period_label: str, d: dict | None, *, required_days_desc: str) -> str:
    """§6-2(2026-09-20 정책 변경) DD·MDD를 하나의 문장으로 — 둘 다 확정 종가 기준이다.

    한 기간의 데이터 부족(또는 결측)이 다른 기간이나 RSI까지 함께 막지 않도록, 이 함수는 그
    기간 자체의 계산 가능 여부만 본다(agent._indicators_summary 참고) — "결측"이라는 단어는
    일부러 쓰지 않는다(그 단어는 RSI/MA200의 전체 구간 결측 검사 메시지 전용으로 남겨둔다 —
    구간이 아예 그만큼 확보되지 않은 경우와, 확보된 구간 중간에 실제 결측이 있는 경우를 이 값만
    보고는 구분할 수 없어서, 두 경우 모두 같은 문구로 정직하게 "계산 불가"라고만 말한다).
    """
    if d is None:
        return f"{period_label}: 계산 불가({required_days_desc} 구간이 확보되지 않음)"
    return (
        f"{period_label}: DD {d['dd_pct']:.2f}%(구간 {d['start_date']}~{d['end_date']} 중 최고 "
        f"종가 {d['dd_peak_close']:,.0f}원은 {d['dd_peak_date']}에 기록, 그 대비), "
        f"MDD {d['mdd_pct']:.2f}%(고점 {d['mdd_peak_date']} {d['mdd_peak_close']:,.0f}원 → 저점 "
        f"{d['mdd_trough_date']} {d['mdd_trough_close']:,.0f}원)"
    )


def _indicators_summary(records: list[dict]) -> str:
    """records(확정 일봉, 과거->최신)로부터 RSI·MA200 괴리율·기간별 DD·MDD 요약 문자열을 만든다.

    `get_indicators` 도구의 순수 로직 부분만 떼어낸 함수 — 네트워크(update_incremental) 없이
    고정 레코드로 직접 테스트하기 위해 분리했다(결측 구간 회귀 테스트가 이 함수를 직접 부른다).

    2026-09-20 정책 변경: RSI/MA200(누적 RMA라 records[0]~records[-1] 전체 구간에 결측이 없어야
    함, §7-1과 별개)과 기간별 DD·MDD(각자 자기 구간만 확보되면 됨)를 **서로 독립적으로** 판단한다
    — 한쪽이 데이터 부족이라고 해서 다른 쪽까지 일괄로 "계산 불가" 처리하지 않는다. 예: 확정
    일봉이 100일뿐이면 RSI(200일 필요)는 계산 불가지만 30일 DD·MDD는 그대로 계산된다.
    """
    if not records:
        return "지표 계산에 필요한 데이터가 아직 없습니다."

    lines = [f"기준일 {records[-1]['date_kst']}(확정 종가 {records[-1]['close']:,.0f}원)."]

    if len(records) < 200:
        lines.append(
            f"RSI(14)·200일 이동평균 괴리율: 계산 불가(최소 200일 확정 일봉 필요, 현재 "
            f"{len(records)}일)."
        )
    else:
        gap_msg = _required_range_gap_message(records)
        if gap_msg:
            lines.append(f"RSI(14)·200일 이동평균 괴리율: 계산 불가({gap_msg})")
        else:
            closes = [r["close"] for r in records]
            rsi = indicators.compute_rsi_series(closes)[-1]
            ma_dev = indicators.compute_ma_deviation_pct(closes, 200)
            ma_desc = (
                "계산 불가" if ma_dev is None
                else f"{ma_dev:.2f}%({'저평가 방향' if ma_dev < 0 else '고평가 방향'})"
            )
            lines.append(f"RSI(14, RMA) {_rsi_desc(rsi)}. 200일 이동평균 괴리율 {ma_desc}.")

    dd_30 = indicators.compute_dd_mdd(records, days=30)
    dd_365 = indicators.compute_dd_mdd(records, days=365)
    dd_48m = indicators.compute_dd_mdd(records, months=48)
    lines.append("기간별 DD(현재 하락률)·MDD(최대 낙폭) — 확정 종가 기준:")
    lines.append("  " + _dd_mdd_desc("최근 30일", dd_30, required_days_desc="최근 30일"))
    lines.append("  " + _dd_mdd_desc("최근 365일", dd_365, required_days_desc="최근 365일"))
    lines.append("  " + _dd_mdd_desc("최근 48개월", dd_48m, required_days_desc="최근 48개월"))
    lines.append(
        "(DD·MDD는 과거 가격을 요약한 참고 정보일 뿐 미래의 최대 하락폭이나 바닥을 예측하지 "
        "않습니다.)"
    )
    lines.append(_staleness_caveat())
    return "\n".join(line for line in lines if line)


@tool
def get_indicators() -> str:
    """RSI(14, RMA)·200일 이동평균 괴리율·기간별(최근 30일/365일/48개월) DD(현재 하락률)·
    MDD(최대 낙폭)의 값과 의미를 설명합니다. DD·MDD는 확정 종가(close) 기준으로 통일돼 있습니다
    (2026-09-20 정책 변경 — 이전의 최고 고가 기준은 폐기). 종합 매수 등급이나 새 매수 조건은
    만들지 않습니다 — 매수 조건 판정은 select_strategy로 고른 전략이 따로 봅니다."""
    price_history.update_incremental()
    gate_msg = _freshness_gate_message()
    if gate_msg:
        return gate_msg

    records = price_history.confirmed_records()
    return _indicators_summary(records)


@tool
def get_month_status(year: int = 0, month: int = 0) -> str:
    """이번 달(연·월을 지정하지 않으면 오늘 기준)의 예산·선택 전략·남은 예산·전략 변경 가능 여부를
    조회합니다. '조회 대상 월'과 '계획 시작월'은 서로 다른 개념입니다 — 조회 대상 월이 계획
    시작월보다 이르면 그 달은 계획 시작 전이지만, 계획 자체나 다른 달의 전략 선택 여부와는
    무관합니다. 응답에 항상 두 값을 구분해서 표시합니다."""
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    status = month_state.get_month_status(year, month, now=now)
    start_month = status.get("plan_start_month")
    header = f"[조회 대상 {year}-{month:02d}]"

    if not status["plan_started"]:
        if start_month:
            start_info = f"계획 시작월은 {start_month}입니다 — 그 달부터 예산 집행·전략 선택이 시작됩니다."
        else:
            start_info = "아직 어떤 계획도 시작하지 않았습니다(set_monthly_budget으로 시작할 수 있습니다)."
        return (
            f"{header} 이 달은 계획 시작 전이라 예산·전략 상태가 없습니다. {start_info} "
            "(이 달 이후 다른 달의 계획 시작 여부·전략 선택 여부와는 별개입니다.)"
        )

    lines = [f"{header} 계획 상태 — 월 예산 {status['monthly_budget_krw']:,.0f}원"]
    if start_month:
        lines.append(f"계획 시작월: {start_month}")
    if "spent_krw" in status:
        over = " (예산 초과 — 추가 매수는 권하지 않습니다)" if status["over_budget"] else ""
        lines.append(f"사용액 {status['spent_krw']:,.0f}원, 남은 예산 {status['remaining_krw']:,.0f}원{over}")
        # 실사용 신고(2026-09-20, #4): 월중 시작·매수 기록 0건 상태를 "남은 예산 전액을 전략
        # 조건(RSI 등) 대기금으로 본다"고 잘못 설명한 사례가 있었다 — 이 서비스는 첫 절반은
        # 조건 없이 정액 매수하고 나머지 절반만 전략 조건에 따른다는 사실이 자유 서술에만 맡겨져
        # 있으면 이렇게 흐려질 수 있다. get_month_status의 사실 텍스트 자체에 첫 매수 단계 안내를
        # 포함시켜, LLM이 이 부분을 매번 정확히 재구성하지 않아도 되게 한다(드로다운 high_date와
        # 같은 이유의 구조적 보강 — REPORT.md 참고). "기록이 없다"를 "매수 안 함"으로 단정하지
        # 않도록 항상 "현재 기록 기준"이라는 단서를 붙인다.
        if status["buy_count"] == 0:
            if month_state.month_end_cutoff_passed(year, month, now):
                lines.append(
                    "이번 달 매수 기록이 아직 없습니다(현재 기록 기준 사용액 0원 — 실제로 이미 "
                    "매수했다면 그 내역을 알려주시면 기록해드립니다). 월말 마감 규칙에 따라 절반씩 "
                    f"나누지 말고 남은 예산 {status['remaining_krw']:,.0f}원 전액을 매수하세요(실제 "
                    "주문은 직접 하고, 결과를 신고해야 사용액에 반영됩니다)."
                )
            else:
                half = status["monthly_budget_krw"] / 2
                lines.append(
                    "이번 달 매수 기록이 아직 없습니다(현재 기록 기준 사용액 0원 — 실제로 이미 "
                    "매수했다면 그 내역을 알려주시면 기록해드립니다). 이 서비스는 예산의 절반을 "
                    f"조건 없이 첫 매수 단계로 둡니다 — 첫 단계로 {half:,.0f}원 매수를 고려해보세요"
                    "(실제 주문은 직접 하고, 결과를 신고해야 사용액에 반영됩니다). 남은 예산 전체를 "
                    "전략 조건 대기금으로 보지 마세요 — 나머지 절반만 선택한 전략의 조건에 따라 "
                    "매수합니다."
                )
        else:
            lines.append(f"이번 달 매수 기록 {status['buy_count']}건이 이미 반영돼 있습니다.")
    lines.append(f"선택 전략: {status['strategy'] or '미선택'}")
    lines.append(
        "정기 분할 변경: " + ("가능" if status["biweekly_change_allowed"] else "불가(15일 09:00 KST 마감)")
    )
    lines.append(
        "전략 변경 전체: " + ("가능" if status["strategy_change_allowed"] else "불가(월말 00:00 KST 마감)")
    )
    return "\n".join(lines)


@tool
def request_monthly_budget_amount() -> str:
    """계획이 아직 시작되지 않았거나 사용자가 월 예산 금액을 아직 말하지 않았는데 얼마로
    시작/변경할지 물어야 할 때 반드시 이 도구를 먼저 호출하세요 — "얼마로 하시겠어요?"라고 직접
    말만 하지 말고 이 도구를 거쳐야 합니다.

    이유(SPEC §2-2 후속 입력 상태): 이 API는 대화 기록이 없는 무상태입니다. 이 도구를 호출하면
    서버가 "다음 사용자 메시지는 방금 물어본 예산 금액에 대한 답"이라는 상태를 토큰으로 기억해
    응답에 실어 보내고, 클라이언트가 다음 요청에 그 토큰을 그대로 실어 보내면 "200만원"처럼
    금액만 담긴 짧은 답장도 서버가 정확히 예산 제안으로 연결합니다 — 이 도구를 안 거치면 그
    구조가 전혀 동작하지 않아, 사용자가 짧게 답한 금액이 그냥 범위 밖 질문처럼 거절됩니다.

    이 도구 자체는 아무것도 결정하지 않습니다 — 반환 문구는 내부 신호일 뿐이니, 실제로 사용자에게
    보여줄 질문 문장(예: "월 예산으로 얼마를 쓰고 싶으신가요?")은 당신이 직접 자연스럽게 작성해서
    최종 답변에 포함하세요."""
    global _LAST_AWAITING_INPUT
    token = uuid.uuid4().hex
    _PENDING_AWAITING_INPUT[token] = {"kind": "monthly_budget_amount"}
    _LAST_AWAITING_INPUT = {"kind": "monthly_budget_amount", "awaiting_input_token": token}
    return (
        "내부 신호를 기록했습니다 — 이제 사용자에게 월 예산으로 얼마를 쓰고 싶은지 직접 자연스럽게 "
        "물어보세요. 이 도구의 반환 문구를 그대로 사용자에게 보여주지 마세요."
    )


def _propose_budget(amount_krw: float, requested_year: int = 0, requested_month: int = 0,
                     now: datetime | None = None) -> dict:
    """set_monthly_budget 도구와 _resolve_awaiting_input(후속 입력 상태, 2026-09-20 실사용 신고
    #1)이 공유하는 실제 제안 로직 — 두 경로 모두 같은 방식으로 _LAST_BUDGET_PROPOSAL을 채워야
    최종 답변 구조화 블록이 어느 경로로 왔든 똑같이 정확해진다."""
    global _LAST_BUDGET_PROPOSAL
    now = now or datetime.now(KST)
    requested_month_str = (
        f"{requested_year:04d}-{requested_month:02d}" if requested_year and requested_month else None
    )
    proposal = month_state.propose_budget_change(amount_krw, requested_month=requested_month_str, now=now)
    if not proposal["ok"]:
        return proposal
    _LAST_BUDGET_PROPOSAL = {
        "confirmation_token": proposal["confirmation_token"],
        "amount_krw": proposal["amount_krw"],
        "action": proposal["action"],
        "effective_month": proposal["effective_month"],
        "is_initial": proposal["is_initial"],
        "requested_month": proposal.get("requested_month"),
        "month_mismatch": proposal.get("month_mismatch", False),
        "previous_start_month": proposal.get("previous_start_month"),
        "previous_start_amount": proposal.get("previous_start_amount"),
    }
    return proposal


@tool
def set_monthly_budget(amount_krw: float, requested_year: int = 0, requested_month: int = 0) -> str:
    """월 투자 예산을 설정/변경하는 안을 제안합니다(SPEC §4-2, §2-0 — 이 호출만으로는 아직
    반영되지 않습니다). 실제 설정/변경 지시일 때만 호출하세요 — 예시·계산 목적의 질문에는 호출하지
    말고 말로 설명하세요. 서버가 현재 계획 상태를 보고 아래 세 동작 중 하나를 자동으로 고릅니다:

    (1) **최초 설정**(아직 계획이 없음): 2026-09-20 정책 변경 — 이번 달 또는 다음 달 중 선택해
    시작할 수 있습니다(월중 이용자도 이번 달부터 시작 가능, 과거 달로는 시작할 수 없습니다).
    requested_year/requested_month를 생략하면 **이번 달**을 기본으로 제안합니다.
    (2) **앞당기기**(계획은 있지만 아직 시작 전 — 다음 달 시작으로 이미 설정돼 있음): 사용자가
    "이번 달부터 시작하고 싶어"처럼 정확히 이번 달을 요청하면, 아직 시작하지 않은 계획의 시작월을
    이번 달로 앞당기는 제안을 만듭니다. 이미 설정된 다음 달 예산은 삭제되지 않고 그대로 남습니다 —
    이번 달치 예산만 새로 추가됩니다.
    (3) **변경**(계획이 이미 시작됨): 기존 §4 규칙 그대로 항상 다음 달부터 적용됩니다.

    사용자가 "9월부터 시작할게"/"9월 예산은 200만원으로 할게"처럼 특정 연·월을 명시했다면
    requested_year/requested_month에 반드시 그 값을 채우세요(오늘 날짜 기준으로 "9월"이 몇
    년도인지 직접 계산하세요). 서버가 받아들일 수 없는 달이면(과거 달 등) 이유·대안을 구조화된
    안내에 자동으로 포함시킵니다(당신이 그 이유를 따로 설명할 필요는 없습니다 — 예산 제안이 있으면
    당신의 자유 서술 자체가 최종 답변에서 빠지기 때문입니다). 특정 월을 언급하지 않았다면 둘 다
    0으로 두세요. 사용자가 동의하면 이 응답의 budget_change_needs_confirmation.confirmation_token을
    POST /confirm_budget_change 로 보내야 실제로 저장됩니다(POST /cancel_budget_change 로 취소
    가능)."""
    now = datetime.now(KST)
    proposal = _propose_budget(amount_krw, requested_year, requested_month, now=now)
    if not proposal["ok"]:
        return f"제안 실패: {proposal['error']}"
    verb = {"initial": "시작", "advance": "앞당겨 시작", "change": "변경"}.get(proposal["action"], "변경")
    return (
        f"월 예산을 {amount_krw:,.0f}원으로 {verb}하는 안입니다 — {proposal['effective_month']}부터 "
        "적용됩니다. 사용자가 동의하면 이 응답의 budget_change_needs_confirmation.confirmation_token을 "
        "POST /confirm_budget_change 로 보내 적용하세요(POST /cancel_budget_change 로 취소 가능)."
    )


@tool
def select_strategy(strategy: str, year: int = 0, month: int = 0, confirmation_token: str = "") -> str:
    """이번 달 남은 예산에 적용할 전략을 선택/변경합니다. strategy는 decline_day(하락일 매수)/
    biweekly(정기 분할 — 매월 1일과 15일, 두 번 매수. "2주 간격"이 아니라 이 두 날짜를 뜻하는
    내부 식별자일 뿐입니다)/rsi(RSI 매수) 중 하나입니다. 처음 호출하면 적용되지 않고 확인 절차만
    시작됩니다 — 실제 적용은 사용자가 POST /confirm_strategy_change로 확인해야 이뤄집니다(아직
    채팅 UI가 없으므로 '버튼을 누르라'는 식으로 안내하지 마세요 — 이 API 경로만 존재합니다). 이
    도구에 confirmation_token을 직접 다시 채워 호출하는 경로도 있지만(같은 대화 안에서 LLM이
    토큰을 계속 기억하고 있을 때만 동작), 이 API는 대화 기록이 없는 무상태라 일반적으로는 전용
    엔드포인트 쪽이 실제로 쓰이는 경로입니다."""
    global _LAST_STRATEGY_PROPOSAL
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
    _LAST_STRATEGY_PROPOSAL = {
        "confirmation_token": proposal["confirmation_token"],
        "year": year,
        "month": month,
        "strategy": strategy,
    }
    return (
        f"{year}-{month:02d} 전략을 '{strategy}'(으)로 변경하는 안입니다. 사용자가 동의하면 "
        f"이 응답의 strategy_change_needs_confirmation.confirmation_token을 "
        "POST /confirm_strategy_change 로 보내 적용하세요(POST /cancel_strategy_change 로 취소 가능)."
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
def evaluate_current_condition() -> str:
    """이번 달 지금까지 확정된 일봉만으로, 이번 달 선택한 전략의 매수 조건이 이미 충족된 적
    있는지 다시 계산합니다(§5 놓친 신호 재계산). "며칠 전에 조건이 충족됐었는데 지금도 매수
    가능한가?" 같은 질문에 씁니다. 과거에 충족된 적이 있다는 사실은 안내하되, 그러니 지금도
    매수해도 된다는 뜻은 아닙니다 — 실제 매수 여부와 기록은 항상 사용자가 직접 정합니다."""
    now = datetime.now(KST)
    year, month = now.year, now.month
    strategy = month_state.get_selected_strategy(year, month)
    if not strategy:
        return f"{year}-{month:02d}에 선택된 전략이 없어 조건을 판정할 수 없습니다."

    price_history.update_incremental()
    gate_msg = _freshness_gate_message()
    if gate_msg:
        return gate_msg

    records = price_history.confirmed_records()
    prefix = f"{year:04d}-{month:02d}"
    days_this_month = [r for r in records if r["date_kst"].startswith(prefix)]
    if not days_this_month:
        return f"{year}-{month:02d} 확정된 일봉이 아직 없어 판정할 수 없습니다."

    closes = [r["close"] for r in records]
    rsi_series = indicators.compute_rsi_series(closes)
    rsi_by_date = {records[i]["date_kst"]: rsi_series[i] for i in range(len(records))}
    first_buy_price = ledger.first_buy_price_for_month(year, month)

    result = month_state.evaluate_current_condition(
        year, month, strategy, days_this_month, rsi_by_date, first_buy_price
    )
    labels = {"decline_day": "하락일 매수", "biweekly": "정기 분할", "rsi": "RSI 매수"}
    label = labels.get(strategy, strategy)
    if not result["triggered"]:
        return f"{year}-{month:02d} '{label}' 조건 재계산 결과: {result.get('reason', '충족된 적이 없습니다.')}"
    return (
        f"{year}-{month:02d} '{label}' 조건이 {result['trigger_date']}에 충족된 것으로 계산됩니다"
        f"(그 다음 확정 일봉 시가 매수 기준 매수일: {result.get('buy_date') or '아직 확정되지 않음'}). "
        "과거 신호를 지금 다시 확인한 결과일 뿐이며, 지금 시점에 자동으로 매수 조건이 이어지는 건 "
        "아닙니다 — 실제 매수는 사용자가 직접 결정해 기록해야 합니다."
    )


@tool
def retrieve_docs(query: str) -> str:
    """BTC·DCA·지표·서비스 규칙 문서에서 질문과 관련된 내용을 검색합니다."""
    docs = retriever.search_docs(query)
    _LAST_RETRIEVED_DOCS.extend(docs)
    if not docs:
        return "관련 문서를 찾지 못했습니다."
    return retriever.format_docs(docs)


@tool
def search_ledger(year: int = 0, month: int = 0) -> str:
    """지금까지 남긴 실제 매수·관망 기록을 조회합니다. '이번 달'/'다음 달'처럼 특정 달을 콕 집어
    물으면 반드시 year/month를 채우세요 — 비워두면 전체 기간 기록이 나와, plan_agent가 같은 질문에
    특정 달 기준으로 답한 예산 상태와 범위가 달라 모순돼 보일 수 있습니다."""
    entries = ledger.search_ledger(year=year, month=month)
    if not entries:
        scope = f"{year}-{month:02d}" if year and month else "전체 기간"
        return f"{scope} 기록이 없습니다."
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
    executed_date는 실제 매수한 날짜(YYYY-MM-DD, KST)입니다. price_krw는 **BTC 1개당 체결
    단가**입니다(총 지불 금액이 아닙니다) — amount_krw가 총 매수 금액입니다. (승인 필요)"""
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
def request_buy_execution_detail(amount_krw: float, executed_date: str) -> str:
    """사용자가 실제 매수를 보고했는데 체결 가격(price_krw)이나 수량(quantity_btc)이 둘 다 빠져
    있어 record_virtual_buy를 바로 호출할 수 없을 때 반드시 이 도구를 먼저 호출하세요 — 현재
    시세를 체결 가격 대신 넣거나, 수량을 임의로 추정해서 record_virtual_buy를 호출하지 마세요.

    amount_krw는 사용자가 말한 금액, executed_date는 실제 매수한 날짜(YYYY-MM-DD, KST — "오늘"
    이면 오늘 날짜를 직접 계산하세요)입니다. 이 둘은 이미 알고 있으니 다시 묻지 말고 그대로
    넘기세요.

    이유(SPEC §2-3 후속 입력 상태, request_monthly_budget_amount와 같은 원리): 이 API는 무상태라
    "1억원에 샀어"처럼 가격/수량만 담긴 짧은 다음 메시지를 이 보고와 연결할 방법이 없습니다. 이
    도구를 호출하면 서버가 amount_krw/executed_date를 기억해뒀다가, 다음 메시지에서 가격이나
    수량을 찾으면 자동으로 매수 기록 승인 요청을 만듭니다 — 당신이 그 다음 턴에서 다시
    record_virtual_buy를 호출할 필요가 없습니다(그 시점엔 이 대화 내용 자체를 기억하지 못합니다).

    이 도구 자체는 아무것도 기록하지 않습니다 — 반환 문구는 내부 신호일 뿐이니, 실제로 사용자에게
    보여줄 질문 문장(예: "체결 가격이나 매수한 수량을 알려주시겠어요?")은 당신이 직접 자연스럽게
    작성해서 최종 답변에 포함하세요."""
    global _LAST_AWAITING_INPUT
    token = uuid.uuid4().hex
    _PENDING_AWAITING_INPUT[token] = {
        "kind": "buy_execution_detail",
        "amount_krw": amount_krw,
        "executed_date": executed_date,
    }
    _LAST_AWAITING_INPUT = {"kind": "buy_execution_detail", "awaiting_input_token": token}
    return (
        "내부 신호를 기록했습니다 — 이제 사용자에게 체결 가격 또는 매수 수량을 직접 자연스럽게 "
        "물어보세요(금액·날짜는 이미 알고 있으니 다시 묻지 마세요). 이 도구의 반환 문구를 그대로 "
        "사용자에게 보여주지 마세요."
    )


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
    # 결측 구간이 있으면(get_indicators와 같은 기준 — _required_range_gap_message) RSI/MA200은
    # 스냅샷에서 비운다 — 개수만 맞다고 잘못된 RSI를 관망 기록에 남기지 않는다. 관망 결정 자체
    # (note)는 그대로 기록된다(§2-2: 자금 이동 없는 결정이라 지표 계산 가능 여부와 무관하게
    # 승인 불필요). 2026-09-20 정책 변경(RSI/DD·MDD 확장): DD·MDD는 RSI와 서로 독립적으로
    # 판단한다(_indicators_summary와 같은 원칙 — 한 지표의 데이터 부족이 다른 지표까지 막지
    # 않는다) — RSI가 결측으로 비워져도 결측과 안 겹치는 기간의 DD·MDD는 그대로 남긴다.
    if len(records) >= 200 and _required_range_gap_message(records) is None:
        closes = [r["close"] for r in records]
        rsi_series = indicators.compute_rsi_series(closes)
        snapshot["rsi14"] = rsi_series[-1]
        snapshot["ma200_deviation_pct"] = indicators.compute_ma_deviation_pct(closes, 200)
        snapshot["as_of"] = records[-1]["date_kst"]
    if records:
        dd_mdd = {
            "30d": indicators.compute_dd_mdd(records, days=30),
            "365d": indicators.compute_dd_mdd(records, days=365),
            "48m": indicators.compute_dd_mdd(records, months=48),
        }
        if any(v is not None for v in dd_mdd.values()):
            # 계산 기준/버전을 스냅샷에 남긴다 — 이 키가 없는 과거 기록은 이번 정책 변경 이전
            # (또는 DD·MDD 자체가 전혀 계산되지 않은) 스냅샷이라는 뜻으로 자연히 구분된다.
            snapshot["dd_mdd_calc_basis"] = indicators.DD_MDD_CALC_BASIS
            snapshot["dd_mdd"] = dd_mdd
    result = ledger.record_watch_decision(note=note, indicators_snapshot=snapshot)
    return f"관망 기록 추가: {result['record']['record_id']}"


@tool
def reset_ledger() -> str:
    """실제 매수·관망 기록을 전부 삭제합니다. 되돌릴 수 없습니다. (승인 + 이중확인 필요)"""
    result = ledger.reset_ledger()
    return f"기록 {result['cleared_count']}건을 모두 삭제했습니다. 이 작업은 되돌릴 수 없습니다."


AGENT_TOOLS: dict[str, list[str]] = {
    "price_agent": ["get_btc_price", "get_indicators"],
    "plan_agent": [
        "get_month_status", "request_monthly_budget_amount", "set_monthly_budget", "select_strategy",
        "run_backtest", "evaluate_current_condition",
    ],
    "research_agent": ["retrieve_docs"],
    "ledger_agent": [
        "search_ledger",
        "record_virtual_buy",
        "request_buy_execution_detail",
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
    request_monthly_budget_amount,
    set_monthly_budget,
    select_strategy,
    run_backtest,
    evaluate_current_condition,
    retrieve_docs,
    search_ledger,
    record_virtual_buy,
    request_buy_execution_detail,
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
        "하지 마세요.\n\n"
        "중요 — 데이터가 최신인지 의심하거나('아직 안 들어온 것 같은데') 오래된 데이터라도 계산해 "
        "달라는 질문에는, 되묻지 말고 먼저 get_indicators를 호출하세요 — 데이터가 실제로 오래됐다면 "
        "도구 자체가 확인을 요청하는 안내를 돌려줍니다. 어떤 지표인지 되묻는 것보다 일단 시도해보는 "
        "편이 낫습니다.\n\n"
        "중요 — '지금 매수하기 좋은 시기야?'/'지금 사도 될까?'처럼 예/아니오 판정을 요구하는 질문 "
        "(2026-09-19 발견 — 이전엔 라우팅이 안 걸려 무조건 거절됐습니다): 이런 질문에도 '예/아니오'로 "
        "단정하지 마세요 — 이 서비스는 여러 지표를 합쳐 하나의 매수 등급을 내는 기능을 의도적으로 "
        "제공하지 않습니다(과거에 있었다가 폐기된 기능입니다). 대신 get_indicators를 호출해 RSI·"
        "200일 이동평균 괴리율·기간별 DD(현재 하락률)·MDD(최대 낙폭)의 실제 값과 개별 의미를 "
        "알려주고, '이 지표들을 종합해 지금이 적기라고 판정해드리지는 않습니다'라고 명확히 "
        "안내하세요. 사용자가 이미 전략을 선택해뒀다면, 그 전략의 조건 충족 여부는 plan_agent가 "
        "별도로 확인해줄 수 있다는 점만 짧게 덧붙이고, 그 판정 자체를 당신이 대신 내리지 "
        "마세요.\n\n"
        "중요 — DD·MDD(2026-09-20 정책 변경): 둘 다 확정 종가(close) 기준입니다 — '최고가'라고만 "
        "부르지 말고 '최고 종가'라고 정확히 표시하세요. DD는 그 기간 최고 종가 대비 지금 얼마나 "
        "빠져 있는지, MDD는 그 기간 중 어느 시점에서든 발생했던 가장 큰 고점 대비 하락폭입니다 — "
        "서로 다른 개념이니 섞어 말하지 마세요. 이 값들은 과거 가격을 요약한 참고 정보일 뿐 "
        "미래의 최대 하락폭이나 바닥을 예측하지 않습니다 — '과거 MDD까지 아직 하락 여유가 "
        "있다'는 식으로 설명하지 마세요. DD·MDD를 새로운 매수 조건이나 종합 점수로 연결하지 "
        "마세요.\n\n"
        "중요 — 개인 지출·예산 질문이 함께 섞여 있을 때(2026-09-20 재현): '이번 달 얼마 썼어? "
        "그리고 BTC 지금 가격도 알려줘'처럼 가격 질문에 개인 사용액·예산 질문이 함께 오면, 당신은 "
        "가격·지표 부분만 답하세요 — 사용액·예산 부분은 plan_agent가 같은 응답 안에서 이미 답합니다. "
        "'지출 내역은 다른 부서에 문의하세요'/'그건 제 담당이 아닙니다' 같은 거절 문장을 덧붙이지 "
        "마세요 — 답을 안 하는 게 아니라 이미 다른 Agent가 그 부분을 답하고 있는 것뿐입니다.\n\n"
        "중요 — 'BTC 분석해줘'/'BTC 현재 상황 알려줘'처럼 뭉뚱그린 분석 요청도(2026-09-20 재현) "
        "get_indicators로 답하세요 — '분석'이라는 단어가 종합 판정을 요구하는 것처럼 들려도, "
        "이 서비스의 답은 항상 'RSI·이동평균·DD·MDD 각각의 값과 의미'입니다. 이 요청에서도 "
        "종합 매수 등급이나 '사도 된다/기다려라' 같은 판단을 내리지 마세요 — 위 문단과 동일한 "
        "원칙입니다."
    ),
    "plan_agent": (
        "당신은 이번 달 예산·전략 계획 담당 Agent입니다. get_month_status로 현재 상태를 확인하고, "
        "run_backtest로 과거 48개월 비교를 보여주고, select_strategy로 전략을 선택/변경하고, "
        "set_monthly_budget으로 월 예산을 설정/변경하고, evaluate_current_condition으로 이번 달 "
        "놓친 매수 신호를 다시 확인합니다(예: '며칠 전에 조건 충족됐다는데 지금도 매수 가능해?'). "
        "이 재계산은 과거 신호가 있었다는 사실만 알려줄 뿐 '그러니 지금도 매수하라'는 뜻은 아니라는 "
        "점을 항상 함께 안내하세요. "
        "select_strategy와 set_monthly_budget은 둘 다 호출 즉시 반영되지 않고 제안만 만듭니다 — "
        "제안 내용을 사용자에게 보여주면, 실제 적용은 사용자가 각각 POST /confirm_strategy_change /  "
        "POST /confirm_budget_change 로 확인해야 이뤄집니다(도구 자체에 확인 토큰을 다시 채워 부르는 "
        "경로도 있지만, 이 API는 대화 기록이 없는 무상태라 보통은 전용 엔드포인트가 실제로 쓰입니다). "
        "동의 없이 바로 적용된 것처럼 말하지 마세요. 과거 성과가 가장 좋았던 전략을 대신 골라주지 "
        "마세요, 선택은 항상 사용자가 합니다.\n\n"
        "중요 — 전략 선택 요청 인식(2026-09-20 수정): 'RSI매수로 할게'/'RSI로 할래'/'하락일로 "
        "바꿔줘'/'정기 분할로 정할래'처럼 전략 이름과 결정 어미가 함께 있는 문장은 '전략'이라는 "
        "단어를 안 써도 명확한 전략 선택/변경 지시입니다 — 개념 설명 요청으로 취급해 뭉개지 말고 "
        "대상 연/월을 확인한 뒤 select_strategy로 제안을 만드세요(set_monthly_budget의 '명확한 "
        "지시면 되묻지 말고 바로 호출' 원칙과 동일합니다). 같은 응답에 price_agent/research_agent의 "
        "RSI 설명이 함께 나올 수 있는데, 그건 그 Agent들 몫이니 당신은 전략 제안에만 집중하고 "
        "불필요한 지표 설명을 반복하지 마세요. **전략 선택 가능 여부와 그 전략의 매수 조건 충족 "
        "여부는 서로 다른 질문입니다** — 예를 들어 RSI가 지금 30보다 높아도 RSI 방식을 '선택'하는 "
        "것 자체는 아무 문제 없습니다(그 조건은 앞으로 매수가 실행될 때 확인되는 것이지, 선택 "
        "시점에 이미 충족돼 있어야 하는 게 아닙니다) — 현재 조건 미충족을 이유로 선택을 거부하거나 "
        "되묻지 마세요. can_select_strategy가 이미 실제로 막아야 할 경우(계획 시작 전, 마감 시각 "
        "경과 등)를 검증하니, 그 외의 이유로 당신이 임의로 더 막지 마세요.\n\n"
        "중요 — '예산'이라는 단어 없는 사용액·잔여액 조회(2026-09-20 재현): '이번 달 얼마 "
        "썼어?'/'이번 달 얼마나 샀어?'/'얼마 남았어?'/'이번 달 매수에 쓴 금액 알려줘'처럼 '예산'을 "
        "안 써도 사용액·잔여 예산을 묻는 질문은 당신이 담당합니다 — get_month_status를 호출해 "
        "사용액·남은 예산·월 예산 총액을 함께 답하세요(어느 달 기준인지도 밝히세요 — 달을 특정하지 "
        "않았으면 이번 달 기준으로 답한다고 명시하세요). 이런 질문에 '개인 지출은 담당하지 "
        "않는다'는 식으로 거절하지 마세요 — 그건 이 서비스가 실제로 추적하는 정보입니다. 대상이 "
        "정말로 모호하면(예: 여러 달에 걸친 합계를 묻는 건지 이번 달만 묻는 건지 판단이 안 서면) "
        "짧게 되물으세요 — 하지만 일반적인 '얼마 남았어?' 같은 질문은 이번 달 기준으로 답하는 게 "
        "자연스러운 기본값이니 매번 되묻지 마세요.\n\n"
        "중요 — 월 구분: '계획 시작월'(월 예산을 처음 설정한 이후 실제로 집행이 시작되는 달 — "
        "2026-09-20 정책 변경으로 월중 가입도 이번 달 또는 다음 달 중 사용자가 확인한 달로 시작할 "
        "수 있습니다, 더는 무조건 다음 달이 아닙니다), '조회 대상 월'(사용자 질문이 가리키는 그 달), "
        "'그 달의 전략 선택 "
        "여부'는 서로 다른 개념입니다. '다음 달' 질문에는 다음 달 연/월로만 get_month_status/ "
        "select_strategy를 호출해 그 결과로만 답하세요 — 답을 만들며 이번 달 상태도 함께 확인했다면 "
        "반드시 어느 달 이야기인지 문장마다 명확히 밝히고, '이번 달은 계획이 시작되지 않았다'는 "
        "사실과 '다음 달 전략이 선택/제안됐다'는 사실을 뒤섞어 계획 전체가 시작 안 된 것처럼 들리게 "
        "하지 마세요.\n\n"
        "중요 — 전략 '조건/정의' 질문은 당신 몫이 아닙니다: 하락일/정기 분할/RSI 전략의 정확한 "
        "발동 조건이나 계산 방식을 묻는 질문(예: '하락일 조건이 뭐야?')에는 당신이 아는 대로 "
        "답하지 마세요 — 도구가 없어 근거 없이 답하면 실제 규칙과 다를 수 있습니다(실사용 중 발견:  "
        "'전일 대비 하락'이라고 잘못 답한 적 있음 — 실제 규칙은 '당일 시가 대비 -5% 이하 AND 월 "
        "첫 매수가 미만'). 그런 질문은 '정확한 조건은 문서 검색 결과를 참고하세요'라고만 안내하고, "
        "당신은 상태 조회·백테스트·전략 선택 결과만 책임지세요.\n\n"
        "중요 — set_monthly_budget은 호출한다고 바로 반영되지 않고 제안만 만듭니다(승인 없이 즉시 "
        "반영되던 이전 방식에서 2026-09-18 변경) — 이 도구 자체가 이미 확인을 기다리는 '제안' "
        "단계이므로, **금액과 결정 의사가 명확한 지시라면 되묻지 말고 바로 이 도구를 호출하세요.** "
        "이 도구는 원래 '이번 달 예산'을 다루는 것이라 '매달 200만원씩 투자할래'처럼 매번 반복 "
        "주기를 언급할 필요는 없습니다 — 백테스트 결과를 본 뒤 '나는 일단 200만원으로 진행해볼래'/ "
        "'200만원으로 시작할래'/'200만원으로 정할래'처럼 금액 + 결정 표현만으로도 충분히 명확한 "
        "지시입니다. 도구가 만든 제안에 대한 실제 동의 여부는 사용자가 별도로 "
        "/confirm_budget_change로 표시하니, 당신이 도구 호출 전에 자연어로 '진행할까요?'라고 먼저 "
        "되물을 필요가 없습니다 — 그렇게 하면 제안 자체가 생성되지 않아 사용자가 확인할 대상(토큰)이 "
        "없어져 버립니다. 다음 경우에만 호출하지 말고 말로 답하세요(단, 설명을 생략하고 '설정할까요?' "
        "라고만 되묻지 마세요 — 실제 내용을 먼저 설명하세요): "
        "(1) '~하면 얼마나 살 수 있어?'처럼 금액을 예시·계산에만 쓰는 질문 — 이런 계산은 "
        "price_agent 몫이니 당신에게 왔다면 라우팅이 잘못됐을 수 있습니다, "
        "(2) '~로 하는 예시를 설명해줘'처럼 실제 적용이 아니라 설명을 요청하는 질문 — 월 200만원 "
        "이면 전략별로 어떻게 나눠 매수되는지 등을 실제로 설명하세요, "
        "(3) 이 서비스는 BTC 전용 예산만 관리합니다 — 사용자가 BTC가 아닌 다른 자산(이더리움 등)에 "
        "대한 금액을 말하면, 그 금액으로 BTC 예산 제안을 만들지 말고 '이 서비스는 BTC 예산만 "
        "관리합니다. BTC로 설정하시겠어요?'라고 되물으세요.\n\n"
        "중요 — 예산 제안 응답에서는 금액·적용월·저장 여부·확인/취소 방법을 당신이 직접 서술하지 "
        "마세요(2026-09-19 수정: 이전엔 '반드시 포함하라'였는데, 그러면 시스템이 덧붙이는 안내와 "
        "내용이 겹치거나 서로 다른 표현이 되어 모순처럼 보일 위험이 있었습니다) — set_monthly_budget "
        "제안이 있으면 시스템이 금액·적용월·'아직 저장되지 않았다'는 사실·정확한 API 경로(POST "
        "/confirm_budget_change, POST /cancel_budget_change)를 항상 정확한 문구로 **한 번만** "
        "자동으로 덧붙입니다. **당신의 답변에는 'POST /confirm_budget_change'나 "
        "'POST /cancel_budget_change'라는 문자열 자체를 쓰지 마세요** — '확인하려면 ~로 보내세요' "
        "식의 안내 문장도 쓰지 마세요, 그 안내 자체가 시스템이 이미 붙일 몫입니다. 당신의 문장은 "
        "'예산 설정 제안을 만들었습니다' 정도로 끝내고, 구체적인 숫자·날짜·API 경로·확인 방법 안내는 "
        "전부 생략하세요(생략해도 사용자가 못 보는 게 아니라, 뒤에 시스템이 자동으로 붙입니다). "
        "다음도 함께 지키세요: "
        "(a) 전략을 아직 안 골랐다는 이유로 예산 확인을 미루라고 하거나 전략 선택을 먼저 요구하지 "
        "마세요 — 예산 확인과 전략 선택은 독립된 절차입니다. "
        "(b) 아직 채팅 UI가 없습니다 — 어떤 경우에도 '확인 버튼을 누르세요' 같은 존재하지 않는 UI를 "
        "안내하지 마세요. "
        "(c) 예산 제안 직후 응답에서는 '다음 단계'를 예산 확인/취소 하나로만 못박으세요 — 전략 "
        "선택이나 백테스트를 '선택 사항으로라도' 다음 단계처럼 함께 물어보지 마세요(예: '전략도 "
        "골라보시겠어요?' 같은 문장 금지). 예산 확인 전에는 둘 다 아직 언급할 단계가 아닙니다 — "
        "사용자가 먼저 예산을 확인/취소한 뒤, 별도로 물어보면 그때 전략·백테스트를 안내하세요. "
        "같은 응답에 research_agent의 설명이 함께 나올 수 있는데, 당신도 research_agent도 서로 "
        "다른 다음 단계를 재촉하며 끝맺지 마세요 — 다음 행동은 '예산 확인 또는 취소'(위 API로) "
        "하나뿐이어야 합니다.\n\n"
        "중요 — '뭐부터 해야 해?'/'어떻게 시작해?'/'처음인데 도와줘'처럼 서비스를 어떻게 시작할지 "
        "묻는 질문(2026-09-19 발견 — 라우팅이 안 걸려 도메인 밖으로 거절되던 결함을 고쳤습니다): "
        "먼저 get_month_status를 호출해 **실제 상태를 확인**하고 그 결과로만 답하세요, 짐작하지 "
        "마세요. "
        "(a) 계획이 아직 시작되지 않았다면(get_month_status가 그렇게 알려줍니다), '월 예산부터 "
        "정하면 시작됩니다'라고 안내하세요 — 하지만 **사용자가 구체적인 금액을 말하지 않았다면 "
        "set_monthly_budget을 임의의 금액으로 추측해서 호출하지 마세요.** 얼마로 시작하고 싶은지 "
        "물어볼 때는 **반드시 먼저 request_monthly_budget_amount를 호출한 뒤** 그 결과를 참고해 "
        "직접 자연스러운 질문 문장을 쓰세요(2026-09-20 수정 — 이 도구 호출 없이 그냥 말로만 물으면, "
        "이 API가 무상태라 사용자가 짧게 '200만원'이라고만 답했을 때 서버가 그 답을 방금 물어본 "
        "질문과 연결하지 못해 범위 밖 질문처럼 거절됩니다). 사용자가 실제 금액을 말하면(직접 "
        "메시지로 오든, 위 도구 호출 이후 서버가 자동으로 연결해주든) 그때 set_monthly_budget으로 "
        "제안을 만드세요. "
        "(b) 계획은 시작됐지만 전략이 미선택이라면, 전략을 고르라고 안내하되 마찬가지로 "
        "select_strategy를 임의의 전략으로 대신 호출하지 마세요 — 선택은 항상 사용자가 합니다. "
        "(c) 이미 예산·전략이 모두 설정돼 있다면, 그 상태를 그대로 안내하고 특별히 뭘 더 하라고 "
        "재촉하지 마세요. "
        "(d) 이 API는 대화 기록이 없는 무상태입니다 — '그다음은?'처럼 이전 대화를 전제하는 질문이 "
        "와도 실제로 나눈 적 없는 대화 내용을 기억하는 척 추측해 답하지 마세요. get_month_status가 "
        "돌려준 실제 상태만 근거로 삼고, 무엇을 묻는 건지 애매하면 무엇을 원하는지 한두 마디로 짧게 "
        "되물으세요.\n\n"
        "중요 — 시작월 선택(2026-09-20 정책 변경, 월중에도 이번 달부터 DCA 시작 지원): 최초 설정은 "
        "이번 달 또는 다음 달 중 선택할 수 있습니다. 사용자가 '9월부터 시작할게'/'이번 달 예산은 "
        "200만원'처럼 특정 월을 명시하면 그 의도를 set_monthly_budget의 requested_year/ "
        "requested_month에 반영하세요(오늘 날짜 기준으로 직접 계산). 특정 월을 말하지 않았다면 "
        "요청사항에 따라 **이번 달을 기본으로 제안**합니다(도구가 자동으로 처리, 당신이 직접 계산할 "
        "필요 없음). 과거 달로 시작하거나 사용자 확인 없이 시작월을 임의로 바꾸지 마세요 — 서버가 "
        "받아들일 수 없는 달이면 이유·대안을 구조화된 안내에 자동으로 포함시킵니다.\n\n"
        "중요 — 이미 다음 달 시작으로 설정돼 있는데 '이번 달부터 시작하고 싶어'라고 하면(아직 "
        "시작 전인 계획을 앞당기는 경우): set_monthly_budget을 금액과 함께 이번 달 "
        "requested_year/requested_month로 다시 호출하세요 — 서버가 자동으로 '앞당기기' 제안으로 "
        "처리하고, 기존에 설정된 다음 달 예산은 삭제하지 않고 그대로 유지합니다(이번 달분 예산만 "
        "새로 추가됩니다). 사용자가 이번 달 금액을 말하지 않았다면 임의로 다음 달과 같은 금액을 "
        "쓰지 말고 물어보세요(원하면 다음 달에 이미 설정된 금액을 참고용으로 언급해도 됩니다). 이건 "
        "**아직 시작하지 않은** 계획의 시작월을 앞당기는 기능입니다 — 계획이 이미 시작된 뒤의 일반 "
        "예산 변경은 여전히 항상 다음 달부터 적용됩니다(정책 그대로 유지).\n\n"
        "중요 — 월중 시작 시 첫 매수 처리: 이번 달 계획이 막 확인됐다고 해서 이미 1일에 매수한 "
        "것으로 가정하지 마세요. 반드시 get_month_status로 이번 달 실제 상태를 먼저 확인하세요. "
        "(1) 이번 달에 이미 활성 매수 기록이 있다면(get_month_status의 spent_krw/buy_count로 "
        "확인), 이미 반영된 사용액·남은 예산(remaining_krw)을 그대로 안내하고, '첫 절반을 사라'는 "
        "안내를 다시 반복하지 마세요 — 남은 예산 안에서 다음 매수를 안내하면 됩니다. 사용자가 "
        "'예전에 이미 샀어'라고 말했는데 실제 기록이 없다면, 그 매수의 금액·수량·가격·날짜를 확인해 "
        "record_virtual_buy로 기록하도록 안내하세요(승인 절차를 그대로 거칩니다) — 절대로 가짜 "
        "매수 기록이나 날짜·가격을 당신이 지어내 답하지 마세요. "
        "(2) 이번 달에 매수 기록이 없다면, 월 예산의 절반을 '첫 매수 단계'로 안내하세요(오늘이 "
        "1일이 아니어도 이 서비스에서는 그게 첫 단계입니다) — 하지만 실제 주문은 사용자가 직접 "
        "하고, 그 결과를 신고해 승인·기록해야 사용액에 반영된다는 점을 분명히 하세요. 예산 확인 "
        "자체만으로 매수가 완료됐거나 잔여 예산이 줄어든 것처럼 말하지 마세요. 실제 매수액이 "
        "절반과 다르더라도 실제 기록을 그대로 인정하고, 추가 매수를 권할 때는 남은 예산 "
        "(remaining_krw)을 넘지 않게 안내하세요.\n\n"
        "중요 — 월중 전략 선택: 15일 09:00(KST) 이전이면 하락일/정기 분할/RSI 모두, 그 이후면 "
        "하락일·RSI만 선택할 수 있습니다(select_strategy가 이미 이 경계를 강제합니다 — 당신은 "
        "임의로 더 엄격하게 굴거나 완화하지 마세요). 하락일 조건은 '이번 달 첫 매수가'가 있어야 "
        "판정할 수 있습니다 — 첫 매수 기록이 아직 없으면 evaluate_current_condition이 '판정할 수 "
        "없다'고 알려주는데, 이걸 임의로 '조건 미충족'이라고 단정해 답하지 마세요. 과거 어느 날짜에 "
        "가상으로 매수한 것으로 쳐서 조건을 계산해주지 마세요 — 실제 매수 기록이 필요하다고 "
        "안내하세요. 다음 달부터는 이번 정책 변경과 무관하게 기존처럼 1일 절반 매수 + 선택 전략 "
        "흐름이 그대로 적용되고, 전략은 다음 달로 자동 승계되지 않습니다(매달 새로 선택).\n\n"
        "중요 — 백테스트와 실제 이번 달 계획은 다른 것입니다: run_backtest는 항상 매월 1일부터 "
        "시작하는 과거 48개월 가상 비교이며, 월중에 시작한 사용자의 실제 이번 달 진행 상황을 그 "
        "안에 끼워 넣거나, 백테스트 결과를 사용자의 실제 매수 이력을 재현한 것처럼 설명하지 "
        "마세요 — 둘은 완전히 분리된 계산입니다.\n\n"
        "중요 — BTC 가격·지표 질문이 함께 섞여 있을 때(2026-09-20 재현): '이번 달 얼마 썼어? "
        "그리고 BTC 지금 가격도 알려줘'처럼 사용액·예산 질문에 가격 질문이 함께 오면, 당신은 "
        "예산·사용액·전략 부분만 답하세요 — 가격·지표 부분은 price_agent가 같은 응답 안에서 이미 "
        "답합니다. 'BTC 가격은 제 담당이 아닙니다, price_agent에게 물어보세요' 같은 문장을 덧붙이지 "
        "마세요 — 이미 같은 응답에 그 답이 포함돼 있으니 되돌려 안내할 필요가 없습니다."
    ),
    "research_agent": (
        "당신은 BTC·DCA·지표·서비스 규칙 문서 검색 담당 Agent입니다. retrieve_docs 도구로 찾은 내용만 "
        "근거로 답하세요. 문서에 없는 내용은 답하지 마세요 — 검색 결과가 질문에 정확히 답하지 못하면, "
        "지어내 채우지 말고 '문서에서 그 부분은 확인하지 못했습니다'라고 인정하세요.\n\n"
        "중요 — 검색어 구성: 검색은 의미 유사도 기반이라 질문을 대충 요약한 검색어로는 이 서비스 "
        "고유 규칙 문서(`dca_strategy.md`/`service_rules.md`)보다 일반 개념 문서(`DCA.md`)만 걸릴 "
        "수 있습니다(실사용 중 발견: '서비스 개요 소개 BTC DCA 투자'로 검색하면 `DCA.md`만 4개 나오고 "
        "`dca_strategy.md`/`service_rules.md`는 전혀 안 나옴). '이 서비스가 뭘 해주는지'/'처음 이용'/"
        "'세 가지 매수 방식'을 설명해야 하는 질문에는 '최초 이용 세 가지 매수 방식 하락일 정기 분할 "
        "RSI 매수 조건'처럼 이 서비스 고유 용어를 구체적으로 넣어 검색하세요 — 'DCA'라는 단어 하나에만 "
        "기대지 마세요.\n\n"
        "중요 — 절대 틀리면 안 되는 사실 세 가지(문서 내용과 무관하게 항상 참): "
        "(1) 세 전략 모두 매달 1일에 예산 절반을 조건 없이 정액 매수하고, 나머지 절반만 전략별로 "
        "다르게 처리합니다 — '정기 분할'만 특별한 게 아닙니다. "
        "(2) '정기 분할'은 매월 15일에 나머지 절반을 매수합니다 — '2주마다'/'격주'가 아니라 한 달에 "
        "1일·15일 두 번입니다. 내부 식별자 'biweekly'의 영어 뜻(2주 간격)에 이끌려 '2주마다'라고 "
        "쓰지 마세요, 실제 규칙은 이 문서의 '1일·15일'입니다. "
        "(3) 이 서비스는 실제 주문을 자동으로 넣지 않습니다 — 매수는 항상 사용자가 직접 하고 그 "
        "결과를 신고합니다. '자동 매수'/'자동으로 진행됩니다' 같은 표현을 쓰지 마세요.\n\n"
        "중요 — 역할 경계: 이 문서들은 서비스의 일반 개념·규칙만 설명하며, 특정 사용자의 실제 상태"
        "(현재 예산, 실제로 선택된 전략, 계획 시작 여부, 매수·관망 기록 등)는 담고 있지 않습니다. "
        "질문에 '내 전략', '이번 달', '다음 달' 처럼 개인 상태를 묻는 부분이 섞여 있어도, 문서 내용을 "
        "근거로 '선택됐다/안 됐다', '시작됐다/안 됐다' 같은 사용자의 실제 현재 상태를 추측하거나 "
        "단정하지 마세요 — 그 부분은 plan_agent/ledger_agent가 실시간 조회로 따로 답합니다. 일반"
        "개념·서비스 규칙 설명에만 집중하고, 실제 상태 안내가 필요하면 '실제 현재 상태는 계획 상태 "
        "조회 결과를 참고하세요' 정도로 한 문장만 짧게 덧붙이세요 — 이미 다른 Agent가 실시간 상태를 "
        "답했을 수 있으니 어떻게 확인하라고 길게 설명하지 마세요. 실행 관련 다음 단계(전략 선택 여부 "
        "등)를 먼저 묻거나 재촉하지 마세요 — 그건 plan_agent 몫입니다, 당신은 개념 설명으로 "
        "마무리하세요."
    ),
    "ledger_agent": (
        "당신은 실제 매수·관망 기록 담당 Agent입니다. 실제 거래소 주문은 발생하지 않는다는 점을 항상 "
        "분명히 하세요. 매수 기록 생성·수정·취소·초기화는 도구가 승인 절차를 거칩니다. 매수 기록 "
        "수정·취소는 장부 정정일 뿐 실제 거래 취소가 아니라는 점도 함께 안내하세요.\n\n"
        "중요 — '이번 달'/'다음 달'처럼 특정 달을 콕 집어 기록을 물으면 search_ledger에 반드시 "
        "그 연/월을 채워 호출하세요. 비워서 부르면 전체 기간 기록이 나오는데, plan_agent가 같은 "
        "질문에 특정 달 기준 예산·매수 여부로 답하면 두 답이 서로 다른 범위를 보고 있는데도 "
        "모순처럼 들릴 수 있습니다.\n\n"
        "중요 — 실제 매수 '보고' 인식(2026-09-20 수정): '오늘 50만원어치 BTC 매수했어'/'오늘 BTC "
        "50만원 샀어'/'비트코인 50만 원어치 구매했어'/'오늘 정기 매수로 100만원 넣었어'/'100만원 "
        "투자했어'처럼 이미 끝난 매수를 과거형으로 알리는 문장은 명령형('매수해줘')이 아니어도 실제 "
        "매수 보고입니다 — record_virtual_buy로 기록하도록 "
        "안내하세요. 반대로 다음은 실제 매수가 아니므로 기록을 만들거나 후속 정보를 요구하지 "
        "마세요: (1) '50만원 사면 얼마나 돼?'류 계산 질문(price_agent 몫), (2) '50만원 살까?'류 "
        "판단을 돕는 질문(아직 결정 전 — 매수 확정이 아님), (3) '아직 안 샀어'/'안 살래'처럼 매수를 "
        "부정하거나 보류하는 문장 — 이런 문장이 라우팅상 당신에게 와도, 실제 매수 사실이 없으므로 "
        "기록을 만들지 말고(관망 의도면 record_watch_decision을 대신 쓰세요) 그냥 그 사실을 "
        "인정하는 정도로 답하세요. (4) '이번 달 얼마나 샀어?'/'이번 달 매수에 쓴 금액 알려줘'처럼 "
        "이미 기록된 내역의 **총액·건수를 되묻는 조회 질문**(2026-09-20 재현) — 새 매수를 보고하는 "
        "문장이 아니므로 새 기록을 만들거나 체결 가격·수량을 되묻지 마세요. search_ledger로 해당 "
        "달의 기록을 조회해 합계·건수로 답하세요(연/월을 채워 호출 — 위 문단 참고).\n\n"
        "중요 — 체결 가격·수량이 빠졌을 때: 실제 매수 보고에 price_krw나 quantity_btc 중 어느 것도 "
        "없으면(예: '오늘 50만원어치 BTC 매수했어'는 금액·날짜만 있고 가격·수량이 없습니다) "
        "**반드시 먼저 request_buy_execution_detail(amount_krw, executed_date)를 호출한 뒤** 그 "
        "결과를 참고해 직접 자연스러운 질문 문장을 쓰세요(예: '체결 가격이나 매수한 수량을 알려주실 "
        "수 있나요?'). 이 도구 호출 없이 그냥 말로만 물으면, 이 API가 무상태라 사용자가 '1억원에 "
        "샀어'처럼 가격만 답했을 때 서버가 그 답을 방금 물어본 보고와 연결하지 못합니다. **현재 "
        "시세(get_btc_price 등)를 체결 가격으로 대신 넣지 마세요** — 사용자가 실제로 산 가격은 "
        "다를 수 있습니다. 수량도 임의로 추정해서 채우지 마세요. amount_krw·executed_date는 이미 "
        "알고 있으니(오늘이면 오늘 날짜를 직접 계산) 다시 묻지 마세요 — 이 값들은 "
        "request_buy_execution_detail에 그대로 넘기면 서버가 기억해 다음 턴에 자동으로 이어줍니다. "
        "가격·수량이 이미 메시지에 다 있다면 이 도구를 거치지 말고 record_virtual_buy를 바로 "
        "호출하세요.\n\n"
        "중요 — 체결 '가격' 표현의 단위(제출 평가 최종 검증 중 발견, 2026-09-20): '100만원어치를 "
        "1억원에 매수했어'처럼 'OOO원에 샀어/매수했어'라고만 말하면, 그 금액은 **BTC 1개당 "
        "단가**(record_virtual_buy의 price_krw)를 뜻하는 자연스러운 한국어 표현입니다 — '총 "
        "지불한 금액'이 아닙니다(총액은 이미 amount_krw로 따로 말한 금액입니다). 이 경우 단가인지 "
        "총액인지 되묻지 말고 그대로 price_krw로 record_virtual_buy를 호출하세요. 사용자가 "
        "명시적으로 '총 지불 금액은 ~'이라고 구분해서 말했을 때만 그게 다른 값임을 알아채고 "
        "필요하면 그때 되물으세요.\n\n"
        "중요 — 정확한 시각을 지어내지 마세요: executed_date는 실제 매수한 날짜(YYYY-MM-DD)만 "
        "받습니다 — 사용자가 정확한 시각(예: '오후 3시에')을 말하지 않았다면 임의의 시각을 만들어 "
        "채우지 마세요. '오늘'/'어제' 같은 상대 표현은 지금 시스템 프롬프트에 주어진 오늘 날짜(KST) "
        "기준으로 직접 계산하세요.\n\n"
        "중요 — 같은 날짜·금액이라는 이유만으로 이미 있는 기록과 무조건 같은 매수로 보고 다시 "
        "기록하지 않으려 하지 마세요 — 사용자가 실제로 여러 번 나눠 샀을 수 있습니다. 중복 여부를 "
        "판단하거나 걸러내는 건 당신 몫이 아닙니다, 사용자가 보고한 그대로 새 기록을 제안하세요."
    ),
}


# ══════════════════════════════════════════════════════════════════
# 라우팅 (Day6 패턴 — LLM 없이 결정적 규칙)
# ══════════════════════════════════════════════════════════════════

_AGENT_KEYWORDS: dict[str, list[str]] = {
    "price_agent": [
        "가격", "시세", "현재가", "지표", "이동평균", "이평", "rsi", "드로다운", "mdd", "최대낙폭",
        "최대 낙폭",
        # "얼마"는 여기 없다 — route_question 아래쪽의 조건부 처리를 거친다(2026-09-20 실사용
        # 신고 #2 참고: "10월 예산은 얼마야?"에서 "얼마"가 무조건 price_agent까지 끌어들여
        # plan_agent의 정상 답변 옆에 관련 없는 거절 문장이 함께 뜨는 문제가 있었다).
        "급등", "급락", "price", "indicator",
        # 실사용 중 발견(2026-09-17, evaluation #15): "데이터가 아직 안 들어온 것 같은데 그래도
        # 계산해줘"처럼 지표 계산/신선도를 묻는 질문인데 위 키워드를 하나도 안 써서 route_question이
        # 아무 Agent도 못 걸려 도메인 밖 취급하던 것을 발견했다.
        "계산",
        # 실사용 중 발견(2026-09-19): "현재 btc를 매수하기에 좋은시기인지 알려줘"가 위 키워드를
        # 하나도 안 써서 route_question이 아무 Agent도 못 찾아 도메인 밖으로 거절됐다. 이 서비스는
        # 종합 매수 판정을 의도적으로 제공하지 않지만(SPEC §3-1, assess_dca_signal 폐기 확정), 그건
        # "예/아니오로 답하지 않는다"는 뜻이지 "질문 자체를 거절한다"는 뜻이 아니다 — price_agent가
        # 지표 값+의미로 답하고 판정만 거절해야 한다(프롬프트 참고).
        "매수하기", "매수 타이밍", "살 때", "사기 좋은",
        # 실사용 중 추가 발견(2026-09-19): "오늘 btc 사기에 어때?"는 위 매수 시기 표현 중 어느
        # 것과도 문자열이 안 맞아(예: "사기 좋은"과 다름) 여전히 route=[]로 거절됐다 — 같은 매수
        # 시기 질문의 표현 변형이다. "사기에 어때"/"살까"/"사도 될까"/"사도 괜찮을까"를 추가한다.
        "사기에 어때", "살까", "사도 될까", "사도 괜찮을까",
        # 실사용 UI 신고(2026-09-20, #5): "BTC 분석해줘"/"BTC 현재 상황 알려줘"가 위 키워드를
        # 하나도 안 써서(같은 뜻인 "BTC 지표 알려줘"는 "지표" 덕에 이미 정상 동작) route=[]로
        # 범위 밖 거절됐다 — "분석"/"현재 상황"도 결국 get_indicators가 답하는 것과 같은 요청이다.
        "분석", "현재 상황", "상황 알려줘", "상황이 어때",
    ],
    "plan_agent": [
        "예산", "남은 예산", "이번 달", "이번달", "전략", "백테스트", "시뮬레이션", "비교",
        "선택", "변경", "budget", "backtest", "strategy", "정기 분할", "정기분할", "하락일",
        # evaluate_current_condition(§5 놓친 신호 재계산) 연결(2026-09-18) — "며칠 전에 조건이
        # 충족됐는데 지금도 매수 가능해?" 같은 질문을 라우팅에 걸리게 한다.
        "조건", "충족", "신호", "놓친",
        # 2026-09-18: 예산 액수 표현("매달 200만원씩...")은 리터럴 키워드가 아니라 아래
        # _KRW_AMOUNT_RE + 반복 주기 단어 조합으로 route_question에서 별도 처리한다 — "만원"만
        # 단독 키워드로 두면 "BTC 1만원이면 얼마나 살 수 있어?"(예산 변경 의도가 전혀 아닌 조회
        # 질문)까지 걸려버려서, 실제로는 반복 주기 단어("매달"/"한 달에" 등)와 함께 있을 때만
        # 매칭하도록 뒤에서 추가 검사한다. 쉼표 표기("2,000,000원")·띄어쓰기("200만 원") 모두
        # 커버해야 한다는 지적을 받아 정규식으로 바꿨다(리터럴 "만원" 문자열 검사로는 두 표현 다
        # 놓친다). 아래 route_question 참고.
        # 수동 테스트 중 발견(2026-09-19): "뭐부터 해야할지 알려줘"/"어떻게 시작해?"/"처음인데
        # 도와줘"/"뭐부터 하면 돼?"/"어떻게 시작하면 돼?"처럼 서비스 이용을 어떻게 시작할지 묻는
        # 질문이 "예산"/"전략" 같은 기존 키워드를 전혀 안 써서 route_question이 빈 목록을 반환하고
        # 도메인 밖으로 거절되던 것을 발견했다. "시작" 한 단어만 넣으면 "이 캔들은 언제 시작해?" 같은
        # 무관한 질문까지 걸릴 수 있어(지적받음), 서비스 이용 시작 의도를 나타내는 구체적인 문구
        # ("뭐부터", "어떻게 시작", "처음인데")만 매칭시킨다 — 실제 상태 확인은 get_month_status가
        # 하므로 이 세 문구는 plan_agent에만 매칭시켜도 충분하다.
        "뭐부터", "어떻게 시작", "처음인데",
    ],
    "research_agent": [
        "전략", "원칙", "정의", "용어", "리스크", "관리", "란", "무엇", "dca",
        "strategy", "glossary", "위험",
        # RSI/이동평균/드로다운은 문서(data/docs)에 정확한 기준값이 정의돼 있어, price_agent의
        # 실시간 조회만으로는 답이 grounding 없이 LLM의 일반 지식(부정확할 수 있음)에 의존하게
        # 됩니다. research_agent도 함께 매칭시켜 retrieve_docs로 문서 기준값을 근거로 답하게 합니다.
        "이동평균", "이평", "rsi", "드로다운", "과매도", "과매수", "mdd", "최대낙폭", "최대 낙폭",
        # 실사용 중 발견(2026-09-17, evaluation #4): "하락일 매수 조건이 뭐야?" 같은 질문이 "전략"
        # 이라는 단어를 안 써서 research_agent가 안 걸리고 plan_agent(도구에 retrieve_docs가 없어
        # 문서 근거 없이 답함)만 매칭되던 것을 발견했다 — plan_agent와 같은 전략명 키워드를 공유한다.
        "정기 분할", "정기분할", "하락일",
        # RAGAS 전용 평가 세트(evaluation/rag_eval_set.json) 구성 중 발견(2026-09-18): "백테스트
        # 결과는 어떻게 해석해야 하는가?"/"월말 잔여 예산은 어떻게 처리되는가?"가 plan_agent에만
        # 걸리고(도구에 retrieve_docs가 없음) research_agent는 안 걸려 retrieve_docs가 한 번도
        # 호출되지 않았다 — RAGAS 실행에서 해당 두 문항의 context가 0건으로 나와 발견했다.
        "백테스트", "월말", "잔여",
        # 수동 테스트 중 발견(2026-09-18): "처음 쓰는데 어떤 서비스야?"/"이 서비스는 뭐 해주는
        # 거야?"/"처음인데 사용법 알려줘"처럼 서비스 자체를 소개해달라는 최초 이용 질문이 위
        # 어떤 키워드에도 안 걸려 route_question이 빈 목록을 반환하고 "지원 범위 밖"으로
        # 거절되던 것을 발견했다. "서비스"/"사용법"은 최초 이용 설명을 요청할 때만 쓰이는
        # 특정도 높은 단어라(예: "이 서비스는 뭐야?", "사용법 알려줘") 일반적인 투자 질문 전체를
        # 허용 범위로 넓히지 않는다 — "그냥 투자 조언해줘"처럼 서비스/사용법을 언급하지 않는
        # 진짜 범위 밖 질문은 여전히 아무 키워드에도 안 걸려 기존처럼 거절된다.
        "서비스", "사용법",
    ],
    "ledger_agent": [
        # 매수해도/매도해도 같은 질문형("~해도 괜찮아?")까지 명령으로 오인하지 않도록,
        # 실행을 요청하는 명령형 종결(줘/라)만 매칭합니다.
        "기록", "매수해줘", "매수해라", "매도해줘", "매도해라", "청산해줘", "청산해라",
        "초기화", "리셋", "관망", "buy", "ledger", "장부",
        # 실사용 중 발견(2026-09-17): "이번엔 안 사고 지켜볼래"처럼 "관망"이라는 단어를 안 쓰고도
        # 관망 의도를 표현하는 자연스러운 구어체 표현이 실제로 라우팅에 안 걸려 record_watch_decision이
        # 전혀 호출되지 않았다. "관망" 한 단어만으로는 실제 대화 표현을 못 따라가서 추가한다.
        "지켜볼래", "지켜보자", "지켜볼게", "지켜볼", "안 살래", "안살래", "패스할래", "보류할래",
        # 실사용 중 발견(2026-09-20): "오늘 50만원어치 BTC 매수했어"가 "기록"/"매수해줘"(명령형) 중
        # 어느 것과도 안 맞아(과거 시제 보고문) route=[]로 범위 밖 거절됐다 — 실제 매수를 이미 한
        # 사실을 "보고"하는 표현은 명령형이 아니다. "아직 안 샀어"처럼 부정문에도 "샀어"가 부분
        # 문자열로 걸리지만, 실제로 기록을 만들지는 ledger_agent 프롬프트가 판단한다(라우팅 매칭과
        # 쓰기 도구 호출은 다른 층위 — §13-1과 같은 원칙).
        "매수했어", "샀어", "구매했어",
        # 제출 평가 최종 검증 중 발견(2026-09-20): "오늘 정기 매수로 100만원 넣었어"처럼 "기록"
        # 단어 없이 "넣었어"/"투자했어" 같은 구어체 표현만으로 보고하면 위 세 단어 중 어느 것과도
        # 안 맞아 라우팅이 안 걸릴 수 있었다(이 CSV 문항은 "기록해줘"가 붙어 있어 우연히
        # 걸렸지만, "기록해줘" 없이 "오늘 100만원 넣었어"만 오면 그마저도 안 걸림).
        "넣었어", "투자했어",
    ],
}


# 원화 금액 표현 — "200만원"/"200만 원"(띄어쓰기)/"2,000,000원"(쉼표)/"1억원"(억) 전부 잡는다.
# 단독으로는 매칭에 안 쓰고, 아래에서 반복 주기 단어와 함께 있을 때만 plan_agent에 추가로
# 매칭시킨다. "매칭 여부"만 보고 실제 금액은 뽑아내지 않는다 — 값 추출은 아래 별도 정규식/함수
# (_parse_krw_amount) 몫이다.
_KRW_AMOUNT_RE = re.compile(r"\d[\d,]*\s*(?:억|만)?\s*원")
# 실사용 신고(2026-09-20, #2): "1억원에 샀어"(BTC 체결 가격 답변)가 원래 정규식(만 단위만 인식)
# 으로는 전혀 안 잡혔다 — "1"과 "원" 사이에 "억"이 있어 숫자 바로 뒤에 "만?원"이 안 오는 형태라
# 매칭 자체가 실패했다(비트코인 가격은 보통 억 단위라 이 경로에서 특히 자주 나온다). "억"과
# "억+만"(예: "1억 500만원") 조합까지 값을 정확히 계산하도록 별도 정규식을 둔다.
_KRW_AMOUNT_VALUE_RE = re.compile(
    r"(?P<eok>\d[\d,]*)\s*억(?:\s*(?P<man_after_eok>\d[\d,]*)\s*만)?\s*원"
    r"|(?P<man>\d[\d,]*)\s*만\s*원"
    r"|(?P<plain>\d[\d,]*)\s*원"
)


def _parse_krw_amount(text: str) -> float | None:
    """"200만원"/"200만 원"/"2,000,000원"/"1억원"/"1억 500만원"에서 실제 금액(원)을 뽑는다.
    매치가 여러 개면 첫 번째만 쓴다 — 후속 입력 상태(awaiting_input) 응답은 보통 금액 하나만 담긴
    짧은 문장이라는 전제다. 못 찾거나 0 이하이면 None을 반환한다(호출부가 "이 메시지는 금액
    답변이 아니다"로 해석한다)."""
    m = _KRW_AMOUNT_VALUE_RE.search(text or "")
    if not m:
        return None
    try:
        if m.group("eok") is not None:
            value = float(m.group("eok").replace(",", "")) * 100_000_000
            if m.group("man_after_eok"):
                value += float(m.group("man_after_eok").replace(",", "")) * 10_000
        elif m.group("man") is not None:
            value = float(m.group("man").replace(",", "")) * 10_000
        else:
            value = float(m.group("plain").replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


# 실사용 신고(2026-09-20, #2): "오늘 50만원어치 BTC 매수했어"처럼 금액·날짜는 있지만 체결
# 가격·수량이 빠진 매수 보고에 필요한 정보를 물은 뒤(request_buy_execution_detail), 후속 답장에서
# 가격 또는 수량을 뽑는다. "1억원"/"100,000,000원"처럼 원화 표기면 가격, "0.005개"/"0.005 BTC"처럼
# 단위가 붙은 숫자면 수량으로 본다 — 두 표기는 문법적으로 겹치지 않는다(원화 표기는 "원"으로,
# 수량 표기는 "btc"/"비트코인"/"개"로 끝난다).
_BTC_QUANTITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:btc|비트코인|개)\b", re.IGNORECASE)


def _parse_buy_execution_detail(text: str) -> dict | None:
    """후속 답장에서 체결 가격(원) 또는 수량(BTC)을 하나 뽑는다. 못 찾으면 None — 호출부가 "이
    메시지는 가격/수량 답변이 아니다(다른 주제로 넘어감)"로 해석한다."""
    price = _parse_krw_amount(text)
    if price is not None:
        return {"price_krw": price}
    m = _BTC_QUANTITY_RE.search(text or "")
    if m:
        try:
            qty = float(m.group(1))
        except ValueError:
            return None
        if qty > 0:
            return {"quantity_btc": qty}
    return None


# 실사용 신고(2026-09-20, #2) 라이브 검증 중 발견: 시스템 프롬프트가 "가격·수량이 빠졌으면 반드시
# 먼저 request_buy_execution_detail을 호출하라"고 지시해도, 실제 Haiku 4.5가 그 도구를 부르지 않고
# 그냥 말로만 "체결 가격이나 수량을 알려주세요"라고 되묻기만 한 사례가 실제로 재현됐다 — 도구를 안
# 불렀으니 토큰이 발급되지 않고, 다음 턴에 사용자가 "1억원에 샀어"라고 답해도 서버가 방금 물어본
# 보고와 연결할 방법이 없다(무상태 API). 프롬프트 준수에만 기대지 않고, 이 조건(과거형 매수 보고
# + 금액 1건만 + 수량 없음 + 부정 표현 없음 + 이번 턴에 승인도 awaiting_input도 안 생김)을 모두
# 만족하면 서버가 직접 request_buy_execution_detail을 호출해 토큰을 강제로 발급한다.
# 제출 평가(evaluation/test_queries.csv) 최종 검증 중 발견(2026-09-20): "오늘 정기 매수로 100만원
# 넣었어 기록해줘"는 위 세 단어 중 어느 것도 안 써서("넣었어"는 없음) 이 안전망이 개입하지 않았고,
# 모델이 프롬프트 지시(request_buy_execution_detail 먼저 호출)를 어기고 말로만 되물어도 그대로
# 방치됐다 — ledger_agent 라우팅 자체는 "기록" 키워드로 이미 걸렸으니(_AGENT_KEYWORDS 참고),
# 안전망의 보고 키워드 목록만 좁았던 것이 문제. 구어체("넣었어"/"투자했어")와 격식체 종결
# ("~습니다")을 함께 넓힌다 — 매번 신고되는 대로 보강해야 하는 구조적 한계는 그대로다.
_BUY_REPORT_KEYWORDS = (
    "매수했어", "샀어", "구매했어", "넣었어", "투자했어",
    "매수했습니다", "샀습니다", "구매했습니다", "넣었습니다", "투자했습니다",
)
_BUY_REPORT_NEGATION_WORDS = ("안 사", "안샀", "안 샀", "않았", "안 살", "안살")


def _maybe_force_buy_execution_request(question: str, agents: list[str], approvals_needed: list[dict],
                                        already_awaiting: bool) -> dict | None:
    """이번 턴에 이미 승인 요청이나 awaiting_input이 생겼으면(=LLM이 알아서 처리함) None. 과거형
    매수 보고인데 금액은 1건만, 수량은 없고, 부정 표현도 없으면 강제 발급할 {amount_krw,
    executed_date}를 반환한다 — 그 외에는 애매하니(예: 가격까지 이미 다 있는 문장) 손대지 않는다."""
    if "ledger_agent" not in agents or approvals_needed or already_awaiting:
        return None
    if not any(k in question for k in _BUY_REPORT_KEYWORDS):
        return None
    if any(w in question for w in _BUY_REPORT_NEGATION_WORDS):
        return None
    if _BTC_QUANTITY_RE.search(question):
        return None
    if len(_KRW_AMOUNT_RE.findall(question)) != 1:
        return None
    amount = _parse_krw_amount(question)
    if amount is None:
        return None
    executed_date = datetime.now(KST).strftime("%Y-%m-%d")
    return {"amount_krw": amount, "executed_date": executed_date}


# 실사용 신고(2026-09-20, #2): "10월 예산은 얼마야?"에서 "얼마"가 무조건 price_agent를 끌어들여,
# plan_agent의 정상적인 예산 조회 답변 옆에 "저는 예산을 다루지 않습니다"류의 무관한 거절 문장이
# 함께 뜨는 문제가 있었다. "예산 문맥에서의 '얼마'"와 "가격 문맥에서의 '얼마'"를 구분해야 한다 —
# "예산" 키워드는 있는데 가격을 가리키는 다른 단어(가격/시세/현재가/btc/price)가 전혀 없으면
# price_agent를 끌어들이지 않는다. "10월 예산과 BTC 현재가 알려줘"처럼 둘 다 있으면 그대로 둘 다
# 매칭된다(가격 문맥 단어가 이미 있으니). "BTC 1만원이면 얼마나 살 수 있어?"처럼 "예산" 자체가
# 없는 순수 가격/수량 질문은 기존처럼 그대로 price_agent에 매칭된다.
_AMOUNT_QUESTION_RE = re.compile(r"얼마")
_BUDGET_CONTEXT_WORDS = ("예산",)
_PRICE_CONTEXT_WORDS = ("가격", "시세", "현재가", "btc", "price")
# 실사용 재현(2026-09-20, #4): "이번 달 얼마 썼어?"는 "예산"이라는 단어를 안 써서 위
# _BUDGET_CONTEXT_WORDS 조합에 안 걸리고, "얼마"만으로 price_agent가 끌려와 plan_agent의 정확한
# 사용액·잔여 예산 답변 옆에 "개인 지출은 담당하지 않는다"류의 무관한 거절이 함께 떴다. "예산"
# 문맥과 마찬가지로 "사용액/잔여액을 묻는 문맥"에서도 "얼마"만으로 price_agent를 끌어들이지 않는다
# (가격 문맥 단어가 함께 있으면 기존처럼 그대로 둘 다 매칭 — 아래 has_price_context 예외는 그대로
# 적용된다). "샀어"/"구매했어"는 ledger_agent의 과거형 매수 보고 키워드와 겹치지만, 여기서는
# "얼마"와 결합된 조회 질문("얼마나 샀어?")만을 가리키는 별도 용도로 쓴다.
_SPENDING_QUERY_WORDS = ("썼어", "쓴 금액", "지출", "남았어", "샀어", "구매했어")
# "매달 200만원씩 투자하고 싶어"류의 예산 설정 의도는 "반복 주기를 나타내는 말 + 금액"의 조합으로
# 나타난다 — 이 조합이 아니라 금액만 있는 경우(예: "BTC 1만원이면 얼마나 살 수 있어?")는 단발성
# 조회/계산 질문이지 예산 변경 의도가 아니므로 plan_agent에 매칭시키지 않는다(2026-09-18, 수동
# 테스트에서 "만원" 단독 키워드가 이 경우까지 잘못 끌어들이는 것을 발견해 조합 조건으로 좁혔다).
_RECURRING_CADENCE_WORDS = ("매달", "한 달에", "한달에", "매월")

# 실사용 중 발견(2026-09-19): 백테스트 결과를 본 뒤 "나는 일단 200만원으로 진행해볼래"처럼 금액을
# 결정하는 말인데 "매달"/"한 달에" 같은 반복 주기 단어를 안 써서 위 _RECURRING_CADENCE_WORDS 조합에
# 안 걸리고 route_question이 빈 목록을 반환했다 — set_monthly_budget은 애초에 "이번 달 예산"을
# 다루는 도구라 매번 "매달"을 반복해야만 예산 설정 의도인 게 아니다. 금액 + "이 금액으로 결정한다"는
# 의사표현(진행/시작/정하다/설정하다 계열)도 같은 의도로 본다 — 순수 조회/계산 질문("~하면 얼마나
# 살 수 있어?")에는 이런 결정 어미가 안 붙으므로 오탐 위험이 낮다.
_BUDGET_DECISION_WORDS = (
    "진행할래", "진행해볼래", "진행하고 싶어", "진행하고싶어",
    "시작할래", "시작해볼래", "정할래", "설정할래",
    # 추가 발견(2026-09-19): "100만원으로 시작한다"처럼 (아마 "예산을 말해달라"는 안내에 대한
    # 답으로) 평서형/구어체 종결로 결정을 표현하는 경우도 위 목록의 "~ㄹ래"/"~고 싶어" 어미와
    # 안 맞아 빠졌다. 이 서비스가 매수 시기 표현(§18-2)에서와 같은 종류의 한계다 — 결정 표현은
    # 사실상 무한히 다양해 키워드 나열로 전부 선제 포괄은 못 한다. 평서형("~ㄴ다")·구어체
    # 진행형("~할게")·과거형("~했어") 세 종결을 추가로 포괄한다.
    "진행한다", "진행할게", "진행했어",
    "시작한다", "시작할게", "시작했어",
    "정한다", "정할게", "정했어",
    "설정한다", "설정할게", "설정했어",
)

# 실사용 신고(2026-09-20, #1): "RSI매수로 할게"가 price_agent/research_agent(둘 다 "rsi" 키워드로
# 매칭)에만 걸리고 plan_agent는 안 걸려, 전략 변경 제안·확인 카드가 아예 안 만들어졌다. 전략
# "이름"(RSI/하락일/정기 분할)만으로는 plan_agent를 매칭시키지 않는다(순수 설명·조회 질문과 구분이
# 안 된다 — 그런 일반적인 경우는 이미 "전략" 키워드가 담당한다) — 대신 전략 이름 + "이걸로
# 결정한다"는 명확한 어미가 함께 있을 때만 plan_agent를 추가로 매칭시킨다. "RSI로 바꾸지 마"처럼
# 부정형("바꾸지 마")은 아래 목록의 긍정 어미("바꿔줘"/"바꿀래" 등)와 문자열이 안 맞아 매칭되지
# 않는다 — 부정 처리를 별도로 구현할 필요 없이, 애초에 매칭 자체가 안 되어 제안이 생기지 않는다.
_STRATEGY_NAME_WORDS = ("rsi", "하락일", "정기 분할", "정기분할", "decline_day", "biweekly")
_STRATEGY_DECISION_WORDS = (
    "로 할게", "로 할래", "으로 할게", "으로 할래",
    "로 바꿔줘", "로 바꿀래", "으로 바꿔줘", "으로 바꿀래",
    "로 바꾸고 싶어", "으로 바꾸고 싶어",
    "로 변경할래", "으로 변경할래", "로 정할래", "으로 정할래",
    "로 선택할래", "으로 선택할래",
)

# 실사용 신고(2026-09-20, #3): "9월의 예산과 전략을 알려줘"에서 "전략"이 research_agent의 일반
# 키워드와 겹쳐, plan_agent가 이미 정확히 답한 뒤에도 research_agent가 "개인 상태는 담당하지
# 않는다"류의 불필요한 안내를 덧붙였다. "전략"이 "실제 선택 상태를 묻는 개인 상태 질문"인지
# "전략 개념을 설명해달라는 질문"인지 구분해야 한다 — 특정 월을 콕 집거나("9월"/"이번 달"/
# "다음 달") 소유격("내"/"제")이 있는데 개념 설명을 요구하는 표현(무슨 뜻/정의/조건이 뭐/원리 등)이
# 전혀 없으면 "전략"만으로 research_agent를 끌어들이지 않는다. 다른 이유(rsi/하락일/정기 분할/
# 백테스트 등)로 이미 매칭됐다면 그대로 둔다 — "내 전략은 뭐고 RSI는 무슨 뜻이야?"처럼 복합
# 질문은 "rsi"·"무슨 뜻" 둘 다 있어 영향받지 않는다.
_MONTH_NUMBER_RE = re.compile(r"\d{1,2}\s*월")
_PERSONAL_STATUS_CONTEXT_WORDS = (
    "내 ", "제 ", "저의 ", "이번 달", "이번달", "다음 달", "다음달",
    # 실사용 재현(2026-09-20): "현재 전략이 뭔지 알려주고, 매수 조건이 충족되었는지 확인해줘"는
    # "내"/"제"/월 표현 어느 것도 안 써서 위 목록으로 개인 상태 문맥을 못 잡았다 — "현재 전략"/
    # "지금 전략"도 "이번 달 전략"과 똑같이 "내가 지금 선택해둔 전략"을 묻는 개인 상태 표현이다.
    "현재 전략", "지금 전략", "현재 선택", "지금 선택", "선택된 전략", "선택한 전략",
)
_STRATEGY_CONCEPT_QUERY_WORDS = (
    "무슨 뜻", "뜻이 뭐", "뭔 뜻", "정의가", "원리가", "조건이 뭐", "방식이 뭐", "어떻게 계산", "뭘 의미",
)

# 실사용 UI 신고(2026-09-20, #6): "BTC가 뭐야"/"비트코인이 뭐야"/"비트코인에 대해 설명해줘"가
# research_agent(문서 data/docs/BTC.md 보유)의 어느 키워드에도 안 걸려 범위 밖으로 거절됐다 —
# "DCA가 뭐야?"는 "dca"가 이미 키워드라 정상 동작하는데 "btc"/"비트코인"은 애초에 어떤 Agent의
# 키워드 목록에도 없었다. 그렇다고 "btc"/"비트코인"을 research_agent에 단독 키워드로 추가하면
# "오늘 50만원어치 BTC 매수했어"(ledger_agent 몫) 같은 문장까지 전부 research_agent를 끌어들이게
# 된다 — "개념을 묻는 어미(뭐야/란/설명해줘)와 함께 있을 때만" 매칭하는 조합 규칙으로 좁힌다
# (§22-1의 전략 선택 조합 규칙과 같은 설계).
_BTC_CONCEPT_RE = re.compile(
    r"(btc|비트코인)\s*(가|이|는|은)?\s*(뭐야|뭔가요|무엇|란\b|이란)"
    r"|(btc|비트코인).{0,10}(설명해|소개해)"
    r"|(설명해|소개해).{0,10}(btc|비트코인)",
    re.IGNORECASE,
)

# 실사용 UI 신고(2026-09-20, #5): "안녕"/"넌 무슨일을 할수있니"가 어느 Agent 키워드에도 안 걸려
# 다른 범위 밖 질문과 똑같이 "~에 대해서만 답할 수 있습니다"로 거절됐다. 인사·기능 소개 요청은
# 특정 Agent가 아니라 서비스 전체에 대한 메타 질문이라 route_question의 일반 매칭 대상이 아니다 —
# run()에서 agents가 비었을 때만 별도로 확인한다(정상적으로 어느 Agent에 걸리는 질문에 "안녕"이
# 우연히 포함돼 있어도 이 분기를 타지 않는다). 띄어쓰기가 없는 구어체 표현("무슨일을할수있니")도
# 흔해 각 어절 사이에 공백을 선택적으로 허용하는 정규식으로 잡는다.
_GREETING_OR_CAPABILITY_RE = re.compile(
    r"^\s*(안녕|하이|헬로)"  # 한글은 \b(단어 경계)가 음절 사이에서 작동하지 않아 접두 매칭으로 처리
    r"|\b(hi|hello)\b"
    r"|무슨\s*일을?\s*할\s*수\s*있"
    r"|뭘\s*할\s*수\s*있"
    r"|뭐\s*를?\s*할\s*수\s*있"
    r"|어떤\s*(걸|것을?)\s*도와"
    r"|무엇을\s*도와"
    r"|(어떤|무슨)\s*도움"
    r"|뭐\s*하는\s*서비스|뭘\s*하는\s*서비스"
    r"|뭐\s*해주는|뭘\s*해주는"
    r"|기능이\s*뭐",
    re.IGNORECASE,
)
_CAPABILITY_INTRO_TEXT = (
    "안녕하세요! 이 어시스턴트는 비트코인(BTC) DCA(분할 매수) 계획을 돕는 도우미입니다. "
    "다음을 도와드릴 수 있어요:\n\n"
    "- **예산·전략 계획**: 월 예산 설정, 세 가지 매수 전략(하락일/정기 분할/RSI) 선택·변경, "
    "과거 48개월 백테스트 비교\n"
    "- **시세·지표 조회**: 실시간 BTC 가격, RSI·200일 이동평균 괴리율·기간별 DD(현재 하락률)·"
    "MDD(최대 낙폭)\n"
    "- **매수 기록 관리**: 실제 매수·관망 기록, 이번 달 사용액·남은 예산 조회\n"
    "- **개념 설명**: DCA·RSI 등 투자 개념과 이 서비스의 매수 규칙 설명\n\n"
    "예시: \"이번 달 예산 얼마 남았어?\", \"BTC 지표 알려줘\", \"RSI 매수로 할게\", "
    "\"오늘 50만원어치 매수했어\"\n\n"
    "이 범위를 벗어난 질문(다른 코인, 실시간 알림, 자동매매 등)은 도와드릴 수 없어요."
)


def route_question(question: str) -> list[str]:
    """질문을 읽고 어느 전문 Agent로 보낼지 LLM 없이 결정적으로 고릅니다.

    여러 주제가 섞이면 해당 Agent를 모두, 어디에도 안 걸리면 빈 목록을 반환합니다.
    """
    q = (question or "").lower()
    matched = [name for name, kws in _AGENT_KEYWORDS.items() if any(kw.lower() in q for kw in kws)]
    if (
        "plan_agent" not in matched
        and _KRW_AMOUNT_RE.search(q)
        and (
            any(w in q for w in _RECURRING_CADENCE_WORDS)
            or any(w in q for w in _BUDGET_DECISION_WORDS)
        )
    ):
        matched.append("plan_agent")
    if "price_agent" not in matched and _AMOUNT_QUESTION_RE.search(q):
        has_budget_context = any(w in q for w in _BUDGET_CONTEXT_WORDS) or any(
            w in q for w in _SPENDING_QUERY_WORDS
        )
        has_price_context = any(w in q for w in _PRICE_CONTEXT_WORDS)
        if not has_budget_context or has_price_context:
            matched.append("price_agent")
    if (
        "plan_agent" not in matched
        and _AMOUNT_QUESTION_RE.search(q)
        and any(w in q for w in _SPENDING_QUERY_WORDS)
    ):
        # "얼마 남았어?"처럼 "이번 달"/"예산" 같은 기존 키워드가 전혀 없는 순수 사용액·잔여액
        # 조회 질문도 plan_agent(사용액·잔여 예산·예산 총액을 함께 답하는 상태 담당 Agent)에
        # 매칭시킨다 — "이번 달"이 이미 있는 문장은 기존 키워드로도 매칭되므로 여기서는 중복
        # 추가되지 않는다.
        matched.append("plan_agent")
    if (
        "plan_agent" not in matched
        and any(w in q for w in _STRATEGY_NAME_WORDS)
        and any(w in q for w in _STRATEGY_DECISION_WORDS)
    ):
        matched.append("plan_agent")
    if "research_agent" not in matched and _BTC_CONCEPT_RE.search(q):
        matched.append("research_agent")
    if "research_agent" in matched:
        has_personal_context = bool(_MONTH_NUMBER_RE.search(q)) or any(w in q for w in _PERSONAL_STATUS_CONTEXT_WORDS)
        has_concept_query = any(w in q for w in _STRATEGY_CONCEPT_QUERY_WORDS) or bool(_BTC_CONCEPT_RE.search(q))
        if has_personal_context and not has_concept_query:
            other_research_keywords = [kw for kw in _AGENT_KEYWORDS["research_agent"] if kw != "전략"]
            if not any(kw.lower() in q for kw in other_research_keywords):
                matched.remove("research_agent")
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
        # "오늘"/"어제"/"이번 달"/"다음 달" 같은 상대적 표현을 실제 날짜·연월로 옮기려면 LLM이
        # 지금이 언제인지 알아야 한다. "오늘"만 알려줬을 때는 executed_date 누락(§7-1 아님, 실사용
        # 발견)과 "다음 달" 계산을 틀려 get_month_status/select_strategy에 엉뚱한 연/월을 넘기는
        # 문제가 둘 다 실제로 발견됐다 — 그래서 이번 달/다음 달까지 서버가 직접 계산해 명시적으로
        # 박아준다(LLM의 날짜 산수에 기대지 않는다).
        now = datetime.now(KST)
        today_str = now.strftime("%Y-%m-%d(%a)")
        this_y, this_m = now.year, now.month
        next_y, next_m = (this_y + 1, 1) if this_m == 12 else (this_y, this_m + 1)
        dated_prompt = (
            f"{system_prompt}\n\n오늘 날짜는 {today_str}입니다(KST 기준). "
            f"이번 달은 {this_y}년 {this_m}월(year={this_y}, month={this_m})이고, "
            f"다음 달은 {next_y}년 {next_m}월(year={next_y}, month={next_m})입니다. "
            "get_month_status/select_strategy 등 year/month 인자가 있는 도구를 부를 때 "
            "'이번 달'/'다음 달' 같은 표현이 나오면 직접 계산하지 말고 위 숫자를 그대로 쓰세요. "
            "다른 상대적 날짜 표현(어제 등)도 오늘 날짜를 기준으로 실제 날짜(YYYY-MM-DD)로 변환해 "
            "도구 인자에 채우세요."
        )
        messages = [SystemMessage(content=dated_prompt)] + state["messages"]
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


_STRATEGY_LABELS = {"decline_day": "하락일 매수", "biweekly": "정기 분할(매월 1일·15일)", "rsi": "RSI 매수"}
_ANSWER_PRIORITY = ["price_agent", "plan_agent", "ledger_agent", "research_agent"]


def _build_narrative_text(answers_by_agent: dict[str, str], budget_proposal: dict | None) -> str:
    """_build_final_answer가 구조화 요약(승인/전략/예산 확인 안내 — confirmation_token·API 경로
    포함)을 덧붙이기 전, Agent별 자연어 설명만 모은 문자열. plan_agent 제외 규칙은
    _build_final_answer와 반드시 동일해야 한다(둘이 갈라지면 어느 한쪽이 실제와 다른 내용을
    보여주게 된다) — 그래서 _build_final_answer가 이 함수를 그대로 호출해서 쓴다.

    실사용 UI 신고(2026-09-20, #4): UI가 answer를 그대로 보여주면 토큰·POST 경로 문구가 카드
    안내와 중복 노출된다. answer 자체를 정규식으로 잘라내는 대신, "설명만"과 "설명+구조화 요약"을
    애초에 서로 다른 필드로 만들어 UI가 설명만 보여주고 요약은 자기 카드로 대체할 수 있게 한다 —
    run()이 이 함수의 결과를 `narrative` 필드로 별도 반환한다(§4-2 API 계약 확장, SPEC.md 참고).
    """
    included = dict(answers_by_agent)
    if budget_proposal is not None:
        included.pop("plan_agent", None)
    ordered_names = sorted(
        included, key=lambda n: _ANSWER_PRIORITY.index(n) if n in _ANSWER_PRIORITY else len(_ANSWER_PRIORITY)
    )
    return "\n\n".join(f"[{name}] {included[name]}" for name in ordered_names)


def _build_final_answer(
    answers_by_agent: dict[str, str],
    approvals_needed: list[dict],
    strategy_proposal: dict | None,
    budget_proposal: dict | None,
) -> str:
    """Agent별 자연어 답을 우선순위대로 이어 붙인 뒤, 승인/전략변경/예산변경 제안이 있으면 항상
    정확한 구조화 요약을 덧붙인다.

    실사용 중 발견(2026-09-17, 승인): 승인이 필요한 도구 호출은 그래프가 await_approval에서 멈추면서
    그 Worker의 안내 문구를 answers에 전혀 안 남긴다 — 다른 Agent가 답을 안 냈으면 사용자는
    approvals_needed 필드를 직접 안 보는 한 "뭘 승인해야 하는지" 전혀 알 수 없었다.

    실사용 중 발견(2026-09-18, 예산): 예산 제안이 성공적으로 생성돼도, 그 결과를 자연어로 옮기는
    몫이 전적으로 LLM에게 맡겨져 있으면 도구가 돌려준 금액·적용월을 최종 답변에서 빠뜨리거나,
    존재하지 않는 "확인 버튼"을 안내하거나, 전략 선택을 예산 확인의 선행 조건처럼 요구하는 등
    부정확한 답이 나올 수 있다(judge_output의 keep=true는 "내용이 있고 근거 없는 단정이 없다"만
    보지 "금액·월·API 경로가 정확한가"는 전혀 보지 않는다).

    **2026-09-19 재수정 — 프롬프트 준수에 의존하지 않는 강제**: 처음엔 "요약 블록을 정확하게
    덧붙이되 plan_agent의 자유 서술도 그대로 유지"하는 방식이었다. 하지만 plan_agent가 프롬프트를
    어기고(또는 모델이 그 지시를 놓쳐서) "설정 완료했습니다"/"2주마다 자동으로 매수합니다" 같은
    문장을 쓰면, 그 문장이 구조화 블록과 나란히 최종 답변에 그대로 노출되는 문제가 실제로
    지적됐다 — 문서화된 한계로 남기는 것만으로는 부족하다는 지적이었다. 그래서 예산 제안이 있으면
    **`plan_agent`의 자유 서술 자체를 최종 답변에서 제외**하고, 그 정보 전부를 구조화 요약으로
    완전히 대체한다 — 이제 plan_agent가 프롬프트를 어겨서 뭐라고 쓰든(모델 능력에 의존하지 않고)
    그 텍스트 자체가 애초에 합쳐지지 않으므로 노출될 수 없다. 같은 응답에 요청된 서비스 소개·전략
    설명(예: research_agent)은 그대로 유지한다 — 제외되는 건 plan_agent의 서술뿐이다. 전략 변경
    제안(`strategy_proposal`)에는 아직 이 처리를 적용하지 않았다(이번 요청 범위 밖 — 요청받으면
    같은 방식으로 확장할 수 있다).
    """
    narrative = _build_narrative_text(answers_by_agent, budget_proposal)
    answers = [narrative] if narrative else []

    approval_lines = [f"- {a['reason']} (도구: {a['tool']}, 승인 ID: {a['approval_id']})" for a in approvals_needed]
    approval_summary = ("[실행 전 확인 필요]\n" + "\n".join(approval_lines)) if approval_lines else ""

    strategy_summary = ""
    if strategy_proposal is not None:
        sp = strategy_proposal
        strategy_label = _STRATEGY_LABELS.get(sp["strategy"], sp["strategy"])
        strategy_summary = (
            "[전략 변경 확인 필요]\n"
            f"- {sp['year']}년 {sp['month']}월 전략을 '{strategy_label}'(으)로 바꾸는 제안이며, "
            "아직 저장되지 않았습니다.\n"
            f"- 적용하려면 confirmation_token(\"{sp['confirmation_token']}\")을 "
            "POST /confirm_strategy_change 로 보내세요(POST /cancel_strategy_change 로 취소)."
        )

    budget_summary = ""
    if budget_proposal is not None:
        bp = budget_proposal
        # "action"이 없는(구버전 형태로 직접 만들어진 테스트 등) 제안 dict와의 하위 호환 —
        # is_initial만으로 이전과 동일하게 initial/change 둘 중 하나로 판단한다.
        action = bp.get("action") or ("initial" if bp.get("is_initial") else "change")
        verb = {"initial": "시작", "advance": "앞당겨 시작", "change": "변경"}.get(action, "변경")
        extra_line = ""
        # 실사용 신고(2026-09-20, #3 / 정책 변경 이후): 요청 월과 실제 적용월이 다르면 그 사실과
        # 이유를 구조화 블록에 명시한다 — plan_agent의 자유 서술에만 맡기면 예산 제안이 있을 때 그
        # 텍스트 자체가 최종 답변에서 빠지므로(위 _build_narrative_text) 이유가 통째로 사라진다.
        # 최초 설정(이번 달/다음 달 중 선택 가능)과 변경(항상 다음 달만)은 허용 범위가 달라
        # 안내 문구도 다르게 쓴다.
        if bp.get("month_mismatch"):
            if action == "initial":
                extra_line = (
                    f"- 요청하신 {bp['requested_month']}에는 시작할 수 없습니다 — 최초 시작은 이번 달 "
                    f"또는 다음 달만 가능합니다. 대신 {bp['effective_month']}부터 시작하는 제안입니다.\n"
                )
            else:
                extra_line = (
                    f"- 요청하신 {bp['requested_month']}에는 적용할 수 없습니다 — 월 예산 변경은 정책상 "
                    f"다음 달부터만 적용됩니다. 대신 {bp['effective_month']}부터 적용하는 제안입니다.\n"
                )
        elif action == "advance" and bp.get("previous_start_month"):
            # 정책 변경(2026-09-20, 월중에도 이번 달부터 DCA 시작 지원) §2: 아직 시작하지 않은
            # 계획(다음 달 시작으로 이미 설정됨)의 시작월을 이번 달로 앞당기는 경우 — 기존에
            # 설정된 미래 시작월의 예산은 삭제·복사하지 않고 그대로 유지된다는 사실을 반드시
            # 구분해서 안내한다(요청사항: "이미 설정된 다음 달 예산과 다르면 각각 구분해 안내").
            prev_amount = bp.get("previous_start_amount")
            prev_amount_text = f"{prev_amount:,.0f}원" if prev_amount is not None else "미설정"
            extra_line = (
                f"- 아직 시작하지 않은 계획의 시작월을 {bp['previous_start_month']}에서 "
                f"{bp['effective_month']}(이번 달)로 앞당기는 제안입니다. 기존에 설정된 "
                f"{bp['previous_start_month']} 예산({prev_amount_text})은 삭제되지 않고 그대로 "
                "유지됩니다 — 이번 달분 예산만 새로 추가됩니다.\n"
            )
        budget_summary = (
            "[예산 확인 필요]\n"
            f"{extra_line}"
            f"- 월 예산을 {bp['amount_krw']:,.0f}원으로 {verb}하는 제안이며, {bp['effective_month']}부터 "
            "적용될 예정입니다. 아직 저장되지 않았습니다.\n"
            f"- 적용하려면 confirmation_token(\"{bp['confirmation_token']}\")을 "
            "POST /confirm_budget_change 로 보내세요(POST /cancel_budget_change 로 취소)."
        )

    if not answers and not approval_summary and not strategy_summary and not budget_summary:
        return "쓸 만한 답을 만들지 못했습니다. 질문을 조금 더 구체적으로 해주세요."

    parts = list(answers)
    for summary in (approval_summary, strategy_summary, budget_summary):
        if summary:
            parts.append(summary)
    return "\n\n".join(parts) if parts else "승인이 필요한 작업이 있어 답변을 만들지 못했습니다."


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

    def run(question: str, proceed_with_stale_data: bool = False, awaiting_input_token: str = "") -> dict:
        global _LAST_DATA_GAP, _LAST_STRATEGY_PROPOSAL, _LAST_BUDGET_PROPOSAL, _LAST_AWAITING_INPUT
        _REQUEST_CONTEXT["proceed_with_stale_data"] = proceed_with_stale_data
        _LAST_DATA_GAP = None
        _LAST_STRATEGY_PROPOSAL = None
        _LAST_BUDGET_PROPOSAL = None
        _LAST_AWAITING_INPUT = None
        trace: list[dict] = []

        blocked, guard_reason = guardrails.input_guard(question)
        trace.append({"step": "guard", "input": question, "output": {"blocked": blocked, "reason": guard_reason}})
        if blocked:
            text = f"요청을 처리할 수 없습니다: {guard_reason}"
            return {
                "answer": text,
                "narrative": text,
                "contexts": [],
                "trace": trace,
                "agents_used": [],
                "approvals_needed": [],
            }

        safe_question = guardrails.mask_pii(question)

        # 후속 입력 상태(awaiting_input) 해석 — 실사용 UI 신고(2026-09-20, #1): "뭐부터 시작하면
        # 돼?" → (얼마로 시작할지 되물음) → "200만원"이 어떤 키워드에도 안 걸려 범위 밖으로
        # 거절됐다. request_monthly_budget_amount가 발급한 토큰이 이번 요청에 함께 왔으면, 일반
        # 라우팅보다 먼저 처리한다 — "200만원" 같은 문장은 원래 아무 Agent 키워드에도 안 걸리므로,
        # 여기서 처리하지 않으면 뒤의 route_question에서 그대로 다시 거절된다. 토큰은 1회용으로
        # 바로 소진한다(다른 주제로 넘어갔을 때 이 상태가 계속 남아있으면 안 되므로 — 요청사항).
        pending_awaiting = _PENDING_AWAITING_INPUT.pop(awaiting_input_token, None) if awaiting_input_token else None
        if pending_awaiting and pending_awaiting["kind"] == "monthly_budget_amount":
            amount = _parse_krw_amount(safe_question)
            trace.append({
                "step": "awaiting_input",
                "input": safe_question,
                "output": {"kind": "monthly_budget_amount", "amount_parsed": amount},
            })
            if amount is not None:
                proposal = _propose_budget(amount)
                if proposal["ok"]:
                    answer = _build_final_answer({}, [], None, _LAST_BUDGET_PROPOSAL)
                    return {
                        "answer": answer,
                        "narrative": _build_narrative_text({}, _LAST_BUDGET_PROPOSAL),
                        "contexts": [],
                        "trace": trace,
                        "agents_used": ["plan_agent"],
                        "approvals_needed": [],
                        "budget_change_needs_confirmation": _LAST_BUDGET_PROPOSAL,
                    }
                # propose_budget_change 실패(예: amount_krw<=0 — 이미 위에서 걸러졌으므로 사실상
                # 도달하지 않음)는 일반 라우팅으로 흘려보낸다.
            # amount가 없으면(다른 주제로 전환) — 토큰은 이미 소진했으니 평소처럼 라우팅한다.
        elif pending_awaiting and pending_awaiting["kind"] == "buy_execution_detail":
            # 실사용 신고(2026-09-20, #2): "오늘 50만원어치 BTC 매수했어" → (체결 가격/수량을
            # 물음) → "1억원에 샀어" 흐름 — 위 예산 금액 흐름과 같은 원리지만, 이번엔 도구가 바로
            # 실행되는 게 아니라 record_virtual_buy가 원래 요구하는 승인 절차(approvals.py)로
            # 이어져야 한다. LLM이 두 메시지에 걸친 정보(1차: 금액·날짜, 2차: 가격·수량)를 다시
            # 정확히 합치는 걸 기대하는 대신, 서버가 직접 합쳐서 승인 요청을 만든다 — 파싱된 값만
            # 쓰므로 금액이 둔갑하거나 현재 시세가 체결가로 둔갑할 위험이 없다.
            detail = _parse_buy_execution_detail(safe_question)
            trace.append({
                "step": "awaiting_input",
                "input": safe_question,
                "output": {"kind": "buy_execution_detail", "detail_parsed": detail},
            })
            if detail is not None:
                args = {
                    "amount_krw": pending_awaiting["amount_krw"],
                    "executed_date": pending_awaiting["executed_date"],
                    "execution_time_precision": "date",
                    **detail,
                }
                reason = "실제 매수 기록 생성 — 사용자가 직접 보고한 매수 내역"
                approval_id = approvals.create("record_virtual_buy", args, reason=reason)
                pending_approval = {
                    "approval_id": approval_id, "agent": "ledger_agent",
                    "tool": "record_virtual_buy", "args": args, "reason": reason,
                }
                trace.append({"step": "approval_required", "input": pending_approval, "output": "실행 보류"})
                answer = _build_final_answer({}, [pending_approval], None, None)
                return {
                    "answer": answer,
                    "narrative": _build_narrative_text({}, None),
                    "contexts": [],
                    "trace": trace,
                    "agents_used": ["ledger_agent"],
                    "approvals_needed": [pending_approval],
                }
            # detail이 없으면(다른 주제로 전환) — 토큰은 이미 소진했으니 평소처럼 라우팅한다.

        agents = route_question(safe_question)
        trace.append({"step": "route", "input": safe_question, "output": agents})
        if not agents:
            # 실사용 UI 신고(2026-09-20, #5): "안녕"/"넌 무슨일을 할수있니"처럼 인사·기능 소개
            # 요청까지 다른 범위 밖 질문과 똑같은 "~에 대해서만 답할 수 있습니다" 한 줄로 거절돼,
            # 처음 쓰는 사용자가 이 서비스로 뭘 할 수 있는지 전혀 감을 못 잡았다. 어느 Agent도
            # 이 질문을 소유하지 않으므로(모든 Agent가 각자의 좁은 전문 영역만 담당) LLM 호출 없이
            # 서버가 직접 고정된 소개 문구를 반환한다 — 매번 같은 정확한 소개가 보장되고, 이 흔한
            # 첫 질문 하나에 불필요한 모델 호출 비용도 들지 않는다.
            if _GREETING_OR_CAPABILITY_RE.search(safe_question):
                return {
                    "answer": _CAPABILITY_INTRO_TEXT,
                    "narrative": _CAPABILITY_INTRO_TEXT,
                    "contexts": [],
                    "trace": trace,
                    "agents_used": [],
                    "approvals_needed": [],
                }
            text = "이 어시스턴트는 BTC 예산·전략 계획·시세·지표·매수 기록에 대해서만 답할 수 있습니다."
            return {
                "answer": text,
                "narrative": text,
                "contexts": [],
                "trace": trace,
                "agents_used": [],
                "approvals_needed": [],
            }

        answers_by_agent: dict[str, str] = {}
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

            # ChatBedrockConverse(특히 sonnet-4-5)는 최종 답변의 content를 문자열이 아니라
            # 블록 리스트로 줄 때가 실제로 있다(retriever.get_text가 이미 이 문제를 처리해 둔
            # 이유와 같음) — 그대로 str()하면 "[{'type': 'text', ...}]" 같은 걸 그대로 노출하고,
            # judge_output은 .strip() 호출에서 바로 죽는다. 같은 헬퍼를 재사용해 정규화한다.
            final_text = retriever.get_text(result["messages"][-1])
            verdict = judge_output(name, final_text)
            trace.append({"step": f"judge:{name}", "input": final_text, "output": verdict})
            if verdict["keep"]:
                answers_by_agent[name] = final_text

        contexts = [
            {"doc_id": d.metadata.get("source", "unknown"), "text": d.page_content} for d in _LAST_RETRIEVED_DOCS
        ]

        forced = _maybe_force_buy_execution_request(
            safe_question, agents, approvals_needed, _LAST_AWAITING_INPUT is not None
        )
        if forced is not None:
            request_buy_execution_detail.invoke(forced)
            trace.append({
                "step": "force_awaiting_input",
                "input": forced,
                "output": "모델이 request_buy_execution_detail을 직접 호출하지 않아 서버가 대신 발급",
            })
            answers_by_agent["ledger_agent"] = (
                "실제 매수 보고를 받았습니다. 기록을 완성하려면 체결 가격(원/BTC) 또는 매수한 수량"
                "(BTC) 중 하나를 알려주세요. 참고로 이 기록은 실제 거래소 주문이 아닌 장부 기록이며, "
                "승인 절차를 거칩니다."
            )

        # 실시간 상태 답(price/plan/ledger_agent)을 항상 일반 개념 설명(research_agent)보다 앞에
        # 배치한다 — 사용자 상태 정보와 일반 설명의 역할을 답변 순서에서도 구분해, 상태 답이 먼저
        # 읽히고 문서 기반 설명은 보충 설명으로 뒤에 오도록 한다(기획팀 지적: 개인 상태와 일반 개념
        # 설명이 나란히 섞이면 모순처럼 보일 수 있다). 정렬·plan_agent 제외 로직은
        # _build_final_answer로 옮겼다 — Agent별 딕셔너리를 그대로 넘겨야 예산 제안이 있을 때
        # plan_agent 항목만 선택적으로 뺄 수 있다(2026-09-19).
        answer = _build_final_answer(answers_by_agent, approvals_needed, _LAST_STRATEGY_PROPOSAL, _LAST_BUDGET_PROPOSAL)

        response = {
            "answer": answer,
            "narrative": _build_narrative_text(answers_by_agent, _LAST_BUDGET_PROPOSAL),
            "contexts": contexts,
            "trace": trace,
            "agents_used": agents,
            "approvals_needed": approvals_needed,
        }
        if _LAST_DATA_GAP is not None:
            response["data_gap_needs_confirmation"] = _LAST_DATA_GAP
        if _LAST_STRATEGY_PROPOSAL is not None:
            response["strategy_change_needs_confirmation"] = _LAST_STRATEGY_PROPOSAL
        if _LAST_BUDGET_PROPOSAL is not None:
            response["budget_change_needs_confirmation"] = _LAST_BUDGET_PROPOSAL
        if _LAST_AWAITING_INPUT is not None:
            response["awaiting_input"] = _LAST_AWAITING_INPUT
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


def confirm_strategy_change_action(confirmation_token: str) -> dict:
    """`select_strategy`가 제안한 전략 변경을 실제로 적용한다 — approvals.py의 승인ID와는 별개의
    저장소(month_state._PENDING_STRATEGY_CHANGES)를 쓰지만, 대화·라우팅을 거치지 않고 토큰만으로
    직접 처리한다는 점은 execute_approved_action과 같은 이유다(실사용 중 발견: 이 API가 무상태라
    "응 확인했어" 같은 자연어 확인은 라우팅조차 안 되므로, 확인은 반드시 이 구조화 경로로 와야 한다).
    """
    result = month_state.confirm_strategy_change(confirmation_token)
    if not result["ok"]:
        return {"http_status": 404, "error": result["error"]}
    return {"http_status": 200, **result}


def cancel_strategy_change_action(confirmation_token: str) -> dict:
    """`select_strategy`가 제안한 전략 변경을 취소한다 — 적용하지 않고 토큰만 무효화한다."""
    result = month_state.cancel_strategy_change(confirmation_token)
    if not result["ok"]:
        return {"http_status": 404, "error": result["error"]}
    return {"http_status": 200, **result}


def confirm_budget_change_action(confirmation_token: str) -> dict:
    """`set_monthly_budget`이 제안한 예산 설정/변경을 실제로 적용한다(SPEC §4-2, 2026-09-18 확정).
    존재한 적 없는 토큰은 404, 이미 확인·취소로 소진됐거나 제안 이후 상태·시점이 달라져 낡은
    제안이 된 경우는 409로 구분한다 — approvals.py/select_strategy 확인과 같은 원칙."""
    result = month_state.confirm_budget_change(confirmation_token)
    if not result["ok"]:
        status = 409 if (result.get("already_processed") or result.get("stale")) else 404
        return {"http_status": status, "error": result["error"]}
    return {"http_status": 200, **result}


def cancel_budget_change_action(confirmation_token: str) -> dict:
    """`set_monthly_budget`이 제안한 예산 설정/변경을 취소한다 — 적용하지 않고 토큰만 무효화한다."""
    result = month_state.cancel_budget_change(confirmation_token)
    if not result["ok"]:
        status = 409 if result.get("already_processed") else 404
        return {"http_status": status, "error": result["error"]}
    return {"http_status": 200, **result}
