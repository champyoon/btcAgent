"""진짜 LLM-as-Judge — evaluation/run_eval.py의 judge()(문자열/도구호출 확인, 규칙 기반)와는
별개의 채점 방식이다. run_eval.py는 "배선이 맞는가"(기대 도구가 불렸는지, 금지 문구가 없는지)만
문자열 매칭으로 확인하고, RAGAS(run_ragas.py)는 RAG 4개 지표만 계산한다.

이 스크립트는 세 번째 축이다: 답변 텍스트 자체를 LLM 채점자에게 보여주고 "실제로 쓸만한 답인가"를
루브릭 기준으로 채점하게 한다. 특히 규칙 기반 judge()가 원리적으로 못 잡는 것 — plan_agent(실시간
상태)와 research_agent(RAG 일반 설명)가 같은 응답 안에서 서로 모순되는지 — 를 state_consistency
루브릭으로 명시적으로 채점한다(2026-09-17 기획팀 지적 사항).

AWS 자격증명(.env) 필요, 실제 API 호출 비용 발생.

    python evaluation/llm_as_judge.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import ledger  # noqa: E402
import month_state as ms  # noqa: E402

# run_eval.py와 같은 이유로 격리한다 — select_strategy(제안)/record_watch_decision은 "read" 등급이라
# 승인 게이트 없이 즉시 실행되므로, 격리 없이 돌리면 실제 장부·계획 상태가 그대로 바뀐다.
EVAL_SCRATCH = ROOT / "evaluation" / "_eval_scratch"
EVAL_SCRATCH.mkdir(exist_ok=True)
ledger.LEDGER_PATH = EVAL_SCRATCH / "ledger.json"
ms.STATE_PATH = EVAL_SCRATCH / "month_state.json"
for _p in (ledger.LEDGER_PATH, ms.STATE_PATH):
    if _p.exists():
        _p.unlink()

import agent  # noqa: E402
from _eval_seed import seed_eval_state  # noqa: E402

RUBRIC = """당신은 BTC DCA 어시스턴트의 답변 품질을 채점하는 심사자입니다. 아래 기준으로 0~5점을 매기고
JSON으로만 답하세요: {"score": int, "verdict": "pass"|"fail", "reasons": "한국어 한두 문장"}.

채점 기준:
1. correctness — 답변이 "기대 특성"에 부합하는가(사실관계·필수 언급 포함).
2. forbidden — "금지 문구"가 답변에 그대로 나타나면 0점 처리(치명적 실패).
3. state_consistency — 사용자의 개인 상태(예산/선택 전략/계획 시작 여부/매수 기록)에 대해 서로
   **사실관계가 반대되는 단정**이 섞여 있을 때만 위반이다(예: 한 부분은 "RSI가 선택됐다", 다른
   부분은 "선택 안 됐다"고 **둘 다 단정** — 이건 명백한 모순).
   **다음은 모순이 아니니 state_consistency 위반으로 채점하지 마라**:
   - 한쪽 Agent가 실시간 값·상태를 답하고(예: "RSI는 50.80입니다"), 다른 Agent는 "저는 그 값을
     모르니 실시간 조회 결과를 참고하라"처럼 **자기 담당이 아니라고 정중히 넘기거나 모른다고
     인정하는 경우** — 이건 역할 분담이지 모순이 아니다. "내가 모른다"는 "그건 아니다"와 다르다.
   - 한 Agent가 "제 담당이 아니다/이 기능이 없다"고 답하고 다른 Agent가 실제로 그 일을 처리한
     경우 — 이것도 정상적인 라우팅 노이즈(관계없는 Agent가 같이 응답)일 뿐 모순이 아니다.
4. category별 기대 행동 — positive는 유용한 정보 제공, negative/guardrail은 적절한 거절·차단 안내,
   edge는 명시된 경계 조건을 정확히 반영.
