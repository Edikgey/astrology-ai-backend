# Personal astrology conversation: implementation and local validation

Follow-up: the birth-time and house-system issues recorded in this historical AI audit
have since been corrected for new charts; see [BIRTH_TIME_HOUSES.md](BIRTH_TIME_HOUSES.md)
for the additional migration, legacy behavior, changed files and current test results.
The AI context now consumes saved houses and performs no astronomy calculations.

## Audit

Previously `/ask-gpt` authenticated the chart owner, reserved account quota, constructed
`ChartInterpreter` (recalculating astrology even on a cache hit), and sent one system
message containing instructions, cached `ChartInterpretationData.raw_text`, and the new
question. `GPTMessage` history was persisted and rendered by the browser but never sent
to OpenAI. The raw chart cache used symbol/sign/house/retrograde/aspects/pattern rows,
without explicit birth metadata, degree columns or a separate conversation context.

The existing client is `openai==1.62.0`, Chat Completions, `gpt-4o-mini`, timeout 60 seconds,
no retries. These defaults are retained. Optional `OPENAI_MODEL` can choose another
compatible Chat Completions model; compatibility with its sampling/max_tokens parameters
must be checked before changing it. No model migration or fine-tuning is included.

## Request and accounting

The request now contains, in order: the dedicated system prompt; compact calculated
chart JSON as reference data; persisted older conversation memory if present; recent
verbatim user/assistant messages; the complete new question as the final user message.
Neither user text nor memory is interpolated into system instructions. The prompt asks
for direct, specific synthesis of relevant factors, language matching (RU/UK/EN), use of
earlier discussion, uncertainty, and nonfatalistic symbolic reflection. It distinguishes
assistant interpretations from user facts and prohibits high-stakes decisions based on
astrology. Its quality is not proven by mocked tests; human response evaluation remains.

JWT and chart ownership are checked by the route and context builder. History queries
join the owning chart; memory is filtered by both user_id and chart_id. Guests cannot
call AI/history. No memory HTTP endpoint or browser-supplied history is trusted.

Existing reserve/commit/provider/finalize-or-release semantics remain. User row locks
also reject a concurrent reserved request for the SAME chart with
`GPT_REQUEST_IN_PROGRESS` (409), even when quota remains. Other charts may progress.
All DB read transactions finish before provider calls. The existing five-minute lease
covers at most two summary calls and one answer call, each with a 60-second timeout and
no automatic retries. Expired responses cannot finalize or overwrite newer memory.

Finalization saves both messages, token metadata, the summary candidate/cursor, and one
successful quota entry in one transaction. It checks the prior memory cursor under the
existing user/chart locks. On provider/empty/truncated-response/persistence failure,
candidate memory is discarded and reservation released. Raw history is never shortened.
Deleting a chart deletes its memory/history but keeps the quota ledger and token costs.
Free remains 10 successful user questions lifetime; Premium remains 300 per billing
period. Paddle billing logic and limits are unchanged.

## Memory and budgets

`modules/ai_conversation.py` owns the budgets. UTF-8 byte lengths plus message overhead
provide conservative text-token bounds without a downloaded tokenizer; these are not
exact provider token counts or new product quotas.

| Part | Budget |
| --- | --- |
| New question | 1–4000 characters, nonblank; never truncated |
| Chart | At most 24,000 UTF-8 bytes; oversized data fails explicitly, never silently trimmed |
| Summary trigger | More than 20 unsummarized messages OR more than 12,000 bytes of recent message content/overhead |
| Recent history after compaction | Up to 10 messages, 12,000 bytes; drop oldest messages/exchanges if needed |
| Between compactions | Up to 20 unsummarized messages, within the same history budget |
| Summary output | Prompt requests at most 1800 characters; provider limit 650 tokens; hard 6000-byte validation |
| Each summary input | Up to 18,000 raw dialogue bytes and 32,000 serialized JSON bytes, plus fixed summary instructions |
| Compaction work per request | At most 32 old rows loaded and 2 summary calls |
| Answer request | Conservative 64,000-byte/message-overhead ceiling |
| Answer output | At most 1200 tokens; incomplete `length` responses are not saved/charged |

The summary stores topics, previous interpretations, explicit clarifications/preferences
and unresolved subjects. It merges the previous notes with a contiguous older prefix.
`through_message_id` marks that prefix. The next request does not send those messages
verbatim. Recent exchanges remain exact. A very large legacy backlog catches up over
successive requests; `older_history_pending` tells the model the memory is incomplete.
For a single legacy message beyond a summary batch budget, only a labeled head/tail
excerpt is summarized; the omitted middle remains in the full UI history. This is a
deliberate bounded-cost limitation, not lossless retrieval.

A typical short/medium conversation is estimated at roughly 4–10k input tokens,
depending on calculated aspects and answer lengths; first turns can be smaller. This
is an estimate, not a paid API measurement. Actual counts in `gpt_usage` should replace
the estimate after authorized real usage. Summary calls add cost only at compaction.

