"""assess_retrieval()의 키워드 일치율 게이트 — 실사용 UI 신고(2026-09-20, #6) 라이브 검증 중 발견.

재현: "비트코인에 대해 설명해줘"는 라우팅도 정확했고(research_agent) 임베딩 검색도 BTC.md의
정확한 청크를 실제로 찾았는데, 모델이 retrieve_docs를 "비트코인 개념 정의"라는 쿼리로 호출하는
바람에 assess_retrieval의 키워드 일치율 게이트에서 걸러졌다("관련 문서를 찾지 못했습니다") —
"개념"/"정의"가 질문 형식을 나타내는 메타 단어일 뿐인데 명사라서 핵심어 집합에 들어가 일치율
분모를 부풀렸다.

Kiwi 형태소 분석기만 쓰고 AWS/임베딩 호출은 없다 — 결정적이고 빠르다.
"""

from __future__ import annotations

from langchain_core.documents import Document

import retriever


def test_meta_nouns_excluded_from_keywords():
    keywords = retriever._keywords("비트코인 개념 정의")
    assert "비트코인" in keywords
    assert "개념" not in keywords
    assert "정의" not in keywords


def test_explanation_query_word_does_not_dilute_real_topic_match():
    """실제 재현 사례 — "비트코인 개념 정의" 쿼리로 BTC.md 청크를 검색했을 때, "개념"/"정의"가
    핵심어에서 빠져 진짜 주제어("비트코인")만으로 일치율 100%가 되어 통과해야 한다."""
    docs = [
        Document(
            page_content=(
                "비트코인은 특정 회사나 정부가 아니라 전 세계에 분산된 컴퓨터 네트워크가 함께 "
                "기록·검증하는 디지털 자산입니다. 발행량 상한이 정해져 있습니다."
            ),
            metadata={"source": "BTC.md"},
        )
    ]
    verdict = retriever.assess_retrieval(docs, "비트코인 개념 정의")
    assert verdict["usable"] is True


def test_genuinely_unrelated_query_is_still_rejected():
    """실제 주제어 자체가 문서 본문과 안 겹치면(메타 단어 제외와 무관하게) 여전히 거부돼야 한다 —
    이번 수정이 게이트를 무력화하는 쪽으로 번지지 않았는지 확인."""
    docs = [
        Document(
            page_content="정기 분할 매수는 매월 1일과 15일에 나눠 매수하는 방식입니다.",
            metadata={"source": "dca_strategy.md"},
        )
    ]
    verdict = retriever.assess_retrieval(docs, "오늘 날씨 어때")
    assert verdict["usable"] is False


def test_empty_keywords_after_meta_noun_removal_is_not_usable():
    """질의어가 전부 메타 단어뿐이면(예: "설명해줘") 실제 핵심어가 하나도 안 남는다 — 이 경우
    빈 핵심어 집합으로 처리돼 usable=False가 나와야 한다(임의로 통과시키지 않는다)."""
    docs = [Document(page_content="비트코인은 디지털 자산입니다.", metadata={"source": "BTC.md"})]
    verdict = retriever.assess_retrieval(docs, "설명해줘")
    assert verdict["usable"] is False
