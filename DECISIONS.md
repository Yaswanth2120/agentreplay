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

## M4 — replay regression engine and feature-branch workflow (2026-11-21)

- **Owner workflow:** start new work on a `feature/...` or `branch/...` branch;
  record that continuing instruction in AGENTS.md. This milestone uses
  `feature/m4-replay-engine`, with one meaningful implementation commit and a
  reviewable GitHub PR. GitHub achievements are not a completion criterion; use
  normal project work without fabricated activity or guaranteed award claims.
- **No schema bump:** all persisted fields already exist in schema v4. The new
  strict Pydantic models describe control-plane payloads only; no stored rows or
  golden export format change, and existing fixtures still regenerate identically.
- **Session boundary:** `ReplaySession` owns one run's LLM and tool matchers plus
  its lock, finding persistence, and finalization. Validate it against the persisted
  unfinished run before loading its trace. Keep low-level matching and JSON diff
  in replay.py, HTTP in proxy.py, SQL in store.py, lifecycle/registry in runs.py,
  and offline checking in checks.py rather than putting logic in argparse.
- **Nearest evidence:** count every attempted LLM request, even a miss. Prefer a
  recorded exchange with that sequence, otherwise minimize absolute message-count
  difference and then sequence. Search all recorded exchanges, including consumed
  ones. Empty recordings use null nearest evidence and a diff from an empty object.
  The finding includes request hash, attempted position, nearest ID/seq/request,
  and JSON diff; a miss never consumes an event.
- **Diff representation:** escaped JSON Pointer paths; arrays compare by index.
  Emit added/removed/changed lists with canonical JSON value previews truncated to
  200 characters, not unbounded values. Numeric types matter, so 0 and 0.0 differ.
  Diff the full recorded/incoming requests for useful context, even though hashing
  intentionally ignores nonsemantic fields such as `stream`.
- **Order and tool evidence:** an exchange matching beyond the earliest pending
  position emits `replay_order` warning in strict mode only. Tool misses store name,
  argument hash, remaining count, and attempted tool sequence. Repeated identical
  events still consume earliest recorded matches. Do not invent tool-order warnings;
  section 6.3 specifies order detection for exchanges.
- **Finish:** aggregate remaining exchange/tool sequences into one error-severity
  `replay_leftover` finding. The run update and finish findings share one transaction;
  duplicate finish is rejected before inserting findings. Errors and aborted runs
  are also checked for leftovers. Other detectors cannot be invoked before their
  M6 implementation; control-plane finish currently triggers replay checks only.
- **Run routing:** one manager per proxy, with lazy sessions keyed by run ID.
  Accept same-mode runs stored in process, including context-created runs and record/
  live runs on a recording proxy. Unknown/finished/incompatible runs fail with 400;
  strict payload validation uses FastAPI's 422. Create requires an existing trace
  and returns 201. The absent-header default remains the startup run.
- **In-flight lifecycle:** count HTTP requests until their JSON response is ready
  or their streaming response has completed/closed. Reject finish while busy.
  ClosingStreamingResponse explicitly closes its iterator and runs final cleanup,
  including if the client disconnects before headers or during data delivery.
  Close upstream before releasing the run. A disconnect before capture begins can
  leave a reserved sequence gap with no exchange; do not fabricate received chunks.
- **Context/tool sharing:** optional `manager=...` on RunRecorder retrieves the same
  session used by the proxy; standalone contexts own a session instead. Tools take
  an optional shared session and reject another store/trace/record-mode binding.
  Standalone replay recorders now create a persisted run/session so misses are
  findings too; callers explicitly finish that session when complete. Separate
  managers/processes do not coordinate consumption; share one session per run.
- **Golden checker contract:** `(httpx_client, tool_recorder)` callable, optionally
  async, imported through MODULE:CALLABLE. No separate user-supplied agent framework
  or OpenAI SDK. Create a fresh in-memory store per trace and use the in-process
  replay proxy with a shared tool session; no proxy network server is started.
  Temporarily include cwd in sys.path for installed-CLI imports, restoring it even
  on errors. Reject absent goldens, invalid modes, references, and signatures as
  configuration errors rather than reporting success with n=0.
