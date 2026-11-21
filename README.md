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
- [x] Pydantic trace schema (v4), SQLite store, strict/lenient replayer, record/replay proxy, CLI, tests
- [x] Streaming capture/replay, interleaved tool-call reassembly, partial-error capture, and stream/non-stream mismatch warnings (M1 offline tests)
- [ ] Fault injection (timeout, malformed JSON, wrong tool call, loop)
- [ ] Failure detector + labeled eval set (200 tasks x 3 trials)
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

The current schema v4 records a database-level version in `schema_meta`. Older/unversioned
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
implicit overwrite. The current schema is v4; older databases need a fresh path.
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
  requests stay active until completion or disconnect cleanup. Finish currently
  performs replay checks; the other failure detectors remain M6 work.

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

Development now uses dedicated feature branches and GitHub pull requests. M4 is
on `feature/m4-replay-engine`; local checks run before committing and publishing.
