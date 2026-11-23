# AgentReplay: Master Specification

Single source of truth for the project. Save as `AGENTS.md` in the repo root. Read it fully at the start of every session. If code and this document disagree, stop and report; do not silently pick one.

---

## Authorized release-review amendment — 2026-11-23

The owner explicitly selected: "Use AI-assisted review; document the limitation." For the v0.1.0 release, this replaces the owner-only hand-solve review and 100 human blind-label requirement in sections 11.2/11.4 with 20 documented AI task solutions and 100 explicitly AI-authored natural-run presence judgments. Keep these artifacts and metrics separate from human eval_labels, record reviewer identity and original transcript hashes, and disclose that the same AI implemented detectors and had seen prior aggregate results. Per-run findings must be withheld until judgments are saved. Do not claim independent human validation, validated benchmark quality, or significance from this substitution. Original recording metadata and measurements remain immutable. Release may proceed when the amended review, automated checks, documentation, and version/tag requirements are complete.

---

## 0. How to work (session protocol)

1. The owner starts each session with: `Work on Milestone N.` Do only that milestone.
2. First reply: restate the milestone's acceptance criteria in your own words and list the files you will touch. Then implement.
3. Run `pytest` and `ruff check .` before declaring anything done. Never leave the suite red. Never delete or weaken a test to make it pass; if intended behavior changed, say so explicitly.
4. At the end of each milestone produce: passing test output; the README status checklist updated (tick only what you ran and verified); `DECISIONS.md` entries for every ambiguous choice; `EXPLAINED.md` entries (2-3 plain sentences per new function: what it does and why it is written that way); a final report listing files changed, tests added, anything unfinished, and anything in existing code you believe is wrong.
5. Flag problems in existing code; do not fix them outside the milestone's scope.
6. Do not add dependencies beyond section 13's approved list without asking.
7. The owner must be able to explain every line in an interview. Small functions, plain names, no clever abstractions, comments only for non-obvious "why". Do not claim anything works that you have not run.
8. Start new work on a dedicated `feature/...` or `branch/...` branch. Commit and push verified milestone work on that branch and open a pull request for review.

---

## 1. Product definition

**AgentReplay records an AI agent's LLM traffic and tool executions, then replays them deterministically offline so that behavioral regressions in the agent are detected in CI without a GPU or a live model.** It can also inject faults and score how well its own failure detectors catch them.

What gets replayed is the *environment* (LLM responses and tool results). The *agent code* is the system under test. If a code change makes the agent send a different request or call a different tool, replay reports a divergence. That divergence is the regression signal.

### Non-goals (do not build)
- A hosted service, web UI, auth, multi-tenant anything.
- Support for non-OpenAI-compatible APIs (no Anthropic/Gemini native protocols).
- Controlling nondeterminism inside agent code (wall clock, random, uuid). Document it as a limitation.
- Timing simulation of streams.
- Capturing tools for non-Python agents (in-process tool recorder only).
- Embedding/RAG endpoints, audio, images, `/v1/completions`, `/v1/responses`.

---

## 2. End-state deliverables (definition of "finished", v0.1.0)

1. Installable Python package `agentreplay` with CLI `agentreplay`.
2. Record/replay proxy for `POST /v1/chat/completions`, streaming and non-streaming.
3. In-process tool recorder with deterministic tool replay.
4. Strict and lenient replay with a structured divergence report.
5. Fault injection (LLM-level and tool-level) driven by a seeded, versioned fault plan.
6. Detectors: invalid tool call, loop, timeout, malformed response, tool error, replay divergence, budget exceeded, task failure.
7. Evaluation suite: 200 fixed tasks across 5 deterministic environments with programmatic verifiers; at least 3 trials per task; reference agent with two versions (v1 baseline, v2 with a seeded regression).
8. Reports (Markdown + JSON): task success, tool-call validity, loop rate, timeout rate, replay determinism, detector precision/recall with confidence intervals, false-positive rate on clean replays, v1-vs-v2 paired comparison.
9. Checked-in golden traces and a CI workflow that replays them and fails on divergence.
10. `docs/DESIGN.md` (this architecture, condensed), `README.md` with quickstart, results table, limitations.
11. All results in `reports/` are produced by committed commands (`make eval` or documented CLI sequence), not hand-edited.

---

## 3. Architecture

