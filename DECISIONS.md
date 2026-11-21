# Decisions

## M1 — streaming (2026-11-21)

- **Scope approved by the owner:** move canonical hashing out of `schema.py` and
  add enough runs/findings storage to persist `stream_mismatch`. `schema.py`
  keeps its old hashing imports available so existing callers and tests work;
  implementation and key definitions live in `canonical.py`. No M2 work was
  started.
- **Versioning:** bump `SCHEMA_VERSION` from 1 to 2. Check `schema_meta` before
  changing a nonempty database, rejecting old, unversioned, and future versions
  without migration. New databases use canonical JSON, foreign keys, and WAL
  (SQLite in-memory databases retain their normal memory journal).
- **SSE representation:** `Exchange.chunks` contains raw decoded `data:` lines,
  including `[DONE]` and usage-only chunks. Forward all incoming lines immediately;
  replay preserves data content and order, normalizes separators to LF blank
  lines, and adds `[DONE]` if absent. Byte segmentation, comments, and timing are
  transport details, outside the replay guarantee. OpenAI-compatible one-line JSON
  data events are the supported streaming format.
- **Completion shape:** store a full non-streaming completion envelope in
  `Exchange.response`, with messages at `choices[i].message`. Sort choice and tool
  indices for their output lists; concatenate content/argument fragments in arrival
  order, retain the first ID/name and last non-null finish reason, and retain usage
  on the envelope. This lets the existing `tool_calls()` work unchanged.
- **Partial errors:** on an upstream HTTP transport error after streaming begins,
  retain received data, partial reassembly, and an error string with stored status
  599. The already-sent client status remains unchanged and capture adds no success
  marker. Malformed stream payloads remain available in raw chunks with a parse
  error and no fabricated completion. Replay still follows the specification's
  stored-status and final-marker rules.
- **Conversions:** non-stream recordings become a single synthetic JSON chunk plus
  `[DONE]`, including indexed tool calls; stream recordings return their assembled
  completion for JSON requests. Persist a warning with both stream flags and the
  exchange sequence. HTTP error JSON stays JSON even when `stream=true` was
  requested; converting it into a success-shaped chunk would lose the error body.
- **Minimal run support:** each app creates a startup run. The runs table has the
  specification's columns, but the M1 model and store expose only creation fields.
  Run finalization, `RunRecorder`, inspection commands, per-request run headers,
  and control endpoints remain M3/M4 work. Findings are accessed through the store;
  no new CLI is introduced in M1.
- **Concurrency:** reserve sequence numbers under the shared store's lock before
  upstream I/O, rather than when responses finish. Reservations are scoped to one
  store instance; cross-process writers are not supported. Interrupted requests
  can leave sequence gaps. SQLite reads and writes share the same lock.
- **Testing:** use injected HTTPX transports and clocks, with an autouse socket
  connection guard. A direct ASGI test proves passthrough happens before upstream
  completion; buffered TestClient responses alone cannot prove that property.
  The old 501 test was deliberately replaced because streaming support changes
  that expected behavior. No existing matching or non-stream test was weakened.
- **Tooling:** Ruff is an approved dev dependency and is now declared. Validation
  uses a local `.venv`; `.gitignore` excludes it, caches, generated package metadata,
  and trace databases. No unapproved dependencies were added.
- **Live validation deferred:** do not rent a GPU for unit/integration tests. A real
  vLLM recording and observed chunk fixtures should be added when an upstream is
  available, per the specification's known streaming risk. M1 acceptance is offline.

## Existing gaps outside M1

- The baseline has no `/healthz` or `/v1/models`, and no upstream authorization
  configuration. These remain unimplemented; this milestone does not claim them.
- Structured replay-miss findings, order warnings, leftovers, and per-run request
  routing remain M4; the baseline matcher still only raises `ReplayMiss`.
- The trace model lacks the later task/agent/model metadata columns. The approved
  M1 storage work adds only streaming fields and the runs/findings prerequisites.
- A non-stream upstream body that is valid JSON but not an object is incompatible
  with the baseline `Exchange.response` type. This is outside streaming acceptance.
- This workspace is not a Git repository, so no commit or Git diff is available.
- Only local Python 3.14 was run. The required 3.10/3.11/3.12 CI matrix is M10 work.

## M2 — tool capture and minimal agent (2026-11-21)

