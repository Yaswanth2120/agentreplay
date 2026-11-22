# AgentReplay

Record an AI agent's OpenAI-compatible LLM traffic, then replay it deterministically offline to catch behavioral regressions in CI. No GPU or live model needed for replay.

## Differentiator (write this before building more)
Generic HTTP cassette tools (VCR-style) replay bytes. AgentReplay replays *agent semantics*:
- **strict vs lenient** matching defined over model/messages/tools/sampling params
- a **replay miss is the regression signal** (agent issued a request not in the recording; looped more than recorded)
- **fault injection** + **failure detector with measured precision/recall** (roadmap)

## Quickstart
```bash
pip install -e '.[dev]'
pytest

# record against a vLLM server
vllm serve Qwen/Qwen3-8B --enable-auto-tool-choice --tool-call-parser hermes
agentreplay record --upstream http://localhost:8000 --name smoke --port 9000
# point the agent's OpenAI base_url at http://127.0.0.1:9000/v1

agentreplay list
agentreplay replay --trace-id <id> --mode strict --port 9000
```

## Status
- [x] Pydantic trace schema (v7), SQLite store, strict/lenient replayer, record/replay proxy, CLI, tests
- [x] Streaming capture/replay, interleaved tool-call reassembly, partial-error capture, and stream/non-stream mismatch warnings (M1 offline tests)
- [x] Versioned seeded fault plans, all 11 fault classes, persisted labels, and fixed-trajectory fault exercise (M5)
- [x] Pure failure detectors, durable observed run views, and detection CLI (M6 offline tests)
- [x] Five deterministic environments, 200 frozen tasks, independent verifiers, reference agent v1/v2 (M7 automated checks)
- [ ] Owner hand-solve review of 20 tasks
- [x] Evaluation harness, statistics, and generated reports (M8: 20 tasks × 3 trials per version, offline)
- [x] M9 live-run configuration, resumable runner, and verified backup tooling (offline tests only)
- [ ] Full live evaluation (200 tasks × 3 trials per version), owner review, 100 manual labels, and off-GPU preservation
- [x] In-process Python tool capture/replay, decorator API, and minimal HTTPX agent (M2 offline tests)
- [x] Run lifecycle, validated trace export/import, inspection CLI, and two offline golden traces (M3)
- [x] Per-run replay, structured divergences, control endpoints, run diff, and offline golden-check CLI (M4)
- [ ] CI workflow


## M1 verification

No GPU or live model is needed for development tests:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

M1 verification: 31 tests passed with socket connections blocked; Ruff passed.
The local verification used Python 3.14. Python 3.10–3.12 CI validation is still
scheduled for M10. The installed FastAPI/Starlette emits a TestClient deprecation
warning for HTTPX; tests still pass using the approved dependency list.

Streaming requests use the same `/v1/chat/completions` endpoint with
`"stream": true`. Record mode forwards SSE lines as they arrive and stores the
raw `data:` lines (including usage chunks) plus a reassembled completion.
Replay emits those data lines immediately, without timing simulation, ending
with `[DONE]`. Transport byte boundaries, CRLF versus LF, comments, and SSE
keepalive spacing are not replayed; data-line content and order are preserved.
Tool-call fragments are grouped by choice and tool index before arguments are
concatenated.

Replay can convert between streamed and non-streamed responses. Each conversion
writes a `stream_mismatch` warning associated with the startup run; its ID is
available as `app.state.run_id`, and warnings can be read through
`TraceStore.findings(run_id)`. Run completion is available through `RunRecorder`, and `show` includes persisted
runs and findings. Per-request run routing is available through `X-AgentReplay-Run` (see M4 below).

The current schema v7 records a database-level version in `schema_meta`. Older/unversioned
or newer databases raise `SchemaVersionError` when opened; use a new database
path. There is no migration in this pre-release milestone.

## Limitations

Given the same trace, the same agent code, and an agent that is deterministic given its inputs, replay produces identical outputs every time. AgentReplay does not make agent code deterministic.

A mid-stream upstream failure saves partial chunks and the error with status
599. Once response headers have reached the client, its HTTP status cannot be
changed; the stream ends without a synthetic success marker. Replay appends the
required `[DONE]` marker even for an incomplete recording and returns its stored
status. A live vLLM streaming smoke test and real chunk fixtures remain pending;
no rented GPU is required to finish M1's offline acceptance tests.

Traces store request and response bodies, which may contain sensitive prompts.
Request headers and API keys are not persisted. Concurrent recording is supported
through one shared `TraceStore`; sequence reservations are not coordinated across
independent processes. Tool I/O is captured only for in-process Python tools.