## Chart facts and audit limitations

`modules/ai_chart_context.py` uses saved `ChartData.bodies_for_circle`,
`aspects_for_circle` and `patterns_data`. Placements contain body/name/sign/longitude/
house/retrograde; Sun, Moon and Ascendant are explicitly identified when available.
Aspects are undirected and deduplicated; pattern member bodies are listed once.
Birth date, stored calculator hour, locality and coordinates are included. Twelve
Placidus cusps are calculated by the existing `Ephemeris.get_houses()` using those
same stored inputs and the same system as placement house assignments. No transits,
new aspects, fabricated birth facts or interpretation cache text are injected.

The backend already stores enough calculated placements for a compact context; the
duplicate points strings, full visualization objects and old raw prompt cache are
unnecessary. The AI path no longer recalculates planets/asteroids on every question.
A chart missing its saved placements fails with 409 without calling OpenAI or consuming
quota; support must repair its data rather than the AI silently inventing a chart.

Important pre-existing calculator issues found during this audit, left outside this task:

- `TryFreePage` sends `parseInt(formData.hour)` and drops selected minutes.
- Birth timezone/offset is not stored or converted, while Ephemeris expects UTC.
- The visualization's `get_house_cusps()` uses Swiss Ephemeris code `A`, while placement
  house assignment uses `P`; its comment calls both Placidus. The AI explicitly uses P.
- The legacy missing-chart asteroid calculation can call external Horizons and has an
  error path referencing unassigned `eph`. AI now consumes saved data instead.

Before launch, address birth-time/minute provenance and house-system consistency as
a separate calculator change, then validate known reference charts. Prompt quality
cannot compensate for incorrect source birth inputs.

## Database and rollout

`migrations/ai_conversation.sql` is a standalone additive transaction in the existing
manual SQL style. Apply ONCE after `plan_usage.sql`, before running this new backend
against an existing database. It adds `gpt_conversations` (chart primary key, user ID,
summary, cursor, timestamp), a `(chart_id,id)` message index, and nullable
`gpt_usage.input_tokens`, `output_tokens`, `total_tokens`, `model`, `summary_usage`.
Repeated/partial application fails rather than resetting data. No backfill is required.
Old nullable token fields mean unavailable, not zero cost.

The main successful answer's provider counts/model are stored directly. `summary_usage`
stores a list of separate compaction-call counts/models associated with that successful
request. Add these costs when estimating total cost. Summary calls do not consume extra
product messages. Paid provider work preceding a failed overall request is not recorded
by this minimal successful-request analytics; centralized failure-cost tracking is a
possible future improvement.

No production/Railway migration, environment change, push, commit or deployment was
performed for this AI task. Only isolated local test schemas were migrated. The existing
portable local PostgreSQL runtime was started for tests and stopped afterwards.

## Files and validation

Backend: `api/endpoints.py`, `database/queries.py`, `models/natal_chart.py`,
`modules/interpretation.py`, `modules/usage.py`, new `modules/ai_chart_context.py`,
`modules/ai_conversation.py`, `modules/astrology_prompt.py`,
`migrations/ai_conversation.sql`, this document, `tests/test_ai_conversation.py`,
`tests/test_postgres_usage.py`, `tests/test_usage.py`.
Frontend: `src/components/AskGptForm.js` (matching question-length limit only) and
`src/components/AskGptForm.test.js` (refresh/no-duplicate coverage). UI design retained.

18 new mocked AI tests cover second/third turns, owner/chart isolation, summary
persistence and rollback, transcript exclusion/preservation, both budgets, backlog,
oversized legacy messages, token metadata, one-time finalization, expired responses,
missing chart data, no open DB transaction during provider calls, deletion and input
validation. Two new PostgreSQL tests cover the exact SQL and simultaneous same-chart
reservations with spare quota. Existing auth, usage, Paddle and frontend regressions run.
Final backend suite: 81/81 tests pass, no skips, including 8 PostgreSQL checks on
local PostgreSQL 18.6. The exact AI SQL migration was applied in isolated test schemas,
with preserved legacy rows, refusal of repeat application, and real row-lock checks.
Frontend: 6 suites / 71 tests pass; production build succeeds. Existing Pydantic
`orm_mode` and old Browserslist-data warnings remain. No paid OpenAI automated calls.

## Remaining quality work

Evaluate real authorized multi-turn conversations on representative charts in RU/UK/EN:
pronoun resolution, personalized synthesis, factual consistency and summary drift.
Inspect actual token metadata and latency before changing model or thresholds. More
durable accounting for failed provider calls and incremental history pagination can
follow measured need. A small curated astrology reference layer might help only if
evaluation reveals specific inconsistent symbolic explanations; none was added.
There is no demonstrated need for RAG, vector search, embeddings or fine-tuning now.

API shape checked against the [official OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat):
role-separated messages and `usage.prompt_tokens`, `completion_tokens`, `total_tokens`.