- **Schema:** bump v2 to v3 for `ToolEvent` and the `tool_events` table/index from
  section 5. Keep exchange and tool sequence counters independent. The earlier
  version-rejection test now derives the expected current version and additionally
  rejects v2; this changes the intended schema version, not the rejection behavior.
- **Matching placement:** `ToolReplayer` lives in `replay.py`, and argument hashing
  lives in `canonical.py`. Each recorder owns a fresh matcher loaded from ordered
  stored events at construction. Match on name plus argument hash, consume once,
  and retain pending events on a miss. No tool order/leftover findings are added
  ahead of M4. Construct replay recorders after recording finishes.
- **Argument strings:** `tool_args_hash` hashes canonical parsed objects, including
  objects supplied as JSON strings. Invalid JSON and valid non-object JSON use the
  SHA-256 of `raw:` plus the original string. `ToolRecorder.call` itself requires
  a dict because the specified execution interface is `fn(**args)`.
- **Snapshots:** snapshot arguments before invocation and results before persistence
  to prevent later mutation from altering a recording. Record returns the real
  result; replay returns its JSON representation. JSON-native types retain exact
  shapes; JSON-serializable tuples and non-string mapping keys follow stdlib JSON
  conversion rules. Reject sets, arbitrary objects, cycles, NaN, and infinity with
  a named `TypeError`; there is no fallback to `str()`.
- **Errors and engineering rule:** section 8 requires recording arbitrary tool
  exceptions, while section 15 forbids broad `except Exception` outside the proxy.
  Use `try/finally` and `sys.exc_info()` to persist failures while allowing their
  original exceptions and tracebacks to propagate. A success flag prevents an
  unrelated exception being handled by the caller from becoming a tool error.
  A serialization failure after execution is persisted as an error event too.
  Invalid arguments rejected before execution do not create tool events.
- **Null results:** store a successful `None` as JSON `null`, with no error.
  An error stores SQL NULL in `result` and the original exception type/message in
  `error`; replay distinguishes them using the error field and raises
  `ToolReplayedError(original_error_string)`. Even error events are consumed once.
- **Decorator:** bind positional/keyword invocations to named parameters and apply
  defaults before calling the recorder. Reject positional-only, `*args`, and
  `**kwargs` signatures at decoration because the public recorder executes via
  keyword arguments. Preserve the original function's name and docstring.
- **Fault extension:** the optional `faults` argument is a callable taking the
  matched/stored `ToolEvent` and the result. It runs only after a successful real
  or replayed result is available, and after base event storage on record, in
  either mode. This is an extension hook, not a fault-plan implementation; M5
  must add plan validation, mutations, and durable fault logging before return.
- **Example:** expose a small async `run_agent(client, recorder, add_fn=add)`
  callable using HTTPX and one `add` tool. Injecting the client lets the exact same
  agent execute against live proxy HTTP or an in-process mock proxy. Bound it to
  three LLM requests and reject unknown tools explicitly. The repository root is
  added to pytest's `pythonpath` so integration tests can import the example;
  examples are not bundled into the library package.
- **Verification:** keep the autouse socket guard. Test record/replay after closing
  and reopening the database, with upstream and real-tool counters unchanged by
  replay; also pass a forbidden upstream transport. Unit tests verify matching,
  failures, decorators, snapshots, stored JSON, isolated recorders, result hooks,
  and concurrent sequence reservations. No dependencies were added.
- **Scope:** no M3 context manager, export/import, golden traces, inspection CLI,
  or new run lifecycle behavior. Existing M1 gaps above remain, including missing
  health/model endpoints, valid non-object upstream JSON handling, live vLLM
  validation, and the unrun Python 3.10–3.12 matrix. The workspace still has no Git
  metadata; no commit is claimed.

## M3 — run lifecycle, portable traces, and inspection (2026-11-21)

- **Schema v4:** add section 5's trace task/agent/model metadata columns, complete
  the run model to match the existing runs table, and add its task/version/model
  index. The trace layout changes, so v3 databases now fail the existing explicit
  version check. Extend the old version test to include v3; no migration or test
  weakening is introduced. The previously documented missing trace metadata is
  resolved by this milestone.
- **Context API:** `RunRecorder(store, Run(...), clock=...)` creates the run on
  entry, copies the input model, and returns the active model. Configuration is
  stored on entry; task success and detail can be set before exit. On exit it
  updates both the database and returned model. A context is single-use and refuses
  an already-finished model. The caller owns the trace and store lifetime.
