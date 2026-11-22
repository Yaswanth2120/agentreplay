# Function notes

## M1 production functions

### `canonical.canonical_json`
Serializes a value with sorted keys, compact separators, and unescaped Unicode.
Using one serializer keeps persisted JSON and request hashes consistent without
changing the distinction between integers and floats.

### `canonical.canonical_hash`
Selects only the specified semantic request fields and hashes their canonical JSON
with SHA-256. Stream flags and unrelated metadata cannot change a replay match.

### `Exchange.from_request` (existing, now using the extracted helper)
Builds an exchange and computes its strict and lenient hashes. Keeping this small
constructor preserves the baseline API while hashing rules live in their own module.

### `Exchange.tool_calls` (existing, verified for streaming)
Extracts tool names, IDs, and raw argument strings from completion messages.
Stored stream responses have the same message shape, so this method needs no
special branch for streaming.

### `TraceStore.__init__`
Opens SQLite, validates the database version, enables foreign keys and WAL, and
creates the current tables. It checks incompatible databases before changing them
and closes the connection when initialization fails.

### `TraceStore.close`
Closes the SQLite connection. The caller owns the store lifetime so that several
proxy apps can share a store without closing it unexpectedly.

### `TraceStore.create_trace`
Persists a trace and canonicalizes its metadata. A lock and transaction prevent
concurrent writes from sharing an unfinished transaction on this connection.

### `TraceStore.get_trace`
Loads one trace or returns `None` when it does not exist. It checks the row version
as well as the database version so incompatible trace records fail clearly.

### `TraceStore.list_traces`
Loads trace IDs in creation order and resolves them through `get_trace`. Reusing
the loader applies the same validation to both single and multiple trace reads.

### `TraceStore.next_seq`
Reserves the next sequence for a trace under a lock, considering both stored and
already-reserved positions. Reserving before upstream I/O preserves request order
even when streams complete in reverse order.

### `TraceStore.add_exchange`
Writes a completed or partially failed exchange, its raw chunks, and reassembled
response in one transaction. Canonical JSON is used for every JSON-valued column.

### `TraceStore.exchanges`
Returns all exchanges for a trace in sequence order. It copies the query results
under the lock and then uses the row converter to restore validated models.

### `TraceStore._row`
Decodes the request, response, and chunks JSON columns into an `Exchange`.
Keeping this conversion in one place prevents read paths from disagreeing about
nullable fields or stream representation.

### `TraceStore.create_run`
Creates the minimal startup run needed to associate warnings with a replay.
Explicit column names leave the remaining run lifecycle columns available for
later milestones without inventing outcomes during M1.

### `TraceStore.add_finding`
Persists a finding and canonicalizes its evidence in a transaction. A foreign key
requires the startup run to exist before any streaming warning is recorded.

### `TraceStore.findings`
Returns a run's findings as validated models, ordered by creation time and insertion
order for ties. Decoding evidence here keeps SQL and JSON storage details out of
the proxy and tests that consume findings.

### `streaming.parse_data_line`
Parses a JSON object from an SSE `data:` line, ignoring comments and the final
`[DONE]` marker. Malformed JSON and non-object payloads raise explicit errors
instead of creating misleading completion content.

### `streaming.reassemble`
Combines content and indexed tool fragments into non-streaming completion messages.
Separate dictionaries per choice and tool index handle interleaved calls while
preserving argument fragment order and first identifiers.

### `streaming.synthesize_chunks`
Converts a stored completion into one JSON delta chunk and a completion marker.
Adding indices to tool calls makes that chunk consumable by streaming clients and
by the same reassembly helper.

### `streaming.emit_chunks`
Yields stored data lines as SSE events and appends `[DONE]` when the recording
lacks it. There are no sleeps because replay preserves data order rather than
simulating transport timing.

### `proxy.create_app`
Builds a record or replay application with a startup run and injected transport,
timeout, and clocks. Replay creates no HTTP client, making its offline behavior
independent of upstream availability.

### `create_app.lifespan`
Keeps the upstream client available while the app runs and closes it at shutdown.
The store stays open because its lifetime belongs to the caller.

### `create_app.save`
Constructs and stores an exchange using the reserved sequence and measured latency.
Both JSON and streaming capture use this path so timestamps and hashes stay
consistent, and tests can inject clocks instead of depending on elapsed wall time.

### `create_app.replay_response`
Matches and consumes an exchange, returning JSON or an immediate SSE response.
When the requested stream flag differs, it persists a warning before returning
the converted representation; recorded HTTP error bodies remain intact.

### `create_app.capture_stream`
Forwards upstream SSE lines immediately while collecting raw data lines.
Its finalization saves partial data and transport or parsing errors and closes
the upstream response, even when the stream cannot complete normally.

### `create_app.chat`
Validates the request body and routes it to replay or upstream capture.
It reserves the sequence before awaiting upstream work and records connection
failures as 599 while returning 502 when response headers have not yet been sent.

## M1 test functions and helpers

### `conftest.disable_network` and its `fail_connect` helper
The autouse fixture replaces socket connection entry points for every test.
The helper fails immediately if a test accidentally attempts real networking,
while mock HTTPX transports continue to work in process.

### `test_streaming.data`
Builds one representative OpenAI-compatible SSE data line with configurable delta,
choice index, and finish reason. Tests use explicit fragment payloads to make
content and tool-call expectations easy to inspect.

### `BytesStream.__init__`, `BytesStream.__aiter__`, and `BytesStream.aclose`
The constructor stores transport fragments and an optional simulated failure.
The iterator yields those fragments before raising the failure, and the close
method records cleanup so tests can verify resources are released.

### `transport_for` and its `handler`
The factory returns a mock HTTP transport around a supplied async byte stream.
Its handler records requests and supplies SSE headers, allowing upstream call
counts and fragmented streaming to be tested without a server.

### `test_stream_record_replay_identical_offline` and `no_network`
The test records byte-fragmented UTF-8 SSE, checks stored data, and compares the
complete replay body with the recorded body. The nested network trap ensures
that replay cannot silently use HTTPX's default network transport.

### `test_interleaved_tool_fragments_multiple_choices`
Asserts that multiple choices and interleaved tool indices produce the expected
messages, first IDs/names, concatenated arguments, and finish reasons.
A synthesis/reassembly round trip also verifies the same message shape is retained.

### `test_midstream_error_saves_partial_and_injected_clock`
Raises a transport error after delivering one content chunk and verifies partial
storage, error status, cleanup, and exact latency from injected clocks.
It checks the downstream status stays 200 because headers have already been sent.