- **Check exits and errors:** fail on any of the four divergence kinds, any error
  severity finding, or agent failure; strict order warnings therefore fail but
  stream_mismatch warnings alone do not. Expected HTTP/replay and standard agent
  errors become JSON report details. Avoid a broad Exception catch: unknown custom
  exceptions finalize the run in finally and propagate, failing the process without
  a full JSON report. Normal outcomes remain separate from regression verdicts.
- **Run comparison:** implement section 12's diff command alongside check. Compare
  findings plus source-trace requests by sequence, labeling the latter explicitly;
  replay stores findings rather than a new exchange log, so do not imply that the
  comparison reconstructs all successful incoming replay requests.
- **Verification:** 141 pytest cases pass with sockets blocked and Ruff passes.
  Tests cover all four divergence kinds, nearest fallback, truncated pointer diffs,
  concurrent runs on different and identical traces, busy/closed runs, stream
  disconnects, context binding, strict vs lenient checks, and custom-error cleanup.
  The installed CLI independently returns 0 for the minimal agent and 1 for a
  temporary changed-prompt agent with replay_miss/leftover evidence. No new
  dependencies or changes to existing expected acceptance behavior were needed.
- **Remaining gaps:** missing health/model endpoints, valid non-object upstream JSON
  handling, upstream auth configuration, and a real vLLM chunk fixture remain as
  previously documented. CI/Python-version coverage remains M10. No fault plans,
  M6 detectors, or live evaluation are claimed, and server shutdown still does not
  infer an agent's completion; use explicit finish or RunRecorder.

## M5 — seeded fault injection (2026-11-21)

- **Workflow:** M4 PR #1 was squash-merged as c177499. Start M5 from updated main
  on feature/m5-fault-injection; publish a separate reviewable PR, without starting
  M6 or merging M5 automatically.
- **Schema v5:** add exactly section 5's faults table and Fault model. Existing run
  plan/config columns are reused. Reject v4 databases explicitly, extend the version
  test, and regenerate both golden fixtures using the committed generator. Only the
  schema header changes; request/response/tool data and hashes are unchanged.
- **Dependencies:** add only the approved PyYAML and jsonschema libraries. Use
  safe_load, strict Pydantic validation, and Draft 2020-12 schema checks. No SDK,
  numerical library, ORM, or new external service is introduced.
- **Plan validation:** version 1 and integer seed, known class/target combinations,
  exactly one of seq/probability/every_nth, unique nonnegative explicit sequences,
  probability in [0,1], and positive every_nth. Reject unknown params and non-JSON
  native values/keys. Normalize by filling class defaults, then hash canonical JSON
  of the complete validated plan. Whitespace/mapping order are irrelevant; array
  order and explicitly different numeric types in arbitrary params still matter.
- **Canonical randomness:** use random.Random(seed), generating slots in ascending
  sequence and plan-rule order for all probability rules, regardless of event
  target. Cache slots when a higher sequence arrives first. Explicit/nth selectors
  consume no draws. nth counts positions starting at 1 over zero-based seq. This
  avoids arrival-order-dependent fault choices without changing global RNG state.
- **Precedence:** first applicable selected rule wins for each event. Missing tool
  calls or unconstrained schemas make tool-response mutations ineligible; expose
  skips instead of writing a ground-truth label for a value never injected.
  Standalone empty plans are valid clean controls. Reproducibility comparisons use
  fault_class/target/target_seq/params, excluding random identities and wall time.
- **Mutation ownership:** faults.py owns plan models, selectors, and pure mutation
  functions; fault_runtime.py is their run-bound caller and writes through store
  methods. Runtime verifies the persisted unfinished run's plan hash/config, and
  exposes observed values separately from ground-truth rows. A failure to persist a
  fault prevents delivery. Closed runtimes reject further injection/tool calls.
- **HTTP faults:** when the first selected applicable preflight rule is HTTP error
  or timeout, synthesize it without upstream access. Body/tool-call mutations need
  the upstream/recorded response before applicability is known; if an earlier
  selected mutation is ineligible, a later HTTP fault may apply after upstream.
  Errors use an OpenAI-style error object; timeout is 504 with a marker. Record/live
  timeout waits delay_ms (default 301000), which users should set above their
  client's timeout; replay ignores delay and returns immediately. Persist the label
  before delaying, and save the exchange in finally even if delay is cancelled.
