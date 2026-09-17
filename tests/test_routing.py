"""route_question()의 키워드 라우팅 — 순수 함수, LLM 호출 없음.

수동(Swagger) 테스트 중 발견(2026-09-18, 1차): "처음 쓰는데 어떤 서비스야? 매달 200만원씩 투자하고
싶어."가 어떤 _AGENT_KEYWORDS에도 안 걸려 route_question이 빈 목록을 반환했고, 그 결과 agent.run()이
"BTC 예산·전략 계획·시세·지표·매수 기록에 대해서만 답할 수 있습니다"로 최초 이용 질문 전체를
거절했다 — 서비스 소개(research_agent)와 예산 설정 의도(plan_agent) 둘 다 실제로는 지원 범위
안이었는데 라우팅 단계에서 아예 못 들어간 것이다. "서비스"/"사용법"(research_agent)을 키워드에
추가해 고쳤다.

**2차 지적(2026-09-18)**: 1차 수정에서 예산 의도 감지에 "만원" 리터럴 키워드를 썼는데, 이건 "예산
설정 의도"와 동치가 아니다 — "BTC 1만원이면 얼마나 살 수 있어?"(순수 조회/계산 질문)에도 "만원"이
들어있어 plan_agent가 불필요하게 매칭됐고, 반대로 "매달 2,000,000원씩 투자할래"(쉼표 표기)·"매달
200만 원씩 투자할래"(띄어쓰기)는 "만원"과 문자열이 안 맞아 전혀 안 걸렸다. `_KRW_AMOUNT_RE`(쉼표·
띄어쓰기 모두 허용하는 금액 정규식) + `_RECURRING_CADENCE_WORDS`("매달"/"한 달에" 등 반복 주기
단어) **조합**으로 바꿔, "금액 표현이 있다"와 "그 금액으로 예산을 설정/변경하려는 의도가 있다"를
구분한다. 라우팅 매칭 여부와 별개로 "실제로 쓰기 도구(set_monthly_budget)가 호출되는지"는 순수
함수 테스트로 볼 수 없어 `evaluation/manual_test_guide.md`에 실제 `/query` 재현 절차로 남겼다
(에이전트에 전달되는 것과 쓰기 도구가 실행되는 것은 다른 층위).
"""

from __future__ import annotations

import agent


def test_original_reported_sentence_is_no_longer_rejected():
    matched = agent.route_question("처음 쓰는데 어떤 서비스야? 매달 200만원씩 투자하고 싶어.")
    assert matched, "최초 이용 설명 + 예산 설정 의도가 섞인 질문이 어떤 Agent에도 안 걸림(전체 거절됨)"
    assert "research_agent" in matched  # 서비스 소개
    assert "plan_agent" in matched  # 예산 설정 의도


def test_service_intro_phrasings_reach_research_agent():
    for q in ["처음인데 사용법 알려줘", "이 서비스는 뭐 해주는 거야?"]:
        matched = agent.route_question(q)
        assert "research_agent" in matched, f"서비스 소개 질문이 research_agent에 안 걸림: {q!r}"


def test_budget_setting_intent_phrasings_reach_plan_agent():
    for q in ["매달 200만원씩 투자하고 싶어", "한 달에 50만원으로 시작할래"]:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"예산 설정 의도 질문이 plan_agent에 안 걸림: {q!r}"


def test_genuine_out_of_scope_questions_are_still_rejected():
    """서비스 소개 허용이 "모든 투자 질문 허용"으로 번지지 않았는지 확인 — 서비스/사용법/예산 관련
    단어를 전혀 안 쓰는 진짜 범위 밖 질문은 여전히 빈 목록이어야 한다."""
    for q in ["오늘 저녁 메뉴 추천해줘", "주식 종목 하나 추천해줘", "내일 날씨 어때?"]:
        matched = agent.route_question(q)
        assert matched == [], f"범위 밖 질문인데 라우팅이 걸림(허용 범위가 의도치 않게 넓어짐): {q!r} -> {matched}"


