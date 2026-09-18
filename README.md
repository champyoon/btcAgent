# BTC DCA Agent

월 예산으로 BTC를 꾸준히 모으도록 **세 전략 비교, 매수 조건 확인, 실제 매수 기록과 잔여 예산 관리**를 돕는 개인용 AI 에이전트입니다. 실제 거래소 주문은 실행하지 않습니다.

## 주요 기능

- 월 예산 제안·확인, 이번 달/다음 달 시작 선택, 아직 시작 전인 계획 앞당기기
- 하락일·정기 분할·RSI 전략 선택 및 직전 달까지 완료된 48개월 백테스트
- 실제 매수 기록 생성·수정·취소·조회, 평균 매수가·수량·남은 예산 집계
- 관망 기록과 결정 시점의 지표 스냅샷
- RSI(14, RMA), MA200 괴리율, 종가 기준 30일·365일·48개월 DD·MDD
- 문서 RAG 설명, 승인·확인 버튼, 도구 호출·검색 근거 디버그 표시

계속 이용하는 달에는 1일에 예산 절반을 매수하고, 나머지는 선택 전략으로 진행합니다. 월중 신규 시작은 기존 실제 매수 기록을 먼저 확인하고, 기록이 없으면 첫 절반 매수 단계를 안내합니다. 실제 매수액에 따라 잔여 예산을 계산하며, 월말에는 남은 예산을 안내합니다.

## 빠른 시작 — PowerShell

프로젝트에서 사용할 Python 환경에 의존성을 설치합니다. API의 모델·문서 임베딩 호출에는 해당 Amazon Bedrock 모델 접근 권한과 AWS 자격증명·리전 설정이 필요합니다. 비밀정보는 저장소에 커밋하지 않습니다.

```powershell
cd C:\btcAgent
python -m pip install -r requirements.txt
```

### 터미널 1: 백엔드

테스트 장부로 실행하는 예시입니다. 환경변수는 **백엔드 프로세스**에 설정해야 합니다.

```powershell
$env:BTC_AGENT_DATA_DIR = "C:\btcAgent\data_manual_test"
python app.py
```

- API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- 상태 확인: http://localhost:8000/health

환경변수 미설정 시 기본 `data/`를 사용합니다. 같은 터미널에 설정된 환경변수는 서버를 다시 켜도 유지됩니다. 실제 장부로 전환하려는 경우에만 환경변수를 해제하고 백엔드를 재시작하세요.

### 터미널 2: 채팅 UI

```powershell
cd C:\btcAgent
python -m streamlit run ui\streamlit_app.py
```

- UI: http://localhost:8501
- 사이드바 백엔드 주소 기본값: `http://localhost:8000`
- 백엔드와 UI를 모두 실행해야 합니다.
- 모델 호출 제한·연결 오류는 성공으로 처리하지 않습니다. 변경 요청이 타임아웃이면 결과 미확인 상태이므로 상태를 조회한 뒤 판단하세요.

UI의 환경 경고는 실제 데이터 디렉터리를 자동 판별하지 못한다는 뜻입니다. 또한 현재 `src/tools.py`의 실시간 가격 캐시 `data/price_cache.json`은 `BTC_AGENT_DATA_DIR`를 따르지 않습니다. 장부·월 상태·일봉 캐시 격리와 이 예외를 구분해야 합니다.

자세한 조작·오류 처리는 [UI 가이드](ui/UI_GUIDE.md)를 참고하세요.

## 처음 사용하는 흐름

1. “처음인데 뭐부터 하면 돼?”
2. “100만원. 이번 달부터 시작할게” → 예산 카드 **확인**
3. “RSI매수로 할게” → 전략 카드 **확인**
4. 실제 매수 후 “오늘 50만원어치 BTC 매수했어”
5. 실제 체결 가격 또는 수량 입력 → 기록 카드 **승인**
6. “이번 달 사용액, 남은 예산, 기록된 수량과 평균 매수가 알려줘”

UI는 제안·승인 정보를 카드로 보여주고 토큰을 내부에서 전달합니다. 일반 대화 전체를 기억하는 서비스는 아니며, 예산 금액과 매수 체결 정보의 지정된 후속 입력만 토큰으로 연결합니다. 서버 재시작 시 대기 중 토큰은 사라집니다.

## 전략과 지표

| 전략 | 두 번째 매수 조건 |
|---|---|
| 하락일 | 확정 종가가 이번 달 첫 실제 매수가보다 낮고, 같은 일봉 시가 대비 -5% 이하 |
| 정기 분할 | 매월 15일. 내부 식별자 `biweekly`는 “2주마다”라는 뜻으로 사용하지 않음 |
| RSI | 일봉 RSI(14, RMA) ≤ 30 |

- 전략 선택과 매수 조건 충족은 별개입니다. RSI가 30보다 높아도 RSI 전략을 선택할 수 있습니다.
- 정기 분할 신규 선택·변경은 15일 09:00 KST, 모든 당월 전략 신규 선택·변경은 월 마지막 날 00:00 KST에 마감합니다.
- 시작월 미지정 시 이번 달을 제안합니다. 이미 시작된 계획의 일반 예산 변경은 다음 달부터 적용합니다.
- 실시간 현재가와 확정 일봉 지표를 구분합니다. 일봉 경계는 09:00 KST입니다.
- DD는 기간 최고 종가 대비 최신 확정 종가 하락률, MDD는 기간 내 고점 종가 이후 최대 낙폭입니다. 기간은 최근 30일·365일·48개월이며 사용자 계좌 MDD가 아닙니다.
- 백테스트는 완료된 48개월, 해당 매수일 시가, 수수료·슬리피지 0 가정입니다. 월중 실제 계획과 별개이며 미래 수익을 보장하지 않습니다.