- **Malformed and streamed data:** truncate canonical full response JSON halfway;
  retain raw malformed text in error with response=None. For streaming, emit a
  malformed data line without fabricating a valid completion. Selected content
  mutations buffer/reassemble the stream before applying and emitting SSE; normal
  unselected streams keep M1 progressive passthrough. Recorded source rows describe
  the delivered mutated response/chunks, and replay source rows remain immutable.
- **Tool arguments/names:** corrupt the first call's argument JSON, choose an
  undefined name with deterministic underscore collision avoidance, or find a valid
  JSON value rejected by its declared schema. Local schema references are allowed;
  external $ref/$dynamicRef are rejected before resolution to preserve offline
  replay. A schema accepting every candidate yields a skip rather than a false
  injection label.
- **Loop targets:** support both exchange and section 9's example tool_event target.
  An exchange loop repeats the first emitted call; a tool event schedules its
  name/args for the next eligible LLM response. Repeated calls get unique IDs but
  identical semantic arguments. Require repeat >=3 to represent the failure class
  under the stated detector threshold. Log against the original tool sequence only
  when the next response changes; unfulfilled schedules remain pending, not injected.
  Do not invent additional replay results to hide the consequent extra-call misses.
- **Tool execution/results:** hooks run after base execution/replay as section 8
  requires. No rollback of real tool side effects is implied. Safe built-in error
  names are explicitly allowed; unknown classes fail at load. Bad-result plans must
  declare a result schema and supply (or default to null for) a value violating it;
  skip already-invalid base JSON values (validate the captured projection, so a
  serializable Python tuple is an array). Persist injected record/live outcomes into the
  existing event; replay only captures observed copies. Natural base errors are
  observed without being mislabeled as injections. Legacy callable hooks retain
  their M2 behavior and storage semantics.
- **Sharing and configuration:** replay session owns its runtime; manager exposes
  the same runtime for proxy and tools. Record/live callers share the manager's
  runtime explicitly. Standalone ToolRecorder can take a FaultPlan and creates a
  run with the normalized plan/hash. Reject cross-store/trace/run runtime bindings.
  Control runs inherit the factory plan or override it using config.fault_plan;
  explicit null disables it, and invalid overrides fail before a run row is created.
- **Eval skeleton honesty:** eval faults walks stored event sequences, with exchanges
  before tools on ties, and never calls a real tool/upstream. It does not execute
  an adaptive agent or feed mutated results into subsequent requests. Report
  execution=fixed-recorded-trajectory and scored=false, with observations/errors,
  skips, pending loops, and labels. Store that execution mode in run.config and leave
  task_success unset. No detector precision/recall, task score, or robustness claim
  is made. Finish even on infrastructure failures; the caller owns the store.
- **Observations and future M6:** mutated replay observations are memory objects
  and JSON command output; ground-truth rows are durable. Do not reconstruct detector
  findings from labels. M6 must integrate observed RunViews independently, including
  durable observations needed by detect-after-restart, before reporting scores.
- **Verification:** 218 pytest cases pass offline, with sockets blocked and Ruff
  clean. Every fault class has a mutated-output test, including tool-targeted loops;
  tests cover record/replay tools, stream mutations, instant replay, logging before
  live delay/delivery, actual stored-trace fault-row reproducibility, source
  immutability, plan IDs, invalid loads, CLI flags/evaluation, and failure cleanup.
  Terminal import/eval faults/show plus clean check were run with a scoped
  PYTHONPATH=src and the example plan: one persisted 429 fault, SHA-256 plan ID,
  scored=false, clean goldens pass.
- **Environment note:** after refreshing the editable install, this environment's
  standalone interpreter still did not resolve agentreplay from src automatically.
  Explicit PYTHONPATH=src was used for generator/terminal smoke checks; pytest
  already declares its source paths. Do not claim fresh-virtualenv installation
  validation is fixed or complete; investigate it in M10. The existing TestClient
  warning and previously flagged health/model endpoints, non-object upstream JSON,
  upstream auth, real vLLM fixtures, cross-process consumption, and Python-version
  CI gaps remain outside M5.


## M6 — 2026-11-22

- Persist observed exchanges, executed tools, and replay attempt counts in an
  additive schema-v6 `run_observations` table. This makes cold detection possible
  without consulting injection labels or mutating the original trace. Finalization
  commits run status, snapshots, replay findings, and detector findings together.
- Separate the I/O adapter (`detection.py`) from pure `detectors.py`. Sanitize run
  metadata to four fields; pass only detector thresholds from config. Derive IDs
  from evidence and timestamps from the persisted finish time for repeatability.
