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

# Deterministic calculation tests (no AWS needed, no LLM calls) — 143/143 as of 2026-09-19
python -m pytest tests/ -v

# Evaluation (real Bedrock/API calls — cost incurred, needs .env; CSV/tool names now match current
# tool set — see evaluation/round3_report.md for the full breakdown)
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
raise anything, it'll just quietly corrupt the averages. See `evaluation/round4_report.md` §4 for the
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
0 errors, 0 NaN, context_recall 1.00 avg — see `evaluation/round5_report.md`. The result file
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
  zero→50, gain-only→100, loss-only→0), `compute_ma_deviation_pct()`, `compute_drawdown()` (1-month/
  1-year by day-count, 4-year by calendar-month arithmetic per SPEC §6-2 — *different* windowing rules,
  don't conflate them; returns `None` on any gap in the window rather than stretching it). None of these
  round internally (SPEC §6-5: rounding is display-only) — `agent.py`'s tool wrappers do the formatting.
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
  plan-start month (mid-month signup ⇒ starts next month, exposed via `get_month_status()`'s
  `plan_start_month` field so `agent.py`'s tool wrapper can always state "queried month" vs. "plan start
  month" vs. "strategy selected in that month" as three distinct facts — added 2026-09-17 after finding
  a worker response that blurred them together, see "How this was verified"), per-month budget history
  (`set_monthly_budget` always schedules for *next* calendar month), per-month selected strategy + change
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
POST /query {question, proceed_with_stale_data}
  → guardrails.input_guard   (blocks prompt-injection/secret-leak/jailbreak/approval-bypass attempts)
  → guardrails.mask_pii      (masks phone/email/resident-id/AWS keys/exchange API secrets)
  → agent.route_question     (deterministic keyword routing, NO LLM call — src/agent.py:_AGENT_KEYWORDS)
  → one LangGraph worker per matched agent, each its own agent↔tools loop (MAX_STEPS=4):
       price_agent    (get_btc_price / get_indicators — values+meaning, no composite verdict, SPEC §3-1)
       plan_agent     (get_month_status / set_monthly_budget / select_strategy / run_backtest)
       research_agent (retrieve_docs → RAG over data/docs/*.md — SPEC §8's 8-file restructuring is done:
                        BTC.md/DCA.md/RSI.md/market_indicators.md/backtest_guide.md/service_rules.md
                        (new) + rewritten dca_strategy.md + unchanged risk_management.md; glossary.md
                        retired, content redistributed)
       ledger_agent   (search_ledger read; record_virtual_buy/amend_virtual_buy/cancel_virtual_buy
                        write; record_watch_decision read; reset_ledger destructive)
  → per-call gate: guardrails.needs_approval(tool, args) — write/destructive tool_calls route to
       await_approval_node instead of executing (conditional graph edge, not a prompt instruction).
       That node calls approvals.create() to mint an approval_id and stores {tool, args} server-side —
       the id (not raw tool/args) is what comes back in approvals_needed.
  → agent.judge_output       (rule-based: drops low-content or unsupported-claim-without-evidence answers)
→ {"answer", "contexts", "trace", "agents_used", "approvals_needed": [{"approval_id", "tool", "args",
    "reason"}], "data_gap_needs_confirmation"?, "strategy_change_needs_confirmation"?,
    "budget_change_needs_confirmation"?}
    (base contract + extras — this response shape isn't pinned to one SPEC.md section number since it's
    grown incrementally; SPEC.md §4-2/§5 describe the budget/strategy confirmation fields specifically).
    `answer` always includes a human-readable summary of any pending approvals too (not just the
    structured `approvals_needed` field) — see the "answer synthesis" invariant below.

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
pytest 70/70; rule-based 19/22; LLM-as-Judge 17/22 (`evaluation/round3_report.md`). Round 4
(2026-09-18, Sonnet's daily quota ran out mid-round, switched to Haiku *with the user's explicit
approval* for both answer generation and judging): pytest 102/102 (model-independent); rule-based
22/22; LLM-as-Judge 14/22 → 19/22 (`evaluation/round4_report.md`, which also lists the 3 remaining
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

**Still open / needs a decision**: `evaluation/run_ragas.py` doesn't currently run in this environment
(ragas 0.3.0 vs. Python 3.14 asyncio incompatibility, not an app bug) — see Commands above and
`evaluation/round3_report.md` §6.

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
  `evaluation/round{1,2}_report.md` hold old-concept results, now stale; `evaluation/round3_report.md` is
  current. `evaluation/_eval_seed.py` gives both `run_eval.py`/`llm_as_judge.py` a shared "plan already
  started last month, one buy record exists" precondition (several CSV rows assume this).
  `evaluation/_eval_scratch/` (gitignored) is where those two scripts point `ledger.LEDGER_PATH`/
  `month_state.STATE_PATH` — never point them at the real `data/*.json` (see Commands section above for
  why).
- `tests/` — pytest suite, 70 deterministic tests, isolated via `tests/conftest.py`'s autouse fixture
  (redirects `ledger.LEDGER_PATH`/`month_state.STATE_PATH`/`price_history.HISTORY_PATH` to `tmp_path` and
  clears `approvals._STORE`/`month_state._PENDING_STRATEGY_CHANGES` per test).
