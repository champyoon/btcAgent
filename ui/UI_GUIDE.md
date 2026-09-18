# BTC DCA Agent — 실사용 테스트용 채팅 UI 실행 가이드

Swagger에서 토큰·승인 ID를 손으로 복사하며 테스트하기 번거로운 문제를 풀기 위한 Streamlit 채팅
UI입니다. 기존 `app.py`(FastAPI)를 HTTP로만 호출합니다. 로그인·디자인 고도화·자동 주문·알림·
전체 대화 메모리 기능은 없습니다.

**2026-09-20 갱신**: 첫 버전(2026-09-18)은 "백엔드 코드는 한 줄도 안 바뀌었다"였지만, 이후 실사용
UI 테스트에서 발견된 3개 결함(아래 §4 참고: 후속 금액 입력 거절, 예산 조회에 무관한 거절 문장 혼입,
요청 월과 적용월 불일치를 설명 없이 처리)을 고치면서 **최소한의 백엔드 변경이 이번에 추가됐습니다**
(§3 참고) — UI만으로는 세 결함 모두 근본적으로 고칠 수 없었기 때문입니다(예: 무상태 API에 "후속
입력을 기다린다"는 서버 측 상태가 아예 없었음).

## 1. 실행 방법 (PowerShell, 별도 터미널 2개)

**터미널 1 — 백엔드(FastAPI)를 격리된 테스트 데이터로 실행**

실제 `data/*.json`을 건드리지 않으려면 `BTC_AGENT_DATA_DIR`을 반드시 설정하세요(CLAUDE.md
"Data files" 항목 참고 — 이 UI가 아니라 **백엔드 프로세스**에 설정해야 실제로 격리됩니다):

```powershell
cd C:\btcAgent
$env:BTC_AGENT_DATA_DIR = "C:\btcAgent\data_manual_test"
python app.py
```

`data_manual_test\`가 없으면 첫 요청 시 자동 생성됩니다. 실제 제출용 데이터로 테스트하려면
`$env:BTC_AGENT_DATA_DIR`을 설정하지 말고 그냥 `python app.py`만 실행하세요(단, 그러면 실제
장부가 바뀝니다 — 의도한 경우에만).

**터미널 2 — Streamlit UI**

```powershell
cd C:\btcAgent
pip install -r requirements.txt   # streamlit이 처음이면 필요
streamlit run ui\streamlit_app.py
```

브라우저가 자동으로 열립니다(보통 `http://localhost:8501`). 사이드바의 "백엔드 주소"는 기본값이
`http://localhost:8000`이며, 터미널 1이 다른 포트/호스트에 떠 있다면 여기서 바꾸세요.

## 2. 왜 "테스트 환경 확인 안 됨" 경고가 항상 뜨는가

UI는 사이드바에 "테스트 환경 확인 안 됨" 경고를 **항상** 띄웁니다. `GET /health`는 백엔드가
살아있는지만 알려줄 뿐 어느 데이터 디렉터리로 떴는지는 알려주지 않고, 이 엔드포인트를 확장하는
것은 백엔드 변경이라 이번 작업 범위 밖으로 뒀습니다(아래 3번 참고). UI가 자체적으로 "테스트
환경입니다"라고 단정하면 실제로는 아닌데도 안심하고 실제 장부를 건드릴 위험이 있어, 대신 **항상
경고를 띄우고 사용자가 직접 1번 절차대로 백엔드를 격리해서 켰는지 확인하게** 했습니다.

## 3. 백엔드 변경 범위

**2026-09-18 첫 버전**: 변경 없음 — UI는 그때까지의 `/query`, `/approve`, `/reject`,
`/confirm_strategy_change`, `/cancel_strategy_change`, `/confirm_budget_change`,
`/cancel_budget_change`, `/health` 스키마만 그대로 사용했습니다.

**2026-09-20 추가 변경 (실사용 UI 테스트 3개 결함 수정, 최소 범위)**: UI만으로는 근본적으로 못
고치는 문제라 아래만 최소로 바꿨습니다 — 상세 원인·전후 비교는 REPORT.md, 계약 변경은 SPEC.md
§2-3/§4-2 참고:

- **`POST /query` 요청**에 `awaiting_input_token: str = ""`(생략 가능) 추가 — 직전 응답의
  `awaiting_input.awaiting_input_token`을 그대로 실어 보내면, "200만원"처럼 그 자체로는 주제를
  알 수 없는 답변도 서버가 방금 물어본 질문(현재는 월 예산 금액)에 대한 답으로 연결합니다.
- **`POST /query` 응답**에 `awaiting_input`(후속 입력 상태 발급, 없으면 필드 없음)과
  `narrative`(승인/확인 안내의 토큰·API 경로를 뺀 설명 전용 텍스트, §4 참고) 추가.
  `budget_change_needs_confirmation`에 `requested_month`/`month_mismatch` 필드 추가(요청한
  달과 실제 적용월이 다른지).
- `src/agent.py`: `request_monthly_budget_amount` 도구 신설(위 후속 입력 상태 발급용, 무인자·
  무부작용), `set_monthly_budget`에 `requested_year`/`requested_month` 선택 인자 추가, 예산
  키워드 라우팅에서 "얼마"를 조건부로만 price_agent에 매칭(예산 문맥에서는 제외).
- `src/month_state.py`: `propose_budget_change`에 `requested_month` 선택 인자와
  `month_mismatch` 계산 추가.
- `src/guardrails.py`: `request_monthly_budget_amount`를 `RISK_LEVELS`에 `"read"`로 등록
  (라이브 검증 중 이 등록을 빠뜨려 승인 대기로 잘못 멈추는 걸 발견·즉시 수정 — REPORT.md 참고).

기존 필드는 하나도 제거·변경하지 않았습니다 — 새 요청 필드는 전부 기본값이 있어 생략 가능하고,
새 응답 필드는 없을 수도 있는 선택 필드라 **기존 Swagger 단독 질문 호환성은 그대로 유지됩니다**
(신규 회귀 테스트로 확인 — REPORT.md 참고).

**2026-09-20 정책 변경 (같은 날 추가 — 월중에도 이번 달부터 DCA 시작 지원, 사용자 확정)**: 기존
"월중 최초 이용자는 무조건 다음 달부터"라는 제한을 사용자와 협의해 바꿨습니다 — 상세 정책은
SPEC.md §2-0, 백엔드 구현은 REPORT.md §21 참고. UI 관련 변경만 요약합니다:
- `budget_change_needs_confirmation`에 `action`("initial"/"advance"/"change"),
  `previous_start_month`/`previous_start_amount`(앞당기기일 때만) 필드 추가 — 전부 선택 필드라
  이전 필드만 보는 클라이언트는 영향 없습니다.
- `src/month_state.py`: `_budget_plan_snapshot`이 이제 세 동작을 계산(이전에는 최초/변경 둘뿐),
  `advance_plan_start`(신규) — 아직 시작 전인 계획의 시작월을 이번 달로 앞당기며 기존 미래
  예산 항목은 보존.
- `src/agent.py`: `set_monthly_budget` 도구가 이 세 동작을 자동으로 골라 제안, `_build_final_answer`가
  동작별로 다른 안내 문구(최초 시작 불일치 vs 변경 불일치 vs 앞당기기) 생성, plan_agent 프롬프트에
  월중 첫 매수 처리·전략 선택·백테스트 구분 안내 추가.

**2026-09-20 추가 변경 (실사용 결함 통합 수정 — 전략 선택·매수 기록·상태 안내, 4건)**: 상세 원인·
전후 비교는 REPORT.md, 계약 변경은 SPEC.md §2-3(두 번째 `awaiting_input` kind)/CLAUDE.md 참고:

- **`POST /query` 응답**의 `awaiting_input.kind`에 `"buy_execution_detail"`이 새로 추가됩니다
  (기존 `"monthly_budget_amount"`와 별개 종류). 실제 매수 보고에 체결 가격·수량이 모두 빠졌을 때
  발급되며, 후속 메시지에서 가격 또는 수량을 뽑아 `record_virtual_buy` 승인 카드로 이어집니다
  (예산 흐름과 달리 즉시 반영이 아니라 §10-1 승인 절차를 거칩니다 — `approvals_needed`에 카드가
  나타납니다).
- `src/agent.py`: `route_question`에 전략 선택(이름+결정 어미 조합) 매칭 규칙과 개인 상태 질문에서
  불필요한 `research_agent` 매칭을 제거하는 규칙 추가; `ledger_agent` 키워드에 과거형 매수 보고
  표현 추가; `request_buy_execution_detail` 도구 신설(무인자성·무부작용, 위 새 `awaiting_input`
  kind 발급용); `get_month_status` 도구 출력에 첫 매수 단계/기존 매수 건수/월말 전액 안내 문장을
  구조적으로 추가; `_parse_krw_amount`가 "억" 단위를 처음으로 지원(기존엔 "만"만 지원, "1억원"이
  조용히 파싱 실패했음); `_maybe_force_buy_execution_request` — 모델이 프롬프트 지시를 어기고
  `request_buy_execution_detail`을 직접 호출하지 않을 때 서버가 대신 발급하는 안전망(실제 Haiku
  4.5 라이브 검증에서 재현된 문제).
- `src/guardrails.py`: `request_buy_execution_detail`을 `RISK_LEVELS`에 `"read"`로 등록.

기존 필드는 하나도 제거·변경하지 않았습니다 — UI는 `awaiting_input.kind`별로 다른 힌트 문구만
보여주면 되고, 승인 카드 렌더링은 기존 `approvals_needed` 처리 로직을 그대로 재사용합니다(UI
코드 변경 없이 자동으로 동작 — 아래 §4 참고).

## 4. UI가 하는 일 / 안 하는 일

- **화면에는 `narrative`(설명 전용, 토큰·API 경로 없음)를 보여주고, `answer`(원문, 토큰·API 경로
  포함)는 접힌 디버그 영역으로 옮깁니다**(2026-09-20 변경 — 실사용 UI 신고: `answer`를 그대로
  보여주면 confirmation_token·POST 경로 문구가 카드 안내와 중복 노출됨). `answer`를 정규식으로
  잘라내는 게 아니라 백엔드가 두 필드를 애초에 따로 내려줍니다(SPEC.md §4-2 "narrative 필드"). 그
  아래에 **구조화된 응답 필드**(`budget_change_needs_confirmation`/
  `strategy_change_needs_confirmation`/`approvals_needed`/`data_gap_needs_confirmation`)를 근거로
  확인/취소/승인/거절 버튼을 별도로 그려, 사용자가 토큰을 직접 복사할 필요가 없게 합니다.
- **후속 입력 상태(awaiting_input, 2026-09-20 추가)**: 응답에 `awaiting_input`이 있으면(예:
  "뭐부터 시작하면 돼?"에 대한 답으로 서버가 예산 금액을 물어본 경우) 그 토큰을 세션에 기억해뒀다가
  **바로 다음** 채팅 메시지에 자동으로 실어 보냅니다 — 사용자는 "200만원"처럼 금액만 입력해도
  됩니다. 화면 힌트는 `awaiting_input.kind`별로 다릅니다(2026-09-20 추가, 4건 결함 통합 수정) —
  `"monthly_budget_amount"`면 "💬 바로 다음 메시지에 금액만 입력해도 예산 제안으로 이어집니다",
  `"buy_execution_detail"`(실제 매수 보고에 체결 가격·수량이 빠졌을 때)이면 "💬 바로 다음
  메시지에 체결 가격 또는 수량만 입력해도 매수 기록 승인 요청으로 이어집니다"를 보여주고, 그 외
  알 수 없는 kind는 일반 문구로 대체합니다. `"buy_execution_detail"`의 후속 입력은 예산 흐름과
  달리 곧바로 `approvals_needed`에 승인 카드가 나타나는데, 이건 **기존 승인 카드 렌더링 로직이
  그대로 처리**합니다(§3에서 언급했듯 UI 코드 변경 없이 자동으로 동작 — 새 kind 전용 렌더링을
  따로 만들지 않았습니다). 다른 주제로 넘어가거나 데이터 신선도 재요청처럼 시스템이 대신 보내는
  메시지에는 이 토큰을 쓰지 않습니다(사용자의 실제 다음 대답을 위해 그대로 남겨둡니다).
- **요청 월과 적용월이 다르면(2026-09-20 추가)** 예산 확인 카드에 경고로 표시합니다 —
  `budget_change_needs_confirmation.month_mismatch`/`requested_month`를 그대로 보여줄 뿐, UI가
  이유를 새로 지어내지 않습니다(서버가 구조화된 필드로 이미 계산해 보내줍니다). `action`에 따라
  문구를 구분합니다 — `"initial"`(최초 시작, 이번 달/다음 달 중 하나만 가능: "요청하신 8월에는
  시작할 수 없습니다 — 최초 시작은 이번 달 또는 다음 달만 가능합니다")와 `"change"`(이미 시작된
  계획 변경, 항상 다음 달만: "요청하신 9월에는 적용할 수 없습니다 — 정책상 다음 달부터만 적용")는
  허용 범위가 달라 같은 문구를 쓰지 않습니다.
- **시작월 앞당기기(`action == "advance"`, 2026-09-20 정책 변경 추가)**: 아직 시작하지 않은(다음
  달 시작으로 이미 설정된) 계획을 이번 달로 앞당기는 제안이면, 카드에 "시작월을 2026-10 →
  2026-09(이번 달)로 앞당깁니다. 기존에 설정된 2026-10 예산(2,000,000원)은 삭제되지 않고 그대로
  유지됩니다"처럼 변경 전/후 시작월과 기존 예산을 함께 표시합니다(`previous_start_month`/
  `previous_start_amount`) — 확인/취소 버튼은 다른 예산 제안과 동일하게 동작합니다(같은
  `/confirm_budget_change`·`/cancel_budget_change`를 그대로 씁니다, 앞당기기 전용 API가 따로 있는
  게 아닙니다).