### `test_stream_mismatch_both_directions_persisted`
Checks conversion from JSON to SSE and from SSE to JSON against the same answer
and tool call. It verifies each conversion creates exactly one warning with the
recorded sequence and both stream flags.

### `test_reject_old_and_future_database_without_modifying`
Constructs legacy and mismatched-version databases and checks the clear version
error on opening them. A byte comparison verifies rejection does not modify them.

### `test_database_reopen_and_pragmas`
Writes a trace, run, and finding, then reopens the database and compares models.
It also checks schema metadata, WAL, foreign keys, listing, and missing-trace reads.

### `test_sequences_reserved_under_lock`
Reserves 100 sequence numbers from concurrent threads and checks uniqueness and
contiguity. A second trace verifies counters are isolated by trace ID.

### `test_canonical_json_and_stream_excluded`
Checks canonical key ordering, Unicode, and the distinction between integer and
floating-point JSON. It asserts both hash modes ignore absent, false, and true
stream flags.

### `test_invalid_request_400` and its `handler` lambda
Checks malformed JSON, arrays, and null request bodies return HTTP 400.
The mock upstream fails if called, proving rejection happens before upstream I/O
or exchange recording.

### `test_parser_and_done_emission` and `collect`
Checks comments, compact data prefixes, the final marker, and invalid payloads.
The nested async collector verifies replay appends a missing completion marker.

### `test_stream_is_forwarded_before_upstream_finishes`, `GatedStream.__aiter__`,
### `exercise`, `receive`, and `send`
The gated upstream asserts that its first data line has reached the ASGI send
callback before it produces the next chunk. The async exercise, receive, and send
helpers drive the app directly so response buffering in HTTP test clients cannot
hide a failure to stream progressively.

### `test_concurrent_streams_keep_request_sequence`, its `exercise`, `handler`,
### `OrderedStream.__init__`, and `OrderedStream.__aiter__`
The test starts two requests and uses an event to let the second upstream finish
before the first. The stream constructor identifies the request, its iterator
coordinates completion, and the handler/exercise helpers run everything in process
before asserting the stored sequences still reflect request arrival order.

### `test_stream_request_upstream_failures` and its `handler`
Exercises connect errors, timeouts, and an HTTP 429 JSON response for stream requests.
The handler injects each case; the test also replays the 429 response to ensure
stream handling never replaces its error body with a synthetic success chunk.

### `test_malformed_stream_is_preserved_with_error`
Sends a broken JSON data line and verifies passthrough and raw-chunk preservation.
It checks reassembly reports a parse error and closes the upstream response.

### `test_replay_missing_done_adds_marker`
Stores a stream without a final marker and checks the exact replay text.
This documents the specified completion behavior for interrupted recordings.

### `test_foreign_keys_and_canonical_stored_bodies`
Verifies orphan exchanges fail and persisted request, response, and chunk columns
use canonical JSON. The test reads raw SQLite columns because serialization and
foreign-key enforcement are storage contracts rather than replay behavior.

## M2 production functions

### `canonical.tool_args_hash`
Hashes canonical JSON for an argument dictionary or JSON string containing one.
Malformed or non-object strings use a `raw:` prefix so invalid arguments remain
identifiable without being confused with parsed objects.

### `TraceStore.next_tool_seq`
Reserves a tool-event sequence under the store lock before execution starts.
Its separate counter keeps tool order independent of LLM exchange order while
preventing repeated reservations from colliding in a shared store.

### `TraceStore.add_tool_event`
Writes an event's canonical arguments, result or error, and timing in one transaction.
It distinguishes successful JSON null from a failed call by retaining a separate
error column, with foreign keys and unique sequence constraints enforced by SQLite.

### `TraceStore.tool_events`
Loads a trace's tool events in sequence order and decodes their JSON columns.
Returning validated models keeps SQL and serialization details out of the recorder
and replay matcher.

### `ToolReplayMiss.__init__`
Builds a specific miss exception containing the tool name, argument hash, and
remaining event count. Those attributes let callers inspect a divergence without
parsing its human-readable message.

### `ToolReplayer.__init__`
Copies events into a private pending list sorted by recorded sequence.
A separate matcher per recorder prevents one replay run from consuming another
run's events, including when both replay the same trace.

### `ToolReplayer.next`
Finds and removes the earliest pending event with matching name and argument hash.
A miss leaves the list unchanged and raises `ToolReplayMiss`, so an incorrect call
cannot consume a later valid match.

### `ToolReplayedError.__init__`
Retains a recorded error string and uses it as the exception message.
Replay intentionally uses one specific wrapper instead of reconstructing arbitrary
Python exception types from untrusted recorded strings.

### `tools._json_snapshot`
Serializes and decodes a value to obtain an independent JSON snapshot.
It rejects unsupported objects, cycles, and non-finite numbers with a tool-named
`TypeError` so recording never substitutes a misleading string result.

### `ToolRecorder.__init__`
Validates the recording mode and trace, stores injected clocks and a result hook,
and creates a matcher only in replay mode. Replay snapshots the already-recorded
events at initialization and never shares its consumption state with other recorders.

### `ToolRecorder.call`
Records execution, JSON result or original error, and latency, or consumes a stored
result without invoking the supplied callable. A finally block saves exceptions
while preserving their original propagation; successful values can then pass through
the optional post-result hook in either mode.

### `ToolRecorder.tool`
Returns a decorator that records a function under an explicit tool name.
The name is separate from the Python function name so renamed implementations can
still replay compatible recorded calls.

### `ToolRecorder.tool.decorate`
Inspects a function and rejects signatures that cannot be represented by the
recorder's named keyword arguments. Returning a wrapped callable preserves the
original function metadata for readable examples and debugging.

### `ToolRecorder.tool.decorate.wrapped`
Binds positional and keyword inputs to parameter names and fills in defaults.
Passing that normalized dictionary into `call` makes equivalent decorator invocations
match the same argument hash during replay.

### `minimal_agent.add`
Adds two integers and returns their sum. A deterministic one-line tool makes it
easy to demonstrate that replay can supply the same result without executing it.

### `minimal_agent.run_agent`
Sends HTTPX chat requests, executes only the declared add tool through the recorder,
and returns the final answer. Injected HTTP client/tool callable and a three-request
limit make the example testable offline and bounded when a model repeats calls.

## M2 test functions and helpers

### `test_record_replay_never_calls_function_and_normalizes_arg_order` and `add`
The counted helper returns a nested JSON result so the test can check both execution
count and data shape. The test verifies canonical argument matching, exact injected
timing, result equality, and that replay does not append another stored event.

