"""RAG 인덱스 재사용/재생성 기준 — 청크 개수가 아니라 문서 내용·청킹 설정·임베딩 모델의 지문으로
변경을 감지한다(2026-09-18 보완: "청크 수 일치 시 재사용"만으로는 같은 길이의 다른 문장으로
바뀐 문서 변경을 못 잡는다는 지적).

실제 AWS 임베딩 호출 없이 결정적 가짜 임베딩으로 검증한다 — 이 테스트는 "언제 재사용하고 언제
다시 만드는가"라는 로직 자체를 확인하는 것이 목적이라, 임베딩 벡터의 의미론적 품질은 무관하다.
"""

from __future__ import annotations

import hashlib

import retriever


class _FakeEmbeddings:
    """텍스트 해시 기반의 결정적 가짜 임베딩 — 같은 텍스트는 항상 같은 벡터, 실제 AWS 호출 없음."""

    model_id = "fake-embed-v1"

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:8]]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def _write_doc(dir_path, name: str, content: str) -> str:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def _setup(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    persist_dir = tmp_path / "chroma"
    monkeypatch.setattr(retriever, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(retriever, "PERSIST_DIR", str(persist_dir))
    return docs_dir, persist_dir


def test_no_duplication_on_repeated_restart_with_unchanged_docs(tmp_path, monkeypatch):
    docs_dir, _ = _setup(tmp_path, monkeypatch)
    _write_doc(docs_dir, "a.md", "첫 번째 문서 내용입니다. " * 20)
    _write_doc(docs_dir, "b.md", "두 번째 문서 내용입니다. " * 20)
    embeddings = _FakeEmbeddings()

    vs1 = retriever.build_vectorstore(embeddings=embeddings)
    count1 = vs1._collection.count()
    assert count1 > 0

    # "재시작"을 흉내낸다 — 같은 문서로 build_vectorstore를 다시 호출(이전에는 이때마다 중복 추가됐다)
    vs2 = retriever.build_vectorstore(embeddings=embeddings)
    count2 = vs2._collection.count()
    vs3 = retriever.build_vectorstore(embeddings=embeddings)
    count3 = vs3._collection.count()

    assert count1 == count2 == count3  # 중복 증가 없음


def test_same_chunk_count_but_changed_content_is_detected_and_reflected_in_search(tmp_path, monkeypatch):
    docs_dir, _ = _setup(tmp_path, monkeypatch)
    # 길이를 맞춰 청크 수가 똑같이 나오도록 같은 글자 수의 문장으로 구성한다.
    original = "사과바나나딸기포도수박" * 10  # 11자 x 10
    _write_doc(docs_dir, "a.md", original)
    embeddings = _FakeEmbeddings()

    vs1 = retriever.build_vectorstore(embeddings=embeddings)
    count_before = vs1._collection.count()
    got_before = vs1._collection.get()
    docs_before = set(got_before["documents"])
    assert any("사과" in d for d in docs_before)

    # 같은 글자 수(따라서 같은 청크 개수가 되도록)의 완전히 다른 내용으로 교체한다.
    replaced = "우주비행선탐사대원임무" * 10
    assert len(replaced) == len(original)
    _write_doc(docs_dir, "a.md", replaced)

    vs2 = retriever.build_vectorstore(embeddings=embeddings)
    count_after = vs2._collection.count()
    got_after = vs2._collection.get()
    docs_after = set(got_after["documents"])

    assert count_after == count_before  # 청크 개수는 우연히 같을 수 있다
    assert not any("사과" in d for d in docs_after)  # 그래도 옛 내용은 완전히 사라져야 한다
    assert any("우주" in d for d in docs_after)  # 새 내용이 반영돼야 한다


def test_document_deletion_removes_its_chunks(tmp_path, monkeypatch):
    docs_dir, _ = _setup(tmp_path, monkeypatch)
    _write_doc(docs_dir, "a.md", "첫 번째 문서 고유 표식 ALPHA. " * 20)
    _write_doc(docs_dir, "b.md", "두 번째 문서 고유 표식 BETA. " * 20)
    embeddings = _FakeEmbeddings()

    vs1 = retriever.build_vectorstore(embeddings=embeddings)
    got1 = vs1._collection.get()
    assert any("ALPHA" in d for d in got1["documents"])
    assert any("BETA" in d for d in got1["documents"])

    (docs_dir / "a.md").unlink()  # 문서 삭제

    vs2 = retriever.build_vectorstore(embeddings=embeddings)
    got2 = vs2._collection.get()
    assert not any("ALPHA" in d for d in got2["documents"])  # 삭제된 문서의 청크는 제거됨
    assert any("BETA" in d for d in got2["documents"])  # 남은 문서는 그대로


def test_fingerprint_reflects_content_and_embedding_model(tmp_path):
    doc = tmp_path / "x.md"
    doc.write_text("동일한 내용", encoding="utf-8")

    a = retriever.compute_index_fingerprint([str(doc)], _FakeEmbeddings())
    a_again = retriever.compute_index_fingerprint([str(doc)], _FakeEmbeddings())
    assert a == a_again  # 같은 내용·같은 임베딩 모델 -> 같은 지문

    doc.write_text("다른 내용으로 교체", encoding="utf-8")
    b = retriever.compute_index_fingerprint([str(doc)], _FakeEmbeddings())
    assert a != b  # 내용이 바뀌면 지문도 바뀐다

    class _OtherEmbeddings(_FakeEmbeddings):
        model_id = "fake-embed-v2"

    doc.write_text("동일한 내용", encoding="utf-8")
    c = retriever.compute_index_fingerprint([str(doc)], _OtherEmbeddings())
    assert a != c  # 내용은 같아도 임베딩 모델이 다르면 지문도 다르다