- **Outcome semantics:** normal exit means `completed`; agent exceptions mean
  `agent_error`; KeyboardInterrupt/SystemExit/GeneratorExit mean `aborted`. Store
  exception type/message as detail and never suppress the original exception.
  `task_success=False` does not change a normal execution outcome. Finalization is
  conditional on an unfinished row, so repeated finalization cannot overwrite
  history. Clocks are injected, and no detector or leftover checks are added before
  M4/M6.
- **Proxy scope:** the proxy's startup run remains independent of the optional
  in-process context. Do not guess agent success from server shutdown. Routing
  requests to context/control-plane runs, finish endpoints, and per-run consumption
  remain M4. Inspection honestly labels unfinalized startup runs as unfinished.
- **Format naming:** use `agentreplay.trace.v1` in the required trace header now;
  this is the provisional format for schema v4, not a claim of the M10 freeze.
  Reject unknown format and schema versions independently. JSONL contains only the
  trace, exchanges, and tool events; runs and findings stay in the source DB.
- **Strict validation:** keep wire Pydantic models in `schema.py`, with extra fields
  forbidden and no scalar coercion. Require every exported field, including IDs,
  timestamps, and explicit nulls, so model defaults cannot fabricate missing input.
  Reject blank lines, non-object JSON, non-finite constants, negative sequences or
  latencies, duplicate headers/IDs/sequences, cross-trace references, and incorrect
  request/tool hashes. A failed tool event cannot carry a non-null result.
  UTF-8 errors and numbered validation failures produce `TraceFormatError`.
- **Sequence ordering:** exports interleave the two independent sequence streams by
  sequence number, putting an exchange before a tool at the same number. Imports
  require increasing sequence within each type, allowing other valid interleavings;
  gaps are allowed because earlier capture can reserve a sequence before failing.
- **Import transaction:** parse and validate the complete input before persistence,
  then use one SQLite transaction for all trace/exchange/tool rows. Private insert
  helpers share serialization with normal recording without committing nested
  transactions. Existing IDs cause a clear conflict error; do not replace or remap
  user data. Tests force a conflict after a prior exchange insertion and verify
  byte-identical canonical database rows remain unchanged.
- **Export behavior:** canonical UTF-8 JSON, one record per line and a final newline.
  Preserve all stored identities and timestamps. Callers should export finished traces;
  cross-query snapshots during concurrent recording are not provided. Output paths
  are explicit and can replace an existing export; an unknown trace is rejected
  before touching the output file. Parsing and export materialize one trace in
  memory, appropriate for this small local tool.
- **Inspection and CLI:** `inspection.py` renders text without printing or querying
  SQL directly. `cli.py` only parses arguments, calls operations, prints their
  results, starts Uvicorn, and converts expected configuration/I/O/DB errors to
  argparse exit 2. A finally block closes the store, including server/error paths.
- **Golden provenance:** `python -m scripts.make_golden` executes the actual minimal
  agent through the recording proxy with a mock upstream. Fixtures cover one and
  two identical tool calls. Inject fixed fixture clocks and normalize random row
  IDs into a fresh store before calling the real exporter; never alter captured
  request/response/tool bodies or invent observed latency. Reproducibility tests
  compare two generations and the checked-in files, and replay both offline with
  real tool execution forbidden. These are mock fixtures, not live vLLM evidence.
- **Environment and Git:** refreshed the existing editable install so the standalone
  generator and installed CLI could import the package. No dependency was added.
  The workspace had no Git metadata; initialize a local repository and commit the
  verified project snapshot so the required golden traces are tracked. Earlier
  no-Git observations describe the prior milestones, not the M3 final state.
- **Remaining gaps:** the previously noted missing health/model endpoints, handling
  of valid non-object upstream JSON, and lack of upstream auth configuration remain
  unchanged. A real vLLM streaming fixture and Python 3.10–3.12 matrix are still
  unverified. No M4 divergence engine/control plane, M5 faults, or CI workflow is
  claimed as completed.

- **Final verification:** local Python 3.14, 105 pytest cases passed with socket
  connections blocked, and Ruff passed. The installed CLI was separately exercised
  through import/list/show/export against a temporary database, and its export
  matched the original golden bytes. The existing HTTPX TestClient deprecation
  warning is unchanged.