### `test_repeated_calls_are_consumed_once_in_recorded_order`
Records distinct results for three identical calls and checks their replay order.
It checks a fourth call misses without executing the supplied function.

### `test_exception_is_stored_and_original_reraised_then_replayed` and `fail`
The helper raises a supplied exception instance; the test checks record returns that
same instance through normal propagation and stores its type/message. Replay must
raise the wrapper with that message and consume the error event exactly once.

### `test_non_json_result_raises_named_type_error`
Exercises sets, arbitrary objects, NaN, and infinity as tool results.
It requires a named TypeError and a stored error event rather than a stringified result.

### `test_null_result_roundtrips_as_success`
Records and replays a tool returning None. Checking the absence of an error ensures
JSON null is treated as a successful value rather than an execution failure.

### `test_misses_match_both_name_and_arguments_and_do_not_consume`
Calls an incorrect tool name and incorrect arguments before making the valid call.
It checks miss evidence and confirms failed matches leave the recorded event available.

### `test_decorator_handles_positional_keyword_and_default_arguments`, `add`, and `replaced`
The decorated recording helper adds numbers while the replacement fails if executed.
The test checks positional/keyword equivalence, applied defaults, preserved metadata,
and argument binding failures before any replayed result is consumed.

### `test_replay_instances_and_traces_are_isolated`
Creates independent replay recorders for the same and different traces.
Matching results show that consuming an event in one recorder does not affect another.

### `test_args_and_results_are_snapshots` and `mutate`
The helper mutates its nested input and returns a mutable output that is changed later.
The test proves both stored arguments and stored results describe the original call,
while the tool still receives the actual caller's argument objects.

### `test_success_inside_exception_handler_is_not_marked_error`
Runs a successful tool inside an unrelated exception handler.
This guards against accidentally reading the caller's active exception as a new tool failure.

### `test_tool_storage_reopens_and_uses_canonical_json`
Inspects raw JSON columns, foreign keys, unique sequences, and persistence after reopening.
It checks tool sequence allocation resumes from stored data without advancing LLM sequences.

### `test_tool_sequence_reservations_are_thread_safe_and_independent`
Reserves one hundred tool sequences concurrently and checks they are unique and contiguous.
It separately checks counters for other traces and LLM exchanges do not advance.

### `test_tool_hash_dicts_raw_strings_and_numeric_types`
Checks parsed-object hashing against a directly calculated SHA-256 digest and verifies
raw-string prefix behavior. It also protects Unicode, nested key ordering, and the
difference between integer and floating-point argument values.

### `test_tool_replayer_uses_sequence_order_for_unsorted_input`
Constructs events out of sequence and checks the matcher consumes them in recorded order.
The test isolates the matching rule from the database's normal ordering.

### `test_invalid_configuration_and_arguments_fail_before_execution`
Checks bad modes, unknown traces, non-dictionary arguments, non-JSON values, and a
variadic decorator fail explicitly. It also proves rejected inputs create no tool events.

### `test_result_hook_runs_after_base_result_is_available` and `hook`
The hook checks the base event is already persisted and changes the returned value.
The parametrized test proves both modes invoke it while leaving source results intact.

### `test_example_records_and_replays_offline_after_reopen`, `upstream`, `counted_add`,
### `forbidden_upstream`, and `exercise`
The scripted upstream checks the agent sends the correct tool-result message, and
`counted_add` tracks real tool execution. The async exercise closes and reopens the
database before replay, while the forbidden transport fails on any upstream request;
equality, counters, and full exchange consumption verify the entire agent path.

### `test_example_surfaces_errors_and_limits_steps`, `scripted`, and `exercise`
The scripted transport returns a direct answer, HTTP error, unknown tool, or repeated
add calls. The async exercise verifies each final result or explicit error and checks
the expected tool-event count, including the three-request safety limit.

## M3 production functions

### `RunRecorder.__init__`
Keeps the store, a private copy of the requested run, and an injected clock.
Copying avoids mutating the caller's initial configuration before entry and makes
one context responsible for one execution.

### `RunRecorder.__enter__`
Creates the run with its actual entry time and returns the active model.
It rejects reuse and already-finalized input so a context cannot silently replace
an existing execution's history.

### `RunRecorder.__exit__`
Maps normal completion, agent exceptions, and interruptions to the defined run
outcomes, then saves finish time, success, and detail. It updates the active model
and returns no suppression flag, preserving exceptions and separating task success
from execution outcome.

### `TraceStore._insert_trace`
Inserts all trace columns, including task/agent/model metadata and canonical JSON.
It intentionally opens no transaction so both normal creation and atomic import
can reuse it under the caller's transaction.

### `TraceStore._insert_exchange`
Inserts one exchange using the same canonical request/response/chunk serialization
as recording. Separating insertion from transaction ownership lets an import roll
back earlier exchanges if any later row conflicts.

### `TraceStore._insert_tool_event`
Inserts a tool event with canonical arguments and either a JSON result or stored
error. It leaves transaction ownership to its caller so bulk imports and regular
tool recording share an identical representation.

### `TraceStore.create_run` (expanded)
Serializes every field of the completed Run model, with canonical JSON for config.
Column names come from the model's fields while all values remain SQL parameters,
so stored metadata and later inspection agree.

### `TraceStore.get_run`
Looks up one run ID and returns a validated model or None. Keeping this read in the
store lets the context manager and inspection code avoid their own SQL.

### `TraceStore._run_row`
Decodes the run's JSON configuration and validates the row as a Run.
It is shared by single reads, lists, and finalization so booleans and optional
fields are restored consistently.

### `TraceStore.runs`
Returns the runs belonging to a trace in start-time/ID order. A deterministic tie
break makes inspection stable when fixture clocks produce equal timestamps.

### `TraceStore.finish_run`
Updates outcome, finish time, task success, and detail only for an unfinished run.
It returns the persisted model and raises on an unknown or finished ID, preventing
a second finish from overwriting history.

### `TraceStore.import_trace`
Inserts a trace and every exchange/tool event under one lock and transaction.
Private insert helpers do not commit independently, so any constraint failure
rolls back the entire incoming trace.

### `exportio._reject_constant`
Rejects JSON's nonstandard NaN and infinity constants during parsing.
Doing this before model construction also catches non-finite numbers inside
arbitrarily nested request, metadata, and result values.

### `exportio._parse_line`
Selects a strict wire model by record type and requires every serialized field.
It wraps JSON/type/model errors with the line number so malformed imports are
understandable and cannot fill missing identities or timestamps from defaults.

### `exportio.load_trace`
Reads and validates the header, versions, row types, sequence/ID uniqueness,
trace references, and hashes before returning ordinary models. It performs no
writes, allowing the whole input to fail safely before import begins.

