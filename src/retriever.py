"""BTC DCA 어시스턴트 — RAG 파이프라인.

Day2(청킹·검색·근거 반환·검색 게이트) 패턴을 이 도메인 문서(data/docs/*.md)에 그대로 적용합니다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_text_splitters import RecursiveCharacterTextSplitter

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
REGION = "us-east-1"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = PROJECT_ROOT / "data" / "docs"
PERSIST_DIR = str(PROJECT_ROOT / "chroma_db")
COLLECTION = "btc_dca_docs"

CHUNK_SIZE = 400
CHUNK_OVERLAP = 50

DOC_META = {
    "BTC.md": {"category": "btc", "updated_at": "2026-09-16"},
    "DCA.md": {"category": "dca", "updated_at": "2026-09-16"},
    "RSI.md": {"category": "rsi", "updated_at": "2026-09-16"},
    "market_indicators.md": {"category": "indicators", "updated_at": "2026-09-16"},
    "dca_strategy.md": {"category": "strategy", "updated_at": "2026-09-16"},
    "backtest_guide.md": {"category": "backtest", "updated_at": "2026-09-16"},
    "service_rules.md": {"category": "service_rules", "updated_at": "2026-09-16"},
    "risk_management.md": {"category": "risk", "updated_at": "2026-09-08"},
}
_DEFAULT_DOC_META = {"category": "unknown", "updated_at": "unknown"}


def _default_llm():
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(model=MODEL_ID, region_name=REGION, temperature=0)


def _default_embeddings():
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(model_id=EMBED_MODEL_ID, region_name=REGION)


def get_text(message: Any) -> str:
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환합니다."""
    content = message.content if hasattr(message, "content") else message
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)


def format_docs(docs) -> str:
    """검색된 문서를 프롬프트에 넣을 문자열로 만듭니다."""
    return "\n\n".join(
        f"[출처: {d.metadata.get('source', '알 수 없음')}]\n{d.page_content}" for d in docs
    )


def build_chunks(doc_paths: list[str]) -> list[Document]:
    """문서를 읽어 메타데이터를 붙인 뒤 청크로 쪼갭니다. (메타데이터는 분할 전에 부착 → 상속됨)"""
    docs = []
    for p in doc_paths:
        path = Path(p)
        meta = {"source": path.name, **DOC_META.get(path.name, _DEFAULT_DOC_META)}
        docs.append(Document(page_content=path.read_text(encoding="utf-8"), metadata=meta))

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    return splitter.split_documents(docs)


def _embedding_model_fingerprint(embeddings: Any) -> str:
    """임베딩 모델을 식별할 문자열 — 보통 `model_id`(BedrockEmbeddings) 속성을 쓰고, 없으면
    클래스 이름으로 대체한다(완전히 다른 임베딩 구현으로 바뀌기만 해도 인덱스를 무효화하기 위함)."""
    return str(getattr(embeddings, "model_id", None) or type(embeddings).__name__)


def compute_index_fingerprint(doc_paths: list[str], embeddings: Any) -> str:
    """문서 내용·청킹 설정·임베딩 모델을 전부 해시에 반영한 지문. 셋 중 하나라도 바뀌면 값이
    달라진다 — "청크 개수가 같다"는 것만으로는 문서 *내용*이 바뀐 걸 못 잡기 때문에(2026-09-18
    지적: 같은 길이의 다른 문장으로 교체돼도 청크 수는 그대로일 수 있다), 개수 비교 대신 이 지문을
    저장해두고 비교한다.
    """
    hasher = hashlib.sha256()
    for p in sorted(doc_paths, key=lambda x: Path(x).name):
        path = Path(p)
        hasher.update(path.name.encode("utf-8"))
        hasher.update(path.read_bytes())
    hasher.update(f"chunk_size={CHUNK_SIZE},chunk_overlap={CHUNK_OVERLAP}".encode("utf-8"))
    hasher.update(_embedding_model_fingerprint(embeddings).encode("utf-8"))
    return hasher.hexdigest()