def test_amount_without_recurring_intent_does_not_reach_plan_agent():
    """금액 표현만 있고 예산 설정 의도(반복 주기)가 없는 조회/계산 질문은 plan_agent에 매칭되면
    안 된다 — 매칭되면 set_monthly_budget이 호출될 위험 표면이 생긴다(2026-09-18 2차 지적)."""
    matched = agent.route_question("BTC 1만원이면 얼마나 살 수 있어?")
    assert "plan_agent" not in matched, "단발성 금액 계산 질문인데 plan_agent가 매칭됨 — 예산 변경 위험"
    assert "price_agent" in matched  # 가격/수량 계산은 price_agent 몫


def test_comma_and_spaced_amount_notations_reach_plan_agent():
    """쉼표 표기·띄어쓰기 표기 모두 예산 설정 의도로 인식돼야 한다(2026-09-18 2차 지적 — 리터럴
    "만원" 문자열 검사로는 둘 다 놓쳤었다)."""
    for q in ["매달 2,000,000원씩 투자할래", "매달 200만 원씩 투자할래"]:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"금액 표기 변형이 plan_agent에 안 걸림: {q!r}"


def test_non_btc_asset_amount_still_reaches_plan_agent_for_scope_check():
    """다른 자산(이더리움 등) 예산 요청도 plan_agent에는 도달해야 한다 — BTC 전용임을 안내하고
    거절하는 것 자체가 plan_agent의 책임이라, 라우팅에서부터 걸러버리면 그 안내조차 할 수 없다.
    실제로 set_monthly_budget을 호출하지 않는지는 라우팅만으론 확인 불가 — 실 /query 검증은
    evaluation/manual_test_guide.md 참고."""
    matched = agent.route_question("이더리움에 매달 50만원 투자할래")
    assert "plan_agent" in matched