### `exportio.export_trace`
Writes a trace header and canonical rows ordered by sequence, with exchanges
first on ties and a final newline. It preserves stored IDs and values, excludes
run history, and rejects an unknown trace before touching the output path.

### `exportio.import_trace`
Loads all validated models and passes them to the store's transactional import.
It turns SQLite constraint conflicts into a clear trace-format error rather than
silently replacing existing rows.

### `inspection.render_list`
Builds a readable trace listing with exchange, tool-event, and run counts.
It returns text instead of printing so both the CLI and tests use the same output.

### `inspection.render_trace`
Renders trace metadata, requests/responses/chunks, tools, runs, and findings as
labeled sections with indented JSON. Reading through store methods keeps SQL out
of presentation code, and unknown IDs fail explicitly.

### `cli.main` (expanded)
Parses the inspection/import/export commands and routes them to the appropriate
operation alongside existing proxy commands. It prints only at this CLI boundary,
reports expected input/I/O/database failures with exit 2, and always closes its store.

### `make_golden.mock_completion`
Returns scripted tool-call responses until the specified number of add calls is
complete, then returns the final answer. Counting existing tool messages makes the
mock deterministic while the real agent still constructs every outgoing request.

### `make_golden.record_golden`
Runs the actual agent through the recording proxy and tool recorder using fixed
fixture clocks and a mock upstream. It normalizes only random row IDs into a fresh
store before export, preserving the captured request, response, and tool data.

### `make_golden.generate_golden`
Produces the one-call and repeated-call fixture files in the selected directory.
It invokes the same capture path for both cases, making regeneration reproducible
without a live model or hand-edited JSON.

### `make_golden.main`
Parses the optional output directory and calls the generator. A module entry point
makes fixture provenance reproducible with one documented repository-root command.

## M3 test functions and helpers

### `test_run_records_all_config_and_finishes_success`
Checks complete run metadata, canonical config, context entry/exit timestamps, and
persistence of the finished model. It also verifies the initial model stays unchanged
and missing run/trace lookups return the documented empty results.

### `test_run_records_failures_without_suppressing_them`
Raises an agent error or interruption inside the context and checks its persisted
classification and detail. The original exception instance must still propagate,
and the active model must reflect the same finalized state as SQLite.

### `test_verifier_failure_is_still_completed_and_recorder_cannot_reenter`
Confirms a false task result does not imply an execution error. It checks context
reuse, repeat finalization, and unknown run IDs are rejected explicitly.

### `test_context_rejects_finished_run`
Attempts to enter a context with a completed input model. It checks rejection
occurs before a run row is created or the body executes.

### `populated_trace`
Creates a trace with metadata, JSON and stream exchanges, and successful-null/error
tool events. Tests use this mix to exercise preservation of optional and nested
values rather than only a trivial successful text exchange.

### `canonical_rows`
Serializes raw trace/exchange/tool SQLite rows into canonical bytes. Comparing
these bytes verifies storage identity directly, beyond equality of loaded models.

### `test_export_import_preserves_canonical_rows_and_bytes`
Exports a mixed trace, imports it into a fresh database, and compares canonical
rows and a second export byte-for-byte. It also verifies event ordering, metadata,
null/error distinction, and the deliberate absence of imported runs.

### `test_import_rejects_invalid_lines_without_partial_writes`
Mutates exported input across version, type, shape, identity, ordering, numeric,
and hash error cases. Every case must report its offending line and leave all
trace/event tables empty.

### `test_invalid_headers_are_clear_and_leave_empty_database`
Checks empty, non-UTF-8, and unknown-type inputs fail clearly. The target database
must remain empty because input validation precedes all inserts.

### `test_import_conflicts_roll_back_entire_trace`
Seeds a conflicting exchange so import fails after a trace and earlier exchange
have been inserted. Comparing raw canonical rows proves the whole transaction
rolls back; re-importing an existing trace must also fail without replacement.

### `test_export_unknown_trace_does_not_truncate_output`
Targets an existing output file while requesting an unknown trace. It verifies
validation happens before the output file is changed.

### `test_exchange_cannot_be_first_record`
Removes the trace header from an otherwise valid exported file. The loader must
report the missing first trace record rather than accepting orphan exchanges.

### `test_inspection_shows_exchanges_tools_runs_and_findings`
Creates a finished run with a warning and inspects a populated trace. It checks
labels, config, errors, and finding evidence appear, and unknown trace inspection fails.

### `test_empty_list`
Checks listing an empty store returns a readable message. This prevents a blank
CLI response from being mistaken for a failed command.

### `test_cli_import_list_show_export`
Runs the four inspection/transfer command paths against two database files.
It checks readable command output and byte equality of the final exported trace.

### `test_cli_errors_have_exit_code_two_and_clear_message`
Exercises missing IDs/files, malformed input, incompatible databases, and missing
required flags. Each must surface an argparse error with exit 2 and useful context.

### `test_proxy_cli_wiring_closes_store` and `fake_serve`
The fake server checks host/port arguments without opening a socket. The test
exercises both existing proxy command paths and verifies the store connection is
closed when the CLI returns.

### `test_golden_generation_is_reproducible_and_matches_checked_files`
Generates both fixtures twice and compares each generation with the checked-in bytes.
It also checks they carry the minimal agent provenance and the expected exchange/tool counts.

### `test_golden_runs_the_unmodified_agent_offline` and `exercise`
The async helper imports a golden trace and runs the real agent through a replay
proxy with a tool function that fails if invoked. The test checks the answer,
complete LLM/tool consumption, and zero findings under the global socket guard.

### `test_generation_cli_uses_requested_directory`
Calls the generator's CLI entry point with a temporary destination. Comparing
both generated artifacts to the checked-in files verifies argument wiring without
changing repository fixtures during the test.

## M4 production functions

### `replay.json_diff`
Compares JSON recursively and returns added, removed, and changed paths using escaped
JSON Pointer notation. Values become canonical previews capped at 200 characters,
and type checks distinguish integer and floating-point values.

### `Replayer.remaining_seqs`
Returns pending exchange sequence numbers in recorded order. Finalization uses this
snapshot to describe leftovers without exposing or mutating the pending list.

### `Replayer.miss_evidence`
Selects the exchange at the attempted position or the closest recorded message count,
then compares its request to the incoming request. It considers consumed exchanges too,
because their requests can still explain an extra or changed call.

### `Replayer.next` (expanded)
Consumes the earliest hash match and records strict out-of-order match metadata.
A miss leaves pending events intact and raises ReplayMiss with structured evidence;
lenient matches never record order warnings.

