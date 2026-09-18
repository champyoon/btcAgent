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


def test_pure_budget_amount_question_does_not_reach_price_agent():
    """실사용 UI 신고(2026-09-20, #2): "10월 예산은 얼마야?"가 "얼마"(price_agent)와
    "예산"(plan_agent) 둘 다 매칭돼, plan_agent의 정상적인 예산 조회 답변 옆에 price_agent의
    무관한 거절 문장이 함께 떴다. "예산" 문맥에서의 "얼마"는 price_agent를 끌어들이지 않아야
    한다 — 단, 같은 질문에 가격을 가리키는 다른 단어(가격/시세/현재가/btc)가 있으면 그대로 둘 다
    매칭돼야 한다(복합 조회)."""
    for q in ["10월 예산은 얼마야?", "이번 달 예산 얼마 남았어?", "다음 달 예산이 얼마로 잡혀있어?"]:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"예산 조회 질문이 plan_agent에 안 걸림: {q!r}"
        assert "price_agent" not in matched, f"순수 예산 조회 질문인데 price_agent까지 매칭됨: {q!r}"


def test_pure_price_amount_question_still_reaches_price_agent():
    """위 수정이 "얼마가 들어간 모든 질문"을 price_agent에서 빼는 쪽으로 번지지 않았는지 확인 —
    "예산" 문맥이 전혀 없는 순수 가격 질문은 그대로 price_agent에 매칭돼야 한다."""
    for q in ["BTC 현재가는 얼마야?", "지금 얼마야?", "BTC 1만원이면 얼마나 살 수 있어?"]:
        matched = agent.route_question(q)
        assert "price_agent" in matched, f"순수 가격 질문이 price_agent에 안 걸림: {q!r}"


def test_combined_budget_and_price_question_reaches_both_agents():
    """"10월 예산과 BTC 현재가 알려줘"처럼 예산·가격을 함께 묻는 복합 질문은 두 Agent 모두에게
    가야 한다 — 예산 문맥이 있다고 무조건 price_agent를 빼면 이 복합 질문이 깨진다."""
    matched = agent.route_question("10월 예산과 BTC 현재가 알려줘")
    assert "plan_agent" in matched
    assert "price_agent" in matched


# ── 전략 선택 의도 라우팅 — 실사용 신고(2026-09-20, #1) ─────────────────────


