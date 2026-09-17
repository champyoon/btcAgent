"""RAGAS 4개 지표(context_recall, context_precision, faithfulness, answer_relevancy)를
`evaluation/rag_eval_set.json`(RAGAS 전용 평가 세트 — 일반 test_queries.csv와 분리됨, 2026-09-18)
로 계산합니다.

AWS 자격증명(.env)이 준비된 뒤 실행하세요. 실제 API 호출 비용이 발생합니다.

    python evaluation/run_ragas.py

**왜 test_queries.csv에서 분리했는가**: reference를 CSV 행 번호로 연결해뒀더니, 일반 평가 문항이
바뀔 때마다(문항이 추가/삭제/순서변경) reference가 조용히 엉뚱한 질문에 붙는 사고가 실제로 두 번
있었다(2026-09-18 발견·수정, `round4_report.md` §4). `rag_eval_set.json`은 의미 있는 고정 id로
문항을 식별해 이 문제 자체를 구조적으로 없앤다.

**무엇을 평가 대상으로 삼는가**: 개념·서비스 규칙 설명 질문만 담는다(BTC/DCA/RSI가 뭔지, 하락일
조건이 뭔지, 백테스트 해석 방법 등). 실시간 가격·RSI 수치·개인 예산·선택 전략처럼 매번 값이
달라지는 질문은 여기 넣지 않는다 — RAGAS의 context_recall/precision은 고정된 reference와 비교하는
방식이라, 매번 바뀌는 값을 reference로 고정해두면 계산 자체가 성립하지 않는다. "RSI가 뭐고 지금
값은 얼마야?" 같은 복합 질문은 일반 평가(test_queries.csv)에는 그대로 두되, 여기서는 설명 부분만
분리한 문항(rag_rsi_definition)으로 다룬다.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ragas_compat  # noqa: F401,E402 — ragas import 전에 반드시 먼저 적용

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import ledger  # noqa: E402
import month_state as ms  # noqa: E402

# select_strategy(제안)/record_watch_decision은 "read" 등급이라 승인 게이트 없이 즉시 실행되므로,
# 격리 없이 돌리면 실제 장부·계획 상태가 그대로 바뀐다(run_eval.py와 같은 이유 — 이 평가 세트는
# 상태 변경 도구를 안 쓰지만 방어적으로 격리한다).
EVAL_SCRATCH = ROOT / "evaluation" / "_eval_scratch"
EVAL_SCRATCH.mkdir(exist_ok=True)
ledger.LEDGER_PATH = EVAL_SCRATCH / "ledger.json"
ms.STATE_PATH = EVAL_SCRATCH / "month_state.json"
for _p in (ledger.LEDGER_PATH, ms.STATE_PATH):
    if _p.exists():
        _p.unlink()

import agent  # noqa: E402
from ragas import evaluate  # noqa: E402
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import (  # noqa: E402
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
)

RAG_EVAL_SET_PATH = Path(__file__).parent / "rag_eval_set.json"
RESULTS_PATH = Path(__file__).parent / "rag_eval_results.json"
DOCS_DIR = ROOT / "data" / "docs"
METRIC_NAMES = ["context_recall", "context_precision", "faithfulness", "answer_relevancy"]

REQUIRED_FIELDS = ("id", "question", "reference", "source_docs")


def load_and_validate_eval_set() -> tuple[list[dict], list[dict]]:
    """평가 세트를 읽고 검증한다. 반환: (유효한 항목, 오류 항목 — 각각 {"item"|"id", "errors": [...]})

    검증 항목: 필수 필드 존재, id 중복, source_docs가 실제 data/docs/ 파일로 존재하는지.
    오류가 있는 항목은 조용히 빼지 않는다 — 호출부가 반드시 오류 목록을 같이 보고해야 한다.
    """
    raw = json.loads(RAG_EVAL_SET_PATH.read_text(encoding="utf-8"))
    items = raw.get("items", [])

    valid: list[dict] = []
    invalid: list[dict] = []
    seen_ids: set[str] = set()

    for idx, item in enumerate(items):
        errors = []
        item_id = item.get("id") or f"(id 없음, 인덱스 {idx})"

        for field in REQUIRED_FIELDS:
            value = item.get(field)
            if not value:
                errors.append(f"필수 필드 누락 또는 빈 값: {field}")

        if item.get("id") in seen_ids:
            errors.append(f"id 중복: {item.get('id')}")
        elif item.get("id"):
            seen_ids.add(item["id"])

        for doc_name in item.get("source_docs") or []:
            if not (DOCS_DIR / doc_name).exists():
                errors.append(f"근거 문서가 존재하지 않음: data/docs/{doc_name}")

        if errors:
            invalid.append({"id": item_id, "errors": errors})
        else:
            valid.append(item)

    return valid, invalid


def _git_code_version() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=10
        ).stdout.strip()
        return f"{commit}{'+dirty' if dirty else ''}" if commit else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown(git 조회 실패)"


def _eval_set_hash() -> str:
    return hashlib.sha256(RAG_EVAL_SET_PATH.read_bytes()).hexdigest()[:16]


def build_samples(items: list[dict]) -> list[dict]:
    """각 항목에 실제 응답·검색 근거를 채운 레코드를 만든다. 실행 오류도 그대로 기록한다."""
    run = agent.build_supervisor()
    records = []
    for item in items:
        record: dict = {
            "id": item["id"],
            "question": item["question"],
            "reference": item["reference"],
            "source_docs_expected": item["source_docs"],
        }
        try:
            result = run(item["question"])
            contexts = [c["text"] for c in result.get("contexts", [])]
            context_sources = sorted({c["doc_id"] for c in result.get("contexts", [])})
            record.update(
                {
                    "answer": result["answer"],
                    "contexts": contexts,
                    "context_sources": context_sources,
                    "context_count": len(contexts),
                    "error": None,
                }
            )
            if not contexts:
                record["no_context_reason"] = "retrieve_docs가 이 실행에서 문서를 반환하지 않음(검색 결과 없음 또는 도구 미호출)"
        except Exception as exc:  # noqa: BLE001 — 실행 오류도 결과 파일에 그대로 남겨야 한다
            record.update({"answer": None, "contexts": [], "context_sources": [], "context_count": 0, "error": f"{type(exc).__name__}: {exc}"})
        print(f"  #{item['id']}: contexts {record['context_count']}건, error={record['error']}")
        records.append(record)
    return records


def main() -> None:
    valid_items, invalid_items = load_and_validate_eval_set()

    print(f"=== 평가 세트 검증 ({RAG_EVAL_SET_PATH.name}) ===")
    print(f"유효 문항: {len(valid_items)}건, 오류 문항: {len(invalid_items)}건")
    if invalid_items:
        print("!! 오류가 있는 문항 — 평가에서 제외하고 아래에 그대로 보고한다 (조용히 넘기지 않음):")
        for bad in invalid_items:
            print(f"   - {bad['id']}: {bad['errors']}")

    if not valid_items:
        print("유효한 평가 문항이 없어 종료합니다.")
        return

    records = build_samples(valid_items)
    runnable = [r for r in records if r["error"] is None and r["contexts"]]
    skipped_no_context = [r for r in records if r["error"] is None and not r["contexts"]]
    errored = [r for r in records if r["error"] is not None]

    samples = [
        SingleTurnSample(
            user_input=r["question"], response=r["answer"], retrieved_contexts=r["contexts"], reference=r["reference"]
        )
        for r in runnable
    ]

    metadata = {
        "executed_at_kst": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "code_version": _git_code_version(),
        "eval_set_hash": _eval_set_hash(),
        "eval_set_file": str(RAG_EVAL_SET_PATH.relative_to(ROOT)),
        "response_model": agent.MODEL_ID,
        "judge_model": agent.MODEL_ID,  # llm_as_judge/RAGAS 둘 다 agent._default_llm()을 그대로 재사용
        "embedding_model": None,  # 아래에서 채움
    }

    df = None
    if samples:
        dataset = EvaluationDataset(samples)
        llm = LangchainLLMWrapper(agent._default_llm())

        from langchain_aws import BedrockEmbeddings
        import retriever

        metadata["embedding_model"] = retriever.EMBED_MODEL_ID
        embeddings = LangchainEmbeddingsWrapper(
            BedrockEmbeddings(model_id=retriever.EMBED_MODEL_ID, region_name=retriever.REGION)
        )
        eval_result = evaluate(
            dataset,
            metrics=[context_recall, context_precision, faithfulness, answer_relevancy],
            llm=llm,
            embeddings=embeddings,
        )
        df = eval_result.to_pandas()
        for i, r in enumerate(runnable):
            for col in METRIC_NAMES:
                r[col] = None if col not in df.columns else (None if pandas_isna(df[col].iloc[i]) else float(df[col].iloc[i]))
    else:
        import retriever

        metadata["embedding_model"] = retriever.EMBED_MODEL_ID

    for r in skipped_no_context + errored:
        for col in METRIC_NAMES:
            r[col] = None

    print(f"\n=== 평가 대상 수·누락 ===")
    print(f"CSV(JSON)상 유효 문항: {len(valid_items)}건")
    print(f"검증 실패로 제외: {len(invalid_items)}건")
    print(f"실행 오류: {len(errored)}건 {[r['id'] for r in errored]}")
    print(f"검색 근거 없음(NaN 처리): {len(skipped_no_context)}건 {[r['id'] for r in skipped_no_context]}")
    print(f"실제 지표 계산된 문항: {len(runnable)}건")

    print("\n=== 지표별 유효 평가 수·평균 ===")
    for col in METRIC_NAMES:
        vals = [r.get(col) for r in records if r.get(col) is not None]
        valid_count = len(vals)
        mean_val = sum(vals) / valid_count if valid_count else float("nan")
        print(f"{col}: 유효 {valid_count}/{len(records)}건, 평균(유효값만) {mean_val:.2f}" if valid_count else f"{col}: 유효 0건")

    print("\n=== 문항별 상세 ===")
    for r in records:
        scores = " ".join(f"{c}={r.get(c):.2f}" if r.get(c) is not None else f"{c}=NaN" for c in METRIC_NAMES)
        print(f"  #{r['id']} | {r['question']} | {scores}")

    output = {
        "metadata": metadata,
        "invalid_items": invalid_items,
        "results": records,
    }
    RESULTS_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n전체 결과 저장: {RESULTS_PATH}")


def pandas_isna(v) -> bool:
    try:
        import math

        return isinstance(v, float) and math.isnan(v)
    except Exception:  # noqa: BLE001
        return v is None


if __name__ == "__main__":
    main()