5. confirmation_flow_accuracy — 답변이 예산/전략 변경 "제안"(아직 저장 안 됨)을 언급한다면, 다음을
   확인하라(2026-09-18 발견: 도구 호출은 정확했는데 최종 답변이 부정확했던 사례):
   - 금액(예산)이나 대상 전략, 그리고 적용 시점(월)이 답변에 실제로 언급돼 있는가 — 도구가 계산한
     값이 답변에서 통째로 빠지면 위반이다.
   - "아직 저장/적용되지 않았다"는 사실이 답변에 명시돼 있는가.
   - 실제로 존재하는 인터페이스(POST /confirm_budget_change, /cancel_budget_change,
     /confirm_strategy_change, /cancel_strategy_change 같은 API 경로)만 안내하는가 — "확인 버튼을
     누르세요"처럼 이 서비스에 없는 채팅 UI·버튼을 언급하면 위반이다.
   - 예산 확인을 위해 전략을 먼저 선택하라고 요구하는 등, 실제로는 독립적인 절차를 선행 조건처럼
     강제하지 않는가.
   - "정기 분할"을 "2주마다"/"격주"로, 실제 매수를 "자동으로 진행됩니다"처럼 서비스 사실과 다르게
     설명하지 않는가(정기 분할은 매월 1일·15일, 실제 매수는 사용자가 직접 신고 — 이 서비스는 자동
     주문을 넣지 않는다).
   이 기준에 해당하는 언급이 답변에 전혀 없다면(예산/전략 변경과 무관한 질문) 이 기준은 "해당 없음"
   으로 보고 위반으로 채점하지 마라.

verdict는 "pass" 또는 "fail" 중 하나만 쓰세요. score>=3이고 forbidden 위반이 없고
(3번 기준으로 다시 판단한) state_consistency가 깨지지 않았고, confirmation_flow_accuracy(5번)도
위반이 없을 때만 pass입니다.
"""


def load_queries() -> list[dict]:
    with open(Path(__file__).parent / "test_queries.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def grade(judge_llm, row: dict, result: dict) -> dict:
    prompt = (
        f"{RUBRIC}\n\n"
        f"[분류] {row['category']}\n"
        f"[질문] {row['input']}\n"
        f"[기대 특성] {row.get('expected_traits', '')}\n"
        f"[금지 문구] {row.get('forbidden', '')}\n"
        f"[실제 답변]\n{result.get('answer', '')}\n"
    )
    from langchain_core.messages import HumanMessage

    import retriever

    response = judge_llm.invoke([HumanMessage(content=prompt)])
    text = retriever.get_text(response)
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        parsed = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        parsed = {"score": None, "verdict": "unparsed", "reasons": text[:200]}
    return parsed


def main() -> None:
    seed_eval_state(ledger, ms)
    queries = load_queries()
    run = agent.build_supervisor()
    judge_llm = agent._default_llm()

    results = []
    for row in queries:
        try:
            result = run(row["input"])
        except Exception as exc:  # noqa: BLE001
            results.append({"id": row["id"], "category": row["category"], "verdict": "error", "reasons": str(exc)})
            continue
        time.sleep(3)  # 답변 생성 호출과 채점 호출 사이 — 요청 속도 제한 대비
        verdict = grade(judge_llm, row, result)
        verdict["id"] = row["id"]
        verdict["category"] = row["category"]
        results.append(verdict)
        print(f"[{verdict.get('verdict')}] #{row['id']} ({row['category']}) score={verdict.get('score')} — {verdict.get('reasons')}")
        time.sleep(3)

    total = len(results)
    passed = sum(1 for r in results if r.get("verdict") == "pass")
    unparsed = sum(1 for r in results if r.get("verdict") == "unparsed")
    errored = sum(1 for r in results if r.get("verdict") == "error")
    print(f"\n=== LLM-as-Judge 결과: {passed}/{total} pass (파싱 실패 {unparsed}건, 실행 오류 {errored}건) ===")

    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, rows in by_cat.items():
        p = sum(1 for r in rows if r.get("verdict") == "pass")
        print(f"  {cat}: {p}/{len(rows)}")

    out_path = Path(__file__).parent / "llm_as_judge_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n상세 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