def _index_meta_path() -> Path:
    # PERSIST_DIR 기준으로 매번 새로 계산한다(모듈 로드 시점에 고정하면 테스트가 PERSIST_DIR을
    # monkeypatch해도 반영이 안 된다).
    return Path(PERSIST_DIR) / "_index_meta.json"


def _load_index_meta() -> dict | None:
    path = _index_meta_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # 손상된 메타 파일은 "모름"으로 취급 — 아래에서 안전하게 재생성으로 이어진다


def _save_index_meta(fingerprint: str, chunk_count: int) -> None:
    path = _index_meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fingerprint": fingerprint, "chunk_count": chunk_count}, ensure_ascii=False),
        encoding="utf-8",
    )


def build_vectorstore(doc_paths: list[str] | None = None, embeddings=None):
    """docs/*.md를 실제로 임베딩해 Chroma 벡터스토어를 만듭니다. (AWS 자격증명 필요)

    저장된 지문(`_index_meta.json`)이 지금 문서 내용·청킹 설정·임베딩 모델과 정확히 일치하고
    컬렉션의 실제 청크 수도 그때 저장해둔 수와 같을 때만 재사용한다 — 그중 하나라도 다르면(문서
    내용이 바뀌었거나, 문서가 추가/삭제됐거나, 청킹/임베딩 설정이 바뀌었거나, 컬렉션이 예상 밖으로
    변했거나) 컬렉션을 통째로 비우고 처음부터 다시 채운다.

    **2026-09-18 1차 수정 — 청크 개수만 비교**: 예전엔 항상 `Chroma.from_documents(...)`로 무조건
    추가만 했는데, `_shared_retriever()`가 프로세스를 새로 시작할 때마다 이 함수를 다시 부르고
    `persist_directory`는 그대로 남아있어서, 매번 같은 43개 청크가 기존 컬렉션 위에 그대로 또
    쌓였다 — 실제로 이 프로젝트의 반복된 테스트 세션들을 거치며 1,118개(43개의 약 26배)까지
    쌓여 있었다. 그래서 "청크 개수가 일치하면 재사용"으로 1차 수정했다.

    **2026-09-18 2차 보완 — 개수만으로는 부족함**: 청크 개수가 같아도 문서 *내용*이 바뀔 수
    있다(예: 같은 길이의 다른 문장으로 교체). 그래서 개수 대신 문서 내용+청킹 설정+임베딩 모델의
    해시(지문)로 변경을 감지하도록 바꿨다 — `compute_index_fingerprint()`.
    """
    from langchain_chroma import Chroma

    doc_paths = doc_paths or sorted(str(p) for p in DOCS_DIR.glob("*.md"))
    embeddings = embeddings or _default_embeddings()
    expected_chunks = build_chunks(doc_paths)
    fingerprint = compute_index_fingerprint(doc_paths, embeddings)

    vectorstore = Chroma(
        collection_name=COLLECTION, persist_directory=PERSIST_DIR, embedding_function=embeddings
    )
    meta = _load_index_meta()
    current_count = vectorstore._collection.count()
    if (
        meta is not None
        and meta.get("fingerprint") == fingerprint
        and meta.get("chunk_count") == len(expected_chunks)
        and current_count == len(expected_chunks)
    ):
        return vectorstore  # 지문·개수·실제 컬렉션 크기가 전부 일치 — 재사용, 다시 추가하지 않는다

    # 셋 중 하나라도 다르면(최초 생성, 문서 내용 변경, 문서 추가/삭제, 설정 변경, 예전 중복 누적 등)
    # 일부만 다시 넣지 않고 컬렉션을 통째로 비운 뒤 처음부터 다시 채운다 — "부분 갱신"은 무엇이
    # 이미 있고 무엇이 없는지 추적해야 해서 더 복잡하고, 이 문서 세트는 그 정도로 자주/부분적으로
    # 바뀌지 않는다.
    vectorstore.delete_collection()
    vs = Chroma.from_documents(
        expected_chunks, embeddings, collection_name=COLLECTION, persist_directory=PERSIST_DIR
    )
    _save_index_meta(fingerprint, len(expected_chunks))
    return vs