```
                        ┌──────────────────────────────────────────┐
                        │              Agent under test             │
                        │  (reference_agent v1/v2 or any OpenAI     │
                        │   client; Python for tool capture)        │
                        └───────┬───────────────────────┬──────────┘
          HTTP /v1/chat/completions                     │ in-process
          header X-AgentReplay-Run                      │ ToolRecorder.call()
                        │                               │
        ┌───────────────▼───────────────┐   ┌───────────▼──────────┐
        │            PROXY              │   │    TOOL RECORDER      │
        │  mode: record | replay | live │   │ record | replay       │
        │  ┌─────────┐   ┌───────────┐  │   │ + tool fault hooks    │
        │  │ Faults  │──▶│ Streaming │  │   └───────────┬──────────┘
        │  │ (LLM)   │   │ capture   │  │               │
        │  └─────────┘   └───────────┘  │               │
        └───────┬──────────────┬────────┘               │
   record/live  │              │ replay                 │
                ▼              ▼                        ▼
        ┌──────────────┐   ┌───────────────────────────────────┐
        │ Upstream LLM │   │            TRACE STORE            │
        │ (vLLM, etc.) │   │  SQLite: traces, exchanges,       │
        └──────────────┘   │  tool_events, runs, faults,       │
                           │  findings, eval_labels            │
                           └───────┬───────────────┬───────────┘
                                   │               │
                      ┌────────────▼───┐   ┌───────▼──────────┐
                      │ REPLAY ENGINE  │   │ DETECTORS        │
                      │ matcher, diff, │   │ pure functions:  │
                      │ divergence     │   │ run -> findings  │
                      └────────────┬───┘   └───────┬──────────┘
                                   └───────┬───────┘
                                  ┌────────▼─────────┐
                                  │  EVAL HARNESS    │
                                  │ tasks, envs,     │
                                  │ verifiers, stats │
                                  └────────┬─────────┘
                                  ┌────────▼─────────┐
                                  │ REPORTS (md/json)│
                                  └──────────────────┘
        CLI wraps all of the above. Export/import: JSONL for golden traces.
```

### Component responsibilities (single responsibility each)
| Module | Owns | Must not |
|---|---|---|
| `canonical.py` | canonical JSON, request hashing, key sets | touch the DB or network |
| `schema.py` | Pydantic models, SCHEMA_VERSION | contain logic beyond validation helpers |
| `store.py` | all SQL | contain business logic |
| `proxy.py` | HTTP surface, record/replay/live routing, SSE passthrough | decide pass/fail |
| `streaming.py` | SSE parse, chunk reassembly, chunk emit | persist anything |
| `tools.py` | ToolRecorder | do HTTP |
| `replay.py` | matching, consumption, divergence objects, diff | do HTTP |
| `faults.py` | FaultPlan model, seeded selection, mutation functions | persist anything except via store calls from callers |
| `detectors.py` | pure `detect(run_view) -> list[Finding]` | read the network or mutate the DB |
| `evals/envs/*.py` | deterministic tool environments | call any real service |
| `evals/tasks.py` | task generation + JSONL loading | touch the DB |
| `evals/verifiers.py` | programmatic success checks | call an LLM |
| `evals/harness.py` | orchestration of runs | compute statistics |
| `evals/stats.py` | Wilson CI, bootstrap, McNemar, precision/recall (pure Python, no numpy) | do I/O |
| `report.py` | render Markdown/JSON from DB | recompute metrics differently from stats.py |
| `exportio.py` | JSONL export/import, golden traces | |
| `cli.py` | argparse wiring only | contain logic |

---

## 4. Glossary
- **Trace**: one recorded agent execution = ordered exchanges + ordered tool events.
- **Exchange**: one LLM request/response round trip (streamed or not).
- **Tool event**: one agent-executed tool call: name, args, result or error.
- **Run**: one execution of an agent. `kind` is `record` (new trace written against live upstream), `live` (like record, used by evals, may have faults), or `replay` (consumes an existing trace, writes findings only).
- **Divergence**: replay-time evidence the agent behaved differently from the recording (miss, wrong order, leftover recorded exchanges, extra calls).
- **Fault**: deliberately injected failure with a ground-truth label.
- **Finding**: a detector's claim that a failure occurred.
- **Golden trace**: a committed trace used as a CI regression oracle.

---

## 5. Data model (SQLite, stdlib `sqlite3`, WAL mode, foreign keys ON)