### `ReplayMiss.__init__` (expanded)
Keeps the existing hash/mode/remaining exception interface and adds structured evidence.
This lets the HTTP boundary retain its 409 response while the session persists details.

### `ToolReplayer.remaining_seqs`
Returns unused tool sequence numbers without changing consumption. Replay finish can
therefore identify missing tool executions alongside missing LLM exchanges.

### `ReplaySession.__init__`
Validates an existing unfinished replay run and loads independent LLM/tool matchers
for its trace. A shared reentrant lock lets one run serialize matching and finish
without sharing consumption with another run.

### `ReplaySession._ensure_open`
Checks the persisted run is present and unfinished before replay operations.
Reading persisted state also detects a run finalized through another lifecycle path.

### `ReplaySession.finding`
Builds and persists a finding for the session's run with an injected timestamp.
Centralizing this step keeps order, miss, and stream-warning evidence consistent.

### `ReplaySession.next_exchange`
Matches under the session lock and stores miss evidence before re-raising ReplayMiss.
Successful out-of-order strict matches produce a warning while still serving the
recorded response, allowing the checker to decide the regression verdict separately.

### `ReplaySession.next_tool`
Consumes a matching tool event under the same run lock and tracks attempted positions.
A miss persists the name, argument hash, remaining count, and tool sequence before
raising the existing specific ToolReplayMiss exception.

### `ReplaySession.finish`
Collects all unused exchange/tool sequences into one leftover finding and finalizes
the run through the store. Both the finding and run outcome commit together, and a
closed run cannot be finished or consumed again.

### `TraceStore._insert_finding`
Inserts a finding with canonical evidence without starting its own transaction.
Normal finding writes and atomic finish-with-leftovers can share this serialization.

### `TraceStore.finish_run` (expanded)
Finalizes the row and optionally inserts same-run findings inside one transaction.
It rejects findings for another run, so an invalid finish batch rolls back the run
update rather than leaving a misleading completed execution.

### `RunManager.__init__`
Keeps the proxy mode, shared store/clock, per-run sessions, active-request counts,
and a registry lock. Keeping this orchestration outside the store avoids putting
replay business logic into SQL storage code.

### `RunManager.get_run`
Resolves a persisted run and checks unfinished state and proxy-mode compatibility.
The proxy uses these checks for both startup defaults and explicit request headers.

### `RunManager.session`
Lazily creates or returns the one replay session assigned to a run ID.
Using the registry lock prevents two concurrent requests from constructing separate
consumption lists for the same run.

### `RunManager.begin`
Validates a run and increments its active-request count under the registry lock.
This prevents the finish endpoint from completing a run while HTTP work is underway.

### `RunManager.end`
Releases one active request under the same lock. Streaming calls invoke it only
after response cleanup, keeping unfinished capture protected from premature finish.

### `RunManager.finish`
Rejects busy/closed/incompatible runs, then delegates replay completion to its session
or normal recording completion to the store. This gives HTTP control-plane calls
and context-managed execution the same lifecycle rules.

### `RunRecorder.__enter__` and `__exit__` (expanded)
Replay contexts now create a session or share the supplied manager's session and
check leftovers at exit. Manager-backed contexts also use its in-flight guard,
while retaining original exceptions and the completed/agent_error/aborted outcomes.

### `ToolRecorder.__init__` and `call` (expanded)
Replay tools now use a validated shared session, or create their own persisted
replay run/session when none is supplied. Calls consume through that session so
misses become findings and finish can account for unused tool events.

### `ClosingStreamingResponse.__init__`
Builds an SSE response with an optional asynchronous cleanup callback.
The callback can close an already-open upstream response even if downstream headers
fail before the capture iterator starts.

### `ClosingStreamingResponse.__call__`
Runs normal streaming delivery and explicitly closes its iterator in finally.
It then invokes cleanup even on a disconnect, ensuring capture and upstream resources
are closed before the run's active-request count is released.

### `create_app.record_response`
Contains the existing upstream capture path but uses the routed run's trace for
sequence allocation and persistence. Separating it from header/lifetime routing
keeps streamed and JSON record behavior consistent across multiple runs.

### `create_app.chat` and `finish_stream` (expanded)
The endpoint selects the header/default run, marks it active, and routes to recording
or that run's replay session. Its nested cleanup closes upstream and releases the
run for streaming responses; JSON responses release the run before returning.

### `create_app.create_run`
Validates a typed request, existing trace, and compatible run kind before creating
a run with injected start time. It returns the run ID without eagerly consuming
or modifying the recorded trace.

### `create_app.finish_run`
Delegates explicit completion to the manager and returns the persisted run plus
findings. Invalid or busy runs return a clear 400 response instead of being finalized.

### `checks.load_agent`
Imports MODULE:CALLABLE and validates it can accept the provided client and recorder.
Invalid references and signatures become configuration errors before any agent runs.

### `checks.check_trace`
Imports a golden into a fresh store, invokes the actual agent through an in-process
replay proxy, and shares its session with tool replay. It always finalizes and closes
resources; known errors become structured failures, while unexpected exceptions
propagate after finalization rather than being hidden by a broad catch.

### `checks.check_golden`
Finds golden files, validates mode/agent configuration, checks every trace, and
combines verdicts into a JSON-ready report. It scopes cwd module imports to the
check execution so the installed CLI can load repository-local agents without
permanently changing the search path.

### `inspection.render_run_diff`
Renders run metadata, persisted findings, and source-trace request differences by
sequence as JSON. Labeling source comparisons explicitly avoids claiming replay
recorded a complete second copy of incoming requests.

## M4 test functions and helpers

### `fixture_session`
Creates two recorded exchanges and a tool event, then binds an unfinished replay
run to them. An injected clock makes its finding and finish times predictable.

### `test_json_diff_nested_arrays_keys_types_and_truncation`
Checks pointer escaping, array additions/removals, scalar type changes, and preview
length limits. Equal values must produce empty change lists.

### `test_miss_is_persisted_with_nearest_exchange_diff_and_hash`
Changes a recorded request and checks the exception evidence equals the persisted
finding, including position, nearest exchange, hash, and paths. Pending consumption
must remain unchanged after the miss.

### `test_nearest_falls_back_to_message_count_even_after_consumption`
Uses nonmatching sequence positions to exercise nearest-by-message-count selection,
including a previously consumed exchange. An empty recording must still produce
usable added-field evidence with no fabricated nearest exchange.

### `test_order_warning_only_in_strict_mode`
Reorders two matching requests and compares strict and lenient findings.
Only strict mode may emit a warning with expected/actual sequence evidence.

