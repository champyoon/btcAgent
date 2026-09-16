"""test_queries.csv 중 RAG(retrieve_docs)로 답하는 positive 문항을 실제 Supervisor로 돌려
RAGAS 4개 지표(context_recall, context_precision, faithfulness, answer_relevancy)를 계산합니다.

AWS 자격증명(.env)이 준비된 뒤 실행하세요. 실제 API 호출 비용이 발생합니다.

    python evaluation/run_ragas.py

RAGAS의 context_recall/context_precision은 "정답(reference)"이 있어야 채점할 수 있는데,
test_queries.csv의 §4-4 스키마에는 그런 컬럼이 없습니다(expected_traits는 채점 힌트일 뿐 정답
문장이 아님). 그래서 이 스크립트 안에서만 쓰는 별도의 소규모 reference 정답 세트(_REFERENCES)를
data/docs/*.md 원문 그대로에서 뽑아 둡니다 — CSV 스키마 자체는 공식 §4-4 7컬럼을 그대로 유지합니다.

RAG로 답하지 않는 문항(가격 조회·매수 기록 등)은 retrieved_contexts가 항상 비어 있어 이 4개
지표를 계산할 근거가 없으므로, expected_tools에 retrieve_docs가 포함된 positive 문항으로만
범위를 좁혔습니다.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ragas_compat  # noqa: F401,E402 — ragas import 전에 반드시 먼저 적용

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

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

# data/docs/dca_strategy.md, glossary.md 원문 그대로 옮긴 정답 문장 (RAGAS reference 전용, CSV엔 없음)
_REFERENCES: dict[str, str] = {
    "1": (
        "DCA(Dollar Cost Averaging, 분할매수)는 정해진 금액을 정해진 주기(예: 매주, 매월)에 나눠서 "
        "매수하는 전략으로, 매수 시점을 분산시켜 평균 매입 단가를 시장 평균에 가깝게 만드는 것이 목적입니다."
    ),
    "2": "RSI(14일)가 30 이하면 과매도, 70 이상이면 과매수 구간으로 해석합니다.",
    "3": "200일 이동평균 괴리율이 -15% 이하면 강한 저점 신호로 봅니다.",
    "6": "52주 최고가 대비 하락률(드로다운)이 -25% 이하를 조정 구간으로 봅니다.",
}


def load_rag_rows() -> list[dict]:
    with open(Path(__file__).parent / "test_queries.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [
        r
        for r in rows
        if r["category"] == "positive" and "retrieve_docs" in (r.get("expected_tools") or "")
    ]


def build_samples(rows: list[dict]) -> list[SingleTurnSample]:
    run = agent.build_supervisor()
    samples = []
    for row in rows:
        qid = row["id"]
        if qid not in _REFERENCES:
            print(f"  (건너뜀: #{qid} — reference 정답이 없음)")
            continue
        result = run(row["input"])
        contexts = [c["text"] for c in result.get("contexts", [])]
        print(f"  #{qid}: contexts {len(contexts)}건, answer {len(result['answer'])}자")
        samples.append(
            SingleTurnSample(
                user_input=row["input"],
                response=result["answer"],
                retrieved_contexts=contexts,
                reference=_REFERENCES[qid],
            )
        )
    return samples


def main() -> None:
    rows = load_rag_rows()
    print(f"RAG 대상 문항 {len(rows)}건: {[r['id'] for r in rows]}")
    samples = build_samples(rows)
    if not samples:
        print("평가할 샘플이 없습니다 (모두 건너뜀).")
        return

    dataset = EvaluationDataset(samples)
    llm = LangchainLLMWrapper(agent._default_llm())

    from langchain_aws import BedrockEmbeddings
    import retriever

    embeddings = LangchainEmbeddingsWrapper(
        BedrockEmbeddings(model_id=retriever.EMBED_MODEL_ID, region_name=retriever.REGION)
    )

    result = evaluate(
        dataset,
        metrics=[context_recall, context_precision, faithfulness, answer_relevancy],
        llm=llm,
        embeddings=embeddings,
    )
    print("\n=== RAGAS 결과 (평균) ===")
    df = result.to_pandas()
    for col in ["context_recall", "context_precision", "faithfulness", "answer_relevancy"]:
        if col in df.columns:
            print(f"{col}: {df[col].mean():.2f}")
    print("\n문항별 상세:")
    print(df[["user_input", "context_recall", "context_precision", "faithfulness", "answer_relevancy"]])


if __name__ == "__main__":
    main()