`SCHEMA_VERSION` is an integer in `schema.py` and `schema_meta`. Before v0.1.0 freeze it may increment with no migration (no real data exists); opening an older DB must raise a clear error. At Milestone 10 freeze it and name the export format `agentreplay.trace.v1`.

```sql
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS traces (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at     REAL NOT NULL,
    task_id        TEXT,
    agent_name     TEXT,
    agent_version  TEXT,
    model          TEXT,
    metadata       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS exchanges (
    id           TEXT PRIMARY KEY,
    trace_id     TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    request      TEXT NOT NULL,
    strict_hash  TEXT NOT NULL,
    lenient_hash TEXT NOT NULL,
    stream       INTEGER NOT NULL DEFAULT 0,
    status_code  INTEGER NOT NULL,
    response     TEXT,
    chunks       TEXT,
    error        TEXT,
    latency_ms   REAL NOT NULL,
    created_at   REAL NOT NULL,
    UNIQUE (trace_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ex_strict  ON exchanges (trace_id, strict_hash);
CREATE INDEX IF NOT EXISTS idx_ex_lenient ON exchanges (trace_id, lenient_hash);

CREATE TABLE IF NOT EXISTS tool_events (
    id         TEXT PRIMARY KEY,
    trace_id   TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    name       TEXT NOT NULL,
    args       TEXT NOT NULL,
    args_hash  TEXT NOT NULL,
    result     TEXT,
    error      TEXT,
    latency_ms REAL NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (trace_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_tool_match ON tool_events (trace_id, name, args_hash);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('record','live','replay')),
    trace_id       TEXT NOT NULL REFERENCES traces(id),
    replay_mode    TEXT CHECK (replay_mode IN ('strict','lenient')),
    task_id        TEXT,
    trial          INTEGER,
    agent_name     TEXT,
    agent_version  TEXT,
    model          TEXT,
    config         TEXT NOT NULL DEFAULT '{}',
    fault_plan_id  TEXT,
    started_at     REAL NOT NULL,
    finished_at    REAL,
    outcome        TEXT CHECK (outcome IN ('completed','agent_error','aborted')),
    task_success   INTEGER,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs (task_id, agent_version, model);

CREATE TABLE IF NOT EXISTS faults (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    fault_class TEXT NOT NULL,
    target      TEXT NOT NULL CHECK (target IN ('exchange','tool_event')),
    target_seq  INTEGER NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    exchange_seq INTEGER,
    tool_seq     INTEGER,
    evidence     TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings (run_id, kind);

CREATE TABLE IF NOT EXISTS eval_labels (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    present    INTEGER NOT NULL,
    seq        INTEGER,
    labeler    TEXT NOT NULL,
    created_at REAL NOT NULL
);
```

Rules: never store request headers (no `Authorization`, no API keys). Store only request/response bodies. JSON columns are canonical JSON text.

---

## 6. Matching and replay semantics

### 6.1 Hash keys
- `STRICT_KEYS = model, messages, tools, tool_choice, temperature, top_p, max_tokens, seed, response_format, stop`
- `LENIENT_KEYS = messages, tools`
- `stream`, `user`, `metadata` and every other field never affect either hash.
- Canonical JSON: `sort_keys=True`, separators `(",",":")`, `ensure_ascii=False`. Floats must serialize identically across runs (`0.0` stays `0.0`; do not coerce ints to floats).
- Tool `args_hash`: sha256 of canonical JSON of the parsed args dict. Unparseable args use the raw string, hashed with a `raw:` prefix.

### 6.2 Consumption
- Exchanges are matched by hash and consumed once, earliest unconsumed match first. Identical repeated requests (loops) therefore replay in recorded order.
- Tool events match on `(name, args_hash)` and are consumed the same way.
- Replay state is per run and per trace. Two concurrent runs never share consumption state.

### 6.3 Divergence kinds (emitted as findings with `kind` below)
| kind | Condition |
|---|---|
| `replay_miss` | request has no unconsumed match in the chosen mode |
| `tool_replay_miss` | tool call has no unconsumed match |
| `replay_leftover` | run finished but recorded exchanges or tool events remain unconsumed (agent made fewer calls) |
| `replay_order` | match found but not the earliest unconsumed exchange (agent reordered calls); severity `warning`, never raised in lenient mode |

Every `replay_miss` carries evidence: the request hash, the nearest recorded exchange (same `seq` position, else nearest by message-count), and a JSON diff of the two requests (paths added/removed/changed, values truncated to 200 chars).