- 전략 이름(`decline_day`/`biweekly`/`rsi`)은 `src/agent.py`의 `select_strategy` 도구
  docstring에 문서화된 한국어 뜻(하락일 매수/정기 분할 — 매월 1일·15일/RSI 매수)을 그대로
  붙여서 보여줍니다 — UI에서 새로 지어낸 표현이 아닙니다.
- 예산·전략 확인/취소는 각각 `/confirm_budget_change`·`/cancel_budget_change`·
  `/confirm_strategy_change`·`/cancel_strategy_change`를 호출합니다. 매수 기록 등은
  `/approve`·`/reject`를 호출합니다. 데이터 신선도 확인의 "진행"은 **같은 질문을
  `proceed_with_stale_data=true`로 재요청**하는 것으로 처리하고(새 채팅 메시지로 추가됨), "취소"는
  API를 아예 호출하지 않고 "진행하지 않음"으로만 표시합니다(신선도 확인에는 토큰이 없어 확인·취소
  API 자체가 없습니다 — SPEC §7-1 참고).
- 이미 확인/승인/거절된 항목은 버튼 자체가 사라집니다 — 다시 눌러서 같은 요청을 중복으로 보낼
  방법이 UI상 없습니다. Streamlit이 재실행(rerun)될 때도 세션 상태에 저장된 결과만 다시 그릴 뿐,
  API를 다시 부르지 않습니다.