## M2: tool capture and minimal agent

`ToolRecorder` executes synchronous Python tools in record mode and returns their
stored results in replay mode. Replay never calls the supplied function; it
consumes the earliest unconsumed event matching the tool name and canonical
argument hash. Create a fresh recorder per replay run to keep consumption isolated.

```python
from agentreplay.schema import Trace
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder

store = TraceStore()
trace = store.create_trace(Trace(name="tool-demo"))
record = ToolRecorder(store, trace.id, "record")

@record.tool("add")
def add(a: int, b: int = 3) -> int:
    return a + b

assert add(2) == 5
replay = ToolRecorder(store, trace.id, "replay")
assert replay.call("add", {"b": 3, "a": 2}, add) == 5
store.close()
```

Use `call(name, args, fn)` directly or decorate named, keyword-compatible
functions. The decorator accepts positional/keyword invocation and fills defaults;
it rejects positional-only and variadic signatures. Tools must be synchronous,
and arguments/results must serialize as JSON. Use JSON-native types for identical
Python types across record and replay: JSON serialization converts tuples to lists
and non-string dictionary keys to strings. Unsupported objects and non-finite
numbers raise a `TypeError` naming the tool; results are never silently stringified.

Record mode stores tool exceptions and re-raises the original exception. Replay
raises `ToolReplayedError` containing the stored type/message; missing or exhausted
matches raise `ToolReplayMiss`. Failed calls are consumed once, just like successes.
Tool snapshots, errors, and latency persist in `tool_events`; replay does not append
new events to the source trace. Tool args/results/errors may contain sensitive data.

[examples/minimal_agent.py](examples/minimal_agent.py) exposes
`await run_agent(client, recorder)` with one arithmetic tool and at most three
LLM requests. Pass an `httpx.AsyncClient` targeting the proxy and a recorder sharing
its trace ID and database. In record mode this executes the tool; in replay mode
both HTTP responses and tool results come from the recording. The example uses
HTTPX rather than an OpenAI SDK.

Run the complete offline example verification from the repository root:

```bash
pytest -q tests/integration/test_minimal_agent.py
```

The end-to-end test records two LLM exchanges and one tool event, reopens the
SQLite database, and runs the same agent against the replay proxy. It asserts the
same final answer, no additional upstream calls or real tool executions, and no
remaining LLM exchanges. Socket connections are disabled throughout the tests.
Other example tests check HTTP errors, unknown tools, direct answers, and the step
limit. No GPU or model credentials are required.

M2 verification: the full suite has 59 passing tests; `ruff check .` passes. The
existing HTTPX TestClient deprecation warning remains. Adding the tool-events
table bumps the schema to v3; v2 databases are rejected and need a fresh database
path, as specified for pre-release changes. Fault-plan loading and fault logging
remain M5 work; M2 only exposes an optional post-result callback hook.


## M3: runs and portable traces

After installing the package, use a fresh database path to inspect a golden trace:

```bash
agentreplay import --db demo.db --in tests/golden/minimal-add.jsonl
agentreplay list --db demo.db
agentreplay show --db demo.db --trace-id minimal-add
agentreplay export --db demo.db --trace-id minimal-add --out exported.jsonl
```

These commands were run against a temporary fresh database, and the exported file
matched the golden trace byte-for-byte. `show` includes requests, responses, SSE
chunks, tool arguments/results/errors, runs, and their findings. Invalid input,
missing traces/files, and incompatible versions exit with code 2 and a clear error.
The CLI closes its store when it returns.

The UTF-8 JSONL format is `agentreplay.trace.v1`: a trace header carrying `format`
and `schema_version`, followed by exchanges and tool events. IDs, timestamps,
request hashes, null values, and canonical JSON are preserved on round trip.
Records are exported by sequence, with exchanges before tools on tied sequences;
the two sequence counters remain independent. Runs and findings are execution
history and are not part of a portable trace. An imported trace therefore initially
has zero runs. Export recordings after execution has finished.

Import validates every line with strict Pydantic models before writing anything.
It rejects unknown formats/schema versions, missing or extra fields, malformed
JSON, invalid UTF-8, non-finite numbers, mismatched hashes/trace IDs, duplicate IDs,
and unordered or duplicate sequences within either event type. Database conflicts
roll back the whole import. Re-importing the same trace is an error rather than an
implicit overwrite. The current schema is v7; older databases need a fresh path.
The export format remains provisional until the M10 freeze.

For an agent executed in process, wrap its execution in a run context:

```python
from agentreplay.runs import RunRecorder
from agentreplay.schema import Run, Trace
from agentreplay.store import TraceStore

store = TraceStore()
trace = store.create_trace(Trace(name="run-demo"))
with RunRecorder(store, Run(kind="record", trace_id=trace.id)) as run:
    # Execute the agent here; set task_success using a verifier when available.
    run.task_success = True
assert run.outcome == "completed"
store.close()
```

The context stores configuration on entry and finalizes once on exit: normal
return is `completed`, an agent exception is `agent_error`, and an interrupt or
process exit is `aborted`. It preserves the original exception. Task success is
separate: a completed agent may still fail its task. The context does not create
a trace or own the database connection. The proxy creates a default startup run. To share context-managed replay state
with HTTP requests, pass `manager=app.state.runs` to `RunRecorder` and route those
requests with `X-AgentReplay-Run: <run.id>`. Server shutdown does not infer an
agent's outcome; finish the run explicitly.

The two fixtures in `tests/golden/` were produced by the actual minimal agent
against an HTTPX mock upstream. One records one add call; the other records two
identical add calls before a final answer. Their timestamps and latencies are
injected fixture values, and only random row IDs are normalized after capture;
request, response, and tool data are not hand-edited. Regenerate them from the
repository root with:

```bash
python -m scripts.make_golden
pytest -q tests/integration/test_golden.py
```

Tests regenerate the fixtures twice, compare them to the checked-in bytes, and
replay both with the unmodified agent while real tool execution and socket
connections are blocked. M3 verification: 105 tests passed and `ruff check .`
passed on local Python 3.14. The existing TestClient deprecation warning remains.
The `check` command and structured divergence reporting are described below.


## M4: offline regression checks and run routing

Run the actual minimal agent against both golden traces without a server or GPU:

```bash
agentreplay check --golden tests/golden --agent examples.minimal_agent:run_agent
agentreplay check --golden tests/golden --agent examples.minimal_agent:run_agent --mode lenient
```

Run from the repository root after installing the package. The agent reference is
`MODULE:CALLABLE`; the callable receives `(httpx_client, tool_recorder)` and can
return a value or an awaitable. The checker temporarily adds the working directory
to the module search path, then restores it, so repository-local agents work with
the installed executable. It creates a fresh in-memory DB per golden file, runs
the callable through the replay proxy, shares its session with the tool recorder,
and finalizes the run before writing a JSON report to stdout.

Exit codes are 0 for all checks passing, 1 for any replay divergence or error-severity
finding (or agent failure), and 2 for usage/configuration errors. Strict order
warnings count as regressions; lenient mode never emits them. Stream-format mismatch
warnings alone do not fail a check. The JSON report includes per-trace outcomes,
findings, nearest recorded requests, hashes, and request diffs. Known HTTP/replay/
standard agent errors become structured failures; an unexpected custom exception
still finalizes its run and then propagates, producing a failed process rather
than a misleading passing report. Unchanged-agent exit 0 and changed-prompt exit 1
were verified using the installed CLI as well as pytest.

Divergence findings are `replay_miss`, `tool_replay_miss`, `replay_order`, and
`replay_leftover`. A miss preserves pending recordings and returns HTTP 409 for
LLM requests or raises `ToolReplayMiss` for tools. Request evidence selects the
recorded exchange with the attempted sequence position, otherwise the closest
message count (lower sequence wins ties), including already-consumed recordings.
Diffs use escaped JSON Pointer paths with added/removed/changed values previewed
at at most 200 characters. At finish, one leftover finding lists all unused
exchange/tool sequences. Its insertion and run finalization share a transaction.

The proxy supports:

- `POST /_agentreplay/runs` with JSON such as
  `{"trace_id":"minimal-add","kind":"replay","replay_mode":"strict"}`.
  It returns HTTP 201 with `run_id`. Omitted fields use the proxy's defaults;
  trace IDs must already exist in its store.
- `POST /v1/chat/completions` with `X-AgentReplay-Run: <run_id>` routes to that run.
  Without the header, it uses the startup run. Unknown, finished, or incompatible
  run IDs return HTTP 400. Each run has independent replay consumption, including
  concurrent runs of the same trace.
- `POST /_agentreplay/runs/{run_id}/finish` accepts, for example,
  `{"outcome":"completed","task_success":true}` and returns the finished run
  and findings. It rejects repeat finishes and requests still in flight. Streaming
  requests stay active until completion or disconnect cleanup. Finish performs replay checks and M6 failure detection atomically.