def build_retriever(vectorstore):
    """주어진 벡터스토어에서 retriever를 만듭니다. 문서 8종·청크 40여 개라 k=4로 절충."""
    return vectorstore.as_retriever(search_kwargs={"k": 4})


_KIWI = None


def _kiwi():
    global _KIWI
    if _KIWI is None:
        from kiwipiepy import Kiwi

        _KIWI = Kiwi()
    return _KIWI


_NOUN_TAGS = {"NNG", "NNP", "NNB", "SL", "SN"}


def _keywords(text: str) -> set[str]:
    """텍스트에서 명사류 핵심어만 뽑습니다."""
    return {t.form for t in _kiwi().tokenize(text) if t.tag in _NOUN_TAGS and len(t.form) > 1}


def assess_retrieval(docs, question: str) -> dict:
    """검색 결과가 이 질문에 답하기 쓸만한지 LLM 없이 결정적으로 판정합니다."""
    if not docs:
        return {"usable": False, "reason": "검색된 문서가 없습니다.", "matched": 0}

    keywords = _keywords(question)
    if not keywords:
        return {"usable": False, "reason": "질문에서 핵심어를 찾지 못했습니다.", "matched": 0}

    body_keywords = _keywords(" ".join(d.page_content for d in docs))
    matched = len(keywords & body_keywords)
    ratio = matched / len(keywords)
    usable = matched >= 1 and ratio >= 0.4

    reason = f"질의어 {len(keywords)}개 중 {matched}개가 문서 본문에서 발견됨 (일치율 {ratio:.0%})"
    return {"usable": usable, "reason": reason, "matched": matched}


NO_ANSWER = {
    "answer": "제공된 문서에서 답을 찾을 수 없습니다. 투자 판단은 본인 책임 하에 신중히 하세요.",
    "sources": [],
}


def build_rag_chain(retriever, llm=None):
    """질문 문자열을 받아 {"answer": str, "sources": list[str]} 를 반환하는 체인을 만듭니다."""
    llm = llm or _default_llm()

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "당신은 비트코인 DCA(분할매수) 전략을 설명하는 어시스턴트입니다. "
                "아래 컨텍스트에 있는 내용만 근거로 한국어로 간결히 답하고, "
                "확정적인 투자 조언(무조건 오른다/지금이 마지막 기회다 등)은 하지 마세요. "
                "컨텍스트에 없는 내용은 답하지 마세요.",
            ),
            ("human", "컨텍스트:\n{context}\n\n질문: {question}"),
        ]
    )
    chain = prompt | llm

    def _run(question: str) -> dict:
        docs = retriever.invoke(question)
        verdict = assess_retrieval(docs, question)
        if not verdict["usable"]:
            return dict(NO_ANSWER)

        message = chain.invoke({"context": format_docs(docs), "question": question})

        sources = []
        for d in docs:
            src = d.metadata.get("source")
            if src and src not in sources:
                sources.append(src)

        return {"answer": get_text(message), "sources": sources}

    return RunnableLambda(_run)


_RETRIEVER = None


def _shared_retriever():
    """도구(retrieve_docs)에서 쓸 retriever를 최초 호출 시 1회만 만들어 재사용합니다."""
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = build_retriever(build_vectorstore())
    return _RETRIEVER


def search_docs(query: str) -> list[Document]:
    """질문과 관련된 문서 청크를 검색합니다. 쓸 만하지 않으면 빈 리스트를 반환합니다.

    agent.py가 이 함수로 원본 Document를 받아 API 응답의 contexts(doc_id/text)를 채웁니다.
    """
    docs = _shared_retriever().invoke(query)
    verdict = assess_retrieval(docs, query)
    return docs if verdict["usable"] else []


def retrieve_docs(query: str) -> str:
    """BTC·DCA·지표·전략·백테스트·서비스 규칙·리스크 관리 문서에서 질문과 관련된 내용을 검색해
    반환합니다. (read, AWS 자격증명 필요)"""
    docs = search_docs(query)
    if not docs:
        return "관련 문서를 찾지 못했습니다."
    return format_docs(docs)