### `test_tool_miss_and_finish_cover_all_remaining_events`
Calls an unknown tool, consumes one LLM exchange, and finishes the run with leftovers.
It checks both remaining sequence lists, stored tool-miss evidence, duplicate-finish
rejection, and rejection of further consumption.

### `test_full_replay_finishes_without_leftover_findings`
Consumes both exchanges and the tool event before finishing.
The run must finalize at the injected time with no false leftover findings.

### `test_context_detects_leftovers_on_exception_exit`
Raises an agent error after consuming only part of a replay context.
The original error propagates while the run and leftover finding are persisted.

### `test_finalization_and_findings_are_one_transaction`
Attempts to attach another run's finding during finish. It checks the invalid
batch leaves the run unfinished with no inserted finding.

### `test_sessions_reject_forged_runs_and_tool_binding`
Checks a forged trace/run association, record-mode session, cross-trace tool session,
and invalid replay mode fail explicitly. These guards protect per-run consumption
from accidental sharing across incompatible configurations.

### `seed_trace`
Stores two requests whose answers identify their source trace. Concurrent routing
tests can then prove the correct trace was served even when requests hash identically.

### `test_concurrent_runs_same_and_different_traces_never_interfere`, `exercise`, and `consume`
The helpers launch three run consumers together and interleave requests across
identical and different source traces. Each must consume its full recording and
finish cleanly while the startup run retains its independent state.

### `test_control_finish_persists_misses_leftovers_and_rejects_closed_runs`
Makes an HTTP replay miss and then finishes the run through the control endpoint.
It verifies persisted evidence, leftover detection, outcome detail, and rejection
of repeated finish or later requests without duplicate findings.

### `test_run_errors_are_rejected`
Exercises unknown headers, unknown traces, incompatible kinds, and invalid modes.
Rejected requests must leave the default run's recorded exchanges unconsumed.

### `test_record_routing_and_busy_run_cannot_finish`, `exercise`, and `GatedStream.__aiter__`
The gated upstream pauses a stream so the test can attempt finish while it is active.
The async exercise proves finish is rejected until capture ends and verifies the
exchange was saved to the header-selected trace rather than the startup trace.

### `test_run_context_shares_manager_consumption_and_finalizes`
Runs HTTP replay under a manager-backed RunRecorder and routes with its run ID.
It checks the context and proxy use the same session and finish without false leftovers.

### `test_disconnect_closes_capture_before_releasing_run`, `exercise`, `receive`, and `send`
The ASGI helpers simulate disconnects either before headers or during body delivery.
The test checks upstream cleanup, captured partial data when delivery began, and
successful explicit finish after the active request is released.

### `EndlessStream.__aiter__` and `EndlessStream.aclose`
The iterator emits one chunk and then waits indefinitely so disconnect cleanup is
observable. The close method records resource release, ensuring the test cannot
mistake delayed garbage collection for explicit response cleanup.

### `changed_request`
Sends a changed prompt through the supplied client and surfaces HTTP failures.
It is a deliberate agent regression used to verify miss evidence and checker exit 1.

### `changed_tool`
Requests an undefined tool through the supplied recorder. A fail-fast fallback
ensures the regression test never accidentally executes a real tool during replay.

### `short_agent`
Returns without making any calls. It demonstrates that an apparently successful
agent return still fails a check when recorded environment events remain unused.

### `reordered_agent`
Requests two source exchanges in reverse order. This isolates strict-order regression
behavior from request-hash mismatch and allows lenient mode to pass the same agent.

### `test_unchanged_agent_passes_all_goldens_and_cli`
Invokes the CLI with the actual minimal agent in both matching modes.
Both source traces must complete with no findings and a passing JSON verdict.

### `test_modified_agent_cli_exits_one_with_divergences`
Runs changed-prompt, wrong-tool, and early-return agents through the CLI.
Each must exit 1 and retain the expected divergence kinds in its report.

### `test_order_warning_fails_strict_check_but_lenient_passes`
Exports a two-exchange fixture and checks the reordered agent against it.
The test proves warning severity does not hide strict-order regressions and lenient
mode does not emit that warning.

### `test_invalid_agent_reference_is_configuration_error`
Checks malformed references, missing modules, and missing callables fail at load time.
These are configuration errors rather than successful runs with no work performed.

### `test_callable_validation_and_unexpected_errors_finalize` and `fail`
The helper raises a custom exception outside the checker's known error tuple.
The test also checks noncallable/bad-signature configuration, then reopens a file
store to prove the custom error propagated only after agent_error and leftovers
were persisted.

### `test_repository_agent_import_scope_is_restored`
Creates an agent module available only from a temporary working directory.
Checking it verifies installed-CLI import behavior while asserting sys.path is restored.

### `test_check_config_errors_have_exit_two`
Exercises absent goldens, invalid modes, and malformed agent references through argparse.
Each must produce a clear error and exit 2 rather than a regression verdict.

### `test_check_library_rejects_bad_mode`
Calls the library checker directly with an invalid mode. It ensures validation is
not dependent on argparse's choices enforcement.

### `test_diff_cli_includes_source_request_changes_and_findings`
Creates runs on different source requests and persists a miss finding for one.
The CLI report must expose both the source request diff and stored finding evidence,
while unknown comparison IDs fail explicitly.

## M5 production functions

### `FaultSelect.validate_selection`
Requires exactly one supported selector and checks explicit sequences are nonempty,
unique, and nonnegative. Field validation separately rejects invalid probability
bounds and nonpositive nth intervals before runtime selection begins.

### `FaultRule.validate_rule`
Checks the fault class, compatible target, and allowed parameters, then fills
class defaults on an independent copy. This keeps normalization and parameter
errors at plan load time rather than discovering them after an agent runs.

### `faults.schema_validator`
Checks a declared Draft 2020-12 schema and rejects external references recursively.
Keeping resolution local prevents a replay mutation from fetching a remote schema.

### `faults.validate_params`
Validates JSON-native params and class-specific status, delay, repeat, exception,
name/message, and result-schema requirements. Requiring a genuinely invalid bad
result avoids labeling a still-valid value as a shape violation.

### `FaultPlan.validate_version`
Accepts only integer version 1, including rejecting Python's boolean-as-integer case.
An explicit version boundary makes future plan semantics fail clearly in older code.

### `FaultPlan.from_dict`
Validates a parsed plan and wraps expected model/schema/type errors as FaultPlanError.
Both YAML loading and saved-run configuration use this same validation path.

### `FaultPlan.load`
Reads UTF-8 YAML with safe_load and reports path/read/parse errors clearly.
It then delegates structural validation so comments and YAML formatting do not
create a second interpretation of the plan.