- Persisted detection requires a finished run. Replay without a snapshot fails
  clearly; only record/live runs may fall back to their captured trace rows.
  Replace just the seven owned detector kinds, retaining replay/task findings.
- Prefer emitted calls for loops, annotate executed matches without double counting,
  and use executed calls as fallback. Return the earliest qualifying loop, using
  the triggering exchange and starting tool sequence for evidence. Count unmatched
  replay attempts toward budget; do not mistake a missing replay for an LLM response.
- Valid HTTP error envelopes are status errors, not malformed successes. Status
  504/599 and over-threshold latency take timeout priority. Empty text is valid;
  a present tool error counts even when empty, but its text does not imply latency.
- Tool timeout inherits the exchange threshold unless overridden. Thresholds are
  strict validated configuration; default equality is not a timeout or budget breach.
  Repeated cycles must fit the configured window. Task failure awaits independent
  verifiers; tool_bad_result cannot be inferred from a hidden injection schema.
- Validate arguments offline using the already approved jsonschema dependency,
  bounded to tested 4.26–4.x. Its private reference-resolution exception bridges
  unresolved references without importing a new direct dependency or deprecated
  public alias; local/unresolved/external reference behavior has regression tests.
- Intentionally update the M5 HTTP429 assertion: automatic finish now produces
  llm_error. Preserve its unscored report and source immutability assertions.
- Ignore only the four pre-existing untracked identical duplicate paths in tooling;
  leave those files untouched. Legitimate tracked tests remain collected.
- No GPU is required for M6; M7/M8 remain offline. M9 owns GPU rental and live runs.


## M7 — 2026-11-22

- Continue on `feature/m7-eval-tasks` from M6, with a PR based on
  `feature/m6-detectors` while PR #3 remains open. Starting M7 does not merge M6.
- Use per-task environment instances, strict schema dispatch, detached snapshots,
  copied tool definitions/results, and SHA256 state digests. Fixtures are seeded;
  stateless calc and immutable search intentionally need no random behavior.
- Files are entirely virtual POSIX paths, never host file operations. Search is
  a checked-in, generated 500-document synthetic maintenance corpus with literal
  all-token matching, stable ID ranking, and explicit lookup/limit rules.
- Freeze exactly 200 distinct instructions (40 per environment) with 16/16/8
  difficulty distribution. Workflows are 1/3/6 calls; length defines difficulty,
  not measured reasoning complexity. Resample duplicate instructions deterministically.
  Hard ticket tasks resolve two tickets; KV deletion removes an existing fixture
  key rather than an unnecessary temporary key. No reduction to 120 tasks.
- Generate expected outcomes with simple task construction, independently of the
  environment implementation. Test solutions execute only instructions and inspect
  observed tool results, never verifier params. Known arithmetic, conversions,
  corpus facts, and ticket transition assertions separately test environment rules.
- Use strict typed exact final-state checks for mutable environments, ordered JSON
  codes for search, and finite numeric JSON answers within absolute 1e-6 for calc.
  These are task verifiers, not detector outputs or fault-label-derived judgments.
  M8 will emit task_failure findings and persist tasks_version/config metadata.
- Expose Task/TaskVerifier models in schema.py; these add no persisted DB fields,
  so SCHEMA_VERSION stays 6. Include corpus/tasks/review as wheel package data.
- Reference agent v1 uses HTTPX, schema-advertised tools, bounded requests, ordered
  batched execution, injectable recorder/client/run ID, and explicit recoverable
  tool errors. It never sees task verifier params. Client/fixture/reset/finalization
  lifecycle stays with the caller so M8 can orchestrate it.
- Implement v2 as a controlled seeded skipped execution in the first multi-call
  batch. It reports a tool error and otherwise follows v1. Tests prove actual
  state failure and replay divergence, without claiming a live statistical effect.
- Replay tool functions do not execute by design, so fresh mutable environments
  remain untouched. M8 must reconstruct state from executed actions or otherwise
  verify observed outcomes; it cannot verify fresh replay state as final truth.
- Owner review remains pending: sample four random tasks per environment (20 total)
  with a fixed seed, and leave every reviewer/result field unfilled. Scripted
  checks are complete but do not replace the separate mandatory hand-solve review.