- 서버가 알 수 없는 토큰/승인 ID를 돌려주면(서버 재시작으로 사라졌거나 이미 처리된 경우) 실제
  HTTP 상태(404/409)와 서버가 보낸 문구를 그대로 안내합니다. 요청이 시간 초과되면 "실패
  확정"이 아니라 "확인되지 않음"으로 표시하고, **자동으로 다시 보내거나 새 제안을 만들지
  않습니다** — 사용자가 "다시 확인 시도" 버튼을 눌러야 같은 확인/취소 요청을 다시 보냅니다.
- 대화 내역은 화면에 남지만, `/query` 자체는 무상태입니다 — 화면 하단에 이 사실을 안내하는
  문구를 항상 표시합니다. 이전 턴 문맥을 UI가 대신 채워 보내지 않습니다.
- AWS 자격증명은 백엔드(`app.py`) 쪽 `.env`에만 있고, UI(Streamlit 서버 프로세스)는 백엔드를
  HTTP로만 부릅니다 — 브라우저에 AWS 관련 값이 노출되지 않습니다.
- 로그인, 자동 주문, 알림, 대화 기억(멀티턴 문맥 유지)은 구현하지 않았습니다 — 요청 범위 밖.

## 5. 검증 결과

### 5-1. 가짜 API 응답으로 UI 흐름 검증 (`ui/tests/`, 실제 서버·AWS 호출 없음)