def test_strategy_decision_phrasings_reach_plan_agent():
    """"RSI매수로 할게"가 price_agent/research_agent(둘 다 "rsi" 키워드)에만 걸리고 plan_agent는
    안 걸려 전략 변경 제안·확인 카드가 아예 안 만들어졌다. 전략 이름 + 결정 어미 조합이면
    plan_agent도 매칭돼야 한다."""
    phrasings = [
        "RSI매수 로 할게",
        "RSI로 할래",
        "RSI 방식으로 바꿔줘",
        "하락일로 바꿀래",
        "정기 분할로 정할래",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"전략 선택 의도가 plan_agent에 안 걸림: {q!r}"


def test_strategy_name_alone_without_decision_word_does_not_reach_plan_agent_via_this_rule():
    """전략 이름만 있고 결정 어미가 없는 순수 설명/조회 질문은 이 새 규칙으로 plan_agent에
    매칭되면 안 된다(다른 키워드로 매칭되는 것은 별개) — "RSI가 뭐야?"/"지금 RSI 얼마야?"는
    plan_agent와 무관한 질문이다."""
    for q in ["RSI가 뭐야?", "지금 RSI 얼마야?"]:
        matched = agent.route_question(q)
        assert "plan_agent" not in matched, f"설명/조회 질문인데 plan_agent가 매칭됨: {q!r}"


def test_strategy_negation_does_not_reach_plan_agent_via_decision_rule():
    """"RSI로 바꾸지 마"는 부정형이라 긍정 결정 어미("바꿔줘"/"바꿀래" 등)와 문자열이 안 맞아
    plan_agent가 매칭되면 안 된다 — 매칭 자체가 안 되므로 변경 제안도 생기지 않는다."""
    matched = agent.route_question("RSI로 바꾸지 마")
    assert "plan_agent" not in matched


# ── 실제 매수 보고 라우팅 — 실사용 신고(2026-09-20, #2) ─────────────────────


def test_past_tense_buy_reports_reach_ledger_agent():
    """"오늘 50만원어치 BTC 매수했어"가 "기록"/"매수해줘"(명령형) 어느 것과도 안 맞아(과거 시제
    보고문) 범위 밖으로 거절됐다. 실제 매수 사실을 보고하는 표현은 ledger_agent에 매칭돼야 한다."""
    phrasings = [
        "오늘 50만원어치 BTC 매수했어",
        "오늘 BTC 50만원 샀어",
        "비트코인 50만 원어치 구매했어",
        "오늘 산 비트코인 기록해줘",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "ledger_agent" in matched, f"매수 보고 표현이 ledger_agent에 안 걸림: {q!r}"


def test_calculation_and_decision_help_questions_do_not_reach_ledger_agent():
    """"50만원 사면 얼마나 돼?"(계산)·"50만원 살까?"(판단 보조)는 실제 매수 보고가 아니므로
    ledger_agent에 매칭되면 안 된다 — 각각 price_agent 몫이다."""
    for q in ["50만원 사면 얼마나 돼?", "50만원 살까?"]:
        matched = agent.route_question(q)
        assert "ledger_agent" not in matched, f"계산/판단 질문인데 ledger_agent가 매칭됨: {q!r}"
        assert "price_agent" in matched


def test_investing_verb_variants_of_buy_report_reach_ledger_agent():
    """test_queries.csv 최종 검증 중 발견(2026-09-20): "오늘 정기 매수로 100만원 넣었어"는
    "매수했어"/"샀어"/"구매했어" 중 어느 것도 안 써서(실제 evaluation/run_eval.py 실행에서
    "기록" 키워드 덕에 우연히 라우팅은 됐지만) "넣었어"/"투자했어" 단독으로는 어느 키워드와도
    안 맞을 뻔했다. 구어체 투자 표현도 ledger_agent에 매칭돼야 한다."""
    for q in ["오늘 100만원 넣었어", "이번 달 100만원 투자했어", "오늘 정기 매수로 100만원 넣었어 기록해줘"]:
        matched = agent.route_question(q)
        assert "ledger_agent" in matched, f"구어체 매수 보고 표현이 ledger_agent에 안 걸림: {q!r}"


# ── 개인 상태 vs 전략 개념 설명 — 실사용 신고(2026-09-20, #3) ────────────────


def test_personal_status_query_with_month_does_not_pull_in_research_agent_via_strategy_keyword():
    """"9월의 예산과 전략을 알려줘"에서 "전략"이 research_agent의 일반 키워드와 겹쳐, plan_agent가
    이미 답한 뒤에도 research_agent가 불필요한 안내를 덧붙였다. 특정 월을 콕 집은 개인 상태
    질문에서는 "전략"만으로 research_agent가 매칭되면 안 된다."""
    for q in ["9월의 예산과 전략을 알려줘", "다음 달 전략 뭐야?", "이번 달 전략 알려줘"]:
        matched = agent.route_question(q)
        assert "research_agent" not in matched, f"개인 상태 질문인데 research_agent가 매칭됨: {q!r}"
        assert "plan_agent" in matched


def test_current_strategy_query_without_month_word_does_not_pull_in_research_agent():
    """실사용 재현(2026-09-20): "현재 전략이 뭔지 알려주고, 매수 조건이 충족되었는지 확인해줘"는
    "내"/"제"/월 표현 어느 것도 안 써서 위 테스트의 개인 상태 문맥 검사를 못 통과했고, plan_agent가
    정확히 답한 뒤에도 research_agent가 "개인 상태는 확인할 수 없다"는 불필요한 안내를 덧붙였다.
    "현재 전략"/"지금 전략"도 "이번 달 전략"과 똑같이 개인 상태 표현으로 인식돼야 한다."""
    for q in [
        "현재 전략이 뭔지 알려주고, 매수 조건이 충족되었는지 확인해줘",
        "지금 전략이 뭐야?",
        "현재 선택된 전략 알려줘",
    ]:
        matched = agent.route_question(q)
        assert "research_agent" not in matched, f"개인 상태 질문인데 research_agent가 매칭됨: {q!r}"
        assert "plan_agent" in matched


def test_compound_personal_and_concept_question_reaches_both():
    """"내 전략은 뭐고 RSI는 무슨 뜻이야?"처럼 개인 상태와 개념 설명을 함께 묻는 복합 질문은
    두 요구 모두 충족해야 한다 — plan_agent(개인 상태)와 research_agent(RSI 개념 설명) 둘 다."""
    matched = agent.route_question("내 전략은 뭐고 RSI는 무슨 뜻이야?")
    assert "plan_agent" in matched
    assert "research_agent" in matched


def test_general_strategy_concept_questions_still_reach_research_agent():
    """개인 상태 문맥(특정 월·소유격)이 없는 일반적인 전략 개념 질문은 기존처럼 research_agent에
    매칭돼야 한다 — 이번 수정이 "전략"이 들어간 모든 질문에서 research_agent를 빼는 쪽으로
    번지면 안 된다."""
    for q in ["하락일 매수 조건이 뭐야?", "전략이 뭐가 있어?"]:
        matched = agent.route_question(q)
        assert "research_agent" in matched, f"일반 전략 질문인데 research_agent가 안 걸림: {q!r}"


# ── "예산" 없는 사용액·잔여액 조회 — 실사용 재현(2026-09-20, #4) ─────────────


def test_spending_query_without_budget_word_reaches_plan_agent_not_price_agent():
    """"이번 달 얼마 썼어?"가 "얼마"(price_agent)만 걸리고 "예산" 문맥 검사는 통과 못 해(단어
    자체가 없음), plan_agent의 정확한 사용액·잔여 예산 답변 옆에 price_agent의 "개인 지출은
    담당하지 않는다"류 무관한 거절이 함께 떴다. "예산"이라는 단어 없이도 사용액·잔여액을 묻는
    질문(썼어/샀어/구매했어/지출/쓴 금액/남았어)은 plan_agent에만 매칭되고 price_agent는 빠져야
    한다."""
    phrasings = [
        "이번 달 얼마 썼어?",
        "이번 달 얼마나 샀어?",
        "얼마 남았어?",
        "이번 달 매수에 쓴 금액 알려줘",
    ]
    for q in phrasings:
        matched = agent.route_question(q)
        assert "plan_agent" in matched, f"사용액·잔여액 조회 질문이 plan_agent에 안 걸림: {q!r}"
        assert "price_agent" not in matched, f"사용액 조회 질문인데 price_agent까지 매칭됨: {q!r}"


def test_pure_price_and_calc_questions_still_reach_price_agent_after_spending_fix():
    """위 수정이 "얼마"가 들어간 모든 질문에서 price_agent를 빼는 쪽으로 번지지 않았는지 확인 —
    사용액 문맥이 전혀 없는 순수 가격/계산 질문은 그대로 price_agent에 매칭돼야 한다."""
    for q in ["BTC 현재가는 얼마야?", "BTC 1만원이면 얼마나 살 수 있어?", "50만원 사면 얼마나 돼?"]:
        matched = agent.route_question(q)
        assert "price_agent" in matched, f"순수 가격/계산 질문이 price_agent에 안 걸림: {q!r}"


def test_combined_spending_and_price_question_reaches_both_agents():
    """사용액과 가격을 함께 묻는 복합 질문("이번 달 얼마 썼어? 그리고 BTC 지금 가격도 알려줘")은
    가격 문맥 단어(가격/시세/현재가/btc)가 있으니 기존처럼 두 Agent 모두에게 가야 한다 — 사용액
    문맥이 있다고 무조건 price_agent를 빼면 이 복합 질문이 깨진다."""
    matched = agent.route_question("이번 달 얼마 썼어? 그리고 BTC 지금 가격도 알려줘")
    assert "plan_agent" in matched
    assert "price_agent" in matched


def test_spending_reports_still_reach_ledger_agent_alongside_plan_agent():
    """"이번 달 얼마나 샀어?"/"이번 달 매수에 쓴 금액 알려줘"는 실제 매수 기록(ledger_agent 몫)도
    함께 확인할 만한 질문이라, plan_agent 외에 ledger_agent가 추가로 매칭되는 것 자체는 문제가
    아니다 — 이 테스트는 그 매칭이 여전히 유지되는지만 확인한다(중복 매칭 자체를 막는 게 이번
    수정의 목적이 아니라, price_agent의 불필요한 거절만 막는 것이 목적)."""
    matched = agent.route_question("이번 달 얼마나 샀어?")
    assert "ledger_agent" in matched


# ── 뭉뚱그린 분석 요청 — 실사용 UI 신고(2026-09-20, #5) ──────────────────────


def test_general_analysis_requests_reach_price_agent():
    """"BTC 분석해줘"/"BTC 현재 상황 알려줘"는 같은 뜻인 "BTC 지표 알려줘"(기존에 이미 동작)와
    달리 위 키워드를 하나도 안 써서 route=[]로 범위 밖 거절됐다. "분석"/"현재 상황"도
    get_indicators가 답하는 것과 같은 요청이므로 price_agent에 매칭돼야 한다."""
    for q in ["BTC 분석해줘", "BTC 현재 상황 알려줘", "지금 상황이 어때?"]:
        matched = agent.route_question(q)
        assert "price_agent" in matched, f"분석 요청이 price_agent에 안 걸림: {q!r}"


# ── "BTC/비트코인이 뭐야" 개념 질문 — 실사용 UI 신고(2026-09-20, #6) ─────────


def test_btc_concept_questions_reach_research_agent():
    """"DCA가 뭐야?"는 "dca"가 이미 research_agent 키워드라 정상 동작하는데, "BTC가 뭐야"/
    "비트코인이 뭐야"/"비트코인에 대해 설명해줘"는 "btc"/"비트코인"이 어떤 Agent의 키워드에도
    없어서 range=[]로 거절됐다. data/docs/BTC.md가 이미 있으니 research_agent에 매칭돼야 한다."""
    for q in ["BTC가 뭐야", "비트코인이 뭐야", "비트코인에 대해 설명해줘", "비트코인 소개해줘", "btc란 무엇인가요"]:
        matched = agent.route_question(q)
        assert "research_agent" in matched, f"BTC 개념 질문이 research_agent에 안 걸림: {q!r}"


def test_pure_btc_price_or_report_questions_do_not_pull_in_research_agent_via_concept_rule():
    """"btc"/"비트코인"을 언급하는 모든 문장에서 research_agent를 끌어들이면 안 된다 — 개념을
    묻는 어미(뭐야/란/설명해줘/소개해줘)가 없는 순수 가격 질문·매수 보고는 이 규칙의 대상이
    아니다(각각 price_agent·ledger_agent 몫)."""
    matched = agent.route_question("비트코인 가격 알려줘")
    assert "research_agent" not in matched
    assert "price_agent" in matched

    matched2 = agent.route_question("오늘 50만원어치 BTC 매수했어")
    assert "research_agent" not in matched2
    assert "ledger_agent" in matched2


# ── "이력"/"내역" 동의어 누락 — 실사용 UI 신고(2026-09-20, #7) ───────────────


def test_history_synonyms_reach_ledger_agent():
    """"매수 이력 보여줘"가 "기록"의 동의어인 "이력"/"내역"을 써서 어떤 ledger_agent 키워드와도
    안 맞아 범위 밖으로 거절됐다 — search_ledger가 이미 답할 수 있는 질문인데 순전히 동의어
    누락으로 막혔다."""
    for q in ["매수 이력 보여줘", "거래 이력 알려줘", "매수 내역 보여줘"]:
        matched = agent.route_question(q)
        assert "ledger_agent" in matched, f"이력/내역 조회가 ledger_agent에 안 걸림: {q!r}"