- No dependencies added. A wheel build without isolation failed because this
  virtualenv lacks setuptools; the normal isolated build using the existing
  declared backend succeeded. Build from a temporary clean source copy for final
  packaging verification to exclude pre-existing untracked duplicate source files.
- No GPU required. M8 stays offline; request rental before M9 live evaluation.

M7 final verification: 603 tests passed, including all 200 scripted solutions;
Ruff and git diff checks passed. The clean wheel loaded 200 tasks, 500 documents,
and its pending review template. Existing TestClient deprecation warning remains.


## M8 — 2026-11-22

- Stack `feature/m8-eval-harness` on the still-open M7 branch. Do not merge M6/M7
  merely because the owner said Next; create a separate reviewable M8 PR.
- Add schema-v7 evaluation_results for durable final answers/state plus the
  specified eval_labels table absent from the earlier store. Run completion,
  observed snapshots, task_failure/detector/replay findings, and results commit
  together. Reject older databases; regenerate golden version headers only.
- Normalize startup run metadata from known run_config fields so the harness's
  task/trial/agent/model identities exist in both the run row and config. Preserve
  the existing control-plane and standalone APIs with optional result parameters.
- Move reference-agent implementation into evals.agent and preserve the example's
  exported interface. Installed package CLI must not import an unpackaged example.
- Keep harness orchestration separate from pure stats and report rendering. Use a
  clearly labeled scripted backend by default and injectable transports; live mode
  requires explicit model/vLLM version and is only exercised with a fake transport.
- Record every configured task/trial/version, replay each twice unchanged, replay
  v1 traces against v2, and optionally fault only successful finding-free v1 sources.
  M8's committed configuration uses 20 tasks ×3×2 (120 sources, 480 total runs).
  Full 200×3 live runs remain M9. Sampling remains .7/trial as specified.
- Reconstruct virtual replay state independently from consumed original successful
  tool actions, preserving consumption order. Do not execute the recorder callback.
  The five environments validate before mutation, so original failures are no-ops;
  injected post-execution errors retain real side effects. Missing prerequisites
  become reconstruction errors and a failed verification. Do not generalize this
  simulation to external tools. Persist actual/reconstructed provenance.
- Emit task_failure based on verifier outcome, not agent exception or fault labels.
  A state task can succeed even when a later error makes outcome=agent_error.
  Infrastructure/cancellation failures finalize but leave success/result unknown,
  making reports reject the incomplete cohort instead of claiming a measured result.
- Filter clean FP sources by success and absence of findings, so intentional v2
  task failures/natural errors are not mislabeled clean. Replay all sources twice
  for determinism. Include normalized finding sets and verified outputs as well as
  outcome; exclude identity/timestamp fields. Report all count units and correlation.
- Score injected detector claims one-to-one by kind and the correct target namespace
  and sequence, deduplicating identical claims. Manual presence labels are a
  separate run/kind measurement; conflicts need adjudication. Do not pool them.
- task_failure is run-level, so bad-result labels without localized verification
  are disclosed as unscored for sequence-based detector P/R; preserve task-success
  scoring and report unscored n. Assigning the injection sequence to a verifier
  finding would leak ground truth and violate independence.
- Use Wilson intervals for proportions; F1 is non-binomial, so derive conservative
  harmonic bounds from Bonferroni-adjusted Wilson intervals rather than calling
  2TP an independent success count. Undefined rates retain null/n=0. Bootstrap
  paired task/trial observations with fixed seed (10,000 default); exact McNemar
  reports discordant counts. These repeated observations have limited independence.
- Cohort reports validate expected source/replay membership and shared metadata.
  All metric calculations live in stats.py; renderer only formats values and
  refuses missing n. Select the latest cohort by default or an explicit evaluation ID.
- Label CLI displays observations without detector/verifier outputs, collects all
  judgments before saving, and rejects injected runs. Owner hand review and 100
  natural-run labels remain pending; never synthesize either from scripted results.
- Preserve four pre-existing untracked duplicate files untouched and outside commits.
  Existing TestClient deprecation remains; no new dependencies or real GPU calls.
- First complete-run test incorrectly assumed broken v2 never produces a natural
  tool error. Replace that new assertion with an exact source-vs-observed snapshot
  comparison, retaining the no-error check for v1. All pre-existing tests retained.
- Add Makefile offline/report commands and generate committed reports from a fresh
  DB after committing implementation, so the recorded git SHA identifies the code.
  A later report-artifact commit does not rewrite those generated numbers. Retry-safe
  live resume, environment capture scripts, and rental guidance are M9 work.