`streamlit.testing.v1.AppTest`로 브라우저 없이 스크립트를 구동하고 `requests.post`/`get`만
몽키패치했습니다. **34/34 통과** (2026-09-20 최종 집계 — 2026-09-18 최초 21개 + 3개 결함 수정 8개
+ 정책 변경 3개 + 4건 통합 수정 2개 추가):

- `ui/tests/test_api_client.py` (10개) — HTTP 200/404/409/타임아웃/연결 실패/비-JSON 응답을
  각각 올바른 `error_kind`로 정규화하는지 + `awaiting_input_token`이 요청 바디에 정확히 실리는지.
- `ui/tests/test_ui_flow.py` (22개) — 기존 12개(일반 답변, 예산 확인/취소, 여러 승인 독립
  처리, 전략 한국어 라벨, 데이터 신선도 진행/취소, 연결 실패 안내, 재실행 시 중복 재호출 방지)에
  더해: 후속 입력 토큰이 바로 다음 질문에 실제로 실리는지·응답에 없으면 빈 문자열로 나가는지·
  데이터 신선도 재요청이 대기 중인 후속 토큰을 건드리지 않는지, narrative만 화면에 보이고
  토큰 문자열은 본문에 없지만 디버그 영역에서는 확인 가능한지·예산 제안이 있어도 다른 Agent의
  설명은 그대로 보이는지, 예산 카드가 요청월·적용월 불일치를 경고로 표시/생략하는지, **최초 시작
  불일치 문구가 "변경" 불일치 문구와 섞이지 않는지, 앞당기기 카드가 변경 전/후 시작월과 기존
  예산을 함께 보여주는지, 앞당기기 확인도 다른 예산 제안과 같은 `/confirm_budget_change`를 그대로
  호출하는지**(정책 변경분 3건), **`awaiting_input.kind`가 `"buy_execution_detail"`일 때 힌트
  문구가 `"monthly_budget_amount"`와 다른지, 그 토큰이 다음 질문에 실제로 실리고 응답의
  승인 카드가 (새 렌더링 코드 없이) 그대로 화면에 그려지는지**(4건 통합 수정분 2건).

