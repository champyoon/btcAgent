# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service — **BTC DCA Agent** — that helps a single user run a monthly-budget BTC DCA plan:
compare three purchase strategies (decline-day / biweekly / RSI) against a 48-month backtest, track
real (self-reported) buy/watch records and remaining budget, and answer questions via a LangGraph
multi-agent Supervisor backed by Amazon Bedrock (Claude). **`src/agent.py:MODEL_ID` is currently set to
`us.anthropic.claude-haiku-4-5-20251001-v1:0`**, temporarily swapped from
`us.anthropic.claude-sonnet-4-5-20250929-v1:0` because that model's daily Bedrock token quota was
exhausted on this account (see "How this was verified") — switch it back once quota/budget allows;
nothing else needs to change to swap models. `src/retriever.py` has its own separate `MODEL_ID` constant
for `build_rag_chain()`, but that function is currently unused (`agent.py`'s `retrieve_docs` tool calls
`retriever.search_docs()` directly, not the RAG chain) — don't bother changing it unless that changes.

## 모델 교체 (이 계정에서 실제 확인된 사용 가능 모델)

`src/agent.py`의 `MODEL_ID` 상수 하나만 바꾸면 된다(위 참고). 이 AWS 계정에서 `list_inference_profiles`로
실제 확인된 Anthropic/Amazon 모델 중 자주 쓸 만한 것들:

| 모델 ID | 비고 |
|---|---|
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | 원래 기본값. 일일 추론 쿼터 소진 이력 있음(이 문서 위 참고) |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | **지금 사용 중** — 쿼터 여유, 실제 대화 테스트 완료 |
| `us.anthropic.claude-sonnet-4-6` | 미검증 |
| `us.amazon.nova-pro-v1:0` | Anthropic 계열 아님 — 도구 호출(tool-calling) 방식이 달라 `langchain_aws`
쪽 지원 여부·프롬프트 형식 호환을 먼저 확인 필요. 미검증 |
| `us.amazon.nova-lite-v1:0` / `us.amazon.nova-2-lite-v1:0` | 위와 동일한 이유로 미검증 |

`global.` 접두사가 붙은 동일 모델(예: `global.anthropic.claude-sonnet-4-5-20250929-v1:0`)도 이 계정에서
쓸 수 있다 — 리전 라우팅 방식만 다르고(추론 프로파일이 여러 리전에 걸쳐 라우팅될 수 있음), 나머지는
`us.` 버전과 동일하게 다루면 된다. 쿼터가 리전별로 분리돼 있을 수도 있으니, 한쪽이 막히면 같은 모델의
`global.` 버전도 시도해볼 만하다.

**주의**: Nova 계열은 Anthropic Claude와 다른 모델 패밀리라, `ChatBedrockConverse`가 도구 호출
스키마를 문제없이 변환해주는지 실제로 확인 안 된 상태다 — 전환 전에 최소 1회 `.invoke()` 스모크
테스트를 거칠 것.

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