## 구조와 데이터

```text
Streamlit UI → FastAPI → Supervisor
                         ├─ price_agent    시세·지표
                         ├─ plan_agent     예산·전략·백테스트
                         ├─ ledger_agent   매수·관망 기록
                         └─ research_agent 문서 RAG
```

모든 Agent 정의와 라우팅은 `src/agent.py`에 있습니다. 기본 응답 모델은 `global.anthropic.claude-haiku-4-5-20251001-v1:0`입니다. 다른 모델로 얻은 결과는 해당 모델의 검증으로 따로 기록합니다. MCP는 사용하지 않습니다.

| 위치 | 역할 |
|---|---|
| `app.py` | FastAPI 요청·확인·승인 API |
| `ui/` | Streamlit UI, HTTP 클라이언트, UI 테스트 |
| `src/month_state.py`, `strategy.py`, `backtest.py` | 월 계획·전략 조건·시뮬레이션 |
| `src/ledger.py`, `approvals.py`, `guardrails.py` | 기록·승인 상태·입력 보호 |
| `src/price_history.py`, `indicators.py`, `tools.py` | 일봉 캐시·지표·실시간 시세 |
| `src/retriever.py`, `data/docs/`, `chroma_db/` | 문서 검색·원문·벡터 인덱스 |
| `data/ledger.json`, `month_state.json` | 기본 사용자 장부·월 계획 |
| `data/price_history.json`, `price_cache.json` | 확정 일봉·실시간 시세 스냅샷 |
| `tests/`, `evaluation/` | 코드 검사·모델 평가·보고서 |

일봉은 최초 백필 후 필요한 요청에서 증분 갱신합니다. 09:00 정각에 자동 실행되는 상주 스케줄러는 없습니다. 문서 인덱스는 내용·청킹 설정·임베딩 모델 지문에 따라 갱신합니다.

## API 요약

| API | 요청·동작 |
|---|---|
| `POST /query` | `question`, 선택 필드 `proceed_with_stale_data`, `awaiting_input_token` |
| `POST /confirm_budget_change`, `/cancel_budget_change` | `confirmation_token`으로 예산 제안 확인·취소 |
| `POST /confirm_strategy_change`, `/cancel_strategy_change` | `confirmation_token`으로 전략 제안 확인·취소 |
| `POST /approve`, `/reject` | `approval_id`로 매수 기록 등 실행 승인·거절 |
| `GET /health` | 서버 상태. 데이터 환경 검증은 아님 |

`/query`는 `answer`, UI 표시용 `narrative`, `contexts`, `trace`, `agents_used`와 필요 시 승인·제안·후속 입력 필드를 반환합니다. 최신 일봉이 없으면 확인을 요청하며, 사용자가 동의해도 필수 계산 구간의 결측을 임의로 채우지 않습니다.

매수 기록 생성·수정·취소·장부 초기화는 승인 대상입니다. 관망 기록은 승인 없이 저장합니다. 승인·확인 인자는 서버에 보관하며 처리한 ID를 다시 실행하지 않습니다.

## 검증 현황

**개발팀 최신 보고 기준:** 백엔드 236/236, UI 34/34 통과. 사용자가 TC1(예산→전략→매수 기록→집계)·TC2(지표 조회)의 정상 동작을 확인했습니다. 이번 문서 갱신에서 테스트를 재실행하지 않았습니다.

과거 Haiku 평가의 규칙 기반 22/22, LLM-as-Judge 19/22, RAGAS 8문항 평균(recall 1.00 / precision 0.77 / faithfulness 0.67 / relevancy 0.55)은 **당시 버전의 결과**이며 최신 코드 전체 재평가가 아닙니다.

- [1차 제출 보고서](evaluation/round1_report.md)
- [2차 실사용·통합 평가 보고서](evaluation/round2_report.md): 기존 개발 round3·4·5 상세 근거 포함
- [모델 비교](evaluation/model_comparison_report.md)
- [개발 상세 기록](REPORT.md)

코드·UI 테스트 실행:

```powershell
python -m pytest tests/ ui/tests/ -q
```

LLM 평가 스크립트는 `evaluation/run_eval.py`, `llm_as_judge.py`, `run_ragas.py`입니다. 모델 호출 비용·쿼터를 사용하며, `run_eval.py --round N`은 해당 round 보고서를 생성하므로 제출 보고서를 덮어쓰지 않도록 출력 회차를 확인하세요.

## 범위와 남은 한계

- 로그인·사용자별 장부 분리·자동 주문·자동 알림·외부 배포는 포함하지 않습니다. 현재는 단일 사용자·단일 서버 프로세스용입니다.
- 키워드·정규식 라우팅 및 LLM 설명은 새로운 표현에서 오류가 날 수 있습니다.
- 대기 승인·확인·후속 입력 상태는 프로세스 메모리에 있어 재시작 시 소실됩니다.
- RAG 답변 품질, 월말 전체 흐름의 실제 모델 검증, 모든 최신 변경을 반영한 전체 평가가 남아 있습니다.
- 구현 완료와 특정 시나리오 검증 통과를 서비스 전체 검증 완료로 동일시하지 않습니다.

서비스 개요는 [SERVICE.md](SERVICE.md), 상세 정책·API·현재 구현 상태는 [SPEC.md](SPEC.md)를 참고하세요.