이 테스트들은 `tests/`(213개, 결정적 계산 테스트)와는 별도 디렉터리(`ui/tests/`)에 뒀습니다 —
백엔드 계산 정확성을 재는 그 카운트에 UI 상호작용 테스트를 섞지 않기 위해서입니다.

### 5-2. 백엔드(`tests/`) 회귀 테스트

`tests/test_awaiting_input.py`(17개) — `_parse_krw_amount` 표기 3종, LLM 호출 없이
후속 입력 토큰으로 "200만원"이 곧장 예산 제안으로 이어지는지(가짜 LLM으로 "이 경로는 LLM을
부르면 안 된다"까지 확인), 토큰 없이/소진된 토큰/다른 주제로는 예산 제안이 생기지 않는지, 요청
월·적용월 불일치 계산과 최종 안내 문구까지. `tests/test_routing.py`(+3) — "예산은 얼마야" 류
질문이 price_agent를 안 끌어들이면서 순수 가격 질문·복합 조회는 그대로 되는지.
`tests/test_guardrails.py`(+1) — agent.py가 등록한 모든 도구가 `RISK_LEVELS`에도 있는지(아래
5-3의 실제 사고를 다시 안 겪기 위한 회귀 테스트). **정책 변경(2026-09-20, 월중에도 이번 달부터
DCA 시작 지원) — `tests/test_mid_month_start.py`(신규, 21개)**: 이번 달 기본 제안·명시적 다음 달
선택·과거 달 소급 거부·앞당기기 제안/확인/취소/보존/낡은 제안 재확인·이미 활성인 계획은 여전히
항상 다음 달만·기존 매수 기록 반영·매수 기록 없을 때 전액 남은 예산 표시·초과 매수 보존과 음수
미권장·15일 09:00 전후 전략 선택 경계·월말 마지막 날 신규 시작·백테스트가 계획 상태 변경에
영향받지 않음(바이트 단위 동일 결과 확인)까지(21개). **4건 통합 수정(2026-09-20) —
`tests/test_routing.py`(+8): 전략 선택 결정 어미 조합·부정 표현 비매칭·과거형 매수 보고 라우팅·
계산/판단 질문 비매칭·개인 상태 질문의 불필요한 research_agent 제거·복합 질문은 둘 다 매칭·일반
개념 질문은 그대로 research_agent 매칭. `tests/test_mid_month_start.py`(+5): `get_month_status`의
첫 매수 단계 안내·기존 매수 건수 안내·월말 전액 안내(가짜 시각)·실제 매수액 보존·RSI 값과 무관한
전략 선택 가능 여부. `tests/test_buy_report_awaiting_input.py`(신규, 19개): 체결가/수량 파싱·
전체 흐름(보고→후속 입력→승인 카드→승인 전 장부 불변→승인 후 정확한 기록)·거절 시 장부 불변·
중복 승인 방지·같은 날짜·금액의 별도 보고 둘 다 기록·다른 주제 전환 시 상태 해제·알 수 없는 토큰
무시·**모델이 도구를 직접 호출하지 않고 말로만 되물어도 서버가 강제로 토큰을 발급하는 안전망**과
그 안전망이 개입하면 안 되는 경우(수량 이미 있음/금액 2건/부정 표현)까지.
**`tests/` 총 213/213 통과.**

### 5-3. 격리 백엔드 실연동 검증 (실제 HTTP, `BTC_AGENT_DATA_DIR` 격리)

**2026-09-18 (포트 8010)**: 예산 확인/취소, 매수 기록 승인/거절, 404/409 분기 — 상세는 이전
버전 기록 참고.

**2026-09-20 (포트 8020) — 이번 3개 결함 수정 전용, 실제 제출 모델(Haiku 4.5, `global.`
프로파일)로 재현**:

- **#1 후속 입력**: "자 뭐부터 시작하면 돼?" → `get_month_status` 호출 후
  `request_monthly_budget_amount` 호출 → 응답에 `awaiting_input`(토큰 포함) 확인 → **여기서 실제
  사고를 하나 발견**: 이 새 도구를 `guardrails.RISK_LEVELS`에 등록하지 않아 "미등록 도구는 안전하게
  승인 필요"라는 기존 기본 정책이 그대로 적용되면서, 아무 상태도 안 바꾸는 순수 신호 도구가
  승인 대기로 멈춰버렸다 — 라이브 검증 중 바로 발견해 `"read"`로 등록해 수정하고, 위 5-2의
  회귀 테스트로 같은 실수가 재발하면 즉시 잡히게 했다. 수정 후 재실행 — "200만원"(토큰 포함)을
  보내자 **LLM을 다시 거치지 않고**(trace에 guard/awaiting_input 두 단계만 있음) 곧바로
  `budget_change_needs_confirmation`(금액 2,000,000원) 생성, `month_state.json`은 계속
  존재하지 않음(제안만 생성, 미저장) 확인.
- **#2 라우팅**: "10월 예산은 얼마야?" → `agents_used=["plan_agent"]`만(price_agent 없음) 확인.
  "10월 예산과 BTC 현재가 알려줘" → `agents_used`에 `price_agent`/`plan_agent` 둘 다 확인.
- **#3 월 불일치**: "9월 예산은 200만원으로 할게" → LLM이 `set_monthly_budget`을
  `requested_year=2026, requested_month=9`로 정확히 호출 → 응답의 `answer`에 "요청하신 2026-09에는
  적용할 수 없습니다 — 월 예산 변경은 정책상 다음 달부터만 적용됩니다. 대신 2026-10부터 적용하는
  제안입니다"가 그대로 포함됨(구조화 블록에서 나온 문구 — `narrative`는 빈 문자열이라 plan_agent
  자유 서술에는 이 이유가 없었지만, 구조화 블록 덕분에 사라지지 않음) 확인.
