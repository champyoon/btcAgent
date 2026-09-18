# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service — **BTC DCA Agent** — that helps a single user run a monthly-budget BTC DCA plan:
compare three purchase strategies (decline-day / biweekly / RSI) against a 48-month backtest, track
real (self-reported) buy/watch records and remaining budget, and answer questions via a LangGraph
multi-agent Supervisor backed by Amazon Bedrock (Claude). **`src/agent.py:MODEL_ID` is
`global.anthropic.claude-haiku-4-5-20251001-v1:0`** — Haiku 4.5 is the final, deliberately chosen
submission model (2026-09-17), not a temporary stand-in; the `global.` prefix is a 2026-09-19 region-
routing switch, not a model change (the `us.` inference profile started failing with
`ServiceUnavailableException`, the `global.` profile for the exact same model version works). It was
picked after comparing 4 real candidates (haiku-4-5/sonnet-4-6/nova-pro/nova-lite) on the same 3
representative questions under the same isolated state; see `evaluation/model_comparison_report.md` for
the full evidence and §"모델 선정" below for the summary. Sonnet 4.5/4.6 were both ruled out for this
account specifically because their daily Bedrock token quota is repeatedly exhausted
(`ThrottlingException: Too many tokens per day`), not because of response quality — most of this file's
"How this was verified" live-conversation testing was originally run against Sonnet 4.5 before that
switch; where a section is Sonnet-specific it's labeled.
`src/retriever.py` has its own separate `MODEL_ID` constant for `build_rag_chain()`, but that function is
currently unused (`agent.py`'s `retrieve_docs` tool calls `retriever.search_docs()` directly, not the RAG
chain) — don't bother changing it unless that changes.

## 모델 선정 (2026-09-17 확정)

`src/agent.py`의 `MODEL_ID`는 **Haiku 4.5로 확정**했다(2026-09-19부터
`global.anthropic.claude-haiku-4-5-20251001-v1:0` 프로파일 사용 — `us.` 프로파일이
`ServiceUnavailableException`으로 막혀 같은 모델의 리전 프로파일만 전환, 모델 자체는 그대로).
근거는 `evaluation/model_comparison_report.md`(4개 후보를 동일 질문 3개·동일 격리 상태로 비교) —
요약:

**모델 정책(2026-09-19 명확화)**: Haiku 4.5는 이 시점 **채택된** 제출 모델이지, 유일하게 허용된
모델이거나 다른 모델 사용을 금지하는 규칙이 아니다. 다른 모델(Nova 등)로 스모크 테스트·비교를
하는 것 자체는 언제든 괜찮다 — 지켜야 할 건 딱 하나, **서로 다른 모델의 결과를 같은 모델(Haiku)의
검증 결과인 것처럼 합쳐서 보고하지 않는 것**이다. 예를 들어 Nova로 확인한 결과는 "Nova-pro 실
`/query` 스모크 테스트 통과"로 명확히 라벨링해야지, 이걸로 "Haiku 검증 완료"라고 쓰면 안 된다.
반대로, 다른 모델 테스트 결과 하나만으로 제출 모델 자체를 바꾸는 것도 하지 않는다 — 모델 교체는
`evaluation/model_comparison_report.md` 수준의 다중 후보 비교와 별도 결정을 거친다(위 표 참고).

| 모델 ID | 이 계정에서의 상태 | 비교 결과 |
|---|---|---|
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` | **채택(제출용, 2026-09-19부터 이 리전 프로파일 사용)**, 정상 동작 | 3/3 질문 결함 없이 통과, 역할 구분·근거 제시가 가장 정확. 같은 모델의 `us.` 프로파일이 `ServiceUnavailableException`으로 막혀 전환 |
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | **미평가**(일일 토큰 쿼터 상시 소진, `ThrottlingException`) | 품질 문제가 아니라 쿼터 소진으로 응답 자체를 못 받아 이 계정에서 배제 |
| `us.anthropic.claude-sonnet-4-6` | **미평가**(동일 쿼터 소진) | 비교 실행 3문항 전부 응답을 받지 못함 — 품질 문제 아님 |
| `us.amazon.nova-pro-v1:0` | 정상 동작(도구 호출 포함, 확인 완료) | 3/3 통과하고 haiku보다 빠르지만 일부 응답이 짧고 설명력이 떨어짐 — 차선책 |
| `us.amazon.nova-lite-v1:0` | 부분 동작 | **실제 결함 발견**: "다음 달 전략 뭐야?"(조회 질문)에 쓰기 도구 `select_strategy`를 잘못 선택하고 필수 인자 `strategy`까지 누락해 `ValidationError`로 요청 전체가 크래시함 — 채택 불가 |
| `claude-sonnet-5`, `opus-4.1~5`, `fable-5`, `fable-5-1` | `AccessDeniedException` | 이 IAM 계정에 모델 접근 권한이 없음 — **AWS 콘솔에서 사용자가 직접 활성화해야 함**, 이번 선택에는 불필요 |

`global.` 접두사가 붙은 동일 모델(예: `global.anthropic.claude-sonnet-4-5-20250929-v1:0`)도 이 계정에서
쓸 수 있다 — 리전 라우팅 방식만 다르고(추론 프로파일이 여러 리전에 걸쳐 라우팅될 수 있음), 나머지는
`us.` 버전과 동일하게 다루면 된다. 쿼터가 리전별로 분리돼 있을 수도 있으니, 한쪽이 막히면 같은 모델의
`global.` 버전도 시도해볼 만하다.

**Nova 계열 도구 호출은 이번에 실제로 확인됨** (이전에 여기 남아있던 "미검증" 캐비아트는 해소): `nova-pro`/
`nova-lite` 둘 다 `ChatBedrockConverse.bind_tools()`로 도구 호출 자체는 정상 변환된다 — 다만 `nova-lite`는
위 표의 구체적 실패 사례처럼 인자 채우기 신뢰성이 낮다.

**Read [SERVICE.md](SERVICE.md) first** (the 5-section submission-format product spec), then
**[SPEC.md](SPEC.md)** for the full detailed design — strategy conditions, monthly flow, calculation
rules (§6-1~§6-5: candle boundary, drawdown windows, RSI RMA special cases, rounding), API contract,
and the prioritized follow-up list (§14). **Treat SPEC.md's per-section "구현 현황 확인" callouts as
stale** — they were written before the wiring below existed and mostly say "미구현," which is no longer
true for the areas covered here. This file and README.md's "구현 현황" section are the current source of
truth for what's actually wired.

## Commands

```bash
pip install -r requirements.txt          # deps (Bedrock/LangGraph/LangChain, Chroma+kiwipiepy for RAG, FastAPI, ragas==0.3.0)
python app.py                            # run server (also: uvicorn app:app --reload --port 8000)
./run.sh                                 # install + run

# Query the running server (new concept — see Architecture below)
curl -X POST http://localhost:8000/query -H "Content-Type: application/json" \
  -d '{"question": "이번 달 예산 얼마 남았어?"}'
curl -X POST http://localhost:8000/query -H "Content-Type: application/json" \
  -d '{"question": "오늘 100만원어치 매수했어, 가격은 1억원 기록해줘"}'
# -> approvals_needed[].approval_id 를 그대로 /approve 또는 /reject 에:
curl -X POST http://localhost:8000/approve -H "Content-Type: application/json" \
  -d '{"approval_id": "..."}'
curl -X POST http://localhost:8000/reject -H "Content-Type: application/json" \
  -d '{"approval_id": "..."}'

# Deterministic calculation tests (no AWS needed, no LLM calls) — 251/251 as of 2026-09-20
# UI-interaction tests (separate category, not counted above) — ui/tests/: 34/34 as of 2026-09-20
python -m pytest tests/ -v