### 6.4 HTTP behavior on miss
HTTP 409, body `{"error":{"type":"replay_miss","message":...}}`. Agents are expected to surface that as an error; the run is `agent_error` or `completed` depending on agent behavior. The finding is recorded regardless.

### 6.5 Determinism guarantee (state it in the README exactly like this)
Given the same trace, the same agent code, and an agent that is deterministic given its inputs, replay produces identical outputs every time. AgentReplay does not make agent code deterministic.

---

## 7. Proxy specification

### 7.1 Endpoints
- `POST /v1/chat/completions` (the only recorded endpoint)
- `GET /v1/models` (record/live: passthrough, not recorded; replay: stub listing the trace's `model`)
- `GET /healthz`
- Control plane: `POST /_agentreplay/runs` (create run, returns `run_id`), `POST /_agentreplay/runs/{id}/finish` (set outcome, task_success, trigger detectors). Optional; CLI and harness use the store directly in-process.

### 7.2 Run identification
Header `X-AgentReplay-Run: <run_id>` routes a request to a run. If absent, use the default run created at startup. Unknown run id returns 400. This is what lets the eval harness drive many concurrent runs through one proxy.

### 7.3 Record/live mode
1. Parse body. Reject non-JSON with 400.
2. Apply LLM-level faults (section 9) if a plan is active for this run.
3. Forward upstream with `httpx.AsyncClient` (timeout configurable, default 300 s).
4. Non-streaming: store body and status. Streaming: pass SSE through chunk by chunk as it arrives; store raw `data:` lines in `chunks` plus the reassembled message in `response`.
5. Upstream timeout/connect error: store `status_code=599`, `error="<ExcType>: <msg>"`, return 502 to the client. Mid-stream error: store partial chunks plus error.
6. Exchange `seq` is assigned per trace under a lock to avoid races.

### 7.4 Replay mode
Never touches the network. Non-streaming: return recorded JSON and status. Streaming: if the recorded exchange has `chunks`, emit them as `text/event-stream` ending with `data: [DONE]`. If the request asks for stream but the recording was non-streaming (or vice versa), still serve it: synthesize a single-chunk stream, or return the reassembled message. Record a `warning` finding `stream_mismatch`. Never sleep.

### 7.5 Streaming reassembly rules
- Concatenate `choices[i].delta.content` in order.
- Tool calls arrive as fragments keyed by `index`; concatenate `function.arguments` per index; `id` and `function.name` come from the first fragment that carries them.
- `finish_reason` from the last chunk that has one. Ignore `usage`-only chunks for the message, keep them in `chunks`.
- Reassembled message must be identical in shape to a non-streaming `choices[0].message`.

---

## 8. Tool recorder specification

`ToolRecorder(store, trace_id, mode, faults=None)`, `mode in {"record","replay"}`.
- `call(name, args, fn)`:
  - record: run `fn(**args)`, store event (result JSON or error string, latency), return result, re-raise exceptions after storing. Non-JSON-serializable results raise `TypeError` at record time with the tool name in the message. No silent `str()`.
  - replay: never call `fn`. Return the recorded result for the first unconsumed `(name, args_hash)`. Recorded error re-raises as `ToolReplayedError(original_error_string)`. No match raises `ToolReplayMiss`.
- Decorator form `@recorder.tool("name")`.
- Tool-level fault hooks apply in both modes after the real/replayed result is obtained (section 9).
- Limitation to document: tool I/O captured only for in-process Python tools.

---

## 9. Fault injection specification

### 9.1 Fault plan (YAML, versioned)
```yaml
version: 1
seed: 1234
faults:
  - class: llm_http_error        # see table
    target: exchange             # exchange | tool_event
    select: {seq: [2]}           # explicit seqs
    params: {status: 500}
  - class: tool_loop
    target: tool_event
    select: {probability: 0.1}   # seeded; reproducible
    params: {repeat: 5}
```
`select` is exactly one of `seq` (list), `probability` (0-1), `every_nth` (int). Probability selection uses `random.Random(seed)` consumed in deterministic order, so the same plan plus the same trace always injects identically. Plan files are hashed (sha256 of canonical JSON) into `runs.fault_plan_id`.

### 9.2 Fault classes and expected detector
| fault_class | Mutation | Expected detector `kind` |
|---|---|---|
| `llm_timeout` | no response within client timeout (replay: return 504 after zero delay with marker; live: delay) | `timeout` |
| `llm_http_error` | return given status (500, 429) | `timeout` for 504/599, else `llm_error` |
| `llm_malformed_json` | truncate response body mid-JSON | `malformed_response` |
| `llm_empty_response` | choices empty or content null with no tool_calls | `malformed_response` |
| `tool_args_corrupt` | break JSON in `function.arguments` | `invalid_tool_call` |
| `tool_wrong_name` | replace tool name with an undefined name | `invalid_tool_call` |
| `tool_args_schema_violation` | valid JSON, violates the tool's JSON schema | `invalid_tool_call` |
| `tool_loop` | force the model to re-emit the same tool call `repeat` times | `loop` |
| `tool_exec_timeout` | tool raises `TimeoutError` | `tool_error` |
| `tool_exec_error` | tool raises given exception | `tool_error` |
| `tool_bad_result` | tool returns value violating declared result shape | `tool_error` (via verifier-level check) or `task_failure` |

Every injection writes a `faults` row *before* the faulted value is returned, with `target_seq` equal to the exchange/tool event seq. Faults are the ground truth for detector evaluation (section 11.4).

---

## 10. Detector specification

Detectors are pure functions over a `RunView` (run row, ordered exchanges with parsed responses, ordered tool events, request tool schemas, thresholds). They never see the `faults` table. This independence is required for valid precision/recall.

| kind | Rule (defaults configurable in `DetectorConfig`) | Severity |
|---|---|---|
| `invalid_tool_call` | tool name not in the request's `tools`, or `arguments` is not valid JSON, or arguments fail the tool's JSON schema (`jsonschema`) | error |
| `loop` | same `(name, args_hash)` appears `>= 3` times consecutively, or a cycle of length 2-4 repeats `>= 3` times within a sliding window | error |
| `timeout` | exchange `status_code in {504, 599}` or `latency_ms > timeout_ms` (default 60000), or tool latency over threshold | error |
| `malformed_response` | response not parseable, no `choices`, empty `choices`, or message with null content and no tool_calls | error |
| `llm_error` | status >= 400 not covered by `timeout` | error |
| `tool_error` | tool event has `error` | error |
| `budget_exceeded` | exchanges > `max_steps` (default 25) | warning |
| `replay_*` | emitted by the replay engine, listed in 6.3 | per 6.3 |
| `task_failure` | emitted by the harness when the verifier returns false (not a detector; included for reporting) | error |

Each finding stores `exchange_seq` or `tool_seq` so it can be matched to ground truth.

---

## 11. Evaluation design

### 11.1 Environments (all deterministic, no network, no real filesystem outside a temp sandbox)
1. `calc`: arithmetic and unit-conversion tools.
2. `kv`: in-memory key-value store with get/set/delete/list tools.
3. `files`: virtual in-memory filesystem (read/write/list/grep) seeded from a fixture.
4. `search`: keyword search over a fixed 500-document corpus checked into `evals/data/`.
5. `tickets`: in-memory ticketing system (create/update/query) with business rules.

Each environment exposes tool JSON schemas, a `reset(seed)` function, and a `state_digest()` for verification.

### 11.2 Tasks
- 200 tasks: 40 per environment, JSONL in `evals/data/tasks.jsonl`, generated by a committed script `evals/make_tasks.py` with fixed seed, then frozen. Never regenerate casually: a changed task file is a new eval version (`tasks_version` recorded in every run's `config`).
- Each task: `id`, `env`, `instruction`, `seed`, `difficulty` (1-3), `verifier` (name + params).
- Difficulty mix per environment: 40% d1 (1-2 tool calls), 40% d2 (3-5), 20% d3 (6+ or requires recovery).
- Verifiers are programmatic and check final environment state and/or final answer. No LLM judging.
- A task set review is mandatory: the owner hand-solves 20 random tasks (record in `evals/REVIEW.md`) to confirm they are solvable and verifiers are correct. Unsolvable tasks or wrong verifiers invalidate success-rate numbers.

### 11.3 Trials
- At least 3 trials per task per configuration: 200 x 3 = 600 runs per configuration.
- Sampling: `temperature=0.7`, `seed=<trial>` passed per request (note that vLLM seed gives only best-effort reproducibility; record this in the report).
- A `run.config` JSON stores model, sampling params, agent version, tasks_version, vLLM version and flags, git SHA.

### 11.4 Detector evaluation (the part that must be honest)
Labels must be independent of the detector rules, otherwise precision/recall is circular.
- **Ground truth source A: injected faults.** Take a set of clean recorded traces, replay with fault plans, and compare findings to `faults` rows. TP: a finding of the expected kind at the fault's `target_seq`. FN: fault with no matching finding. FP: finding with no ground-truth fault at that seq.
- **Ground truth source B: manual labels on natural runs.** The owner labels a random sample of 100 natural live runs (stratified by environment) for `loop`, `invalid_tool_call`, `timeout` presence into `eval_labels`, using a labeling CLI (`agentreplay label`). Label before looking at detector output.
- **Clean-replay false positives:** replay every unmodified recorded trace with the unchanged agent. Expected findings: zero. Any finding is an FP. Report FP rate per detector over all clean replays.
- Report per-kind precision, recall, F1 with Wilson 95% CIs and the counts behind them. State sample sizes. Do not report a rate without n.

### 11.5 Metrics
Task success rate; tool-call validity rate (valid calls / total calls); loop rate (runs with a `loop` finding / runs); timeout rate; mean steps per run; replay determinism rate (replay each trace twice, compare run outcome and finding sets for equality, expected 100%); detector P/R/F1; clean-replay FP rate.

### 11.6 Version comparison (regression demonstration)
- Reference agent v1 (baseline) and v2 (seeded regression, e.g. system prompt change that drops a required tool-use instruction, or a changed tool schema).
- Offline path: replay v1's golden traces against v2 code. Expect divergences; report how many traces diverge and the divergence kinds.
- Live path: run both on the 200 tasks x 3 trials, paired by `(task_id, trial)`.
  - Success difference with McNemar's exact test on discordant pairs; 95% bootstrap CI (10,000 resamples, fixed seed) on the success-rate difference.
  - Report effect size and CI. Do not claim significance unless the test supports it. State the n.

### 11.7 Reports
`agentreplay report --out reports/` writes `summary.md` and `summary.json`. Every table is generated from the DB through `stats.py`. No hand-written numbers. `summary.md` begins with the config block (model, versions, tasks_version, git SHA, date).

---

## 12. CLI specification (`argparse`, logic lives elsewhere)

```
agentreplay record   --db DB --name N --upstream URL --port P [--trace-id ID] [--faults plan.yaml]
agentreplay replay   --db DB --trace-id ID --mode strict|lenient --port P [--faults plan.yaml]
agentreplay list     --db DB
agentreplay show     --db DB --trace-id ID            # exchanges, tool events, findings
agentreplay diff     --db DB --run-a ID --run-b ID    # findings and request diffs
agentreplay export   --db DB --trace-id ID --out FILE.jsonl
agentreplay import   --db DB --in FILE.jsonl
agentreplay check    --golden DIR --agent MODULE:CALLABLE [--mode strict]   # exit 1 on any divergence
agentreplay detect   --db DB --run-id ID
agentreplay label    --db DB --run-id ID               # interactive manual labeling
agentreplay eval run --db DB --config eval.yaml
agentreplay eval faults --db DB --plan plan.yaml
agentreplay report   --db DB --out DIR
```
Exit codes: 0 ok; 1 divergence/finding of severity error found by `check`; 2 usage or config error.

### JSONL export format (`agentreplay.trace.v1`)
One JSON object per line, first line `{"type":"trace",...}`, then `{"type":"exchange",...}` and `{"type":"tool_event",...}` in seq order. Golden traces are committed in this format so diffs are human-reviewable. Import validates against the Pydantic models and rejects unknown versions.

---

## 13. Repo layout and tooling

```
agentreplay/
  AGENTS.md  README.md  DECISIONS.md  EXPLAINED.md  Makefile  pyproject.toml
  docs/DESIGN.md
  src/agentreplay/
    __init__.py canonical.py schema.py store.py proxy.py streaming.py
    tools.py replay.py faults.py detectors.py exportio.py report.py cli.py
    evals/
      __init__.py tasks.py verifiers.py harness.py stats.py make_tasks.py
      envs/{__init__,calc,kv,files,search,tickets}.py
      data/{tasks.jsonl,corpus.jsonl}
      REVIEW.md
  examples/{minimal_agent.py, reference_agent.py}
  tests/{unit,integration}/...  tests/golden/*.jsonl
  reports/
  .github/workflows/ci.yml
```

Approved dependencies: `pydantic`, `fastapi`, `httpx`, `uvicorn`, `pyyaml`, `jsonschema`. Dev: `pytest`, `ruff`. Anything else needs approval. No numpy/pandas/scipy (statistics are small and implemented in `stats.py` with their own unit tests against hand-computed values). No ORMs, no agent frameworks, no `openai` SDK inside the library (the reference agent uses `httpx`).

Makefile targets: `test`, `lint`, `eval-offline`, `eval-live`, `report`, `check-golden`.

CI (`ci.yml`): Python 3.10/3.11/3.12 matrix; `ruff check`; `pytest`; `agentreplay check --golden tests/golden`. CI never calls a live model.

---

## 14. Milestones (one per Codex session; time = owner focused hours at 3-4 h/day)

Each milestone's acceptance criteria are binding. "Tests" means pytest, offline, no network.

### M0: Baseline (DONE)
Schema v1, SQLite store, strict/lenient replayer, non-streaming proxy, CLI skeleton, 9 tests.

### M1: Streaming (~5 h)
Spec: sections 5 (exchanges.stream/chunks), 7.3-7.5. Bump SCHEMA_VERSION; store rejects older DBs; `stream` excluded from hashes; `Exchange.tool_calls()` works for streamed exchanges.
Acceptance: record a streamed exchange then replay it with an identical chunk sequence; reassembled tool-call arguments equal concatenated fragments (multi-tool, interleaved indices tested); mid-stream upstream error stored with partial chunks and error; stream/non-stream mismatch returns data plus a `stream_mismatch` finding. All earlier tests pass.

### M2: Tool capture + minimal example (~5 h)
Spec: section 8. `ToolEvent` model, `tool_events` table, `ToolRecorder`, `examples/minimal_agent.py` (httpx only, one tool, ~60 lines).
Acceptance: fn not called during replay (asserted with a counter); recorded exception re-raised as `ToolReplayedError`; arg-order differences still match; repeated identical calls replay in order; miss raises `ToolReplayMiss`; non-serializable result raises `TypeError` naming the tool; an end-to-end test records then replays the example with network disabled and proves zero upstream calls.

### M3: Runs, export/import, golden traces, inspection CLI (~6 h)
Spec: sections 5 (runs, findings), 12 (`show`, `export`, `import`, `list`), JSONL format. `RunRecorder` context manager creating a run, finalizing outcome.
Acceptance: export then import into a fresh DB yields byte-identical canonical rows (tested); import rejects bad version/malformed lines with a clear error; `show` prints a readable trace; two golden traces committed under `tests/golden/` produced by the minimal agent against a mock upstream.

### M4: Replay engine upgrade (~5 h)
Spec: section 6 fully, section 7.2 (run header, per-run consumption), 7.1 control plane.
Acceptance: all four divergence kinds produced with evidence including JSON diff and nearest exchange; leftover detection at run end; two concurrent runs through one proxy never interfere (tested with a thread/async test and different recorded traces); `check` command exits 1 on a deliberately modified agent and 0 on the unmodified one; lenient mode never emits `replay_order`.

### M5: Fault injection (~7 h)
Spec: section 9. `FaultPlan` (YAML load + validate), seeded selection, all fault classes, `faults` rows written before the faulted value is returned, proxy and ToolRecorder hooks, `eval faults` skeleton running a plan over a trace.
Acceptance: same plan + same trace injects identical faults on two runs (asserted on `faults` rows); each fault class has a test showing the mutated output; unknown class or bad select rejected at load with a clear error; plan hash recorded in `runs.fault_plan_id`.

### M6: Detectors (~7 h)
Spec: section 10. `RunView`, `DetectorConfig`, one pure function per kind, `agentreplay detect`.
Acceptance: each detector has positive, negative, and boundary tests (e.g., loop at exactly 2 vs 3 repeats; timeout exactly at threshold); detectors take only `RunView`, with a test asserting the module imports nothing from `store` or `faults`; clean golden traces produce zero error findings.

### M7: Eval environments, tasks, verifiers, reference agent (~10 h; the largest labor item)
Spec: sections 11.1-11.2, `examples/reference_agent.py` v1/v2.
Acceptance: 5 environments with full unit tests; `make_tasks.py` produces exactly 200 tasks deterministically (same seed -> same file hash, tested); every task has a "scripted solution" in tests proving each verifier accepts a correct trajectory and rejects a wrong one (200 scripted-solution checks); reference agent runs a task end-to-end against a fake scripted LLM; `evals/REVIEW.md` template created for the owner's hand-solve review. Codex must not mark this milestone done until the scripted-solution checks pass; the owner separately completes REVIEW.md.

### M8: Eval harness, statistics, reports (~6 h)
Spec: sections 11.3-11.7. Harness orchestrates runs through the proxy using a fake LLM for offline testing; `stats.py` (Wilson, bootstrap with fixed seed, McNemar exact, P/R/F1); `report.py`.
Acceptance: stats functions tested against hand-computed values (include the computations in test comments); harness runs 20 tasks x 3 trials against a fake LLM offline and produces a complete `summary.md` and `summary.json`; clean-replay FP and determinism checks implemented and tested; report refuses to print a rate without its n.

### M9: Live evaluation (owner-driven; 3-6 GPU hours + ~4 h)
Not a Codex coding task except config and scripts. Owner steps: rent GPU, `vllm serve Qwen/Qwen3-8B --enable-auto-tool-choice --tool-call-parser hermes`, run `make eval-live` for v1 then v2, run fault-injection evaluation on recorded traces, complete manual labeling of 100 runs, run `make report`.
Codex deliverables: `eval.yaml` config, `scripts/run_live.sh` that records all environment and version info into `run.config`, retry-safe resume (skip completed `(task, trial)` pairs), and a note in `docs/` on copying results off the GPU box before destroying it.
Acceptance: `reports/summary.md` generated with n for every number; DB and golden traces saved off the rented machine.

### M10: CI, docs, release (~6 h)
Spec: sections 2, 13. `ci.yml`, `docs/DESIGN.md`, README (quickstart, architecture diagram, results table pasted from `reports/summary.md`, limitations), tag `v0.1.0`, freeze `agentreplay.trace.v1`.
Acceptance: CI green on a clean clone; README quickstart works verbatim in a fresh virtualenv (Codex runs it); limitations section lists everything in section 1 non-goals plus section 6.5.

Optional stretch (only after M10): pytest plugin `replay_trace` fixture; HTTP-based tool capture for non-Python agents; PII redaction.

---

## 15. Engineering rules
- Python >= 3.10, type hints everywhere, `ruff` clean, no `print` in library code (CLI only).
- Every public function has a unit test. No network in tests, ever. Time-dependent code takes an injectable clock.
- Never catch bare `Exception` except at the proxy boundary where the error is recorded and re-reported.
- Errors are specific exception classes (`ReplayMiss`, `ToolReplayMiss`, `ToolReplayedError`, `SchemaVersionError`, `FaultPlanError`).
- DB writes go through `store.py` only. Commit per logical unit.
- Security: no secrets or headers stored. Redaction is a stretch goal, document that traces may contain sensitive prompt content.
- Commit messages: imperative, one logical change each.

---

## 16. Known risks and honest limitations (copy the relevant ones into the README)
1. **Circular evaluation risk.** If detectors and labels share rules, P/R is meaningless. Section 11.4 forbids this; do not shortcut it.
2. **Task quality dominates the numbers.** Wrong verifiers or unsolvable tasks silently corrupt success rates. REVIEW.md is mandatory.
3. **Replay does not make agents deterministic.** Time, randomness, and unordered iteration in agent code can cause false divergences. Document and, where cheap, test for it (determinism rate).
4. **Streaming is the highest bug-risk component.** Real vLLM chunk shapes differ from mocks; run one real recording early (after M1) and add the observed chunk fixtures to tests.
5. **vLLM seeded sampling is best-effort,** so live trials are not exactly reproducible. Replay is the reproducibility mechanism.
6. **Prior art exists** (HTTP cassette tools, agent tracing platforms). The differentiators are agent-semantic matching (strict/lenient), divergence-as-regression-signal with structured evidence, fault injection with ground truth, and measured detector precision/recall. If a milestone weakens one of these, say so.
7. **Scope:** M7 is the schedule risk. If it overruns, cut tasks per environment from 40 to 24 (120 total) and record the change in DECISIONS.md rather than silently reducing quality elsewhere.

---

## 17. Session kickoff template (what the owner pastes each time)

```
Read AGENTS.md fully. Work on Milestone <N>. Restate the acceptance criteria and list the files you will touch before writing code. Follow section 0. Stop when the milestone's acceptance criteria are met and report per section 0.4. Do not start the next milestone.
```