M8 verification: 658 tests passed, including 55 additional cases (one old-schema
compatibility case). Ruff and diff checks passed; the clean wheel ran the packaged
CLI through evaluation and mutable-state replay/reporting without an examples import.


## M9 preparation — 2026-11-22 (live acceptance pending)

- Work on feature/m9-live-runner, stacked on the open M8 branch; do not merge
  earlier PRs implicitly. Only config/scripts and required resume/preservation
  integration are in scope. M9 cannot be declared complete without real results
  and an off-machine verified copy; no GPU/SSH endpoint is available yet.
- Pin the public Qwen3-8B model/tokenizer SHA and vLLM 0.34.0 after checking the
  official model metadata, Qwen serving guide, and vLLM release. Use Hermes as
  specified, bounded output/context, and disable thinking via recorded request
  kwargs. No vLLM/PyTorch dependency is added to the library or installed locally.
- Support an explicit cohort ID for resume. Logical slots include phase, task,
  version, trial, source, repeat, and fault-plan identity; a new attempt supersedes
  an interrupted/unmeasured one without deleting history. Verified failures are
  completed measurements and never retried to improve the success rate.
- Resume prechecks code SHA, frozen task hash, normalized settings, model revision,
  sampling, and serving version/flags. Runtime inventories may change on a new
  host but are recorded and disclosed in reports. Prior unmeasured rows are marked
  aborted; effective results select the highest unique attempt per logical slot.
- Use a nonblocking canonical-DB file lock in the owner runner. Persist session ID
  before evaluation starts and recognize moved DBs only when the same cohort exists.
  The normal async API expects callers to avoid concurrent resume of one cohort.
- Add replay-only resume with a full source-completeness precheck. It can finish
  offline after copying the DB, without paying for a server or turning unrecorded
  source tasks into network-error measurements.
- Expand from one fault_plan to optional independent fault_plans (mutually exclusive
  settings). Each receives separate CPU-only replay slots and report membership
  checks; the committed live config covers 11 classes plus both tool-loop targets.
  This avoids repeating paid source recordings just to evaluate another fault.
- Capture server argv, process identity, package versions, Python/OS, GPU inventory,
  driver, and PyTorch CUDA build. The same Python starts vLLM; a process receipt and
  models check precede a streamed-tool smoke recording. Real CUDA/stream shapes
  remain unverified until owner hardware is available.
- Use SQLite backup() under the store lock instead of copying an active WAL DB.
  Export goldens and reports from the snapshot, preserve smoke fixtures and frozen
  inputs, and produce checksums plus an archive. Never overwrite outputs; explicitly
  identify incomplete snapshots. Only the owner/SSH transfer can establish an
  off-machine copy, so backup locally is not a completion claim.
- Sample 100 natural live source runs with 20 per environment and a local fixed RNG,
  without consulting findings or task success. Human labeling/review stay pending.
- New fields live in existing JSON config and existing result/label tables, so DB
  schema remains v7. Preserve pre-existing tests and duplicate files; TestClient's
  known warning remains. No paid provisioning or external messages are authorized
  or executed by this preparation work.

Preparation checks: 673 tests passed, Ruff passed, shell syntax and non-GPU plan
passed, and both committed goldens passed. No GPU/model packages were installed
locally. Real live results and an off-machine archive are still pending hardware.

Flag an existing behavior rather than changing agent semantics outside M9 scope:
natural EnvironmentError messages acquire a ToolReplayedError wrapper during
replay. A v2 tickets-017 experiment reproduced completed source / agent_error
repeats with strict misses. Repeat-to-repeat determinism is not source equivalence;
clean FP eligibility excludes originally failing sources. A separate normalization
fix is needed before claiming faithful natural-error replay.

### First owner-provided M9 host

Connected to the supplied Vast instance and read its operating guide. It has an
A100-SXM4 80 GB, Python 3.12, vLLM 0.32.0, and a nonpersistent workspace. Pause the
template's unrelated GPT-OSS service and use the installed vLLM environment rather
than replacing its CUDA/PyTorch stack. The version/port override is explicit in
examples/eval-vast-a100.yaml before starting a cohort; the generic 0.34.0 plan stays
available. Model and evaluation processes use supervisor as the host guide requires.