# Evaluation (real Bedrock/API calls — cost incurred, needs .env; CSV/tool names now match current
# tool set — see evaluation/round2_report.md#legacy-round3 for the full breakdown)
python evaluation/run_eval.py --round 3     # rule-based judge() — string/tool-call checks only
python evaluation/llm_as_judge.py           # separate LLM-as-Judge — semantic/consistency grading
python evaluation/run_ragas.py              # currently fails in this environment, see below
```

`tests/` (pytest) is the actual test framework for this repo now — 70 deterministic tests covering
budget/avg-price math, RSI/decline-day boundary values, KST time cutoffs, approval concurrency (real
threads), backtest reproducibility, and guardrail negatives; isolated via `tests/conftest.py`'s autouse
fixture (never touches real `data/*.json`). `.env` at repo root must supply AWS credentials
(`AWS_ACCESS_KEY_ID` etc.) for Bedrock access — loaded via `load_dotenv()` in `app.py` and the
`evaluation/` scripts. `evaluation/run_eval.py`/`llm_as_judge.py`/`run_ragas.py` isolate their own state
too (`evaluation/_eval_scratch/`, seeded via `evaluation/_eval_seed.py`) — **don't remove that
isolation**: without it, "read"-tier tools that execute immediately (`select_strategy`'s propose step,
`record_watch_decision`) write straight into the real `data/ledger.json`/`data/month_state.json` (this
actually happened once during round-3 prep — a stray watch record ended up in real `data/ledger.json`
and had to be manually reverted).

`evaluation/run_ragas.py` **now runs successfully (fixed 2026-09-18)**. It used to crash: ragas 0.3.0's
executor calls `nest_asyncio.apply()` at import time, and that patch is incompatible with Python 3.14's
stricter `asyncio.wait_for`/`asyncio.timeout()` task-context tracking
(`RuntimeError: Timeout should be used inside a task`, reproduced in isolation: happens 100% of the time
with `nest_asyncio.apply()` applied, never without it). `evaluation/_ragas_compat.py` now neutralizes
`nest_asyncio.apply` before `ragas.executor` imports it — safe here because this script always runs
`ragas.evaluate()` from a plain synchronous top-level script, never from an already-running event loop
(the only case nest_asyncio's reentrancy support is actually for). Separately, `_REFERENCES` in
`run_ragas.py` had gone stale — its keys were row numbers from an *older, shorter* CSV, so the current
CSV's #2 (DCA) and #3 (RSI) questions were scored against completely unrelated reference answers (MA-
deviation and RSI-threshold text swapped between them) and #4 was silently skipped every run — this is
why `context_recall`/`context_precision` came back suspiciously at 0.00 the first time this was actually
run. Fixed by remapping `_REFERENCES` to the current row IDs with text copied verbatim from what
`retrieve_docs` actually returns for each question. Don't add a new RAG-eval CSV row without also
checking whether it needs a `_REFERENCES` entry — a silently-skipped or silently-mismatched row won't
raise anything, it'll just quietly corrupt the averages. See `evaluation/round2_report.md#legacy-round4` §4 for the
before/after numbers and the full question↔reference mapping table this script now prints on every run.

## Architecture

`app.py`/`src/agent.py`/`src/guardrails.py` are wired to the **new** BTC DCA Agent concept. Domain logic
lives in seven standalone modules that `agent.py`'s `@tool`-wrapped functions call into:

- `src/price_history.py` — local OHLC price cache (`data/price_history.json`, gitignored): paginated
  backfill (~1750 days, covers 48-month backtest + ≥200-day indicator lead-in), `confirmed_records()`
  (filters out the not-yet-closed candle per the KST 09:00 boundary — SPEC §7-1),
  `select_backtest_months()` (the specific calendar "last month," not a silent fallback to an older
  complete one — SPEC §6-1 edge case around month-start 09:00), `update_incremental()` (called at the
  top of every tool that needs fresh data — no separate scheduled job). **Only confirmed (closed)
  candles are ever written to the cache** — `backfill()`/`update_incremental()` both run
  `_filter_confirmed()` (checks `now >= candle_start + 1 day @ 09:00 KST`) right before saving, added
  2026-09-18 after finding that the *old* `check_freshness()` only compared date *strings*
  (`last_available >= expected`), which can't tell a truly-confirmed value from an unclosed candle that
  happened to get saved under the correct date label — once that happened, the stale/partial value
  would look "fresh" forever and never get re-fetched. This actually happened to the real
  `data/price_history.json` (its last entry had been saved mid-day, before that candle's own close) —
  found and corrected. **Don't "fix" a future freshness bug by just tweaking the `check_freshness()`
  comparison operator** — if the write path can still store an unconfirmed candle, no comparison at
  read time fixes that; the invariant that must hold is "if a date is in the cache, it's confirmed,"
  enforced at write time. The first recovery attempt only re-fetched the most recent 5 days (reasoning:
  "only the newest date can ever be captured mid-flight in normal operation") — that reasoning doesn't
  hold once you account for this *project's own* dev-time scripts having hit the real cache at
  irregular times, so it was replaced with `_full_recovery_migrate()`: any cache below `CACHE_VERSION`
  (now 3 — bumped specifically so the already-"fixed" v2 file doesn't get skipped) gets its *entire*
  held range re-fetched and validated (`_validate_full_recovery` — required fields, no dups, doesn't
  regress the start date, reaches the current confirmed end date, no internal gaps) before an atomic
  replace; a real re-run counted 4 "changed" records (present-with-different-values or absent) against
  the pre-recovery file, but that count wasn't split apart from "brand new dates added by the refetch
  margin" — and it happens to exactly equal the number of new margin days, so the honest reading is
  "no additional corrected candle is proven beyond the one already found" (the original claim of "3
  more corrected candles" in an earlier report was retracted as unsupported). No pre-recovery snapshot
  was taken before the replace, so this can't be fully re-verified after the fact. See REPORT.md §7.
  **2026-09-18, later same day**: RAGAS's reference set was pulled out of `run_ragas.py`'s hardcoded
`_REFERENCES` dict entirely into `evaluation/rag_eval_set.json` (id/question/reference/source_docs,
validated for duplicate ids/missing fields/nonexistent source docs before every run — a broken item is
reported by id and reason, never silently dropped) — tying reference answers to CSV row numbers was
exactly the kind of thing that breaks again the next time someone edits `test_queries.csv`. The eval set
holds only pure concept/rule questions (BTC/DCA/RSI definitions, "does low RSI guarantee a bounce", the
service's own strategy conditions, backtest interpretation) — never anything with a live-changing answer
(current price, current RSI value, personal budget/strategy), since RAGAS compares against a *fixed*
reference and a live value can't have one. Building this set surfaced one more routing gap: "백테스트
결과는 어떻게 해석해야 하는가?"/"월말 잔여 예산은 어떻게 처리되는가?" matched only `plan_agent`
(which has no `retrieve_docs`), so `research_agent` — and therefore any document search — never fired;
fixed by adding "백테스트"/"월말"/"잔여" to `research_agent`'s keywords. Full run (Haiku): 8/8 evaluated,
0 errors, 0 NaN, context_recall 1.00 avg — see `evaluation/round2_report.md#legacy-round5`. The result file
(`evaluation/rag_eval_results.json`) carries the question, reference, actual answer, retrieved contexts,
all 4 scores, response/judge/embedding model, execution timestamp, git commit (+dirty flag), and a hash
of the eval-set file — enough to tell later whether a given result set is still valid for the current
code. **Never describe a numeric improvement over the old (CSV-row-keyed) reference scores as "the
service got better"** — the old scores were measuring the wrong thing entirely; the new numbers are a
first valid baseline, not a delta.

Also: `find_missing_dates(records, start, end)` is now wired into the actual calculation paths, not
  just available as a utility — `agent._indicators_summary()`/`record_watch_decision`/
  `month_state.evaluate_current_condition()` all refuse to compute (rather than silently spanning a
  gap) when their input range has one, and `price_history._month_has_full_data()` (backtest's month
  completeness check) now uses it too instead of a count comparison that a duplicate could theoretically
  fool. This range-gap check is intentionally independent of `proceed_with_stale_data` — that flag only
  waives "the latest candle isn't in yet," never a gap earlier in the required range. See REPORT.md §8.
- `src/indicators.py` — `compute_rsi_series()` (Wilder's RMA, full SPEC §6-3 special-value table: both-
  zero→50, gain-only→100, loss-only→0), `compute_ma_deviation_pct()`, `compute_dd_mdd()` (SPEC §6-2,
  **2026-09-20 policy change** — replaces the old `compute_drawdown()`: DD and MDD are now both
  **close-based**, not high-based; `high` is never read by this function at all. Windows: 30 days/
  365 days by day-count, 48 months by calendar-month arithmetic — *different* windowing rules, don't
  conflate them; returns `None` on any gap in the window, or if the window can't be fully covered by
  the supplied candles at all (not just an internal gap — insufficient total history is the same
  `None`, deliberately not distinguished, since both mean "don't compute, don't guess"). Single pass:
  tracks a running peak-close per day, DD is that running dd at the final day, MDD is the minimum
  across all days — see its own "Key invariants" bullet below for why this is correct and the full
  worked examples). None of these round internally (SPEC §6-5: rounding is display-only) — `agent.py`'s
  tool wrappers do the formatting.
- `src/strategy.py` — the three strategies' trigger conditions (SPEC §3), shared by `backtest.py` *and*
  `month_state.py` on purpose (SPEC's "live query and backtest use the same calc function" principle,
  stated for RSI in §6-4, applied here too) — a strategy's rule can't quietly drift between historical
  simulation and the real "has this month's condition fired yet" check.
- `src/backtest.py` — `run_backtest()`: 48 *completed* calendar months (excludes the in-progress month
  entirely), evaluates each strategy's condition starting from day 1 itself, buys at the triggering
  day's *next* day open, final valuation priced at the last included month's last close (never "today's"
  price). Returns `{"ready": False, "reason": ...}` if the specific calendar last-month isn't confirmed.
- `src/ledger.py` — SPEC §4/§4-1 record schema (`record_id`, `executed_date`/`executed_at`/
  `execution_time_precision`, `status`, `history`) over `data/ledger.json`. `_normalize()` backfills
  defaults when reading old-schema entries (from before this rewrite) so it doesn't choke on them, but
  always writes the full new schema. Public functions return `{"ok": bool, ...}`, never raise.
  `amend_virtual_buy`/`cancel_virtual_buy` push the pre-change snapshot into `history` rather than
  overwriting; `cancel_virtual_buy` flips `status`, never deletes. `month_budget_status()`/
  `average_buy_price()` only aggregate `type=buy, status=active` records. `search_ledger(year, month)`
  optionally filters to one calendar month (added 2026-09-17 — without it, `ledger_agent` answering "이번
  달 매수 기록 보여줘" would list *all* records regardless of month, which read as contradicting
  `plan_agent`'s month-scoped budget answer for the same question; `_AGENT_SYSTEM_PROMPTS["ledger_agent"]`
  now tells the LLM to always pass year/month when the question names a specific month). Field-omitted-vs-explicit-
  `None` in `amend_virtual_buy` is distinguished via a private `_UNSET` sentinel default — the `agent.py`
  tool wrapper collapses that distinction back to "omit if None" at the LLM-tool-call boundary (the LLM
  has no reason to explicitly null a field), so if you need the finer distinction, call `ledger.py`
  directly rather than through the tool.
- `src/approvals.py` — SPEC §10-1 approval-ID gate: in-memory `dict` (no TTL, no persistence — same
  known limitation as `_LAST_RETRIEVED_DOCS` below), `pending → executing → executed` /
  `pending → rejected` state machine, guarded by an explicit `threading.Lock` (2026-09-18 — previously
  documented as "safe because of the CPython GIL, no lock needed," which is a standard-CPython
  implementation detail, not a language guarantee, and FastAPI runs sync route handlers in a real
  thread pool, so concurrent `/approve`/`/reject` calls genuinely can land on different OS threads).
  `create`/`get`/`begin_execution`/`finish_execution`/`reject` all take the same lock, so a
  `begin_execution` racing a `reject` on the same id resolves to exactly one winner too, not just
  same-function races. Verified with real `threading.Thread` races (both same-function and
  cross-function) in `tests/test_approvals.py`, not just sequential re-calls.
- `src/month_state.py` — SPEC §2/§4/§5 monthly plan state (`data/month_state.json`, gitignored):
  plan-start month, exposed via `get_month_status()`'s `plan_start_month` field so `agent.py`'s tool
  wrapper can always state "queried month" vs. "plan start month" vs. "strategy selected in that month"
  as three distinct facts (added 2026-09-17 after finding a worker response that blurred them together,
  see "How this was verified"). **2026-09-20 policy change — mid-month signup can now start *this*
  month, not just next month** (see its own Key Invariants bullet below for the full three-way
  `initial`/`advance`/`change` model this introduced) — `set_monthly_budget` still always schedules an
  ordinary *change* (plan already active) for *next* calendar month, that part of §4 didn't change.
  Per-month selected strategy + change
  history, the three cutoffs (`biweekly_cutoff_passed` — 15th 09:00 KST, `month_end_cutoff_passed` — last
  day 00:00 KST, plus "plan not started yet"), and `evaluate_current_condition()` for §5's "missed
  signal" recompute (always re-derives from confirmed-only data; now also checks
  `price_history.find_missing_dates()` over the supplied month range and refuses to judge if it's
  gapped). **Wired to `agent.evaluate_current_condition` (2026-09-18)** — `plan_agent`'s tool, routed
  via `_AGENT_KEYWORDS["plan_agent"]` ("조건"/"충족"/"신호"/"놓친"); it resolves the current month's
  selected strategy, builds `days_this_month`/`rsi_by_date` from `price_history.confirmed_records()`,
  and `first_buy_price` from the new `ledger.first_buy_price_for_month()`. Registered `"read"` in
  `guardrails.RISK_LEVELS` (a query, no state change). Also hosts the **separate, lighter** confirmation
  mechanism for `select_strategy`
  (`propose_strategy_change`/`confirm_strategy_change`/`cancel_strategy_change`, a token store distinct
  from `approvals.py` — SPEC §14-0 explicitly says this tool is *not* a `approvals.py` target, since it
  changes no money/records, but still needs server-side — not just prompt-trusted — confirmation before
  it takes effect). `app.py` exposes this as `/confirm_strategy_change`/`/cancel_strategy_change`, mirroring
  `/approve`/`/reject` — added because the API being fully stateless means a natural-language "yes,
  confirmed" can't reliably route back to the right worker *or* carry the token forward (see "How this
  was verified"). **Same structure added for budget changes (SPEC §4-2, 2026-09-18 policy change)**:
  `propose_budget_change`/`confirm_budget_change`/`cancel_budget_change`, its own `_PENDING_BUDGET_CHANGES`
  dict/token namespace (a strategy-change token can never confirm a budget change or vice versa — they're
  different dicts) — `set_monthly_budget` used to apply immediately with no confirmation at all; see the
  "Key invariants" bullet below for why that changed and exactly what the new functions guarantee.

Request flow (`app.py` → `src/agent.py:build_supervisor`):

```
POST /query {question, proceed_with_stale_data, awaiting_input_token?}
  → guardrails.input_guard   (blocks prompt-injection/secret-leak/jailbreak/approval-bypass attempts)
  → guardrails.mask_pii      (masks phone/email/resident-id/AWS keys/exchange API secrets)
  → awaiting_input direct-resolution (2026-09-20, SPEC §2-3) — if awaiting_input_token matches a
       pending entry in agent._PENDING_AWAITING_INPUT (single-use, popped regardless of outcome), the
       message is checked for a KRW amount (_parse_krw_amount) BEFORE routing/LLM. A match short-
       circuits straight to a budget proposal (agent._propose_budget) with NO LangGraph worker
       invoked at all; no match falls through to normal routing (the token is still consumed, so
       switching topics clears the "awaiting" state).
  → agent.route_question     (deterministic keyword routing, NO LLM call — src/agent.py:_AGENT_KEYWORDS)
  → one LangGraph worker per matched agent, each its own agent↔tools loop (MAX_STEPS=4):
       price_agent    (get_btc_price / get_indicators — values+meaning, no composite verdict, SPEC §3-1)
       plan_agent     (get_month_status / request_monthly_budget_amount / set_monthly_budget /
                        select_strategy / run_backtest)
       research_agent (retrieve_docs → RAG over data/docs/*.md — SPEC §8's 8-file restructuring is done:
                        BTC.md/DCA.md/RSI.md/market_indicators.md/backtest_guide.md/service_rules.md
                        (new) + rewritten dca_strategy.md + unchanged risk_management.md; glossary.md
                        retired, content redistributed)
       ledger_agent   (search_ledger read; record_virtual_buy/amend_virtual_buy/cancel_virtual_buy
                        write; record_watch_decision read; reset_ledger destructive)
  → per-call gate: guardrails.needs_approval(tool, args) — write/destructive tool_calls route to
       await_approval_node instead of executing (conditional graph edge, not a prompt instruction).
       That node calls approvals.create() to mint an approval_id and stores {tool, args} server-side —
       the id (not raw tool/args) is what comes back in approvals_needed. **Every tool in _ALL_TOOLS
       must be registered in guardrails.RISK_LEVELS** — an unregistered tool silently defaults to
       "needs approval" even if it's a pure no-op signal tool (this actually happened with
       request_monthly_budget_amount, caught live — see its own Key Invariants bullet below).
  → agent.judge_output       (rule-based: drops low-content or unsupported-claim-without-evidence answers)
→ {"answer", "narrative", "contexts", "trace", "agents_used", "approvals_needed": [{"approval_id",
    "tool", "args", "reason"}], "data_gap_needs_confirmation"?, "strategy_change_needs_confirmation"?,
    "budget_change_needs_confirmation"?, "awaiting_input"?}
    (base contract + extras — this response shape isn't pinned to one SPEC.md section number since it's
    grown incrementally; SPEC.md §2-3/§4-2/§5 describe the awaiting-input/budget/strategy confirmation
    fields specifically). `answer` always includes a human-readable summary of any pending approvals
    too (not just the structured `approvals_needed` field) — see the "answer synthesis" invariant
    below. `narrative` (2026-09-20) is the same content minus the approval/strategy/budget structured
    summary block (token/API-path text) — added so a UI with its own confirm/cancel cards doesn't have
    to show that block twice; `answer` is kept byte-for-byte backward compatible for Swagger-only use.

POST /approve {approval_id}  →  agent.execute_approved_action(approval_id) — looks up the id in
    approvals.py, atomically pending→executing, runs the *stored* tool/args (never trusts anything the
    client sends beyond the id), then executing→executed. 404 if unknown id, 409 if already
    executing/executed/rejected.
POST /reject  {approval_id}  →  agent.reject_approved_action(approval_id) — pending→rejected. Same
    404/409 semantics. A rejected id can never later be approved.

POST /confirm_strategy_change {confirmation_token}  →  agent.confirm_strategy_change_action(token) —
    applies the proposal `select_strategy` staged in `month_state._PENDING_STRATEGY_CHANGES`. 404 if the
    token is unknown/already consumed. Mirrors /approve but targets month_state's own token store, not
    approvals.py (see month_state.py bullet above for why they're separate stores).
POST /cancel_strategy_change  {confirmation_token}  →  agent.cancel_strategy_change_action(token) —
    discards the proposal without applying it. Mirrors /reject.

POST /confirm_budget_change {confirmation_token}  →  agent.confirm_budget_change_action(token) —
    applies the proposal `set_monthly_budget` staged in `month_state._PENDING_BUDGET_CHANGES` (SPEC §4-2,
    2026-09-18 policy change — this tool used to apply immediately). 404 if the token never existed;
    409 if already confirmed/cancelled *or* if the proposal has gone stale (see "Key invariants" below)
    — distinct from strategy change's 404-only handling, since this flow explicitly needed the
    404-vs-409 distinction.
POST /cancel_budget_change  {confirmation_token}  →  agent.cancel_budget_change_action(token) —
    discards the proposal without applying it. Same 404/409 split as confirm.
```

**Data freshness gate (SPEC §7-1)**: `get_indicators`/`run_backtest` call `price_history.
update_incremental()` then check `price_history.check_freshness()`. If stale and the request didn't set
`proceed_with_stale_data: true`, the tool returns a "please confirm" message and `run()` attaches
`data_gap_needs_confirmation` to the response instead of computing — the client re-asks the same question
with `proceed_with_stale_data: true` to force it through (this API is stateless, so the "yes, proceed"
signal has to ride on the next request rather than referencing a stored confirmation). This is
implemented via a request-scoped module-level dict (`agent._REQUEST_CONTEXT`) — same single-request-only
safety scope as `_LAST_RETRIEVED_DOCS` below.

Key invariants to preserve when touching this code:

- **Tools are partitioned per agent, never shared** (`AGENT_TOOLS` in `src/agent.py`) — if you add a
  tool, decide which single agent owns it and add it to `guardrails.RISK_LEVELS` (unregistered tools
  default to requiring approval, "unknown things are blocked"). See also `_AGENT_KEYWORDS` for routing
  — keyword overlaps (e.g. "얼마"/"rsi"/"전략" matching multiple agents) are an accepted tradeoff of
  Day6's deterministic-keyword design, not a bug to "fix" by removing keywords — doing so risks breaking
  legitimate matches (e.g. removing "얼마" from `price_agent` would break "지금 얼마야?"). **Narrower
  claim after 2026-09-17's testing**: this tradeoff is fine when the non-matching agent just politely
  declines ("that's not my department") — it's *not* fine when a matched agent asserts a *wrong* fact
  (e.g. a plan_agent that has no `retrieve_docs` guessing a strategy's exact trigger condition from
  general knowledge, or a `research_agent` asserting the user's actual current state from docs alone).
  Fix that class of problem at the *prompt* level (tell the agent what it's not authoritative on and to
  defer), not by removing the keyword match.
- **Adding a keyword to fix a routing gap is fine when it's a genuine missing case** — e.g.
  `record_watch_decision` never firing for "이번엔 안 사고 지켜볼래" (no literal "관망"), or a strategy
  concept question like "하락일 매수 조건이 뭐야?" not reaching `research_agent` because the specific
  strategy name wasn't in its keyword list. Both were found live and fixed by adding the missing
  colloquial/specific term, mirroring the reasoning already used for RSI/이동평균/드로다운 being shared
  between `price_agent` and `research_agent`. **2026-09-18, found via manual Swagger testing**: a
  first-time-user message combining a service-intro question with a budget-setting intent
  ("처음 쓰는데 어떤 서비스야? 매달 200만원씩 투자하고 싶어.") matched *zero* keywords and got the
  generic out-of-scope rejection, even though both parts were in scope — neither "서비스 소개" nor
  "액수로 표현한 예산 설정 의도" had any matching keyword anywhere. Fixed by adding `"서비스"`/
  `"사용법"` to `research_agent` (service_rules.md's "최초 이용" section already covers this) and
  `"만원"` to `plan_agent` at the time (superseded below, 2026-09-18 second round) — verified the
  original sentence routed correctly and that genuinely out-of-scope questions still got rejected
  (`tests/test_routing.py`, plus a live `/query` re-run through Haiku 4.5 against an isolated
  `BTC_AGENT_DATA_DIR` — see `evaluation/manual_test_guide.md` §0 for that isolation mechanism).
  **Don't "fix" this class of bug by loosening the empty-match fallback in `run()`** (e.g. defaulting
  to `research_agent` when nothing matches) — that would silently answer genuinely out-of-scope
  questions instead of rejecting them; the fix belongs in `_AGENT_KEYWORDS`, one specific term at a
  time, same as every other routing gap here.
- **A bare currency-unit keyword is not the same thing as a budget-*setting* intent — routing match and
  tool execution are different layers, verify both separately** (2026-09-18, second round of manual
  testing on the fix above). The literal `"만원"` keyword was wrong on both sides: "BTC 1만원이면
  얼마나 살 수 있어?" (a pure quantity calculation, no budget-setting intent) contains "만원" and would
  wrongly pull `plan_agent` into scope — a real risk surface, since `set_monthly_budget` applies
  **immediately with no approval gate and no confirmation token** (unlike `select_strategy`'s
  propose→confirm dance). Meanwhile "매달 2,000,000원씩 투자할래" (comma notation) and "매달 200만
  원씩 투자할래" (space before 원) express the same genuine intent as the original fix's example but
  don't contain the literal substring "만원" at all, so they routed to nothing and got rejected.
  Replaced with `_KRW_AMOUNT_RE` (regex covering comma/space/plain notations) **combined with**
  `_RECURRING_CADENCE_WORDS` (매달/한 달에/매월) in `route_question` — a bare amount alone no longer
  reaches `plan_agent`, only "amount + recurring cadence" does. Also hardened `plan_agent`'s system
  prompt: don't call `set_monthly_budget` for an example/hypothetical question ("~예시를 설명해줘") or
  for a non-BTC asset (this service only ever manages a BTC budget — ask "BTC로 설정하시겠어요?"
  instead of silently registering someone's ETH figure as a BTC budget). Verified live (Haiku 4.5,
  isolated `BTC_AGENT_DATA_DIR`, `month_state.json` diffed before/after each call, not just
  `agents_used`): the quantity question never reaches `plan_agent` at all (structurally can't call the
  tool); the example-explanation and the ETH-budget questions both reach `plan_agent` but call **no
  tool** (state unchanged); the two genuine amount expressions (comma and spaced) do call
  `set_monthly_budget` and the state file reflects it correctly. **One honestly-reported inconsistency,
  not a safety bug**: across repeated runs of similar sentences that never say "BTC" explicitly, the
  LLM sometimes calls `set_monthly_budget` directly and sometimes asks one more confirmation question
  first (temperature=0 reduces but doesn't eliminate this) — it never applies a wrong amount or
  registers a non-BTC figure as a BTC budget either way, so this is a UX consistency question, not a
  correctness one. Making it fully consistent would mean giving `set_monthly_budget` its own
  propose→confirm token like `select_strategy` has, which reverses a deliberate SPEC choice (immediate,
  ungated application) — that's a separate decision to ask for, not a silent fix. See REPORT.md §13-1
  for the full per-case table (routing result / tool called / actual state diff). **Superseded
  2026-09-18, same day**: that "separate decision" was made — `set_monthly_budget` now *does* have its
  own propose→confirm token (SPEC §4-2, `month_state.propose_budget_change`/`confirm_budget_change`/
  `cancel_budget_change`), described in the `month_state.py` bullet and its own "Key invariants" entry
  below. This bullet stays as an accurate record of the intermediate (prompt-only-mitigated) state and
  the reasoning that led to the token-based fix, not as the current behavior.
- **`set_monthly_budget` propose→confirm (SPEC §4-2, 2026-09-18 policy change) — confirmation is
  enforced server-side, not just requested in the prompt.** The tool call itself
  (`agent.set_monthly_budget`) now only calls `month_state.propose_budget_change()`, which computes the
  amount/effective-month/is-initial and stores them under a fresh token in
  `month_state._PENDING_BUDGET_CHANGES` — it **cannot** write to `data/month_state.json` at all; only
  `confirm_budget_change()` calls `init_plan()`/`set_monthly_budget()` (the actual domain functions),
  and only after passing every check below. Don't "fix" a future bug in this flow by having the LLM
  double-check itself in the prompt (e.g. "ask the user first before calling the tool") — that was tried
  and made things *worse* (see below) and doesn't actually prevent anything, since a prompt is not a
  control. The guarantees live in `month_state.py`, not in `_AGENT_SYSTEM_PROMPTS`:
  - **Lock, not GIL**: `propose_budget_change`/`confirm_budget_change`/`cancel_budget_change` all take
    `_BUDGET_LOCK` (explicit `threading.Lock`, same reasoning as `approvals.py`'s — see its own bullet).
    `confirm_budget_change` does token-lookup, staleness check, and the actual apply all inside one
    `with _BUDGET_LOCK:` block, so a concurrent confirm-vs-confirm or confirm-vs-cancel on the same
    token always resolves to exactly one winner (verified with real `threading.Thread` + `Barrier`
    races, both same-function and cross-function, 50 threads × 50 trials each,
    `tests/test_budget_confirmation.py`).
  - **404 vs 409, not one generic error**: a token that never existed is 404 (`agent.
    confirm_budget_change_action`/`cancel_budget_change_action` check `result.get("already_processed")`/
    `result.get("stale")` to decide 409 vs 404) — a token already confirmed/cancelled, or one whose
    proposal has gone stale (below), is 409. `month_state._CONSUMED_BUDGET_TOKENS` remembers which
    tokens were already spent (and how) specifically so this distinction is possible — without it,
    a popped-and-gone dict key looks identical to a key that was never there.
  - **Stale-proposal re-confirmation, not silent drift**: `confirm_budget_change` recomputes
    is-initial/effective-month from the *current* time and state and compares against what was
    stored at propose time — if either the plan snapshot (`plan_start_month`/`budget_history`) or the
    month-boundary-dependent calculation has changed since the proposal was made, confirm fails (409,
    "다시 제안해주세요") rather than quietly applying the original amount to a recomputed month, or
    applying a recomputed amount to the original month. This covers both "a different proposal got
    confirmed in the meantime" and "the real-world month rolled over while this proposal sat pending."
  - **Client-supplied amount/month is never trusted**: `POST /confirm_budget_change` only accepts
    `{"confirmation_token"}` (`BudgetConfirmationRequest` in `app.py`) — there is no `amount_krw`/
    `effective_month` field to tamper with in the first place; the applied values always come from the
    server-side pending record. Verified by posting extra `amount_krw`/`effective_month` fields anyway
    (FastAPI/Pydantic silently ignores undeclared fields) and confirming the state file reflects the
    *original* proposed amount, not the injected one.
  - **Separate token namespace from `select_strategy`'s**: a strategy-change token can't confirm a
    budget change and vice versa, simply because they're different dicts
    (`_PENDING_STRATEGY_CHANGES` vs `_PENDING_BUDGET_CHANGES`) — no extra "token type" tag is needed.
  - **A first live-test attempt made this worse, not better, and got fixed same day**: the first prompt
    wording ("call this tool only for a clear setting instruction... don't ask again before calling it")
    still led Haiku to answer "월 200만원으로 설정하시겠다는 뜻이 맞나요? ... 확인 후 진행해도
    될까요?" in plain prose *without calling the tool at all* for "매달 200만원씩 투자할래" — a
    perfectly clear instruction. That's worse than the old immediate-apply bug: now there's no token
    for the user to confirm *at all*, and this API has no memory of the conversation, so a "네" reply
    can't resume it (same class of problem the strategy-change endpoints exist to solve). Fixed by
    telling the prompt explicitly that the tool call *is* the proposal step and the LLM must not add its
    own natural-language pre-check in front of it for an unambiguous instruction — verified live
    afterward that it calls the tool directly and a real `budget_change_needs_confirmation` token comes
    back. **Lesson**: when a tool itself implements a confirm gate, over-cautious prompt language can
    make the model add a *second*, ungrounded confirmation layer in front of it that produces nothing
    the client can act on — test the actual tool-call behavior after any prompt change here, not just
    the wording of the answer.
- **`run()`'s final `answer` always summarizes pending approvals/strategy-changes/budget-changes in
  natural language, not just via their structured response fields** — found via LLM-as-Judge grading
  (2026-09-17) that when the only matched agent's tool call needed approval, `answer` fell back to a
  generic "승인이 필요한 작업이 있어 답변을 만들지 못했습니다" with zero specifics, because the graph
  discards the worker's own notice text on the `pending_approval` path. This logic now lives in the pure
  function `_build_final_answer()` (extracted 2026-09-18, see the next bullet) — don't inline it back
  into `run()`; keeping it as a standalone function is what makes it directly unit-testable
  (`tests/test_answer_synthesis.py`) without a live LLM call.
- **Tool call succeeding is not the same as the final answer being correct — `_build_final_answer()`
  guarantees the structured facts, prompts alone don't** (2026-09-18, manual testing). A real case:
  `set_monthly_budget` was called correctly (right amount, right effective month, token minted), but the
  LLM's own prose then (a) omitted the effective month the tool returned, (b) invented a "확인 버튼"
  that doesn't exist (there's no chat UI yet, only the two API endpoints), (c) demanded the user pick a
  strategy first as if that were a prerequisite for confirming the budget (it isn't — they're
  independent), and (d) `plan_agent`/`research_agent` each pushed a different "next step," reading as
  contradictory guidance. Separately, `research_agent` described "정기 분할" as "2주마다"/"자동으로
  매수" — both wrong (it's 1일+15일, and the user always places the order themselves) — traced to two
  root causes, not a calculation bug (`strategy.py:biweekly_target_day()` already only ever matches the
  15th, verified by existing tests): (1) `retrieve_docs("서비스 개요 소개 BTC DCA 투자")` returned 4
  chunks *all* from `DCA.md` (general DCA theory) and none from `dca_strategy.md`/`service_rules.md`
  (this service's actual rules) — reproduced directly via `retriever.search_docs()`, no LLM needed; a
  more specific query like "최초 이용 세 가지 매수 방식 하락일 정기 분할 RSI 매수 조건" correctly
  surfaces both docs; (2) the internal identifier `biweekly` was exposed bare in `select_strategy`'s
  tool docstring (only glossed as "(정기 분할)"), and its literal English meaning ("every two weeks")
  could bleed into the model's own phrasing when the real 1일/15일 rule wasn't well-grounded by
  retrieval. **`judge_output`'s `keep=true` catches none of this** — it only filters low-content or
  unsupported-claim answers, never checks whether stated facts/amounts/dates are *correct*. Fixed by:
  (a) `_build_final_answer()` now assembles the amount/effective-month/"not yet saved"/exact
  confirm-cancel endpoint paths directly from the tool's own proposal dict for both budget and strategy
  changes — this block is correct regardless of what the LLM's own prose says (see the previous bullet);
  (b) `data/docs/DCA.md` rewritten to state the common "everyone buys half on day 1" rule up front and
  say the 15th is "once a month, not every two weeks," plus "the user always places the order — this
  service never auto-executes"; (c) `research_agent`'s prompt now gives concrete query-construction
  guidance for service-overview questions and states three facts that are always true regardless of what
  docs get retrieved (the day-1 common rule, 1일+15일 not biweekly, no auto-execution), and is told not
  to end with an execution-related next-step question (that's `plan_agent`'s job); (d) `plan_agent`'s
  prompt now explicitly says never to gate budget confirmation on picking a strategy first, never to
  invent UI elements, and — after a first fix that only made strategy-selection "optional" turned out to
  still read as a competing next-step (2026-09-19 follow-up) — to leave strategy/backtest out of the
  answer *entirely* while a budget proposal is pending: the only next step stated is confirm-or-cancel
  the budget; strategy gets discussed only if the user asks separately, after the budget is settled;
  (e) `select_strategy`'s docstring no longer says "confirm 버튼," and spells out biweekly's actual
  rule. **Know the limits of what `_build_final_answer()` actually guarantees** (2026-09-19, don't
  overclaim this): it only guarantees the structured block it appends is correct — it does not inspect
  or remove anything already in `answers`, so a wrong or self-contradicting sentence already in an
  agent's own prose (e.g. "설정 완료했습니다" sitting right next to the block's "아직 저장되지
  않았습니다") is not caught or fixed by this function. **Superseded 2026-09-19 (later same day, see
  next bullet)**: this exact scenario was flagged as a live problem, not just a documented limitation —
  fixed by removing the duplication at the prompt level instead. The test that used to demonstrate the
  limitation (`test_contradictory_llm_prose_is_not_removed_or_detected`) was replaced with
  `test_budget_facts_appear_exactly_once_when_agent_reply_defers_to_structured_block`, which now asserts
  the opposite: given an agent answer that follows the new prompt contract, every fact appears exactly
  once and none of the banned phrases appear. "The summary block is attached" was never a valid
  substitute for "the whole answer is accurate" — that's still true as a general statement about this
  function's scope, but the *specific* duplication/contradiction risk it was illustrating is now closed
  by design, not just documented.
- **`plan_agent` must not restate what `_build_final_answer()` already guarantees — duplication is a
  contradiction risk, not just redundancy** (2026-09-19, follow-up to the bullet above). The first fix
  told `plan_agent` not to repeat the amount/month/save-status, but a Nova-pro re-run showed it still
  wrote out the literal strings "POST /confirm_budget_change"/"POST /cancel_budget_change" in its own
  sentence — appearing twice once combined with the structured block. Avoiding a simple string-replace
  cleanup (asked not to do this), the prompt was tightened again to say explicitly: don't write those
  endpoint path strings, or any "confirm by sending X" instruction, in your own prose at all — a short
  acknowledgment ("예산 설정 제안을 만들었습니다") is enough, since the system always appends the full,
  correct instructions once. Re-verified with Nova-pro afterward: amount/month/"not yet
  saved"/confirm-path/cancel-path each appear **exactly once**, no banned phrases. `plan_agent`'s
  service-intro/three-strategy explanation (via `research_agent`, grounded in confirmed doc rules) was
  left untouched — only the budget-confirmation mechanics were deduplicated. Full before/after
  transcripts and exact timestamps: REPORT.md §15-5/§15-6. **Superseded 2026-09-19, later same day (see
  next bullet)**: this was still a prompt-compliance fix — a model that ignores or half-follows the
  instruction could still leak the duplication. Replaced with a structural guarantee that doesn't depend
  on the model following instructions at all.
- **`_build_final_answer()` structurally excludes `plan_agent`'s own text when a budget proposal
  exists — this does not depend on the model following any instruction** (2026-09-19, final fix in this
  chain). The prompt-based fixes above reduce how often `plan_agent` restates budget facts, but a model
  can still ignore them; asked explicitly not to rely on prompt compliance, the function's signature
  changed from `answers: list[str]` to `answers_by_agent: dict[str, str]` so it knows which text belongs
  to which agent, and when `budget_proposal is not None` it pops `"plan_agent"` out of the dict *before*
  formatting anything — that agent's text, whatever it says, never reaches the final answer. Other
  agents (`research_agent`'s service-intro/three-strategy explanation, `ledger_agent`, etc.) are
  untouched; only `plan_agent`'s slot is affected, and only when a budget proposal exists.
  `strategy_proposal` does not get the same treatment yet (out of scope for this round — extend the same
  way if asked). Proven with a deliberately-violating test case
  (`tests/test_answer_synthesis.py::test_plan_agent_text_is_replaced_even_when_it_violates_instructions`):
  feed `plan_agent` the literal string "설정 완료했습니다. 2주마다 자동으로 매수합니다." and confirm
  none of it survives, `research_agent`'s explanation does, and the structured block's facts each appear
  exactly once — this is what "not relying on prompt compliance" actually looks like as a test, as
  opposed to a test that only proves the limitation exists. A separate test
  (`test_normal_well_formed_input_still_has_no_duplication`) keeps confirming well-behaved input still
  produces no duplication, and `test_plan_agent_text_is_preserved_when_no_budget_proposal` confirms the
  exclusion only triggers when a budget proposal actually exists — plan_agent answers for anything else
  are unaffected. Re-verified live with Nova-pro (2026-09-17T16:15:36+09:00 KST): the final answer
  contains no `[plan_agent]` text at all, `research_agent`'s explanation is intact, and the budget block
  is the sole source of the amount/month/save-status/confirm-cancel facts. **Still open**: the same live
  check on Haiku 4.5 itself (daily quota still exhausted, confirmed as a genuine *daily* cap rather than
  the separate per-minute rate limit by retrying at 90s/150s/300s intervals; don't keep retrying — wait
  for it to actually reset) — Nova-pro passing is recorded as its own item, never merged into or
  substituted for Haiku's verification status (see "모델 선정" policy above). Commits are left to the
  user going forward on this thread of work.
- **"What do I do first?"-style onboarding questions need their own keywords — a bare "시작" is too
  broad, but phrase-level matches aren't** (2026-09-19, manual testing). "뭐부터 해야할지 알려줘"/
  "어떻게 시작해?"/"처음인데 도와줘"/"뭐부터 하면 돼?"/"어떻게 시작하면 돼?" all matched zero
  `_AGENT_KEYWORDS` and got the generic out-of-scope rejection, even though they're squarely in scope
  (asking what to do next in the service). Adding the single word "시작" would have caught these but also
  "이 영화는 언제 시작해?" and similar unrelated questions — so `"뭐부터"`, `"어떻게 시작"`, `"처음인데"`
  were added to `plan_agent` instead: specific enough to cover all five reported phrasings, verified via
  `tests/test_routing.py::test_bare_start_word_in_unrelated_context_is_not_broadly_matched` that unrelated
  "시작"-containing questions still route to nothing. `plan_agent`'s prompt was extended for this intent:
  always call `get_month_status` first and answer only from its real result (never guess); if no plan
  has started, explain that setting a budget starts it — but **never call `set_monthly_budget` with a
  guessed amount just because the user asked "what do I do?"**, ask what amount they want instead; same
  principle for `select_strategy` (never pick one on the user's behalf); if everything is already set
  up, just report that, don't invent more steps to demand; and — since this API is stateless — never
  pretend to remember a prior turn for follow-ups like "그다음은?", ground the answer only in what
  `get_month_status` actually returns and ask a short clarifying question if the request is genuinely
  ambiguous (that phrase itself still matches no keyword and gets the standard rejection, which is safe
  by construction — not modified here). Verified live with Nova-pro (2026-09-17T16:27:59+09:00 KST,
  isolated `BTC_AGENT_DATA_DIR`): with no plan started, both reported phrasings called only
  `get_month_status` (no write tool), asked the user for an amount instead of guessing one, and left
  `month_state.json` untouched; with a plan+strategy already configured, the same question correctly
  reported the existing state via `get_month_status` alone, again with zero file mutation. **Nova-pro
  passing is not a Haiku verification** — Haiku 4.5 remains untested here, daily quota still exhausted,
  no further retries per the same standing instruction as above.
- **`compute_drawdown()` returns the actual date its `high` occurred on (`high_date`), not just the
  window bounds — found via a real Haiku 4.5 response, the first genuine submission-model verification
  this session got to run** (2026-09-19). The `us.` Haiku profile started failing with
  `ServiceUnavailableException`; switching to the `global.` profile for the same model version (see the
  intro above) finally let live Haiku answers be checked directly, and the very first one turned up a
  real bug: for "현재 btc 지표 알려줘" Haiku said the 1-year/4-year drawdown peaks were "작년 9월
  17일"/"4년 전" and the 1-month peak was "8월 18일" — all three are wrong, and all three exactly match
  each window's *start* date rather than the real peak day (verified by scanning the actual price cache:
  the true peaks were 2025-10-09 for both the 1-year and 4-year windows, and 2026-08-28 for the 1-month
  window). Root cause: `compute_drawdown()` computed `high = max(c["high"] for c in window)` but threw
  away which candle produced it, and `agent.py:_dd_desc()` then phrased the result as "(구간시작~구간종료
  고점 X원 대비)" — a window range sitting right next to a peak value, with nothing distinguishing "this
  is the window I searched" from "this is when the peak happened." Without the actual date to work with,
  the model filled the gap with the window's start date, which reads plausibly but is wrong. This is the
  same failure shape as the "2주마다"/"자동 매수" case above (correct calculation, ambiguous tool output,
  wrong model paraphrase) — not a calculation bug. Fixed by having `compute_drawdown()` track the peak
  candle's own `date_kst` as `high_date` and returning it, and rewriting `_dd_desc()` to state the window
  range and the peak date as two distinct facts ("조회 구간 X~Y 중 최고가 Z원은 W에 기록"). Verified
  without a live call via `tests/test_data_freshness.py` (peak forced to a mid-window day, distinct from
  both `start_date` and `end_date`, to make sure `high_date` isn't accidentally right by construction),
  and verified live by re-running the exact reported question through the actual submission model
  (Haiku 4.5, `global.` profile) — the tool output and the final answer both now state the correct dates
  for all three windows. Full transcripts: REPORT.md §17.
- **A buy-timing verdict question ("현재 btc를 매수하기에 좋은시기인지 알려줘") is in scope for
  price_agent to *answer with indicator values*, even though this service deliberately never gives a
  composite yes/no verdict** (found via manual Swagger testing, 2026-09-19). `route_question` returned
  `[]` for it — no keyword covered "매수하기"/"매수 타이밍"/"살 때"/"사기 좋은" — so the whole question
  was rejected as out-of-scope, when the correct behavior is to answer it with RSI/MA-deviation/drawdown
  values and their individual meaning, then explicitly decline the composite judgment (SPEC §3-1's
  `assess_dca_signal` deprecation means "don't answer yes/no," not "don't answer the question at all" —
  don't conflate the two). Fixed by adding those four phrasings to `price_agent`'s keywords (sell-timing
  phrasing was deliberately left out — out of scope, this service is buy-only) and adding a paragraph to
  `price_agent`'s system prompt instructing it to give `get_indicators` values+meaning, explicitly state
  it doesn't produce a combined "적기" judgment, and mention that `plan_agent` can separately check
  whether the user's own selected strategy's condition has fired. Verified via `tests/test_routing.py`
  (`test_buy_timing_verdict_questions_reach_price_agent`, 4 phrasings + 2 unrelated-"시작"-style controls
  still rejected) and live against the actual submission model (Haiku 4.5, `global.` profile,
  2026-09-17T17:16:36+09:00 KST): `agents_used=['price_agent']`, only `get_btc_price`/`get_indicators`
  were called (no verdict-producing tool exists to call), and the final answer stated indicator values
  then explicitly declined a combined "적기" judgment while pointing to the strategy-condition check.
  Full transcript: REPORT.md §18.
- **Buy-timing questions have effectively unlimited phrasing — the first keyword fix (above) doesn't
  cover every variant, and a second one already turned up** (found via manual Swagger testing,
  2026-09-19, same day as the first fix). "오늘 btc 사기에 어때?" still returned `route=[]` because
  none of the four phrasings added for the first fix ("매수하기"/"매수 타이밍"/"살 때"/"사기 좋은")
  string-match "사기에 어때" — this is not a new bug class, it's the same one recurring because
  keyword-list matching can only ever cover phrasings someone has actually reported. Fixed by adding
  "사기에 어때"/"살까"/"사도 될까"/"사도 괜찮을까" to `price_agent`'s keywords. Verified via
  `tests/test_routing.py::test_buy_timing_phrasing_variants_not_covered_by_first_fix_reach_price_agent`
  and live against Haiku 4.5 (`global.` profile, 2026-09-17T17:44:15+09:00 KST): routed to
  `price_agent`, gave RSI(50.80)/MA200-deviation(+1.94%)/drawdown values with meaning, then explicitly
  declined a combined verdict. **Known, accepted limitation, not fully closed**: don't assume the next
  reported phrasing variant of this same question won't need its own keyword addition too — this is a
  structural property of keyword-list routing (documented, not a promise to eventually enumerate every
  case). Full transcript: REPORT.md §18-2.
- **A budget-decision sentence without a recurring-cadence word ("나는 일단 200만원으로 진행해볼래",
  after seeing the 48-month backtest) also has to reach `plan_agent`, not just "매달 X원씩" phrasing**
  (found via manual Swagger testing, 2026-09-19). §13-1's fix required an amount (`_KRW_AMOUNT_RE`) to
  co-occur with a recurring-cadence word (`_RECURRING_CADENCE_WORDS`: 매달/한 달에/매월) specifically to
  stop a bare "만원" from wrongly pulling in pure quantity questions — but `set_monthly_budget` is
  inherently a "this month's budget" tool, so a user doesn't have to say "매달" every time to mean it;
  "진행할래"/"시작할래"/"정할래"/"설정할래"-style decision endings are an equally valid signal of the
  same intent and none of them appeared in the cadence-word list, so `route_question` returned `[]` for
  this exact reported sentence. Fixed by adding `_BUDGET_DECISION_WORDS` (진행할래/진행해볼래/진행하고
  싶어/진행하고싶어/시작할래/시작해볼래/정할래/설정할래) and widening the condition to "amount +
  (cadence word OR decision word)" — a pure calculation question like "BTC 1만원이면 얼마나 살 수
  있어?" carries neither a cadence word nor a decision ending, so §13-1's original false-positive fix
  still holds (re-verified, not just assumed). Also generalized `plan_agent`'s system prompt, which used
  to hold up "매달 200만원씩 투자할래" as *the* example of an unambiguous instruction — that could bias
  the model toward expecting cadence phrasing specifically; now states explicitly that the tool is
  inherently monthly so cadence words don't need repeating, with the reported decision-style sentence
  added as its own example. Verified via `tests/test_routing.py`
  (`test_budget_decision_without_recurring_cadence_word_reaches_plan_agent`: 4 decision-style phrasings
  incl. the exact reported sentence;
  `test_amount_without_recurring_or_decision_intent_still_does_not_reach_plan_agent`: the §13-1 pure
  quantity-question case still doesn't match) and live against the actual submission model (Haiku 4.5,
  `global.` profile, isolated `BTC_AGENT_DATA_DIR`, 2026-09-17T17:33:10+09:00 KST): `agents_used=
  ['plan_agent']`, `set_monthly_budget(amount_krw=2000000)` was called correctly, a real
  `confirmation_token` came back in `budget_change_needs_confirmation`, the isolated `month_state.json`
  was confirmed **not yet written** (propose-only, per §4-2), and the final answer stated the amount/
  effective month/unsaved status/confirm-cancel paths exactly once via the structured block (§15-7's
  exclusion dropped `plan_agent`'s own low-content text, judged `keep=false` by `judge_output`). Full
  transcript: REPORT.md §19.
- **Budget-decision phrasing has the same open-ended-variants problem as buy-timing phrasing (§18-2) —
  a second decision-ending gap turned up the same day** (found via manual Swagger testing, 2026-09-19).
  "100만원으로 시작한다" (likely a direct answer to a prior turn that asked for the amount — this API
  has no memory of that turn, so routing has to work from this sentence alone) still returned
  `route=[]`: the §19 fix only covered casual intent endings ("~ㄹ래"/"~고 싶어"), not the plain
  declarative "~ㄴ다" ending. Fixed by adding 12 more literal words to `_BUDGET_DECISION_WORDS` —
  declarative (진행한다/시작한다/정한다/설정한다), casual-progressive (진행할게/시작할게/정할게/
  설정할게), and past-tense (진행했어/시작했어/정했어/설정했어) endings for the same four stems.
  Verified via `tests/test_routing.py::test_budget_decision_plain_declarative_endings_reach_plan_agent`
  and live against Haiku 4.5 (`global.` profile, isolated `BTC_AGENT_DATA_DIR`,
  2026-09-17T17:50:59+09:00 KST): `agents_used=['plan_agent']`, `set_monthly_budget(amount_krw=1000000)`
  called correctly, a real `confirmation_token` came back, `month_state.json` confirmed not yet
  written. **Same accepted limitation as §18-2, stated again so it doesn't read as a one-off**: Korean
  decision-verb endings are effectively unbounded — don't treat this fix (or the next one) as having
  closed the category, just as having covered what's been reported so far. Full transcript: REPORT.md
  §19-2.
- **Follow-up input state (`awaiting_input`, SPEC §2-3) — a stateless API needs a structural way to
  know "this message answers the question I just asked," found via the chat UI (2026-09-20).**
  "자 뭐부터 시작하면 돼?" → plan_agent asks what monthly amount to use → the user replies just
  "200만원" → that bare reply carries none of `_RECURRING_CADENCE_WORDS`/`_BUDGET_DECISION_WORDS`/
  "예산", so `route_question` returns `[]` and it gets rejected as out-of-scope — the API is
  stateless, so nothing connects it to the question that was just asked. Scanning `answer` text for
  "얼마" was explicitly rejected as a fix (too fragile). Fixed the same way `select_strategy`/
  `set_monthly_budget` confirmation works — a server-issued token the client echoes back — but kept
  in its own store (`agent._PENDING_AWAITING_INPUT`) since this isn't "confirm a proposal," it's
  "resume a question": `request_monthly_budget_amount` (new no-op tool, no args, no side effect other
  than minting the token) is what plan_agent must call before asking for an amount in its own words;
  `run()` checks an incoming `awaiting_input_token` *before* routing, and if the message contains a
  parseable KRW amount (`_parse_krw_amount`, handles "200만원"/"200만 원"/"2,000,000원") it calls
  `_propose_budget()` directly — **no LangGraph worker, no LLM call at all** for that turn — guarantees
  the parsed amount is exactly what gets proposed, not whatever the model decides to do with it. No
  amount found (topic changed) still consumes the token (single-use regardless of outcome) and falls
  through to normal routing — this is what "clears the state on topic change" actually means, not a
  separate cancel affordance. No token at all (a fresh session pasting "200만원" cold) never triggers
  this path — a bare amount alone is still not treated as budget-setting intent, exactly preserving
  §13-1's guard against that false positive. **Found and fixed live, mid-verification**: the new tool
  wasn't added to `guardrails.RISK_LEVELS`, so the "unregistered tool defaults to needing approval"
  rule (see the tools-partition bullet above) caught it — a no-op signal tool got stuck waiting on a
  approval it never needed. Registered as `"read"`; added
  `tests/test_guardrails.py::test_every_tool_agent_py_registers_has_a_risk_level` so a future new tool
  missing this registration fails a test instead of surfacing live. Verified via
  `tests/test_awaiting_input.py` (17 tests, using a `_NoInvokeLLM` fake whose `.invoke()` raises — the
  strongest possible proof the direct path never touches the LLM) and live against Haiku 4.5 (`global.`
  profile, isolated `BTC_AGENT_DATA_DIR`): the full "자 뭐부터 시작하면 돼?" → "200만원" exchange
  produced a correct `budget_change_needs_confirmation` with `amount_krw=2000000.0`, and
  `month_state.json` stayed unwritten (proposal only). Full transcript: REPORT.md §20-1.
- **Budget proposals now carry `requested_month`/`month_mismatch` — the applied-month policy (always
  next calendar month) doesn't change, but silently substituting the user's named month without saying
  so is its own bug** (found via the chat UI, 2026-09-20). "9월 예산은 200만원으로 할게" produced a
  October proposal with no explanation of why September wasn't used — technically correct per §4, but
  the substitution was invisible. `set_monthly_budget` gained optional `requested_year`/
  `requested_month` params (same pattern as `select_strategy`'s year/month — the LLM fills them from
  the already-injected today's-date context); `month_state.propose_budget_change` gained an optional
  `requested_month` param and now returns `month_mismatch` (`requested_month != effective_month`).
  **The reason has to live in the structured block, not plan_agent's prose** — because a budget
  proposal already excludes plan_agent's free text entirely (§15-7's `_build_final_answer` rule), any
  explanation plan_agent writes about *why* the month changed would vanish along with the rest of its
  text. So `_build_final_answer`'s budget summary itself grew a mismatch sentence: "요청하신
  {requested_month}에는 적용할 수 없습니다 — 월 예산 변경은 정책상 다음 달부터만 적용됩니다. 대신
  {effective_month}부터 적용하는 제안입니다." — generated from the structured proposal dict, so it
  can't be dropped by an uncooperative model. Omitting `requested_year`/`requested_month` (existing
  Swagger callers, or a request that never named a month) behaves exactly as before —
  `month_mismatch` is simply `False`. Verified via `tests/test_awaiting_input.py` (mismatch/no-
  mismatch cases for `propose_budget_change` and `_build_final_answer` directly) and live: the LLM
  correctly called `set_monthly_budget(amount_krw=2000000, requested_year=2026, requested_month=9)`
  for "9월 예산은 200만원으로 할게," and the response `answer` contained the exact mismatch sentence
  above. Full transcript: REPORT.md §20-3.
- **A bare "얼마" is not always a price question — "예산" context without any price-indicating word
  should not pull in `price_agent`, but a combined question still needs both agents** (found via the
  chat UI, 2026-09-20). "10월 예산은 얼마야?" matched both `plan_agent` ("예산") and `price_agent`
  ("얼마," previously an unconditional keyword), so `price_agent`'s "that's not my department, ask
  finance" reply showed up next to `plan_agent`'s correct budget answer — right next to each other, it
  read as a contradiction even though both replies were individually correct. Removed "얼마" from
  `price_agent`'s static keyword list; `route_question` now adds `price_agent` for a "얼마" match only
  when the question has no `_BUDGET_CONTEXT_WORDS` ("예산") *or* it also has a
  `_PRICE_CONTEXT_WORDS` match (가격/시세/현재가/btc/price) — so "10월 예산과 BTC 현재가 알려줘"
  still correctly reaches both agents, and a pure price question like "BTC 1만원이면 얼마나 살 수
  있어?" (no "예산" at all) is completely unaffected. This is a routing fix, not an
  answer-quality/prompt fix — deliberately not "call price_agent then delete its refusal text," per
  explicit instruction to avoid that pattern. Verified via `tests/test_routing.py` (+3: pure-budget
  question excludes price_agent, pure-price question still includes it, combined question includes
  both) and live: `agents_used == ["plan_agent"]` only for the pure budget question, both agents for
  the combined one, with no budget/strategy tool calls or state changes triggered by either (still a
  read-only query). Full transcript: REPORT.md §20-2.
- **`narrative` response field — separating "what to show" from "what to act on" structurally, not
  via string surgery** (found via the chat UI, 2026-09-20). The chat UI shows `answer` as the visible
  reply, but `answer` intentionally contains the full confirm/cancel instructions including the raw
  `confirmation_token` and `POST /confirm_...` path text (that's the whole point of the structured
  summary block, §15-7) — a UI that renders its own confirm/cancel buttons from the structured fields
  ends up showing that same token/path text twice: once as prose, once as a button. Regexing pieces
  back out of `answer` was explicitly ruled out (fragile, and this codebase's established stance on
  string-surgery fixes generally). Instead extracted the pre-summary part of `_build_final_answer`
  into its own function, `_build_narrative_text()` (same plan_agent-exclusion-when-budget-proposal
  rule, so the two never disagree on *what* prose survives) — `_build_final_answer` now calls it
  internally, so its own return value and every existing test against it are byte-for-byte unchanged.
  `run()` additionally returns `narrative` = `_build_narrative_text()`'s output. `answer` keeps 100%
  backward compatibility (still the only field a Swagger-only caller needs); a UI can show `narrative`
  for the readable part and drive its cards off the structured fields, with `answer` tucked into a
  collapsed debug area for anyone who wants the raw token/path text. Verified via
  `tests/test_answer_synthesis.py`/`test_awaiting_input.py` (narrative content matches
  `_build_final_answer`'s narrative portion exactly) and `ui/tests/test_ui_flow.py` (token string
  absent from the rendered chat bubble but present in the debug expander; other agents' explanations
  still show when a budget proposal is also present). Full transcript: REPORT.md §20-4.
- **Policy change (2026-09-20, user-confirmed): mid-month signup can now start *this* month, not just
  next month — `_budget_plan_snapshot()` picks one of three actions, not two.** Previously any signup
  after the 1st was unconditionally pushed to next month; the user explicitly changed this policy so a
  mid-month user can start immediately. `month_state._budget_plan_snapshot(now, state,
  requested_month=None)` now returns `{"action": "initial"|"advance"|"change", "effective_month", ...}`:
  - **`"initial"`** (`plan_start_month is None`): allowed target months are `{this_month, next_month}`.
    No `requested_month` ⇒ defaults to **this month** (the actual policy flip — it used to default to
    next month unless today was the 1st). A `requested_month` outside that pair (backdating, or more
    than one month out) doesn't get honored as-is — it falls back to this month and
    `month_mismatch=True`, same "explain, don't silently substitute" pattern as the existing
    next-month-only mismatch case, just with a different allowed set and different wording (`_build_
    final_answer` branches on `action` for this — "최초 시작은 이번 달 또는 다음 달만 가능합니다" vs.
    the change-case "정책상 다음 달부터만 적용됩니다").
  - **`"advance"`** (`plan_start_month` is set but still in the future, i.e. the plan hasn't started
    yet, *and* the caller explicitly asked for `requested_month == this_month`): moving a not-yet-active
    plan's start date earlier. New `month_state.advance_plan_start(amount_krw, new_start_month, now)`
    sets `plan_start_month` to this month and adds a `budget_history` entry for it — it does **not**
    touch the existing future entry (e.g. a previously-confirmed October budget stays exactly as it
    was) — advancing is additive, never destructive, per explicit instruction ("삭제하거나 이번 달로
    복사하지 말 것"). Only triggers on an *explicit* "this month" request while a future start is
    pending — re-proposing without naming a month while a plan is pending-future-start is treated as an
    ordinary `"change"` targeting whatever month is already scheduled, not an advance-by-default.
  - **`"change"`** (everything else — plan already active, or pending-future but no explicit
    this-month ask): unchanged from before — always next calendar month, mismatch-checked against
    exactly that one target.
  `propose_budget_change`/`confirm_budget_change` carry `action` (not just the old `is_initial` bool,
  kept for backward compat) through the whole propose→confirm lifecycle, including the staleness
  re-check (`calc_now["action"] != pending["action"]`, not just effective_month) — an `advance` proposal
  goes stale exactly like any other if the plan state changes underneath it before confirm.
  `init_plan()` gained an optional `start_month` param (the propose→confirm path always passes the
  validated month explicitly now; direct callers that omit it keep the pre-existing 1st-of-month-else-
  next-month fallback, which is why `tests/test_plan_state.py`'s direct-`init_plan()` tests didn't need
  to change — only the propose/confirm path's *default* changed, not the low-level function's own
  fallback). Deliberately broke and rewrote three pre-existing tests in
  `tests/test_budget_confirmation.py` that had encoded the *old* default (asserted `"2026-10"` for a
  bare mid-month proposal) — that was exactly the behavior being replaced, not a regression to guard
  against. New coverage in `tests/test_mid_month_start.py` (21 tests): this-month default, explicit
  next-month choice, backdating/over-shoot mismatch fallback, advance propose/confirm/cancel, advance
  preserving the existing future entry byte-for-byte, advance staleness, active-plan changes still
  always targeting next month (regression guard), existing-buy vs. no-buy `get_month_status` reflection
  mid-month, decline_day refusing to judge without a first-buy record, strategy-selection cutoffs for a
  mid-month-started plan, last-day-of-month signup, and backtest producing byte-identical output before
  and after an advance (`run_backtest` never reads `month_state` at all — confirmed by grep, not just
  assumption). Live-verified against Haiku 4.5 (`global.` profile, isolated `BTC_AGENT_DATA_DIR`): "9월
  부터 시작할게, 예산은 200만원으로 할래" (September being the actual current month in the test)
  produced `action="initial"`, `effective_month="2026-09"`; a full advance flow (explicit next-month
  signup → confirm → "이번 달부터 바로 시작하고 싶어. 이번 달은 150만원으로 할게") correctly called
  `get_month_status` first, then `set_monthly_budget(amount_krw=1500000, requested_year=2026,
  requested_month=9)`, produced `action="advance"` with `previous_start_month="2026-10"`,
  `previous_start_amount=2000000.0`, and after confirm `month_state.json` held **both** budget entries
  (1.5M for September, 2M for October) with `plan_start_month` moved to September; a bare "예산
  200만원으로 시작할래" with no month named at all defaulted to `effective_month="2026-09"` (the this-
  month default, live-confirmed, not just unit-tested). Full transcript: REPORT.md §21.
- **`plan_agent`'s prompt now covers mid-month first-buy handling, mid-month strategy selection, and
  backtest/actual-plan separation — all prompt-level, no new tools, because the underlying data
  (`ledger.month_budget_status()`, `can_select_strategy()`) already had everything needed.** Enabling
  mid-month starts surfaced a class of narrative risk that isn't a routing or state bug: an LLM asked
  "start this month" could plausibly assume "confirmed today ⇒ day-1 half-purchase already happened,"
  or answer a decline_day condition question by inventing a backdated virtual buy, or explain a
  backtest run as if it reproduced the user's actual partial month. None of these are code defects to
  fix (the domain functions were already correct — `ledger.month_budget_status()` aggregates by real
  `executed_date` regardless of which day the plan started; `evaluate_current_condition()` already
  refuses to judge decline_day without a real `first_buy_price`; `backtest.run_backtest()` has zero
  coupling to `month_state`, verified by grep), they're purely about what the model *says* given
  correct data. Added explicit instructions: always call `get_month_status` before saying anything
  about this month's buy status; if `buy_count > 0` already, report the real `remaining_krw` and don't
  repeat a "buy the first half" prompt; if a user claims a past buy that isn't in the ledger, ask for
  amount/quantity/price/date and route it through `record_virtual_buy`'s normal approval flow — never
  fabricate a record; if `buy_count == 0`, frame the budget's half as the first step regardless of which
  calendar day it is, but state plainly that recording only happens after the user reports the real
  execution; never assert a decline_day condition is met/unmet without a real first-buy price, and never
  synthesize a virtual backdated buy to make that judgment possible; strategy stays not auto-carried
  into next month (existing rule, restated in this context since mid-month starts made the question
  "does this month's pick apply going forward" newly relevant); and backtest output is never described
  as reproducing the user's actual partial-month history. No dedicated tests exist for prompt wording
  itself (as with every other prompt-only guidance in this file) — the structural guarantees it leans on
  (ledger aggregation, decline_day's None-first-buy refusal, backtest's independence) are what
  `tests/test_mid_month_start.py` actually covers.
- **`plan_agent`/`research_agent` system prompts each know what they're *not* authoritative on** —
  `research_agent` is told not to assert the user's actual current state (budget/strategy/plan-started)
  from docs alone; `plan_agent` is told not to explain a strategy's exact trigger *condition* from
  general knowledge (it has no `retrieve_docs`) and to defer that to docs instead. Both were added after
  live testing produced an actual contradiction/wrong answer — don't strip these out as "just wording."
- **`agent_node` appends today's KST date to every worker's system prompt at call time** (not baked into
  `_AGENT_SYSTEM_PROMPTS`, which are static strings) — found necessary via live testing: without it, the
  LLM has no way to resolve "오늘"/"어제" into an actual `YYYY-MM-DD` for tools like
  `record_virtual_buy(executed_date=...)`, and silently omits the field instead of guessing. Don't move
  this back into the static prompt strings; it must be computed fresh per call.
- **`select_strategy` is registered as `"read"` in `RISK_LEVELS` on purpose** — it does *not* go through
  `approvals.py`. Its own propose/confirm token flow (`month_state.py`) is the real gate. Don't "fix" this
  by moving it to `"write"` — that would run it through *both* gates redundantly and is explicitly the
  wrong shape per SPEC §14-0.
- **Approval gating is structural, not prompt-based** — `route_after_agent` in `_build_worker_graph`
  calls `guardrails.needs_approval` directly on `tool_calls`; do not move this check into a system
  prompt. The system prompt for `plan_agent` *does* instruct the LLM to call `select_strategy` once
  without a token first — but that's UX framing, not the actual control; `month_state.
  propose_strategy_change`/`confirm_strategy_change` is what actually enforces it.
- **All domain tools return strings/dicts, never raise** — `ledger.py`/`month_state.py` return
  `{"ok": bool, ...}`; `price_history.py`/`indicators.py`/`backtest.py` return `None`/`{"ready": False}`
  on missing data rather than raising. `src/tools.py`'s `classify_failure`/`get_btc_price_backup`
  (retryable/backoff/fatal + cached fallback) still backs the one old tool still in use, `get_btc_price`.
  Keep new tools consistent with this "never raise" convention.
- **`guardrails.py`'s input-guard/PII rules are deliberately narrow in one place**: destructive requests
  ("초기화해줘") are *not* hard-blocked — they're meant to flow through to the approval gate; only
  approval-bypass phrasing + an imperative execute verb together gets blocked. Don't reintroduce a
  blanket destructive-verb block here. `_EXECUTE_STEMS`(하다-conjugation words like 삭제/초기화) and
  `_IRREGULAR_EXECUTE_FORMS`(literal conjugated forms like 지워줘/제거해줘, added 2026-09-17 — "지우다"
  doesn't conjugate as stem+해 so it needs its own pattern) are both checked; if you add a new bypass
  verb that's an irregular conjugation, add its literal forms to the latter, not the former.
- **`_LAST_RETRIEVED_DOCS`/`_REQUEST_CONTEXT`/`_LAST_DATA_GAP` in `src/agent.py` are module-level,
  cleared per request** — this only works for a single in-flight request; not concurrency-safe
  (documented limitation, consistent with the single-process/single-user scope everywhere else).
- **`src/tools.py` still has `assess_dca_signal`/`get_price_history`/old `_compute_rsi`/old
  `search_ledger`/`record_virtual_buy`/`reset_ledger`, but `agent.py` no longer calls any of them**
  (only `tools.get_btc_price` survives) — this is now dead code, same status as `src/observability.py`.
  Don't resurrect it as a reference for how indicators/ledger should work; `indicators.py`/`ledger.py`
  are the current implementations and were built specifically to fix bugs in the old versions (RMA vs
  SMA RSI, 200-day cap mislabeled "52-week," no `record_id` to target amend/cancel at).
- **Strategy *selection* and "is the current condition met" are different questions — don't let a
  keyword-name match alone decide routing, and never gate selection on the condition value** (found via
  a real UI report, 2026-09-20: "RSI매수 로 할게" only ever reached `price_agent`/`research_agent` and
  got an explanation of what RSI is, never a `select_strategy` proposal — RSI ≤ 30 not being currently
  true was irrelevant; the user is allowed to *select* RSI strategy regardless of whether its condition
  has fired yet, same as they could pick 하락일/정기분할 at any RSI value). Rather than add one keyword
  for the exact reported sentence (explicitly rejected per the user's own instruction), `route_question`
  now matches on **strategy-name word × decision-word combination** — `_STRATEGY_NAME_WORDS` (rsi/
  하락일/정기분할/decline_day/biweekly) combined with `_STRATEGY_DECISION_WORDS` (a deliberately
  affirmative-only endings list: "로 할게"/"로 바꿔줘"/"로 정할래"/etc.) pulls in `plan_agent`. The
  affirmative-only word list is what makes negation ("RSI로 바꾸지 마") safe *by construction*, not by a
  separate negation check — "바꾸지 마" simply isn't one of the decision endings, so it can never match
  this rule. `plan_agent`'s own `select_strategy`/`can_select_strategy` gating (15일 이후 정기분할
  restriction, etc.) is untouched — this only fixes whether the request *reaches* `plan_agent` at all.
  Verified via `tests/test_routing.py` (8 new cases: 3 decision phrasings incl. the exact reported one,
  bare-name-without-decision-word non-match, negation non-match, plus the ledger/research cases below)
  and live against Haiku 4.5 (`global.` profile, isolated `BTC_AGENT_DATA_DIR`): "RSI매수 로 할게" →
  `strategy_change_needs_confirmation` token minted → `/confirm_strategy_change` → a follow-up status
  query correctly reports RSI as the selected strategy; "RSI로 바꾸지 마" → no `plan_agent` match, no
  proposal at all.
- **A past-tense buy *report* ("오늘 50만원어치 BTC 매수했어") is a statement of fact, not an
  imperative — routing that only recognizes command forms ("매수해줘") misses it entirely** (same UI
  report, 2026-09-20). Fixed by adding `"매수했어"`/`"샀어"`/`"구매했어"` to `ledger_agent`'s keywords
  and telling its prompt to treat these as reports to be recorded via `record_virtual_buy`, while
  explicitly excluding three phrasings that share surface words but aren't reports: a calculation
  question ("50만원 사면 얼마나 돼?" — price_agent's job), a decision-help question ("50만원 살까?" —
  not yet decided), and negation ("아직 안 샀어"/"안 살래" — no purchase happened). **When the report
  is missing `price_krw`/`quantity_btc` entirely, the fix does not rely on the model reliably calling
  `request_buy_execution_detail` first** — live verification with the actual submission model (Haiku
  4.5) reproduced exactly this failure: the prompt says to call the tool, but the model sometimes just
  answers in prose asking for price/quantity without calling anything, so no `awaiting_input` token gets
  minted and the next turn's "1억원에 샀어" has nothing to attach to. `_maybe_force_buy_execution_request`
  in `run()` is a structural safety net that doesn't depend on the model following that instruction: if
  `ledger_agent` matched, the turn produced neither an approval nor an `awaiting_input`, the message has
  exactly one KRW amount, no quantity mention, and no negation word, the server calls `request_
  buy_execution_detail` itself and overwrites `answers_by_agent["ledger_agent"]` with a clean, correct
  question — a message with two amounts or an already-present quantity is left alone (ambiguous enough
  that guessing which number is what would be worse than not intervening). Also fixed the KRW parser
  itself: `_parse_krw_amount` only ever handled "만" notation, silently returning `None` for "억"
  (100,000,000) — the natural unit for a BTC price — replaced with `_KRW_AMOUNT_VALUE_RE` (named groups
  for eok/man_after_eok/man/plain) so "1억원"/"1억 500만원"/"200만원" all parse correctly; `_BTC_
  QUANTITY_RE` added for the "0.005 BTC"/"0.005개"/"0.005비트코인" quantity notation, which never
  overlaps with the price notation by construction (one always ends in "원", the other never does).
  Full flow — report → `request_buy_execution_detail` (forced when the model doesn't call it itself) →
  follow-up price/quantity → `record_virtual_buy` approval card → ledger unchanged pre-approval →
  approve → correct `amount_krw`/`price_krw`/`executed_date` in the ledger, no fabricated time — is
  covered by `tests/test_buy_report_awaiting_input.py` (19 tests, including the forced-fallback cases
  and the deliberately-adversarial two-amounts/has-quantity/negation non-trigger cases) and re-verified
  live end to end against Haiku 4.5 in an isolated `BTC_AGENT_DATA_DIR`, including the calculation/
  decision-help/negation phrasings correctly producing no record and a double-approval call correctly
  returning 409 without duplicating the ledger entry.
- **A bare "전략" keyword match on `research_agent` for a personal-status question produces an unneeded
  refusal/redirect sentence sitting next to `plan_agent`'s correct answer** (same UI report, 2026-09-20):
  "9월의 예산과 전략을 알려줘" got a correct `plan_agent` answer plus an unnecessary `research_agent`
  note saying it doesn't handle personal state and to ask `plan_agent` — which the user had, in the same
  message, already gotten answered. Rather than special-case this one sentence, `route_question` now
  strips `research_agent` from the match only when **all** of: the question has personal-context signal
  (a `\d{1,2}월` month number, or "내"/"제"/"이번 달"/"다음 달"), it has no concept-query signal ("무슨
  뜻"/"정의가"/"원리가"/"조건이 뭐"/etc.), and "전략" was the *only* `research_agent` keyword that
  matched (re-checked against every other keyword in the list) — that last condition is what keeps a
  compound question like "내 전략은 뭐고 RSI는 무슨 뜻이야?" routing to both agents, since "RSI" is a
  second, independent `research_agent` keyword match. Verified via `tests/test_routing.py` (personal-
  status-alone, compound-question-both-agents, general-concept-question-still-reaches-research_agent)
  and live: the exact reported sentence now returns `agents_used == ["plan_agent"]` only, clean answer.
- **`get_month_status`'s tool output must state "first half is unconditional, second half is condition-
  gated" as two distinct sentences computed from real state — trusting the model to reconstruct that
  distinction from a single ambiguous "매수 신호를 기다리는 상태" produces a wrong answer** (same UI
  report, 2026-09-20, item #4): for `{월 예산 100만원, 사용액 0원, 매수 기록 없음, 선택 전략 RSI}` the
  actual answer was "남은 100만원의 예산으로 매수 신호를 기다리는 상태" — which wrongly implies the
  *entire* remaining budget, including the unconditional first-half amount, is waiting on the RSI
  signal. Fixed structurally (not by prompting the model to phrase it more carefully): the
  `get_month_status` tool wrapper in `agent.py` (not `month_state.py`'s domain function) now appends one
  of three deterministic sentences computed directly from `status["buy_count"]`/`month_state.
  month_end_cutoff_passed()`: (a) no buy yet + not month-end → state "사용액은 0원"(without asserting
  "no purchase happened" from record-absence alone) and name the first-half step amount explicitly; (b)
  buy_count > 0 → report the existing count instead of first-step guidance; (c) month-end already passed
  → state the full-remainder rule instead of a half-amount step (no duplicate first-half+full-remainder
  announcement). Verified via `tests/test_mid_month_start.py` (5 new cases, including a `datetime`-
  patched last-day-of-month case and a case confirming an actual non-half buy amount is preserved in the
  remaining-budget calc rather than assuming exactly half) and live: the exact reported scenario's
  `plan_agent` answer now states "현재 기록 기준 사용액은 0원"/names the first-buy-step amount
  explicitly, and — confirmed in the same live session after an approved buy — correctly switches to
  reporting the existing buy count instead of repeating first-step guidance.
- **DD/MDD policy change (2026-09-20, user-confirmed): drawdown is now close-based, not high-based, and
  MDD (maximum drawdown, not just "current drawdown") is a new, separate value.** The old `high`-based
  DD (`compute_drawdown()`) is gone entirely — `indicators.compute_dd_mdd()` never reads a candle's
  `high` field at all, verified by a dedicated test that changes `high` while holding `close` fixed and
  asserts the result is byte-identical. Both DD and MDD are computed in a **single pass** over the
  period's candles: track a running peak-close (the max close seen so far, starting fresh at the
  window's own start date — a peak from before the window never leaks in), compute
  `(close_t / peak_so_far - 1) * 100` at every day, and MDD is simply the minimum of that series while DD
  is that same value at the *last* day (which is mathematically the same as `(end_close / window_max_close
  - 1) * 100`, since by the last day the running peak *is* the window's global max) — this single-pass
  design is what makes `MDD ≤ DD ≤ 0` hold unconditionally, not something checked after the fact.
  Verified against every worked example in the request, including the two that pin down the "peak-before-
  trough" requirement: closes 100→60→90 gives DD=-10%/MDD=-40% (the deep dip is the whole story), while
  60→100→90 gives DD=-10%/MDD=-10% (the *early* low of 60 happens before any peak was set, so it
  contributes nothing to MDD — a naive "just take the window's min/max" implementation would wrongly
  compute -40% here too). `tests/test_dd_mdd.py` (23 tests) also covers: multiple peak/trough cycles
  picking the single deepest post-peak drop; a genuine month-end-date-correction case for the 48-month
  window that only shows up at a century boundary (2104-02-29's 48-months-back date is 2100-02-29, but
  2100 isn't a leap year since it's divisible by 100 but not 400 — corrected to 2100-02-28, verified by
  directly computing `_shift_months` first before writing the fixture, not by assumption); a 365-day
  window correctly spanning a leap year; a peak sitting just outside the requested window being excluded;
  zero/negative close values inside a window forcing `None` (never silently skipped); and one period's
  data shortfall not blocking a different period's calculation (see the next bullet, same principle at
  the `agent.py` level).
- **RSI/MA200's all-or-nothing gap check and each DD/MDD period's own window check are now independent —
  one indicator's missing data no longer blocks a different indicator that doesn't need that data**
  (2026-09-20, explicit requirement: "한 기간의 데이터 부족을 다른 기간이나 RSI 부족으로 일괄 처리하지
  말고"). Before this change, `_indicators_summary()` had one blanket `if len(records) < 200: return
  "..."` gate in front of *everything*, including the 30-day DD/MDD window, which only actually needs 30
  days of confirmed history — a account with, say, 100 days of history (not unusual right after
  `price_history` backfill or in a from-scratch test environment) would get a hard "insufficient data"
  wall even though its 30-day DD/MDD was perfectly computable. Restructured into two independent checks
  in the same function: RSI/MA200 still requires `len(records) >= 200` *and* a full-range gap check
  (`_required_range_gap_message`, unchanged — RSI's cumulative RMA genuinely needs the *entire* available
  history to be contiguous, not just the last 200 days, since `compute_rsi_series()` recomputes the whole
  series from `records[0]` every call); each of the three DD/MDD periods independently calls
  `compute_dd_mdd()`, which only checks *its own* window's date completeness (via `_window_for_period()`)
  — a gap or shortfall far outside a given period's window can no longer block that period. Two distinct
  "can't compute" messages are deliberately worded differently so they're never conflated: the RSI/MA200
  block message reuses the existing `_required_range_gap_message()` wording (contains "결측") when the
  cause is an actual internal gap, while a DD/MDD period's own "계산 불가" message deliberately never
  says "결측" (it can't tell the difference between "the window has a gap" and "there simply isn't that
  much history yet" — both collapse to the same `None` from `compute_dd_mdd()` — so it states only the
  period name and lets that ambiguity stand honestly rather than overclaiming a specific cause).
  `record_watch_decision`'s indicator snapshot got the identical treatment (RSI/MA200 populated only
  under the full gate, DD/MDD populated independently per period) — proven with a fixture that removes
  one day from the middle of a 250-day series: `rsi14` is correctly absent from the snapshot, but the
  last-30-days DD/MDD (which doesn't touch that removed day) is still present. Verified via
  `tests/test_dd_mdd.py` and updated `tests/test_required_range_gap_detection.py` (both files' assertions
  were rewritten to check for this independence rather than the old all-or-nothing behavior — this was a
  deliberate behavior change, not a regression fix).
- **DD/MDD snapshots carry a calc-basis version tag (`dd_mdd_calc_basis`, currently `"close_v1"`,
  exposed as `indicators.DD_MDD_CALC_BASIS`) so a future policy change can't silently blend with this
  one** (2026-09-20, explicit requirement not to reinterpret old watch-decision snapshots under the new
  basis). No real historical `record_watch_decision` snapshot ever stored a DD value at all before this
  change (the old snapshot only ever held `rsi14`/`ma200_deviation_pct`/`as_of`) — so there's no
  migration to do and no ambiguous old data to reconcile; the version tag exists purely so *this* policy
  change's own snapshots are self-describing going forward, the way `price_history.py`'s `CACHE_VERSION`
  already does for the price cache file. Don't bump this string for anything other than an actual change
  to how DD/MDD itself is calculated.
- **Live verification turned up a real DD/MDD coincidence, not a bug: the 48-month and 365-day windows
  can legitimately produce numerically identical DD/MDD** (2026-09-20, live check against Haiku 4.5, real
  price cache). The real BTC price history's actual 4-year peak (2025-10-08) and the post-peak trough
  (2026-08-14) both happen to fall inside the most recent 365 days, so both windows' running-peak
  algorithm converges on the exact same peak/trough pair even though the windows themselves span very
  different start dates (`start_date` differs, `mdd_peak_date`/`mdd_trough_date` are identical) —
  confirmed by computing both windows directly against the raw cache file before concluding this wasn't
  an off-by-one in `_window_for_period`/`_shift_months`. Don't "fix" this by assuming the two periods
  should generally differ; whether they match is a fact about the actual price history, not a code
  invariant.
- **Incidental finding, not touched this round: `src/tools.py`'s legacy `PRICE_CACHE_PATH` does not
  respect `BTC_AGENT_DATA_DIR`** (found during this round's live verification, 2026-09-20) — it's a
  module-level `Path(__file__).resolve().parents[1] / "data" / "price_cache.json"`, hardcoded independent
  of the env var that `ledger.py`/`month_state.py`/`price_history.py` all honor. A live query that calls
  the still-in-use legacy `get_btc_price` tool (SPEC's other domain modules were rewritten specifically to
  fix bugs in `tools.py`, but this one function was kept — see the `tools.py` bullet elsewhere in this
  file) writes the *real* `data/price_cache.json` even when `BTC_AGENT_DATA_DIR` points elsewhere, as
  happened during this round's isolated verification run. Low actual risk — the file is gitignored,
  holds only a short-lived public price+timestamp with no user data, and gets overwritten by any query
  regardless of isolation — but it's a real gap in the isolation guarantee this file documents elsewhere,
  left unfixed since it's unrelated to this round's actual task and wasn't asked for.
- **A spending/remaining-amount query without the literal word "예산" ("이번 달 얼마 썼어?") has the
  exact same `price_agent`-via-"얼마" false-positive as the budget-word case in §20-2, and needed the
  same fix generalized, not a new one-off word** (2026-09-20, live-reproduced: after a real buy approval,
  `plan_agent` answered spend/remaining/budget correctly, but `price_agent` also fired and added a
  "personal spending isn't my department" refusal next to it). `_BUDGET_CONTEXT_WORDS` alone couldn't
  catch this because "썼어"/"샀어"/"남았어"/"지출"/"쓴 금액" never contain "예산" — added
  `_SPENDING_QUERY_WORDS` and OR'd it into the same has-budget-context check that already existed for
  §20-2, so the fix is additive to the existing mechanism rather than a parallel one. A second, separate
  gap: "얼마 남았어?" on its own contains neither "이번 달" nor "예산" nor any other existing keyword, so
  it matched *nothing* before this fix (would have been rejected as out-of-scope) — a new rule
  (`_AMOUNT_QUESTION_RE` + any `_SPENDING_QUERY_WORDS` word → `plan_agent`) closes that gap the same way
  the budget-decision and buy-timing "open-ended phrasing" rules elsewhere in this file do. Combined
  price+spending questions are deliberately unaffected (the existing `has_price_context` override still
  wins), verified live: "이번 달 얼마 썼어? 그리고 BTC 지금 가격도 알려줘" still reaches both agents.
- **A genuinely compound question (both parts legitimately in scope, both agents correctly matched) can
  still produce boilerplate "that's not my department, ask the other agent" text from *each* agent about
  the *other* agent's already-answered part — this is a different defect from routing overlap and needs
  its own prompt guard on both sides** (2026-09-20, found live while verifying the fix above, not by a
  separate report). Unlike the accepted "keyword-sharing agent politely declines" tradeoff documented
  above (which is about an agent that was never the right target for the *whole* question), this is two
  agents that *were both* correctly matched for a real compound question, each redundantly commenting on
  the half the other one already covers — first observed as `price_agent` writing "지출 내역은 다른
  부서에 문의하시기 바랍니다" right next to `plan_agent`'s own correct spend/remaining answer in the
  very same response, and symmetrically `plan_agent` adding "BTC 가격은 제 담당 범위가 아닙니다, 
  price_agent에게 물어봐 주세요" right below `price_agent`'s own correct price answer. Fixed with a
  short paragraph in *both* prompts telling each agent: when the question also asks about the other
  agent's territory, answer only your own part and don't add a decline/redirect sentence about the
  other part — it's already answered in the same response, not left unanswered. Re-verified live after
  the fix: the outright refusal phrasing is gone; each agent now at most adds one short, non-declining
  cross-reference sentence ("이번 달 지출 내역은 plan_agent가 별도로 답변해드립니다"), which is the
  same accepted brief-mention style already used elsewhere in this file (e.g. `price_agent`'s existing
  "그 전략의 조건 충족 여부는 plan_agent가 별도로 확인해줄 수 있다는 점만 짧게 덧붙이고") — not a new
  problem, and not something this round tried to eliminate entirely.
- **`ledger_agent`'s past-tense-report keyword set ("샀어" etc.) also matches retrospective *summary*
  questions ("이번 달 얼마나 샀어?"), which are queries, not new purchase reports — the existing three
  report-exclusion categories (calculation/decision-help/negation) didn't cover this fourth one** (found
  during the same live check, 2026-09-20). Structurally this was already safe —
  `_maybe_force_buy_execution_request`'s forced-`awaiting_input` safety net requires exactly one KRW
  amount in the message, and a pure summary question like this has none, so it can never accidentally
  mint a token or approval — but the prompt still needed to say explicitly not to treat this as a report
  needing price/quantity follow-up, and to answer it via `search_ledger` (with year/month filled in,
  per the existing month-scoping rule) instead. Verified live: `agents_used` included both `plan_agent`
  and `ledger_agent` for "이번 달 얼마나 샀어?", `ledger_agent`'s answer correctly summed the ledger
  records for the month with no approval/awaiting-input generated, and file-hash comparison before/after
  confirmed the query made zero writes to `ledger.json`/`month_state.json`.
- **Final verification of `evaluation/test_queries.csv` against the real submission model turned up
  three real, distinct issues — one code gap, one stale CSV expectation, one stale judge keyword list;
  don't conflate them** (2026-09-20, running `evaluation/run_eval.py` for real, not just reading the
  CSV). Round 1 of the run: 21/24. Fixed each properly rather than patching the symptom:
  - **Code gap**: `_BUY_REPORT_KEYWORDS`/`_AGENT_KEYWORDS["ledger_agent"]` only covered "매수했어"/
    "샀어"/"구매했어" — CSV #6's "오늘 정기 매수로 100만원 넣었어 기록해줘" uses "넣었어" instead,
    which routing only caught by luck (the generic "기록" keyword happened to also be present) and the
    `_maybe_force_buy_execution_request` safety net didn't recognize at all, so the model's own prose-
    without-tool-call reproduced the exact §22-2 failure again under a phrasing variant. Added "넣었어"/
    "투자했어" (+ formal `-습니다` endings) to both lists — same "keep widening as reported" limitation
    already documented elsewhere in this file, not a one-off patch.
  - **Stale CSV expectation**: #14's `expected_tools=get_month_status` was written in 2026-09-17 when
    `evaluate_current_condition` didn't exist yet; it's been wired since 2026-09-18 (see its own bullet
    above) and is what the model now correctly calls for "10일에 조건 충족됐다는데 지금도 매수
    가능해?" — the CSV's own expectation, not the app, was wrong. Updated it and its note.
  - **Stale judge keyword list**: #9 "이더리움 지금 얼마야?" got a perfectly correct refusal ("저는
    비트코인(BTC) 시세와 지표만 조회할 수 있습니다. 이더리움 가격은 제 담당 범위가 아닙니다") that
    `run_eval.py`'s hardcoded `refusal_markers` list simply didn't contain a matching phrase for — the
    same recurring rule-based-judge-staleness class already documented for round 3. Added "담당 범위가
    아닙니다"/"담당이 아닙니다"/"담당하지 않"/"만 조회할 수 있습니다"/"만 답할 수 있습니다".
  - **A fourth issue surfaced while fixing #6's CSV wording**: giving the model *both* an amount and a
    price in one sentence without a quantity ("100만원어치를 1억원에 매수했어") made it stop and ask
    whether "1억원" meant the per-BTC unit price or the total paid — a reasonable-sounding but incorrect
    hesitation, since `price_krw` is always the per-BTC unit price by convention and the total is already
    `amount_krw`. Clarified this explicitly in both `record_virtual_buy`'s docstring and `ledger_agent`'s
    prompt (with the literal phrasing as the worked example) — but the model **still** asked the same
    question live after that fix. Rather than keep fighting a model quirk that isn't actually unsafe (an
    over-cautious clarifying question is a UX cost, not a wrong action), the CSV question itself was
    rewritten to state amount, quantity, *and* price explicitly, removing the inference the model
    seemed reluctant to make on its own — confirmed live afterward that it goes straight to
    `record_virtual_buy` and `approvals_needed`. The prompt/docstring clarification was kept anyway since
    it's still correct guidance for the more common case where quantity is genuinely omitted.
  - Round 2 after all fixes: **24/24**. `tests/test_routing.py`/`tests/test_buy_report_awaiting_input.py`
    each gained a regression test for the "넣었어"/"투자했어" gap specifically (both the routing keyword
    and the forced-fallback keyword, tested separately since they're two different lists for two
    different purposes). Not persisted as a new `evaluation/roundN_report.md` — the per-round report
    file layout was being reorganized independently of this task (`round3_report.md`–`round5_report.md`
    consolidated into `round2_report.md`, `round1_report.md`'s cross-links updated to match) while this
    verification was in progress; that reorganization was left untouched and this bullet is the record
    of what changed and why instead.
- **A greeting or "what can you do?" meta-question about the service itself is not the same kind of
  out-of-scope question as an unrelated topic — it needs its own fixed answer, not the generic rejection,
  and it needs to bypass the LLM entirely since no single agent owns it** (found via a real screenshot of
  the chat UI, 2026-09-20: "안녕"/"넌 무슨일을 할수있니" both got the same flat "이 어시스턴트는 BTC
  예산·전략 계획·시세·지표·매수 기록에 대해서만 답할 수 있습니다" as a genuinely unrelated question
  would — a new user asking what the service does learns nothing about what it can actually do). No
  `_AGENT_KEYWORDS` entry is the right fix here (this isn't "route to the agent that handles this," it's
  "no agent handles this, but it isn't nonsense either") — `run()` now checks
  `_GREETING_OR_CAPABILITY_RE` only in the already-existing `if not agents:` branch (so a question that
  *does* match a real agent, e.g. "안녕하세요, 이번 달 예산 얼마 남았어?", is never intercepted — the
  greeting text is just a prefix there and the real question routes normally) and returns a fixed
  `_CAPABILITY_INTRO_TEXT` with **zero LLM/graph invocation**, verified the same way the awaiting-input
  direct-resolution path is (`_NoInvokeLLM` fake whose `.invoke()` raises). **Korean text broke the
  naive `\b`-based regex approach**: `\b` is defined relative to `\w`, and Python's `re` treats Hangul
  syllables as `\w`, so there is no boundary *between* adjacent syllables the way there is between an
  English word and a space — `r"^\s*(안녕|...)\b"` matched "안녕" but not "안녕하세요" (found by an
  actual failing test, not by inspection) since "하" right after "녕" is also a word character. Fixed by
  dropping `\b` for the Korean prefix alternatives (anchored on `^\s*` instead, which is what actually
  captures "starts with a greeting") and keeping `\b` only for the Latin "hi"/"hello" alternatives, where
  it works as intended. Capability-question phrasing has the same open-ended-variants limitation as
  buy-timing/budget-decision phrasing elsewhere in this file (`무슨 일을 할 수 있`/`뭘 할 수 있`/`어떤
  도움`/`기능이 뭐` etc. are covered, not an exhaustive set) — accepted, not something this fix claims to
  close completely. Verified via `tests/test_greeting.py` and live against Haiku 4.5 (`global.` profile):
  both reported phrasings produced the exact fixed intro text with `agents_used == []`, a genuinely
  unrelated control question ("오늘 날씨 어때?") still got the original generic rejection unchanged, and
  a real question with a greeting prefix still routed to and was answered by `plan_agent` normally.
- **A vague "analyze BTC for me" request is not the same thing as the buy-timing verdict question this
  file already handles (§18/§18-2) — it needs its own keyword coverage, not an assumption that it's
  already covered by "지표"** (same screenshot-driven report, 2026-09-20). "BTC 분석해줘"/"BTC 현재
  상황 알려줘" matched no `price_agent` keyword at all (the semantically identical "BTC 지표 알려줘"
  already worked, via the literal word "지표") and got the generic out-of-scope rejection. Added
  "분석"/"현재 상황"/"상황 알려줘"/"상황이 어때" to `price_agent`'s keywords, and a short prompt
  paragraph telling it that a request phrased as "분석" still gets the same value-plus-meaning answer
  via `get_indicators` as every other indicator question in this file, not a composite verdict — "분석"
  sounds more like it's asking for a judgment than "지표", so this was called out explicitly rather than
  assumed to be covered by the existing buy-timing-verdict guidance. Verified via
  `tests/test_routing.py::test_general_analysis_requests_reach_price_agent` and live: both phrasings
  routed to `price_agent` alone and produced the same RSI/MA200/DD/MDD-values-plus-meaning answer as
  "BTC 지표 알려줘," ending with the same "종합해 지금이 적기라고 판정해드리지는 않습니다"-style
  disclaimer rather than a buy/wait recommendation.
- **"현재 전략"/"지금 전략" is exactly as personal a question as "이번 달 전략" — §22-3's fix only
  recognized month-phrase and possessive signals, missing this equally common way to ask the same
  thing** (found via a real screenshot, 2026-09-20). "현재 전략이 뭔지 알려주고, 매수 조건이
  충족되었는지 확인해줘" got a correct `plan_agent` answer (current strategy, condition-not-met status,
  budget/spend/remaining) plus an unneeded `research_agent` refusal ("저는 서비스의 일반 개념과 규칙을
  설명하는 역할을 하고 있어서... 개인 상태는 확인할 수 없습니다") — the exact §22-3 failure shape,
  recurring because the question uses neither a month number/word nor "내"/"제" to signal personal
  context, so `has_personal_context` came back `False` and the exclusion rule never fired even though
  "전략" was still the only `research_agent` keyword matched. Added "현재 전략"/"지금 전략"/"현재
  선택"/"지금 선택"/"선택된 전략"/"선택한 전략" to `_PERSONAL_STATUS_CONTEXT_WORDS` — deliberately
  compound phrases, not bare "현재"/"지금" (which would be far too broad and could suppress
  `research_agent` on legitimate concept questions that happen to contain those common words elsewhere
  in the sentence). Same open-ended-phrasing limitation as everywhere else in this file — this closes
  the two reported variants, not personal-status phrasing in general. Verified via
  `tests/test_routing.py::test_current_strategy_query_without_month_word_does_not_pull_in_research_agent`
  and live (Haiku 4.5, `global.` profile, isolated `BTC_AGENT_DATA_DIR`, state seeded to match the
  screenshot exactly — budget 1,000,000원, decline_day selected, one 500,000원 buy approved): the exact
  reported question now returns `agents_used == ['plan_agent']` only, with the same correct
  strategy/condition/budget content as before, just without the extra refusal.
- **"BTC가 뭐야"/"비트코인이 뭐야" had no keyword anywhere that would route them to `research_agent` —
  "DCA가 뭐야?" already worked only because "dca" happens to be a literal keyword, not because generic
  "X가 뭐야" phrasing was covered** (found via a real screenshot, 2026-09-20; `data/docs/BTC.md` exists
  specifically for this and was never reachable for these phrasings). Adding bare "btc"/"비트코인" as
  unconditional `research_agent` keywords was rejected — it would pull `research_agent` into completely
  unrelated territory like "오늘 50만원어치 BTC 매수했어" (a `ledger_agent` buy report) or "비트코인
  가격 알려줘" (a pure `price_agent` query), reintroducing exactly the class of unwanted-extra-agent
  problem fixed repeatedly elsewhere in this file. Instead added `_BTC_CONCEPT_RE`, a combination rule
  (topic word "btc"/"비트코인" *together with* a concept-question ending: "뭐야"/"무엇"/"란"/"이란", or
  "설명해"/"소개해" within a short distance) — mirrors the existing "topic word × decision/concept word"
  combo design (§22-1's strategy-selection rule, `_STRATEGY_CONCEPT_QUERY_WORDS`) rather than adding a
  bare high-collision keyword. Also folded a match of this regex into `has_concept_query` in the existing
  personal-status-removal check (see the "현재 전략" bullet above) so a `research_agent` match added by
  this new rule is never accidentally stripped back out by that unrelated logic — a real edge case that
  would otherwise only surface on a contrived combined sentence, closed defensively rather than left as
  a latent interaction bug between two independently-added rules. Verified via `tests/test_routing.py`
  (BTC-concept phrasings reach `research_agent`; pure price query and pure buy report do *not* pull it in
  via this rule) and live (Haiku 4.5, `global.` profile): "BTC가 뭐야"/"비트코인이 뭐야" both produced
  accurate, `BTC.md`-grounded explanations (supply cap, halving, decentralization, risks) via
  `research_agent` alone.
- **A second, independent bug surfaced live for "비트코인에 대해 설명해줘" even after the routing fix
  above: the question routed correctly and the embedding search found the exact right chunk, but the
  relevance gate (`retriever.assess_retrieval()`) rejected it anyway** (found live, 2026-09-20, not by
  inspection — the model's own choice of `retrieve_docs` query string, "비트코인 개념 정의", is what
  actually exposed this). `assess_retrieval()` extracts noun-tagged keywords from the query and from the
  retrieved chunk text via Kiwi, and requires ≥1 overlapping keyword *and* ≥40% overlap ratio. The bug:
  "개념"/"정의" are meta-nouns describing *what kind of answer* the user wants ("a concept," "a
  definition"), not the topic itself — they are real nouns (so Kiwi tags them, so they entered the
  keyword set) but have no reason to appear verbatim in prose written to explain the topic (`BTC.md`
  never uses the word "개념" at all), so they only ever inflate the denominator without ever being
  matchable — leaving only "비트코인" as an overlap out of 3 query keywords, a 33% ratio that misses the
  40% cutoff despite the retrieval being exactly correct. **The fix is not to lower the 40% threshold**
  (that would just let more genuinely-irrelevant results through, defeating the gate's purpose) — it's to
  stop counting words that were never going to be topical matches in the first place.
  `retriever._QUERY_META_NOUNS` (개념/정의/설명/소개/뜻/의미/질문/궁금/요약/내용) is now excluded inside
  `_keywords()` itself (used for both the query side and the document side, so the exclusion is
  symmetric) — a query that becomes empty after this filtering (e.g. a bare "설명해줘" with no real topic
  noun at all) still correctly yields `usable=False`, since an empty keyword set was already handled as a
  failure case before this change. Verified via `tests/test_retriever_relevance_gate.py` (4 tests, pure
  Kiwi tokenization, no AWS/embedding calls — deterministic and fast): meta-nouns excluded from
  `_keywords()`; the exact reproduced query+chunk pair now scores `usable=True`; a genuinely unrelated
  query (real topic words that truly don't overlap) is still correctly rejected; an all-meta-noun query
  still correctly yields `usable=False` rather than vacuously passing. Re-verified live end-to-end after
  restarting the server: "비트코인에 대해 설명해줘" now returns the full grounded `BTC.md` explanation
  instead of the false "이 서비스의 문서에는... 담겨 있지 않습니다" claim.
- **"이력"/"내역" are common synonyms for "기록" that `ledger_agent`'s keyword list simply didn't have**
  (found via a real screenshot, 2026-09-20). "매수 이력 보여줘" got the generic out-of-scope rejection
  even though `search_ledger` can answer it — a plain missing-synonym gap, same shape as many other
  keyword additions in this file, not a design problem. Added "이력"/"내역" to
  `_AGENT_KEYWORDS["ledger_agent"]`. Verified via `tests/test_routing.py::test_history_synonyms_reach_ledger_agent`
  (3 phrasings) and live (Haiku 4.5, `global.` profile, state seeded to match the screenshot — budget
  1,000,000원, RSI selected, one 500,000원 buy approved): "매수 이력 보여줘" now returns the correct
  record table via `ledger_agent`.

## How this was verified

**Without AWS credentials**: every module was tested directly against real Upbit data with scratch
scripts, not just read for plausibility. `agent.py`/`app.py` import cleanly and every `@tool`-wrapped
function was invoked directly via `.invoke()` (bypasses the LLM) to confirm the wiring, including
`execute_approved_action`/`reject_approved_action`'s 200/404/409 shapes. `retriever.build_chunks()`
(pure Python) confirmed all 8 docs chunk correctly with real metadata. **Deterministic calculation
correctness** (as opposed to "does invoke() run without crashing") is now covered by `tests/` (pytest,
70 tests, see Commands) — budget/avg-price math, RSI-30/±5%-decline boundary values (exactly-at,
just-above, just-below each), KST 09:00/15th/month-end cutoffs, real-thread approval-race concurrency
(2,500 concurrent attempts, always exactly one winner), backtest reproducibility and no-future-data-leak,
no duplicate condition+month-end-fallback buy in the same month, and guardrail negatives.

**With `.env`/AWS credentials**: `boto3`/Bedrock connectivity, RAG embeddings+search (real
`BedrockEmbeddings`, a real similarity search for "RSI가 뭐고 어떻게 계산해?" returned 4 chunks all
correctly from `RSI.md`) — all confirmed working, not the blocker for anything below.

**Full live conversation (2026-09-17, run against sonnet-4-5 before the Haiku 4.5 model decision below —
historical verification, not the current submission model) — every flow in the request-flow
diagram above has now been exercised end to end with real Bedrock tool-calling, not just `.invoke()`**:
initial budget setup → start-month confirmation, 48-month 3-strategy backtest comparison
("2022년 9월 ~ 2026년 8월" — matches the independently-computed 1,461-day window for a 2026-09-17
query), strategy propose→confirm→re-query, strategy-change cancel, buy-record propose→approve→ledger/
budget query, amend/cancel→approve, watch-decision recording, approval-reject→same-id-blocked,
data-freshness-gate block→proceed-with-stale-data retry (staleness reproduced by deliberately truncating
a copy of the real cache, not by waiting for it to actually go stale), PII masking, and prompt-injection
blocking. Full transcripts and exact request/response bodies: `REPORT.md` §3. Real defects found and
fixed this way (each: reproduced → root-caused → fixed → re-verified live) — see `REPORT.md` §4 for the
full table; the highlights that changed durable code behavior (not just this file's own history) are the
ones already called out in "Key invariants" above (research/plan_agent role-boundary prompts, the
`answer`-synthesizes-approvals-too fix, `search_ledger`'s month filter, the "지워줘" guardrail gap, and
the `get_month_status`/`plan_start_month` month-labeling fix).

**A second, independent pass — evaluation pipeline (2026-09-17)**: `evaluation/run_eval.py` (rule-based
judge() — string/tool-call checks only), `evaluation/llm_as_judge.py` (new — a separate LLM call grades
each answer against a rubric; this is the only mechanism that can catch "two agents said opposite things
about the user's state," which rule-based string matching can't express), and `tests/` (pytest,
deterministic calculation correctness) are three genuinely different things and are reported separately
— never collapse them into one "eval passed" number, and **always track which model generated the
answers being graded separately from which model did the grading** — round 3 (2026-09-17, all Sonnet):
pytest 70/70; rule-based 19/22; LLM-as-Judge 17/22 (`evaluation/round2_report.md#legacy-round3`). Round 4
(2026-09-18, Sonnet's daily quota ran out mid-round, switched to Haiku *with the user's explicit
approval* for both answer generation and judging): pytest 102/102 (model-independent); rule-based
22/22; LLM-as-Judge 14/22 → 19/22 (`evaluation/round2_report.md#legacy-round4`, which also lists the 3 remaining
LLM-as-Judge failures by question ID with the actual answer text and why each is judged a scenario/CSV
issue rather than a code defect, and classifies every judge/CSV/marker change made that round as either
"fixed a wrong verdict" or "loosened the bar," with the reasoning for each). **Round 3 and round 4 used
different models — never read round 4's numbers as "Sonnet improved to 22/22"; Sonnet has not been
re-verified against the current code.** Two real bugs were found *in the evaluation scripts themselves*
in round 3 (not the app) — `run_eval.py`'s guardrail branch mis-graded a PII-masking test case as if it
should have been blocked, and its `refusal_markers` keyword list missed several plainly-valid refusal
phrasings — both fixed; a keyword-matching judge can never be made fully airtight, which is exactly why
`llm_as_judge.py` exists as a second, independent check.

**Data preparation, reconfirmed 2026-09-17**: cache spans 2021-12-04 → 2026-09-16 (1,748 daily candles,
directly scanned — span-in-days equals record count, so no gaps, no duplicates). For a query made
2026-09-17, the 48-month backtest window is 2022-09-01 → 2026-08-31 (1,461 days), leaving a 271-day
lead-in before that window starts (200-day indicator minimum + 71-day margin) — both numbers independently
recomputed from the actual dates, not just asserted. Latest confirmed candle: 2026-09-16, confirmed as of
2026-09-17 09:00 KST (`check_freshness()` called directly at 2026-09-17 10:48 KST returned
`{"fresh": true, "expected": "2026-09-16", "last_available": "2026-09-16"}`).

**Resolved 2026-09-18**: `month_state.evaluate_current_condition()` is now wired (see the month_state.py
bullet above) — no longer an open item.

**Full live end-to-end verification of the budget-change propose→confirm flow (2026-09-18, Haiku 4.5,
isolated `BTC_AGENT_DATA_DIR`)**: `POST /query` "매달 200만원씩 투자할래" → `set_monthly_budget` called,
`budget_change_needs_confirmation` came back with a real token, `data_manual_test/month_state.json`
confirmed **not yet written** → `POST /confirm_budget_change` with that token → 200, and the state file
now shows the correct amount/effective month → a follow-up `/query` "다음 달 예산 얼마야?" correctly
reported it back. Separately verified: cancel leaves the file untouched; re-confirming an
already-confirmed token returns 409; all 5 intent-classification cases from the "Key invariants" bullet
above were re-run end to end with real tool-call traces and file diffs, not just routing.

**Full live verification of the 4-issue combined fix (2026-09-20, Haiku 4.5, `global.` profile, isolated
`BTC_AGENT_DATA_DIR`)** — see the four "Key invariants" bullets above for the root causes; this entry is
just the live-verification record: "RSI매수 로 할게" → `strategy_change_needs_confirmation` token →
`/confirm_strategy_change` → a follow-up "9월의 예산과 전략을 알려줘" reports RSI as selected, from
`plan_agent` alone (no `research_agent` redirect note); "RSI로 바꾸지 마" → no proposal, no `plan_agent`
match; the same personal-status query's answer also happened to demonstrate the `get_month_status`
enrichment working correctly ("첫 매수 단계로 500,000원 매수를 고려해보세요... 나머지 500,000원은
RSI 조건에 따라"); the full buy-report flow — "오늘 50만원어치 BTC 매수했어" → model answered in prose
without calling `request_buy_execution_detail` (the exact failure the structural fallback exists for) →
server-forced `awaiting_input` token → "1억원에 샀어" → `record_virtual_buy` approval card
(`amount_krw=500000, price_krw=100000000, executed_date=<today>`) → ledger confirmed **empty** before
approval → approve → ledger shows the exact values, no fabricated time → a follow-up status query
correctly reports 사용액 500,000원/남은 예산 500,000원 and lists the 1 buy record (confirming the
existing-buy-record variant of the `get_month_status` fix too); separately verified live: "50만원 사면
얼마나 돼?"/"50만원 살까?"/"아직 안 샀어" all produce no `awaiting_input` and no record; calling
`/approve` twice on the same approval_id returns 200 then 409 with no duplicate ledger entry. The
month-end-cutoff variant of the `get_month_status` fix was verified only via the mocked-`datetime` unit
test, not live (would require either waiting for an actual month-end or a live run with the real clock
past the 15th/month-end cutoff, neither attempted this round).

**Full live verification of the RSI/DD/MDD indicator expansion (2026-09-20, Haiku 4.5, `global.`
profile, isolated `BTC_AGENT_DATA_DIR` seeded with a read-only copy of the real price cache)** — see the
DD/MDD "Key invariants" bullets above for the design; this entry is the live-verification record: "지금
BTC 지표 요약해줘" → `agents_used=['price_agent']`, tools called `get_btc_price`/`get_indicators` →
final answer correctly presented RSI(52.33)/MA200 deviation(+2.42%) plus a genuine three-row table for
30-day/365-day/48-month DD and MDD, each stating "최고 종가" (not "최고가") with its exact date, plus
the required "미래를 예측하지 않는다" caveat verbatim — all of this came from the tool's own structured
text, not something the model had to reconstruct correctly on its own. A follow-up "최근 48개월 MDD
알려줘" correctly routed to `price_agent` (accurate figure, correct peak/trough dates) plus
`research_agent` (politely declined to supply a live numeric value, consistent with the pre-existing,
documented "keyword-sharing is fine when the non-matching agent just declines" tradeoff — not a new
defect introduced by adding "mdd" to `research_agent`'s keywords, same shape as the existing RSI/이동평균
/드로다운 sharing). No repeated retries were needed (no rate limiting encountered). The month-end-passed
branch of `get_indicators`'s per-period independence (a period whose window has *no* data at all vs. one
with an internal gap) was verified only via `tests/test_dd_mdd.py`'s unit tests, not live — reproducing
either condition live would require deliberately truncating the real price cache, which wasn't done this
round to avoid any risk to the isolated copy being mistaken for real data.

**Full live verification of the spending/remaining-query routing fix (2026-09-20, Haiku 4.5, `global.`
profile, isolated `BTC_AGENT_DATA_DIR`)** — see the "Key invariants" bullets above for the fixes; this
entry is the live-verification record. State was seeded to match the exact reported scenario (budget
1,000,000원 confirmed, one 500,000원 buy approved), then a file-hash of `ledger.json`/`month_state.json`
was taken before and after each of the four reported query phrasings ("이번 달 얼마 썼어?"/"이번 달
얼마나 샀어?"/"얼마 남았어?"/"이번 달 매수에 쓴 금액 알려줘") — for every one: `price_agent` was
absent from `agents_used`, no refusal phrasing (checked against a marker list: "담당하지 않"/"재무
담당자"/"다른 채널"/"제 담당이 아니"/"제공하지 않습니다"/"다른 부서") appeared anywhere in `answer`,
`plan_agent` correctly stated spend/remaining/budget figures, and the before/after file hashes were
identical (confirming a pure query makes zero writes). The combined price+spending question ("이번 달
얼마 썼어? 그리고 BTC 지금 가격도 알려줘") correctly reached both `price_agent` and `plan_agent`, first
without the cross-agent-boundary fix (reproduced the "지출 내역은 다른 부서에 문의" / "BTC 가격은 제
담당 범위가 아닙니다" pair described in its own "Key invariants" bullet) and then, after that fix and a
server restart, with the outright refusal phrasing gone from both sides and only a short non-declining
cross-reference sentence remaining on each. A pure price question ("BTC 지금 가격 얼마야?") and a pure
buy-timing/calculation question set were re-checked to confirm `price_agent`-only routing still works
unaffected. No repeated retries were needed (no rate limiting encountered).

**Full live verification of the greeting/capability-intro and vague-analysis-request fixes (2026-09-20,
Haiku 4.5, `global.` profile, isolated `BTC_AGENT_DATA_DIR`)** — reproduced the exact screenshots that
reported both issues: "안녕" and "넌 무슨일을 할수있니" now both return the fixed
`_CAPABILITY_INTRO_TEXT` with `agents_used == []`; "BTC 분석해줘" and "BTC 현재 상황 알려줘" both
route to `price_agent` alone and produce a full RSI/MA200/DD/MDD values-plus-meaning answer ending in the
standard no-composite-verdict disclaimer, matching the already-working "BTC 지표 알려줘"; a genuinely
unrelated control question ("오늘 날씨 어때?") still received the original generic out-of-scope
rejection unchanged. No repeated retries were needed.

**Full live verification of the "현재 전략" personal-status fix (2026-09-20, Haiku 4.5, `global.`
profile, isolated `BTC_AGENT_DATA_DIR`, state seeded to match the reported screenshot exactly)**: "현재
전략이 뭔지 알려주고, 매수 조건이 충족되었는지 확인해줘" now returns `agents_used == ['plan_agent']`
only — the correct strategy/condition/budget answer is unchanged, only the extra `research_agent`
refusal is gone. No repeated retries were needed.

**Full live verification of the BTC-concept routing + relevance-gate fixes (2026-09-20, Haiku 4.5,
`global.` profile, isolated `BTC_AGENT_DATA_DIR`)**: "BTC가 뭐야"/"비트코인이 뭐야" both now route to
`research_agent` alone and produce accurate `BTC.md`-grounded explanations; "비트코인 가격 알려줘"
still routes to `price_agent` alone (routing fix didn't over-reach); "비트코인에 대해 설명해줘" —
which surfaced the separate relevance-gate bug live, not by inspection — was re-run after the
`retriever.py` fix and the server restart, and now also returns the full grounded explanation instead of
the false "not in our docs" claim. No repeated retries were needed.

**Still open / needs a decision**: `evaluation/run_ragas.py` doesn't currently run in this environment
(ragas 0.3.0 vs. Python 3.14 asyncio incompatibility, not an app bug) — see Commands above and
`evaluation/round2_report.md#legacy-round3` §6.

## Data files

- **`BTC_AGENT_DATA_DIR` env var (added 2026-09-18)** — if set, `ledger.py`/`month_state.py`/
  `price_history.py` all read/write under that directory instead of the repo's `data/`. Unset ⇒ identical
  to before (default path, no behavior change — see `tests/conftest.py`'s isolation, which overrides the
  module attributes directly and doesn't depend on this env var either way). Exists specifically so a
  human can manually exercise the running server through Swagger (`/docs`) without touching the real
  `data/ledger.json`/`data/month_state.json` — see `evaluation/manual_test_guide.md` for the isolated
  `data_manual_test/` directory (gitignored) and the recommended call sequence. `retriever.py`'s
  `chroma_db/` is intentionally *not* covered by this — it's read-only doc embeddings, not user state, so
  sharing it between a manual-test run and production is safe.
- `data/price_history.json`, `data/ledger.json`, `data/month_state.json`, `data/price_cache.json` — all
  gitignored, regenerable runtime state (`price_history.ensure_backfilled()` rebuilds the first one from
  Upbit; the rest start empty). `price_history.json`'s on-disk shape is
  `{"version": int, "candles": [...]}` (added 2026-09-18, `CACHE_VERSION` in price_history.py) — a bare
  JSON list on disk means a pre-migration (potentially candle-confirmation-contaminated) file;
  `load_history_meta()`/`update_incremental()`/`ensure_backfilled()` detect that and migrate
  automatically (re-fetch + overwrite the most recent few days, then bump the version) — don't hand-edit
  this file's shape without updating `load_history_meta()`/`save_history()` together.
- `data/docs/*.md` — 8 files per SPEC.md §8's restructuring (done): `BTC.md`, `DCA.md`, `RSI.md`,
  `market_indicators.md`, `backtest_guide.md`, `service_rules.md` (new), `dca_strategy.md` (rewritten —
  service-specific 3-strategy conditions only, old composite-indicator criteria removed),
  `risk_management.md` (unchanged). `glossary.md` retired, content redistributed. `retriever.DOC_META`
  updated to match. `src/retriever.py`'s `build_chunks()` (pure Python, no AWS) was verified against all
  8 files — 43 chunks, every file gets its real category (none fall through to `_DEFAULT_DOC_META`).
  **`build_vectorstore()` now reuses the persisted collection only when a stored fingerprint
  (`chroma_db/_index_meta.json`) exactly matches a hash of current doc content + chunking config
  (`CHUNK_SIZE`/`CHUNK_OVERLAP`) + embedding model id (`compute_index_fingerprint()`), and the actual
  collection size too — otherwise it deletes and rebuilds from scratch** (added 2026-09-18, hardened
  same day) — it used to unconditionally `Chroma.from_documents(...)` (append) every time a new process
  called it, so `chroma_db/`'s collection silently grew across every session that ever ran
  `research_agent`; by the time this was caught it held **1,118 chunks (~26x the real 43)**, which made
  `k=4` retrieval return near-duplicate copies of whatever the single closest chunk was instead of 4
  actually different ones — the concrete, reproduced symptom was `research_agent` answering "not found
  in the docs" for a query (e.g. "DCA가 뭐야?") whose answer chunk was retrieved but appeared 4 times
  identically. **A first fix compared only chunk *count*** (good enough to fix the immediate bug) **but
  can't detect a document whose content changed while keeping the same chunk count — the fingerprint
  hash is what actually covers that** (verified with fake deterministic embeddings in
  `tests/test_retriever_index_versioning.py`: unchanged-docs restart → no growth; same-length changed
  content → old content gone from search, new content present; deleted doc → only its chunks removed).
  confusing especially weaker models. Don't reintroduce unconditional `from_documents` here.
- `evaluation/test_queries.csv` — 22 rows, current tool set, run against the live server (2026-09-17).
  Submission summaries are in `evaluation/round1_report.md` and `evaluation/round2_report.md`; earlier development results are historical appendices in round2. `evaluation/_eval_seed.py` gives both `run_eval.py`/`llm_as_judge.py` a shared "plan already
  started last month, one buy record exists" precondition (several CSV rows assume this).
  `evaluation/_eval_scratch/` (gitignored) is where those two scripts point `ledger.LEDGER_PATH`/
  `month_state.STATE_PATH` — never point them at the real `data/*.json` (see Commands section above for
  why).
- `tests/` — pytest suite, 70 deterministic tests, isolated via `tests/conftest.py`'s autouse fixture
  (redirects `ledger.LEDGER_PATH`/`month_state.STATE_PATH`/`price_history.HISTORY_PATH` to `tmp_path` and
  clears `approvals._STORE`/`month_state._PENDING_STRATEGY_CHANGES` per test).
