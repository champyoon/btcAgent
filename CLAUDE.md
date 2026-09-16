# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service — **BTC DCA Agent** — that helps a single user run a monthly-budget BTC DCA plan:
compare three purchase strategies (decline-day / biweekly / RSI) against a 48-month backtest, track
real (self-reported) buy/watch records and remaining budget, and answer questions via a LangGraph
multi-agent Supervisor backed by Amazon Bedrock (Claude, `us.anthropic.claude-sonnet-4-5-20250929-v1:0`).

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
  default to requiring approval, "unknown things are blocked"). See also `_AGENT_KEYWORDS` for routing.
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

**Still blocked, not a code problem**: a full live conversation through `agent.build_supervisor()` (i.e.
actual Claude tool-calling, not just `.invoke()`) hit `ThrottlingException: Too many tokens per day` on
the very first attempt — this AWS account's daily Bedrock **inference** token quota (separate from the
embeddings quota, which is unaffected — see (b)) was already exhausted, most likely from earlier
round-1/round-2 testing (SERVICE.md's trial-and-error notes already flagged this exact recurring
constraint). This blocks: verifying the LLM actually picks the right tools in conversation, multi-turn
confirmation UX (approval/reject, `select_strategy`'s token flow) in a real dialogue, and running
`evaluation/run_eval.py`/RAGAS against the live server. Retry once the daily quota resets (or a quota
increase is granted) — nothing code-side needs to change for this specifically.

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