def test_start_guidance_phrasings_reach_plan_agent():
    """수동 테스트 중 발견(2026-09-19): "뭐부터 해야할지 알려줘"류의 "서비스를 어떻게 시작할지"
    묻는 질문이 어떤 키워드에도 안 걸려 route_question이 빈 목록을 반환하고 도메인 밖으로
    거절되던 결함 — "뭐부터"/"어떻게 시작"/"처음인데"를 plan_agent 키워드에 추가해 고쳤다.
    "어떻게 시작하면 돼?"는 최초 신고 이후 추가로 재현된 동일 결함이다."""
    phrasings = [
        "뭐부터 해야할지 알려줘",
        "어떻게 시작해?",
        "처음인데 도와줘",
        "뭐부터 하면 돼?",
        "어떻게 시작하면 돼?",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"시작 안내 질문이 plan_agent에 안 걸림: {q!r}"


def test_bare_start_word_in_unrelated_context_is_not_broadly_matched():
    """"시작" 한 단어만 넓게 매칭하지 말라는 지적(2026-09-19) — 서비스 이용과 무관하게 "시작"이
    들어간 질문(영화·이벤트 등)이나 진짜 범위 밖 질문은 여전히 거절돼야 한다."""
    for q in ["이 영화 언제 시작해?", "오늘 저녁 메뉴 추천해줘", "행사 시작 시간이 몇 시야?"]:
        matched = agent.route_question(q)
        assert matched == [], f"무관한 질문인데 라우팅이 걸림(허용 범위가 의도치 않게 넓어짐): {q!r} -> {matched}"


def test_buy_timing_verdict_questions_reach_price_agent():
    """수동 테스트 중 발견(2026-09-19): "현재 btc를 매수하기에 좋은시기인지 알려줘"가 route=[]로
    거절됐다 — 이 서비스는 종합 매수 판정을 의도적으로 제공하지 않지만(SPEC §3-1), 그건 "예/아니오로
    답하지 않는다"는 뜻이지 "질문 자체를 범위 밖으로 거절한다"는 뜻이 아니다. price_agent가 지표
    값·의미로 답하고 판정만 거절하도록(프롬프트) 라우팅을 고쳤다."""
    phrasings = [
        "현재 btc를 매수하기에 좋은시기인지 알려줘",
        "지금이 매수 타이밍이야?",
        "지금이 살 때야?",
        "BTC 사기 좋은 시기야?",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "price_agent" in matched, f"매수 시기 질문이 price_agent에 안 걸림: {q!r}"


def test_buy_timing_phrasing_variants_not_covered_by_first_fix_reach_price_agent():
    """수동 테스트 중 추가 발견(2026-09-19): "오늘 btc 사기에 어때?"가
    test_buy_timing_verdict_questions_reach_price_agent에서 고친 표현("사기 좋은" 등)과 문자열이
    안 맞아 여전히 route=[]로 거절됐다 — 같은 매수 시기 질문의 표현 변형이다. "사기에 어때"/"살까"/
    "사도 될까"/"사도 괜찮을까"를 추가로 매칭시킨다."""
    phrasings = [
        "오늘 btc 사기에 어때?",
        "지금 살까?",
        "지금 사도 될까?",
        "지금 사도 괜찮을까?",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "price_agent" in matched, f"매수 시기 질문 변형이 price_agent에 안 걸림: {q!r}"


def test_budget_decision_without_recurring_cadence_word_reaches_plan_agent():
    """수동 테스트 중 발견(2026-09-19): 백테스트 결과를 본 뒤 "나는 일단 200만원으로 진행해볼래"가
    "매달"/"한 달에" 같은 반복 주기 단어를 안 써서 _RECURRING_CADENCE_WORDS 조합에 안 걸리고
    route_question이 빈 목록을 반환했다 — set_monthly_budget은 애초에 "이번 달 예산"을 다루는
    도구라 매번 반복 주기를 말해야만 예산 설정 의도인 게 아니다. 금액 + 결정 표현(진행/시작/정하다/
    설정하다 계열)도 같은 의도로 인식하도록 _BUDGET_DECISION_WORDS를 추가했다."""
    phrasings = [
        "나는 일단 200만원으로 진행해볼래",
        "200만원으로 시작할래",
        "200만원으로 정할래",
        "200만원으로 설정할래",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"예산 결정 표현이 plan_agent에 안 걸림: {q!r}"


def test_amount_without_recurring_or_decision_intent_still_does_not_reach_plan_agent():
    """예산 결정 표현 추가가 "금액이 들어간 모든 문장"으로 번지지 않았는지 확인 — 결정 어미도
    반복 주기 단어도 없는 순수 조회/계산 질문은 여전히 plan_agent에 매칭되면 안 된다."""
    matched = agent.route_question("BTC 1만원이면 얼마나 살 수 있어?")
    assert "plan_agent" not in matched, "결정 의사 없는 금액 계산 질문인데 plan_agent가 매칭됨"


def test_budget_decision_plain_declarative_endings_reach_plan_agent():
    """수동 테스트 중 추가 발견(2026-09-19): "100만원으로 시작한다"가(예산 안내를 듣고 답한
    문장으로 보임) test_budget_decision_without_recurring_cadence_word_reaches_plan_agent에서 고친
    "~ㄹ래"/"~고 싶어" 어미와 안 맞아 여전히 route=[]로 거절됐다 — 매수 시기 표현(§18-2)과 같은
    종류의 한계다. 평서형("~ㄴ다")·구어체 진행형("~할게")·과거형("~했어") 종결을 추가로
    포괄한다."""
    phrasings = [
        "100만원으로 시작한다",
        "100만원으로 진행한다",
        "100만원으로 진행할게",
        "100만원으로 정했어",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"예산 결정 평서형 표현이 plan_agent에 안 걸림: {q!r}"