For tools in the same process, obtain `session = app.state.runs.session(run_id)`
and pass `session=session` to `ToolRecorder(store, trace_id, "replay", ...)`.
For a context-managed run, pass `manager=app.state.runs` to `RunRecorder` and use
`context.replay` for the tool recorder. Standalone replay tool recorders create
their own persisted run/session, accessible as `recorder.session`; call its
`finish("completed")` when done. A standalone replay `RunRecorder` creates its
own session at `context.replay` and checks leftovers on exit. Use one manager and
shared session per run; separate proxy processes do not coordinate consumption.

Compare two persisted runs:

```bash
agentreplay diff --db traces.db --run-a RUN_A --run-b RUN_B
```

The JSON comparison includes both runs, their findings (including miss evidence),
and differences between requests in their source traces by sequence. Replay does
not record a second copy of successful incoming requests; this command labels its
request comparison `source_request_diffs` accordingly.

M4 verification: 141 tests passed with socket connections disabled and Ruff passed
on local Python 3.14. Coverage includes concurrent same/different-trace runs,
strict/lenient order behavior, full miss evidence, atomic leftovers, check exit
codes, context/session sharing, and stream disconnect cleanup. The existing
TestClient deprecation warning remains. Schema v4 and golden artifact bytes are
unchanged. Live vLLM fixtures, the Python CI matrix, and the previously noted
health/model endpoints remain unfinished outside this milestone.