- 세 시나리오 전부 `month_state.json`/`ledger.json`이 끝까지 생성되지 않음을 확인(제안만 있었고
  확인 API를 부르지 않았으므로 정상).

이 점검에 쓴 모델 호출은 막히지 않아 재시도가 필요 없었습니다. 실제 `data/*.json`은 이 점검
내내 건드리지 않았습니다(`BTC_AGENT_DATA_DIR`로 완전히 분리된 임시 디렉터리 사용, 점검 후 서버
프로세스 종료·디렉터리 삭제).

**2026-09-20 (포트 8030) — 정책 변경(월중에도 이번 달부터 DCA 시작 지원) 전용, 실제 제출 모델
(Haiku 4.5, `global.` 프로파일)로 재현**:

- **명시적 이번 달 시작**: "9월부터 시작할게, 예산은 200만원으로 할래"(9월 = 실제 오늘 기준
  이번 달) → `set_monthly_budget(requested_year=2026, requested_month=9)` 정확히 호출 →
  `action="initial"`, `effective_month="2026-09"`, `month_mismatch=false` 확인 → 확인 →
  `month_state.json`에 `plan_start_month: "2026-09"` 정확히 반영. 상태 조회("이번 달 시작했어?
  예산 얼마야?")도 `get_month_status`만 호출해 정확한 예산·전략 상태로 답함(가짜 정보 없음).
- **암묵적 기본값**: 월을 아예 언급하지 않은 "예산 200만원으로 시작할래" → `effective_month=
  "2026-09"`로 기본 제안(이전이라면 다음 달로 강제됐을 요청).
- **앞당기기**: 먼저 "다음 달부터 예산 200만원으로 시작할래"로 명시적 다음 달 시작을 확인 →
  `month_state.json`에 `plan_start_month: "2026-10"` 반영 확인. 이어서 "생각해보니까 이번 달부터
  바로 시작하고 싶어. 이번 달은 150만원으로 할게" → LLM이 `get_month_status`를 먼저 호출해 실제
  상태(다음 달 시작으로 이미 설정됨)를 확인한 뒤 `set_monthly_budget(amount_krw=1500000,
  requested_year=2026, requested_month=9)`를 정확히 호출 → `action="advance"`,
  `previous_start_month="2026-10"`, `previous_start_amount=2000000.0` 확인, 응답 `answer`에
  "아직 시작하지 않은 계획의 시작월을 2026-10에서 2026-09(이번 달)로 앞당기는 제안입니다. 기존에
  설정된 2026-10 예산(2,000,000원)은 삭제되지 않고 그대로 유지됩니다"가 그대로 포함됨 → 확인 →
  `month_state.json`이 `plan_start_month: "2026-09"`이면서 `budget_history`에 9월(150만원)·
  10월(200만원) **두 항목 모두** 보존됨을 파일로 직접 확인(삭제·덮어쓰기 없음).

세 시나리오 모두 실제 `data/*.json`을 건드리지 않았고, 모델 호출이 막히지 않아 재시도가 필요
없었습니다.

**2026-09-20 (포트 8040) — 실사용 결함 통합 수정(전략 선택·매수 기록·상태 안내, 4건) 전용, 실제
제출 모델(Haiku 4.5, `global.` 프로파일)로 재현**:

- **#1 전략 선택**: "RSI매수 로 할게" → `strategy_change_needs_confirmation` 토큰 발급 →
  `/confirm_strategy_change` → 이어진 상태 조회에서 RSI가 선택된 전략으로 정확히 반영됨 확인.
  "RSI로 바꾸지 마" → `agents_used`에 `plan_agent` 없음, 전략 제안 자체가 생기지 않음 확인(RSI
  값이 30보다 높아도 선택 자체는 막히지 않는다는 요구사항과, 부정 표현에는 제안이 생기면 안
  된다는 요구사항을 동시에 확인).
- **#3 개인 상태 조회**: "9월의 예산과 전략을 알려줘" → `agents_used=["plan_agent"]`만(불필요한
  `research_agent` 거절/위임 문장 없음) 확인. 같은 응답이 **#4의 재현도 겸했다** — 답변에 "첫
  매수 단계로 500,000원 매수를 고려해보세요... 나머지 500,000원은 RSI 조건에 따라"가 그대로
  포함돼, `get_month_status`의 구조적 안내 문장이 실제로 최종 답변까지 도달함을 확인.