### `FaultPlan.digest`
Hashes canonical JSON of the normalized plan with SHA-256. Filled defaults and
sorted mapping keys make equivalent YAML formatting produce the same run plan ID.

### `faults.plan_config`
Returns the normalized plan under the run config's fault_plan key, or an empty dict.
It lets proxy and standalone tool runs attach the same representation without
making the store depend on fault-plan business logic.

### `FaultSelector.__init__`
Copies the plan, creates a private Random(seed), and initializes a protected draw cache.
It avoids changing global agent randomness or sharing selection state across runs.

### `FaultSelector.selected`
Generates probability slots in sequence/rule order and returns matching rules for
a target and sequence. Caching missing lower positions makes selection independent
of request arrival order, while explicit and nth selectors need no random draw.

### `faults.replace_response`
Copies an exchange and replaces its observed response/error/status and stream chunks.
Mutating the copy protects source traces; rebuilding chunks makes streamed and JSON
fault representations describe the same changed completion.

### `faults.invalid_arguments`
Chooses a deterministic JSON value rejected by the declared tool schema.
An unconstrained schema returns no candidate so callers can skip without inventing
a schema-violation label.

### `faults.mutate_exchange`
Applies one LLM/tool-call mutation to a copied completion or returns None when it
cannot apply. It preserves request matching fields, uses canonical truncation for
malformed JSON, and gives repeated semantic tool calls unique protocol IDs.

### `faults.mutate_tool`
Returns a copied bad result or raises the configured supported execution error.
It performs no storage or real tool execution, leaving ordering and capture to callers.

### `FaultRuntime.__init__`
Verifies the persisted unfinished run and its saved normalized plan/hash, then
creates a selector, pending-loop queue, observation lists, and synchronization lock.
Separating observations from labels gives future detector inputs the values actually
exposed without requiring them to inspect ground-truth rows.

### `FaultRuntime.ensure_open`
Rejects operations after the associated run finishes or disappears.
The tool recorder calls it before real execution so a late call cannot apply state
changes and only then discover its fault run was closed.

### `FaultRuntime._log`
Persists a fault row once per rule/target/sequence with an injected timestamp.
It completes before a changed value or injected exception is exposed and propagates
storage failures instead of silently continuing without ground truth.

### `FaultRuntime._candidates`
Combines exchange-selected rules with scheduled tool loops in plan order, using
tool sequence to break pending-loop ties. This makes rule precedence explicit and
separate from mutation applicability.

### `FaultRuntime.pre_exchange`
Applies a terminal HTTP/timeout rule when it is the first candidate, before upstream.
Other candidates require a response, so they are deferred rather than being guessed
inapplicable without seeing the model output.

### `FaultRuntime.exchange`
Applies the first eligible candidate, logs it, records its observed output, and
tracks an optional delay. Ineligible candidates produce diagnostic skips without
fault rows; a scheduled loop is removed only when its response mutation occurs.

### `FaultRuntime.observe`
Keeps an independent observed exchange copy and replaces an earlier copy at the
same sequence. Recording can therefore update a preflight observation with its
final measured latency and persisted representation without duplicate entries.

### `FaultRuntime.needs_response`
Checks whether a response-dependent candidate is selected or scheduled for a sequence.
The proxy buffers a stream only when this is necessary, preserving clean passthrough.

### `FaultRuntime.delay_ms`
Returns the delay associated with the selected exchange fault, defaulting to zero.
The proxy decides whether to await it, so replay stays instantaneous.

### `FaultRuntime.__call__`
Implements the tool-result hook: schedule a loop or persist a label before returning
a bad result/raising an injected error. Already-invalid base results are skipped
because their failure was not caused by this injection.

### `FaultRuntime.pending_loops`
Reports scheduled tool sequences that have not yet produced a mutated LLM response.
The evaluation summary exposes them without falsely treating schedules as injections.

### `TraceStore.add_fault`
Inserts the run, class, target, sequence, canonical params, and timestamp as one
committed fault row. Keeping the SQL here preserves the project's storage boundary.

### `TraceStore.faults`
Loads a run's labels as validated Fault models in target/sequence order.
Stable retrieval order lets reproducibility comparisons ignore completion order
while still retaining timestamps and insertion order for ties.

### `TraceStore.update_tool_event`
Updates an existing event's observed result/error after a record-mode fault hook.
Replay does not call it, so faulted replay results cannot rewrite the source trace.

### `RunManager.faults`
Returns the replay session's runtime or lazily creates one for a recording/live run.
Sharing this object lets proxy responses and in-process tools coordinate a plan
and pending loops without using global mutable state.

### `ToolRecorder._apply_faults`
Applies a runtime hook and captures the resulting value or exception in an observed
copy through finally. Record mode persists that outcome, while replay retains the
copy in memory and leaves the recorded tool event untouched.

### `ToolRecorder.__init__` and `call` (expanded)
Accept normalized plans, shared runtimes, or the existing callback API, and bind
plans to a run with its hash/config before injection. Calls verify runtime state,
observe natural failures without fault labels, and preserve the old callback behavior.

### `create_app.fault_response`
Renders a mutated completion as JSON, SSE, or deliberately malformed raw content.
It avoids passing a malformed body through JSONResponse, which would repair or
replace the corruption instead of exposing the intended fault.

### `create_app.deliver_fault`
Waits a live injected delay and saves the exchange in finally before returning it.
Fault labels already exist at this point, and cancellation still leaves a recorded
504 observation instead of losing the simulated timeout.

### `create_app.buffered_response`
Collects a selected stream, reassembles it, applies a response-dependent mutation,
and delegates delivery/persistence. It closes upstream and preserves partial/error
information rather than pretending all faulted streams completed successfully.

### `create_app.save`, `record_response`, and `replay_response` (expanded)
Recording stores and observes the delivered representation, using terminal preflight
faults or post-response mutations as appropriate. Replay matches first and mutates
only a copy, retaining divergence checks, source immutability, and zero sleeps.

### `create_app.create_run` (expanded)
Attaches the default plan or validates an explicit config override, with null disabling it.
It stores the corresponding digest before any injection and rejects invalid overrides
before creating a run row.

### `evals.faults.evaluate_trace`
Replays a fixed recorded event trajectory through the actual proxy and shared tool
runtime, collecting observations, labels, errors, skips, and pending loops. It does
not score an adaptive agent, and finally finalizes the run even on infrastructure errors.