# Evaluation (both make real Bedrock/API calls — cost incurred, needs .env; test_queries.csv/run_eval.py
# still reflect the OLD concept's tool names — need re-authoring, see SPEC.md §14 item 14)
python evaluation/run_eval.py --round 1
python evaluation/run_ragas.py
```

No test framework/linter is configured. `.env` at repo root must supply AWS credentials
(`AWS_ACCESS_KEY_ID` etc.) for Bedrock access — loaded via `load_dotenv()` in `app.py` and the
`evaluation/` scripts. Everything described below except the actual Bedrock/LLM call itself has been
verified without needing `.env` (see "How this was verified").

## Architecture

`app.py`/`src/agent.py`/`src/guardrails.py` are wired to the **new** BTC DCA Agent concept. Domain logic
lives in seven standalone modules that `agent.py`'s `@tool`-wrapped functions call into:

- `src/price_history.py` — local OHLC price cache (`data/price_history.json`, gitignored): paginated
  backfill (~1750 days, covers 48-month backtest + ≥200-day indicator lead-in), `confirmed_records()`
  (filters out the not-yet-closed candle per the KST 09:00 boundary — SPEC §7-1),
  `select_backtest_months()` (the specific calendar "last month," not a silent fallback to an older
  complete one — SPEC §6-1 edge case around month-start 09:00), `update_incremental()` (called at the
  top of every tool that needs fresh data — no separate scheduled job).
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
  `average_buy_price()` only aggregate `type=buy, status=active` records. Field-omitted-vs-explicit-
  `None` in `amend_virtual_buy` is distinguished via a private `_UNSET` sentinel default — the `agent.py`
  tool wrapper collapses that distinction back to "omit if None" at the LLM-tool-call boundary (the LLM
  has no reason to explicitly null a field), so if you need the finer distinction, call `ledger.py`
  directly rather than through the tool.
- `src/approvals.py` — SPEC §10-1 approval-ID gate: in-memory `dict` (no TTL, no persistence — same
  known limitation as `_LAST_RETRIEVED_DOCS` below), `pending → executing → executed` /
  `pending → rejected` state machine. `begin_execution()`'s check-then-set has no `await` between them,
  so the CPython GIL alone gives "atomic transition, only one concurrent approval wins" — no lock needed.
- `src/month_state.py` — SPEC §2/§4/§5 monthly plan state (`data/month_state.json`, gitignored):
  plan-start month (mid-month signup ⇒ starts next month), per-month budget history (`set_monthly_budget`
  always schedules for *next* calendar month), per-month selected strategy + change history, the three
  cutoffs (`biweekly_cutoff_passed` — 15th 09:00 KST, `month_end_cutoff_passed` — last day 00:00 KST,
  plus "plan not started yet"), and `evaluate_current_condition()` for §5's "missed signal" recompute
  (always re-derives from confirmed-only data). Also hosts the **separate, lighter** confirmation
  mechanism for `select_strategy` (`propose_strategy_change`/`confirm_strategy_change`, a token store
  distinct from `approvals.py` — SPEC §14-0 explicitly says this tool is *not* a `approvals.py` target,
  since it changes no money/records, but still needs server-side — not just prompt-trusted —
  confirmation before it takes effect).

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
    "reason"}], "data_gap_needs_confirmation"?}   (§4-2 contract + extras)

POST /approve {approval_id}  →  agent.execute_approved_action(approval_id) — looks up the id in
    approvals.py, atomically pending→executing, runs the *stored* tool/args (never trusts anything the
    client sends beyond the id), then executing→executed. 404 if unknown id, 409 if already
    executing/executed/rejected.
POST /reject  {approval_id}  →  agent.reject_approved_action(approval_id) — pending→rejected. Same
    404/409 semantics. A rejected id can never later be approved.
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
  legitimate matches (e.g. removing "얼마" from `price_agent` would break "지금 얼마야?").
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
  blanket destructive-verb block here.
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
scripts, not just read for plausibility — see README.md's "트라이앤에러" for the specific bugs that
surfaced this way. `agent.py`/`app.py` import cleanly and every `@tool`-wrapped function was invoked
directly via `.invoke()` (bypasses the LLM — a tool's `.invoke()` just calls the wrapped Python function)
to confirm the wiring, including `execute_approved_action`/`reject_approved_action`'s 200/404/409 shapes.
`retriever.build_chunks()` (pure Python) confirmed all 8 docs chunk correctly with real metadata.

**With `.env`/AWS credentials (once supplied)**: confirmed (a) `boto3`/Bedrock connectivity itself —
`list_foundation_models` and a raw `bedrock-runtime.converse()` call both succeeded with credentials
loaded via `load_dotenv()`, so credentials/network/IAM permissions are not the blocker for anything
below; (b) **RAG embeddings + search are fully real and working** — `retriever.build_vectorstore()` ran
actual `BedrockEmbeddings` calls to build `chroma_db/` from the new 8-doc set, and a real similarity
search for "RSI가 뭐고 어떻게 계산해?" returned exactly 4 chunks, all correctly from `RSI.md` (a concrete
sign the doc-restructuring split actually improved retrieval precision vs. the old mixed-topic
`glossary.md`); (c) the new `set_monthly_budget` tool (see below) invoked directly worked correctly.

**Full live conversation — now actually verified (after switching to Haiku, see above)**: a real
multi-turn round trip through `agent.build_supervisor()` (genuine Claude tool-calling, not `.invoke()`)
was confirmed end to end: "오늘 매수 기록 남겨줘, 100만원/1억원" → `ledger_agent` correctly called
`record_virtual_buy` with `executed_date` resolved to the real current date → `approvals_needed` carried
a real `approval_id` → `execute_approved_action()` created the record → a follow-up "매수 기록 보여줘"
correctly listed it back via `search_ledger`. RAG (`retrieve_docs`) and `plan_agent` (`get_month_status`
correctly reporting "plan not started" for a not-yet-active month) were also confirmed live.

**Bug found and fixed during this pass**: the first live attempt at the buy-recording flow above failed
at execution — the LLM called `record_virtual_buy` *without* `executed_date`, because "오늘" ("today")
meant nothing to it; nothing in the system prompt said what today's date is. Fixed in `agent_node`
(`_build_worker_graph`): the system prompt now gets `오늘 날짜는 YYYY-MM-DD(요일)입니다` appended at
call time (`datetime.now(KST)`), with an instruction to resolve relative dates against it. Retested with
the same phrasing afterward — `executed_date` was correctly filled and the record was created. This is
the kind of bug that only a real conversation (not `.invoke()`) can surface, since `.invoke()` tests
supply args directly and never exercise the LLM's own date reasoning.

**Two rough edges observed, not fixed (recorded, not acted on without a call on priority)**:
1. **Keyword-routing noise**: generic keywords shared across `_AGENT_KEYWORDS` (e.g. "얼마" for
   price_agent, matched by budget questions like "예산 얼마 남았어?"; "rsi"/"전략" matched by
   `plan_agent`-only requests like "전략을 RSI로 바꿔줘") cause 2-3 agents to fire for a single-intent
   question. The non-matching agents politely decline ("that's not my department") rather than answering
   wrong, so it's not incorrect — but it pads the combined answer with filler. This is the same
   accepted multi-match tradeoff the Day6 routing design already documents (e.g. "RSI" deliberately
   shared between `price_agent`/`research_agent`), just showing up in a few more places than expected.
   Fixing it well would need smarter-than-keyword routing, which is a bigger change than a quick patch.
2. **Rate limiting is a separate axis from the daily token quota**: firing several real conversation
   turns in quick succession hit `ThrottlingException: Too many requests` (a request-rate limit),
   distinct from the earlier `Too many tokens per day` (daily quota). Space out live test calls;
   retrying immediately into a request-rate throttle just extends the wait.

**Still not live-tested**: `select_strategy`'s propose→confirm flow across two conversational turns —
attempted, but ran into the request-rate limit above before completing. Its `.invoke()`-level logic
(direct tool calls, bypassing the LLM) was already verified during wiring (see the Phase 5 agent.py
work), so the mechanism itself is exercised — what's unverified is specifically whether the LLM naturally
completes the two-step confirm dance in real dialogue. Also still open: `evaluation/run_eval.py`/RAGAS
against the live server (needs the CSV/expected_tools re-authoring in SPEC §14 item 14 regardless).

**Gap found and fixed during this live-testing pass**: SPEC §2-0 ("최초 이용" — set a monthly budget to
start the plan) had no corresponding tool — `get_month_status`/`select_strategy` existed but nothing
called `month_state.init_plan()`. Added `agent.set_monthly_budget(amount_krw)` (first call starts the
plan per §2-0's this-month-if-day-1-else-next-month rule; later calls go through
`month_state.set_monthly_budget`, always next-month-effective per §4) to `plan_agent`'s tool list and
`guardrails.RISK_LEVELS` (registered `"read"` — a settings change with no money movement, same reasoning
as `select_strategy` but without needing its own confirmation token since there's no existing value at
stake the first time, and no §10-1 approval since nothing is recorded/spent). SPEC.md §14-0 updated to
match.

## Data files

- `data/price_history.json`, `data/ledger.json`, `data/month_state.json`, `data/price_cache.json` — all
  gitignored, regenerable runtime state (`price_history.ensure_backfilled()` rebuilds the first one from
  Upbit; the rest start empty).
- `data/docs/*.md` — 8 files per SPEC.md §8's restructuring (done): `BTC.md`, `DCA.md`, `RSI.md`,
  `market_indicators.md`, `backtest_guide.md`, `service_rules.md` (new), `dca_strategy.md` (rewritten —
  service-specific 3-strategy conditions only, old composite-indicator criteria removed),
  `risk_management.md` (unchanged). `glossary.md` retired, content redistributed. `retriever.DOC_META`
  updated to match. `src/retriever.py`'s `build_chunks()` (pure Python, no AWS) was verified against all
  8 files — 43 chunks, every file gets its real category (none fall through to `_DEFAULT_DOC_META`).
  **Not verified**: the actual Chroma rebuild/embedding/search quality — that needs
  `BedrockEmbeddings` (AWS credentials). `chroma_db/` (gitignored, "regenerate freely") was deleted since
  it held stale embeddings from the old 3-file set; it rebuilds automatically from the current 8 files
  the first time `research_agent` runs with credentials available.
- `evaluation/test_queries.csv` — eval fixture; already rewritten once to the new concept's *scenarios*,
  but its `expected_tools` values and `evaluation/run_eval.py`'s assertions still assume the old tool
  set/response shape — needs another pass once someone runs it against the live server.
  `evaluation/round{1,2}_report.md` hold old-concept results, now stale.