Development uses dedicated feature branches and GitHub pull requests. M4 was
merged through [PR #1](https://github.com/Yaswanth2120/agentreplay/pull/1); M5 uses
`feature/m5-fault-injection`. Local checks run before committing and publishing.


## M5: seeded fault injection

`examples/faults.yaml` is a version-1 plan that injects HTTP 429 at exchange sequence
0. After installing `.[dev]` and activating the virtualenv, exercise it offline:

```bash
PYTHONPATH=src agentreplay import --db faults.db --in tests/golden/minimal-add.jsonl
PYTHONPATH=src agentreplay eval faults --db faults.db --plan examples/faults.yaml --trace-id minimal-add
PYTHONPATH=src agentreplay show --db faults.db --trace-id minimal-add
```

Use a fresh database path. The evaluation returns JSON containing a run ID, SHA-256
plan ID, mutated exchanges/tool observations, errors, skips, pending loops, and
persisted fault rows. `show` prints those rows alongside the run and findings.
This sequence was verified from the terminal, with `PYTHONPATH=src` set explicitly
for the local source checkout. An editable-install import issue in this environment
remains to be rechecked during M10's fresh-virtualenv validation.

The M5 command is a **fixed recorded trajectory**, marked `scored: false`: it walks
the stored requests and tool calls by sequence, placing exchanges before tools on
tied sequence numbers. It does not execute an adaptive agent, feed mutated results
into later request construction, score task success, or compute detector metrics.
Those capabilities belong to later milestones. Source trace rows remain unchanged;
fault labels and the new replay run are persisted separately. Replay observations are emitted in the command's JSON and saved at finish in
`run_observations`, separately from the immutable source trace. M6 detection findings
are included in the JSON; the command still makes no scoring claims.

Plans contain `version: 1`, an integer `seed`, and a list of faults. Each fault has
`class`, `target`, exactly one selector, and optional class-specific `params`:

```yaml
version: 1
seed: 1234
faults:
  - class: tool_exec_error
    target: tool_event
    select: {every_nth: 3}
    params: {exception: ValueError, message: simulated tool failure}
```

Sequence numbers are zero-based: `seq: [2]` selects the third event;
`every_nth: 3` selects sequences 2, 5, 8; `probability: 0.1` uses seeded draws.
Probability draws are generated in canonical `(sequence, rule-index)` order for all
probability rules, regardless of target or arrival/completion order, and cached.
Every run owns its own selector. The same plan and recorded inputs therefore choose
the same injections; random row IDs, run IDs, and timestamps are not reproducibility
signals. Tests compare the fault-class/target/sequence/params columns directly.

Unknown classes, versions, targets, selectors, parameters, and invalid values fail
with `FaultPlanError`. The plan ID hashes canonical validated JSON including filled
defaults, so YAML whitespace and mapping order do not change it. The normalized
plan is saved in `run.config.fault_plan`; the digest is `runs.fault_plan_id`.
The first applicable selected rule wins for each event. Ineligible tool mutations
are skipped without creating a false injected-fault label.

| Class | Target | Parameters / behavior |
|---|---|---|
| `llm_timeout` | exchange | `delay_ms` defaults to 301000; record/live delay then HTTP 504; replay immediate 504 with marker |
| `llm_http_error` | exchange | `status` defaults to 500; integer 400–599 |
| `llm_malformed_json` | exchange | Truncate the canonical response JSON halfway; retain raw malformed data |
| `llm_empty_response` | exchange | Set `choices` to an empty list |
| `tool_args_corrupt` | exchange | Make the first tool call's arguments invalid JSON |
| `tool_wrong_name` | exchange | `name` defaults to `__undefined_tool__`; ensure it is not a declared tool |
| `tool_args_schema_violation` | exchange | Choose valid JSON rejected by that tool's declared JSON schema |
| `tool_loop` | exchange or tool_event | `repeat` defaults to 5 and must be at least 3; repeat identical name/args with unique call IDs |
| `tool_exec_timeout` | tool_event | Raise `TimeoutError`; configurable `message` |
| `tool_exec_error` | tool_event | Configurable `exception` and `message`; default RuntimeError |
| `tool_bad_result` | tool_event | Require a declared result `schema`; `value` defaults to null and must violate it |

Execution-error exceptions are RuntimeError, ValueError, TypeError, OSError,
KeyError, TimeoutError, or ConnectionError. Params must be JSON-native values with
string object keys. JSON Schema uses local Draft 2020-12 validation; external
schema references are rejected so replay cannot resolve schemas over the network.
Bad-result injection skips a base result that already violates the declared shape.

An exchange-targeted loop repeats the first tool call in that response. A
tool-event-targeted loop schedules that tool's name/args in the next eligible LLM
response; it is logged against the original tool sequence only when that response
is actually changed. A scheduled loop with no following eligible response remains
pending and is not falsely logged as injected. Additional tool calls may legitimately
cause replay misses because the recording has no corresponding tool results.

Pass `--faults plan.yaml` to either `agentreplay record` or `agentreplay replay`.
The API equivalent is `create_app(..., faults=FaultPlan.load(path))`. Proxy control
runs inherit this plan; `config.fault_plan` can supply another validated plan or null
to disable it for an individual run. All runs keep separate runtime/selection state.
For record/live Python tools, share `faults=app.state.runs.faults(run_id)` with the
tool recorder. Replay tools sharing `session=app.state.runs.session(run_id)` inherit
its runtime automatically. A standalone recorder also accepts `faults=FaultPlan`.

Fault rows are committed before a mutated value or injected exception is exposed.
Tool faults happen after the real/replayed base result, so record-mode side effects
are not rolled back. Record/live tool events are updated to the observed result or
error; replay never changes the source event. HTTP errors/timeouts selected before
upstream bypass it, and timeout rows exist before the live delay begins. Live timeout
delays use the configured plan value; choose it to exceed the client's timeout.
Replay never sleeps. Streams selected for content mutation are buffered and then
re-emitted as mutated SSE; clean streams retain progressive passthrough behavior.

Schema v5 adds the `faults` table. Older databases are rejected without migration;
golden files were regenerated with the committed script, changing only their schema
header from v4 to v5. M5 verification: 218 tests pass with socket connections blocked,
and Ruff passes. Every class has a mutated-output test, both tool modes are covered,
and seeded fault-row reproducibility, source immutability, delayed-label ordering,
plan hashes, CLI wiring, and failure cleanup are tested. The existing TestClient
deprecation warning remains. No GPU or live LLM was used.


## M6: offline failure detection

M5 is complete; its fixed-trajectory evaluator intentionally remains unscored.
M6 adds pure detectors taking only a sanitized `RunView`, with no store, fault
plan, injected labels, network, clock, or random identifier access. `DetectorRun`
contains only run ID, trace ID, kind, and outcome. Finding IDs and timestamps are
stable when detection is repeated on a finished run.

```bash
agentreplay detect --db traces.db --run-id <finished-run-id>
```

This prints JSON and replaces only detector-owned findings, preserving replay
findings. Unknown/unfinished runs and invalid configuration exit 2. A replay run
must have an observed snapshot; detection never substitutes the unconsumed source
trace. Record/live runs without a snapshot can use their captured trace rows.
RunRecorder, ReplaySession, and proxy control finishes automatically detect and
atomically commit run completion, observations, and findings.

| Kind | Rule / default |
|---|---|
| `invalid_tool_call` | Undeclared function, invalid JSON object arguments, or declared JSON Schema violation |
| `loop` | 3 consecutive identical name/argument signatures, or a cycle of length 2–4 repeated 3 times within 24 calls |
| `timeout` | HTTP 504/599 or exchange/tool latency strictly above 60000 ms |
| `malformed_response` | Missing/unparseable response, absent/empty choices, or null content without tool calls |
| `llm_error` | HTTP status >=400 unless classified as timeout |
| `tool_error` | Tool error is present, including an empty error string |
| `budget_exceeded` | More than 25 exchange attempts; warning severity |

Other detector findings have error severity. HTTP error envelopes are classified
by status instead of also being called malformed. Empty text is valid. Loop
analysis prefers emitted calls and annotates matching executed tools without
counting them twice; it falls back to executed events when no calls were emitted.
Replay budget counts misses as attempts. A tool TimeoutError is a tool error;
timeout additionally requires measured latency above the configured threshold.

Pass thresholds as `config={"detectors": {"timeout_ms": 60000,
"tool_timeout_ms": 60000, "max_steps": 25, "loop_repeats": 3,
"max_cycle_length": 4, "loop_window": 24}}`. Omitted/null tool timeout inherits
the exchange timeout. Invalid fields and thresholds are rejected before creating
runs. JSON Schema validation supports local references and rejects external ones.
Task failure and undetectable bad tool results require independent task verifiers
in later milestones; injection labels are never used to manufacture findings.

Schema v6 adds `run_observations`; older databases are rejected without migration.
Golden files were regenerated with the committed script (only version headers
changed). All 310 tests pass offline, including 91 additional cases beyond the
schema-version compatibility case, restart detection, atomic rollback, repeat CLI
runs, every detector's boundaries, and both clean goldens. Ruff passes. The M5
HTTP-error test now intentionally expects the automatically produced `llm_error`.
Four pre-existing untracked duplicate files are left untouched and excluded by
exact paths from pytest/Ruff. The existing TestClient deprecation warning and
editable-install environment issue remain; CI and real vLLM chunk validation are
still pending. No GPU was used. M7/M8 can proceed offline; rental is planned for
M9 live evaluation, with notice before any earlier live validation needs it.


## M7: environments, frozen tasks, and reference agent

The task set contains 40 distinct instructions per environment (200 total):
16 difficulty-1 tasks, 16 difficulty-2 tasks, and 8 difficulty-3 tasks in each.
The supplied workflows use 1, 3, and 6 tool calls respectively. These are small,
procedural tool-use tasks; difficulty describes the workflow length, not a claim
about general reasoning difficulty. The corpus is synthetic, not a real search
benchmark. Automated solution checks do not establish task quality by themselves.

| Environment | Tools and rules |
|---|---|
| `calc` | add/subtract/multiply/divide; compatible length, mass, and time conversions; finite values only |
| `kv` | JSON get/set/delete/list; isolated fixture key; missing keys raise recoverable errors |
| `files` | in-memory read/write/list/grep; relative POSIX paths; literal, case-sensitive grep with one-based line numbers |
| `search` | all-token keyword search and document lookup over 500 frozen documents; deterministic ID ordering |
| `tickets` | create/update/query; sequential IDs; assignment required to advance open → in_progress → resolved; resolved tickets immutable |

Use `make_environment(name, seed)`, then `env.call(name, args)`. Dispatch validates
JSON-native, finite arguments against the advertised schema before executing.
`reset(seed)` restores fixtures, `snapshot()` returns detached JSON state, and
`state_digest()` fingerprints it. Tool definitions and results are copied so
mutating a returned value cannot alter another task's environment. Files never
access the host filesystem; search reads only its packaged corpus fixture.

Tasks live in `src/agentreplay/evals/data/tasks.jsonl`; the corpus is beside it.
`load_tasks()` validates models/IDs and `tasks_version()` returns the SHA256 of
exact task-file bytes with a `tasks-v1` prefix. Freeze these files: any later
correction must be treated as a new evaluation version. M8 records that
version in every run; this milestone does not implement the harness or scoring.
To reproduce artifacts in a temporary directory without overwriting frozen data:

```bash
PYTHONPATH=src python -m agentreplay.evals.make_tasks \
  --out /tmp/agentreplay-tasks.jsonl \
  --corpus-out /tmp/agentreplay-corpus.jsonl \
  --review-out /tmp/agentreplay-review.md
```

The fixed generation seed is 20261122. Task/corpus/template generation is checked
byte-for-byte against committed artifacts. Runtime package data includes the JSONL
fixtures and review template. The wheel also passed a data-loading smoke test.

`verify(task, state, final_answer)` compares exact final state for mutable
environments, ordered answer codes for search, and a finite JSON numeric result
with absolute tolerance 0.000001 for calc. Mutable-state comparison preserves JSON
types and unrelated fixture entries; prose claiming success does not pass. The
verifier does not execute solutions or import environments, generators, detectors,
or fault labels. Expected values stay outside the agent prompt.

`examples/reference_agent.py:run_agent` accepts an injected HTTPX AsyncClient,
task, environment, optional ToolRecorder and run ID, and v1/v2 settings. It sends
`temperature=0.7` and `seed=trial`, executes batches in order, returns tool errors
to the model for recovery, and enforces a 25-request default budget. HTTP failures
surface to the caller. The caller owns fresh environment creation and run finish.
Version v2 intentionally skips one execution in its first multi-call batch, chosen
by a local RNG seeded with `regression_seed` (default 7), and returns an explicit
error for that call. Single-call batches are unaffected. This controlled regression
has no claimed live success-rate effect until measured. Tool replay returns saved
results without applying mutations to a fresh environment; M8 must not treat that
untouched environment as reconstructed final state for mutable task verification.

M7 verification: **603 tests passed** (293 additional cases), Ruff and diff checks
passed, and a clean wheel loaded its packaged fixtures. Verification includes all
200 scripted correct/wrong solutions, 15 reference-agent
environment/difficulty combinations, natural error recovery, v2 reproducibility,
and record/replay with zero model/tool execution. The v2 replay produces a miss
and leftover findings. Existing tests are retained. The existing TestClient warning
remains; no live LLM or GPU was used. Full harness/statistics/reports are M8, live
GPU evaluation is M9, and CI/release work is M10.

The owner must separately hand-solve the 20 sampled tasks in
[src/agentreplay/evals/REVIEW.md](src/agentreplay/evals/REVIEW.md), then inspect each
verifier and record solvability/correctness. It remains **PENDING**. Success-rate
results should not be treated as validated until that review is completed.


## M8: evaluation harness and generated reports

Run the committed scripted configuration without a model or GPU:

```bash
make eval-offline PYTHON=.venv/bin/python DB=/tmp/agentreplay-m8.db
make report PYTHON=.venv/bin/python DB=/tmp/agentreplay-m8.db OUT=reports/offline
```

Use a fresh DB path for schema v7; older databases are rejected without migration.
The new `evaluation_results` table stores final answers, verification state, and
reconstruction errors atomically with run finish, snapshots, and findings. Schema
v7 also adds the specified `eval_labels` table. Committed golden files were
regenerated, changing only the version header. No earlier tests were removed.

Equivalent CLI: `agentreplay eval run --db DB --config examples/eval-offline.yaml`
and `agentreplay report --db DB --out reports/offline`. The run command returns
an evaluation ID; report accepts `--evaluation-id ID` to select it explicitly,
or selects the most recent cohort. It refuses missing/unfinished results,
inconsistent metadata, duplicate rows, missing task/trial/version pairs, or
incomplete replay phases. Re-reporting the same DB produces identical files.
Re-executing evaluation creates a new cohort; retry-safe live resume is M9 work.

The committed configuration selects 20 tasks across all five environments and
runs three trials of both v1/v2. It records 120 source runs, repeats each twice
(240 replays), runs v2 against 60 v1 traces, and applies an HTTP429 fault plan to
60 clean v1 traces: 480 runs total. Every source uses a fresh seeded environment;
sampling is temperature 0.7 and seed=trial. Run config records the model, agent
version, task-file hash, vLLM version/flags, git SHA, source linkage, phase, and
backend. `scripted` is an explicitly fake LLM which uses only visible instructions
and tool results. These reports test infrastructure, not live model capability.
The generated examples are in `reports/offline/summary.{md,json}`.

The packaged reference implementation now lives in `agentreplay.evals.agent`;
`examples/reference_agent.py` preserves the existing imports. Installed CLI runs
therefore do not depend on a source-checkout-only examples package. The harness
also accepts an injected transport for the live path, which has been tested with
a fake upstream; no actual vLLM request has been made in M8.

Replay ToolRecorder still never executes its supplied callback. Verification
separately reconstructs the five virtual environments from the original successful
source tool actions that were actually consumed, in their consumption order.
Recorded natural failures have no state effect because these environment operations
validate before mutation. A post-execution injected error preserves its original
side effect. If a regression omits prerequisites, reconstruction records the failed
actions and verification fails. This simulation is specific to these deterministic
virtual environments; it is not a way to rerun arbitrary external side effects.
The harness emits a run-level `task_failure` only when verification fails. A task
can succeed through its final state even if the agent later surfaces a tool/HTTP
error; outcome and task_success deliberately describe different things.

`stats.py` owns all calculations, using only Python's standard library:

- Task success, tool-call validity (emitted calls), loop/timeout rates, and mean steps.
- Clean replay false-positive rates per detector and overall; eligibility requires
  a successful source with no findings. Both repeats count in the displayed n.
- Determinism per source, comparing the two repeats' outcomes, task success,
  normalized finding sets, and verified results. IDs/timestamps are excluded.
- Injected-fault precision/recall with Wilson 95% intervals and actual TP/FP/FN
  counts. Unique kind/target/sequence claims match one-to-one; an exchange sequence
  never substitutes for a tool sequence. Cascaded findings without labels are FP.
- Manual-label presence scores are separate from injection scores; conflicting
  reviewers require adjudication. Sequence annotations are informational here.
- F1 point estimates with support n; conservative confidence bounds combine
  Bonferroni-adjusted 97.5% Wilson precision/recall intervals through the harmonic
  mean. F1 is not falsely treated as an independent binomial proportion.
- Paired v2-minus-v1 success difference with 10,000 seeded bootstrap resamples,
  plus two-sided exact McNemar on discordant task/trial pairs.

Rates carry numerator, n, and interval. Undefined denominators stay `value: null,
n: 0` in JSON and `undefined` in Markdown. Rendering refuses numeric rates without
n. Repeated trials/replays are correlated; displayed sample sizes do not imply
independent-task generalization. No significance claim is made about scripted data.
A run-level task_failure cannot localize a bad-result injection's original sequence;
those fault labels are explicitly counted as unscored for detector P/R, rather than
being credited using hidden fault metadata. They still affect task-success metrics.

For the owner's later 100 natural-run labels:

```bash
agentreplay label --db DB --run-id ID --labeler owner
```

This shows observed exchanges/tools to stderr without detector findings, task
success, or fault labels, then asks y/n for loop, invalid_tool_call, and timeout.
It collects all answers before an atomic save and replaces that reviewer's prior
judgments. Injected runs are rejected. Do not inspect detector output before
labeling. M8 does not fabricate human labels or complete the pending 20-task review.

M8 verification: **658 tests passed** (55 additional cases); Ruff, diff checks,
and the installed-wheel CLI smoke passed. Coverage includes hand-computed statistics, 20×3×2 source executions with
all replay phases, cold/idempotent reports, injected adaptive fault classes,
explicit FP/non-determinism perturbations, reconstruction/side-effect checks,
blind manual labeling, error rollback, cancellation, and installed-wheel CLI use.
The existing TestClient warning remains. The early real-vLLM streaming fixture
check is still outstanding; M9's live work and M10's CI/release checks remain.
No rental GPU has been started. **M9 is the next milestone that needs a GPU**;
prepare configuration and preservation scripts before paying for the rental.


## M9: live preparation (hardware work pending)

The [live runbook](docs/LIVE_RUN.md) covers setup, exact serving flags, resume,
streaming smoke capture, manual labeling, and verified copies off the rented GPU.
`eval.yaml` pins Qwen3-8B model/tokenizer revisions and vLLM, sets bounded generation
with thinking disabled, and defines all 12 independent fault variants. The GPU
records v1 then v2 once; fault sweeps and repeat checks use those recordings offline.
`make live-plan PYTHON=.venv/bin/python` is safe to inspect without a GPU.

Resume keeps completed task/trial/version results, including verified failures,
and retains abandoned attempts as history. `eval run --resume ID --replay-only`
refuses missing source measurements and never makes model calls. State/config/code
checks prevent silent cohort mixing. Backup uses SQLite's WAL-safe API and exports
real goldens plus a checksummed archive; copying and verifying it off-machine is
still an owner-driven step, not something a local backup command can claim.

Preparation verification: **673 tests pass**, Ruff and shell syntax checks pass,
and the non-GPU plan command passes. Tests simulate interruptions, changed
provenance, multi-plan resume, preserved WAL backups, importable golden exports,
blind stratified samples, and server/stream preflights with fake transports.
No dependencies or DB schema version changed. The existing TestClient warning
remains, and the four pre-existing duplicate files stay untouched.

**M9 is not complete:** no actual GPU/server has been accessed, no real streaming
fixture or live 200×3 measurements have been captured, the owner's review and
100 labels remain pending, and no archive has yet been copied off a rented machine.
The next external requirement is a CUDA-capable Linux GPU host and its SSH command.
Recommended headroom target: one 48 GB VRAM GPU; see the runbook for the estimate
and official sources. No paid resources have been started.

Existing issue found in M9 review: natural tool-error text gains a
`ToolReplayedError` wrapper on replay, so an unchanged agent can diverge after that
error. It was reproduced offline on v2 `tickets-017` and is documented in the live
runbook. Clean FP eligibility excludes those failed source runs; repeat determinism
compares two replays and does not establish source-to-replay equivalence. The agent
normalization fix remains outside this config/script milestone.