- **#2 실제 매수 보고 전체 흐름**: "오늘 50만원어치 BTC 매수했어" → **여기서 실제 사고를 하나
  재현**: 시스템 프롬프트가 `request_buy_execution_detail`을 먼저 호출하라고 지시했는데도 Haiku
  4.5가 도구를 부르지 않고 말로만 "체결 가격이나 수량을 알려주세요"라고 되물어 `awaiting_input`이
  아예 생기지 않았다 — 프롬프트를 더 강하게 쓰는 대신 `_maybe_force_buy_execution_request` 안전망을
  추가해 서버가 직접 토큰을 발급하도록 코드를 고치고 재검증: 같은 질문 → 서버가 강제로
  `awaiting_input`(`buy_execution_detail`) 발급 확인(trace에 `force_awaiting_input` 단계로
  기록됨) → "1억원에 샀어" → `record_virtual_buy` 승인 카드(`amount_krw=500000,
  price_krw=100000000, executed_date=오늘`) 확인, **승인 전 `ledger.json` 존재하지 않음(비어
  있음)** 확인 → `/approve` → `ledger.json`에 정확한 금액·가격·날짜로 기록됨, 시각은 임의로
  채워지지 않음(`execution_time_precision: "date"`) 확인 → 이어진 "이번 달 매수 기록 보여줘"가
  사용액 500,000원/남은 예산 500,000원/매수 1건을 정확히 보고함(기존 매수 기록이 있을 때
  `get_month_status`가 첫 매수 단계 안내 대신 건수를 보고하는 #4의 두 번째 갈래도 함께 확인).
  추가로 "50만원 사면 얼마나 돼?"(계산)/"50만원 살까?"(판단)/"아직 안 샀어"(부정) 세 문장 모두
  `awaiting_input`도 `approvals_needed`도 생기지 않음을 확인했고, 같은 approval_id로 `/approve`를
  두 번 호출하면 200 다음 409를 반환하며 장부에 중복 기록이 생기지 않음을 확인했다.
- 이 점검 내내 실제 `data/*.json`은 건드리지 않았고(`BTC_AGENT_DATA_DIR`로 분리), 모델 호출이
  막히지 않아 재시도가 필요 없었습니다. **월말 마지막 날 전액 안내(#4의 세 번째 갈래)는 이번
  라이브 검증에 포함하지 않았습니다** — `tests/test_mid_month_start.py`의 가짜 시각 단위
  테스트로만 확인됨(§5-2 참고).

### 5-4. 남은 한계

- 실제 브라우저(Chrome 등)로 클릭해보는 수동 확인은 이번에도 하지 않았습니다 — 5-1(가짜 응답)과
  5-3(실제 백엔드, `api_client` 계층)을 합치면 버튼/후속 입력→요청→상태 반영의 전체 경로가
  검증되지만, 브라우저 렌더링 자체의 시각적 확인은 남아 있습니다.
- 데이터 신선도(`data_gap_needs_confirmation`) 카드는 가짜 응답으로만 확인했습니다 — 실제로
  최신 확정 일봉이 없는 상태를 안전하게 재현하려면 실제 가격 캐시를 훼손해야 해서 생략했습니다.
- Bedrock 호출이 막히는 경우(쿼터·속도 제한)에 대한 반복 재시도는 하지 않았습니다.
- 승인/제안/후속 입력 토큰은 여전히 서버 재시작 시 사라집니다(기존 한계와 동일한 종류) — UI는
  이 경우 404로 정확히 안내하거나(승인·확인 토큰) 조용히 무시하고 일반 라우팅으로 넘어갈 뿐(후속
  입력 토큰), 이 한계 자체를 없애지는 않습니다.
- "얼마"의 예산/가격 문맥 구분(#2)과 "200만원" 표기 파싱(#1)은 매수 시기·예산 결정 표현 때와
  마찬가지로 키워드/정규식 기반이라 완전히 새로운 표현 변형에는 또 놓칠 수 있습니다(CLAUDE.md에
  이미 기록된 구조적 한계 — 신고되는 대로 계속 보강하는 방식입니다).

## 6. 파일 구성

```
ui/
├── streamlit_app.py   # 메인 UI (실행: streamlit run ui/streamlit_app.py)
├── api_client.py       # 백엔드 HTTP 클라이언트 (예외를 던지지 않고 결과 dict만 반환)
├── UI_GUIDE.md          # 이 문서
└── tests/
    ├── test_api_client.py   # api_client 단위 테스트 (10개)
    └── test_ui_flow.py      # 가짜 응답으로 전체 UI 흐름 검증 (24개, streamlit AppTest)
```

백엔드 쪽 회귀 테스트는 `tests/test_awaiting_input.py`(17개)·`tests/test_buy_report_awaiting_input.py`
(신규, 19개)에 있습니다(§5-2 참고).

실행: `python -m pytest ui/tests/ -v` (AWS 자격증명·실행 중인 백엔드 불필요, 전부 몽키패치).