### `evals.faults.evaluate_faults`
Selects an existing trace or all stored traces and executes the fixed-trajectory
exercise with an injectable clock. It marks the report as unscored and rejects an
empty/missing trace selection rather than claiming a successful evaluation of nothing.

### `inspection.render_trace` and `cli.main` (expanded)
Inspection now includes durable fault labels under each run. Argparse wires --faults
and eval faults to the plan loader/evaluator while leaving selection and mutation
logic outside the CLI.

## M5 test functions and helpers

### `make_plan`
Builds a small validated one-rule plan with explicit target, selector, and params.
Tests use it to make the intended fault readable without repeating YAML boilerplate.

### `tool_exchange`
Creates a representative completion with a declared add tool and valid arguments.
Mutation tests can compare an unchanged source against the changed observable response.

### `runtime_for`
Creates a trace/run with a normalized plan/hash and returns its run-bound runtime.
The injected clock makes fault timestamps deterministic in storage assertions.

### `test_yaml_load_hash_and_defaults_are_canonical`
Loads YAML and compares its plan/hash to an explicitly defaulted equivalent and a
direct SHA-256 computation. It also checks normalized run config can reconstruct
the same plan and a missing plan produces empty config.

### `test_invalid_plans_fail_clearly_at_load`
Exercises invalid versions, seed types, classes, targets, selectors, and params
through the file loader. Every case must fail with FaultPlanError before injection.

### `test_bad_yaml_is_rejected`
Checks malformed YAML, unsafe tags, wrong root shapes, and missing files.
A single clear plan error type keeps these failures distinguishable from agent failures.

### `test_bad_class_parameters_are_rejected`
Checks loop/delay/name/error/result-schema constraints, JSON-native params, and
external schema references. Rejection at load prevents unsupported or nonfaulting
values from entering ground truth.

### `test_selector_matches_canonical_random_order_and_is_arrival_independent`
Compares selection to directly computed private-RNG draws across both event targets.
It also checks reversed arrival, probability boundaries, nth positions, and negative
sequence rejection.

### `test_every_llm_mutation_logs_before_return_and_preserves_input`
Checks each LLM/tool-call mutation has the expected changed status/body/arguments/calls
and a persisted fault row with the run plan hash. It also protects source immutability,
wrong-name collision handling, schema invalidity, and unique repeated-call IDs.

### `test_tool_faults_both_modes_record_actual_outcome_and_no_replay_execution` and `real`
The counted real tool returns a valid result before each injected fault is applied.
The test checks error/bad-result observations in both modes, one persisted label,
recorded outcome updates, and no extra real execution or source mutation in replay.

### `test_tool_targeted_loop_is_logged_when_next_llm_response_is_mutated`
Schedules a loop from a completed tool event and checks no premature label exists.
Only the changed next LLM response creates the tool-targeted row and clears the schedule.

### `test_selection_reproducibility_is_asserted_on_fault_rows`
Applies one plan twice to the same stored trace, reversing event arrival on the
second pass. Canonical fault columns must match while source exchange bytes stay unchanged.

### `test_ineligible_mutations_are_skipped_and_first_applicable_rule_wins`
Starts with an ineligible tool mutation followed by an HTTP rule and checks only
the applicable fault is logged. It also checks unconstrained schemas skip and
external schema references fail without network access.

### `test_logging_failure_prevents_faulted_value_delivery` and `fail_write`
The write helper simulates unavailable storage during label persistence.
The runtime must raise that storage error without returning the mutated response
or claiming an observed injection completed.

### `test_fault_store_reopens_with_canonical_params_and_runtime_rejects_hash_mismatch`
Verifies fault rows survive reopening with canonical params and a mismatched run
plan is rejected. It also checks updating a nonexistent tool event fails explicitly.

### `test_finished_runtime_rejects_injection_before_executing_tool`
Finishes runtimes and attempts further LLM/tool work, including a fail-fast real function.
It checks late operations create no faults or tool events and incompatible runtime modes fail.

### `test_natural_tool_errors_are_observed_without_false_injection_labels` and `fail`
The helper raises a natural tool error before any post-result mutation is possible.
The test requires the error to be observed and stored without an injected-fault label.

### `seed_fault_trace`
Stores a valid tool-call completion in streamed or JSON form.
Proxy tests can reuse the exact source request while checking faults do not alter recording rows.

### `test_replay_terminal_faults_are_instant_logged_and_leave_source_unchanged`, `no_sleep`, and `no_upstream`
The fail-fast helpers forbid replay delays or upstream access for timeout/HTTP faults.
The test checks returned status/marker, stored plan/hash/label, finish, and unchanged source bytes.

### `test_record_terminal_fault_precedes_upstream_and_is_logged_before_delay` and `fake_sleep`
The fake delay checks the fault row already exists while the upstream callback must never run.
The test verifies live timeout/error statuses, measured latency, and the configured delay value.

### `test_stream_faults_are_delivered_and_recorded_in_the_observed_shape`
Exercises empty responses, corrupt tool arguments, and malformed JSON over SSE in both modes.
It checks actual delivered data plus recorded/observed response and chunk shapes, including raw corruption.

### `test_tool_loop_hook_is_shared_with_proxy_and_has_real_tool_target` and `handler`
The scripted handler supplies a real tool-call response followed by a final answer.
The test runs the shared tool hook between requests and verifies the next response
repeats the tool, logs its actual original sequence, and never calls upstream in replay.

### `test_control_runs_can_override_or_disable_default_plan`
Creates runs with a replacement plan and an explicit clean override in one proxy.
It checks each status/hash and verifies invalid overrides create no run rows.

### `test_fault_eval_cli_runs_fixed_trajectory_without_scoring_or_mutating_source`
Runs the CLI evaluator against an imported golden and inspects its JSON and stored run.
It verifies one observed fault, no claimed scoring, source immutability, and completion metadata.

### `test_evaluator_exercises_tool_fault_and_reproducibility`
Executes the same tool-error plan twice against one source trace with fixed clocks.
Observed tool errors and canonical fault rows must agree while the source tool result remains valid.

### `test_fault_eval_usage_errors_are_exit_two`
Exercises empty databases and invalid plans through the evaluation CLI.
Both must report configuration errors rather than returning an unearned successful result.

### `test_evaluator_finalizes_run_when_fault_storage_fails` and `fail_storage`
The helper makes label insertion fail during the proxy request.
The test requires propagation of the infrastructure error, an agent_error run with
context, and no fault label claiming a successful delivery.

### `test_proxy_cli_fault_flag_loads_plan_before_starting_server` and `fake_serve`
The fake server checks the startup run contains the loaded plan and digest for both modes.
The CLI test verifies --faults wiring without opening a socket or calling a live model.
