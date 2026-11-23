# AgentReplay design

AgentReplay records an agent's environment: OpenAI-compatible chat responses and synchronous Python tool results. The agent code is the system under test. On replay, changed requests or tool calls become evidence of a behavioral regression. Replay itself needs no network or GPU.

## Storage and portable format

`schema.py` defines validated models; `store.py` owns all SQLite statements. A database uses WAL mode and foreign keys. A trace has separately ordered LLM exchanges and tool events; runs refer to a trace and have their own configuration, outcomes, findings, injected fault labels, and observed snapshots. Finishing a run atomically persists its observation snapshot, evaluation result, and findings. Task success is distinct from successful agent completion.

Schema version **7** and export identifier **`agentreplay.trace.v1`** are frozen. Existing fixtures and the real evaluation archive remain unchanged. Future incompatible changes require a new explicit version and a documented compatibility decision; changing the schema number in place is not a migration. Opening an incompatible database raises `SchemaVersionError`.

A portable UTF-8 JSONL export starts with a trace header, then exchange and tool-event rows ordered by sequence (exchange first on ties). Independent counters do not imply a shared causal clock. Export preserves canonical data, IDs, and timestamps, but excludes runs, findings, faults, and manual labels. Import validates the entire file before writing, rejects incompatible versions, invalid hashes, non-finite values, duplicates, and unknown fields, and rolls back conflicts. Golden tests enforce byte-identical round trips.

Request headers are never persisted. Request/response bodies and tool data may contain secrets or personal information; automatic redaction is outside this release.

## Matching and consumption

`canonical.py` uses sorted-key compact UTF-8 JSON without coercing integers to floats. Strict request hashes include `model`, `messages`, `tools`, `tool_choice`, `temperature`, `top_p`, `max_tokens`, `seed`, `response_format`, and `stop`. Lenient hashes include only `messages` and `tools`. `stream`, `user`, metadata, and other fields affect neither hash. Tool hashes use parsed canonical arguments; malformed argument strings use a distinct `raw:` prefix.

`replay.py` consumes the earliest unconsumed match. Each run has its own consumption state, shared by that run's proxy and tool recorder. Repeated identical calls consume repeated recordings in order. No replay path invokes the upstream model or real tool function.

A missing LLM match produces HTTP 409 and a `replay_miss` finding with hash, nearest recorded request, and bounded JSON Pointer differences. Missing tool matches raise `ToolReplayMiss`. Strict replay reports out-of-order matches with `replay_order`; lenient replay does not. Finish detects unused exchanges/tools with `replay_leftover`. Request misses do not consume the missing recording. These findings, rather than response similarity, are the regression oracle.

## Proxy, tools, and streaming

`proxy.py` records only `POST /v1/chat/completions`. `GET /v1/models` passes through without recording in record/live mode and uses a trace-model stub in replay. Health and create/finish run endpoints support orchestration. `X-AgentReplay-Run` identifies an active run; missing headers use the startup run and invalid run IDs are rejected. Unknown or finished runs cannot silently record into another run.

`streaming.py` preserves SSE data lines and reassembles choice/tool-index fragments. Content and tool arguments concatenate in order; usage-only chunks remain captured. Timeouts/connect errors are stored with status 599 and returned as 502. Mid-stream errors retain partial chunks; once headers are sent the client status cannot be changed. Replay emits stored data immediately and a terminal `[DONE]`, including for partial recordings. Stream/non-stream conversion is supported with a `stream_mismatch` warning; transport boundaries and timing are not simulated. A real Qwen3/vLLM SSE fixture supplements generated fixtures.

`tools.py` executes synchronous functions in record mode, snapshots JSON results/errors, and re-raises original exceptions after recording. Replay returns stored JSON or raises `ToolReplayedError` carrying the original error string; it cannot reconstruct arbitrary exception classes. Callers normalizing errors must account for that wrapper. The reference agent now returns the original stored error string when it catches this wrapper, preserving the next request. The original M9 runner did not normalize it; its unchanged measurements remain disclosed in the live report. Repeat-to-repeat equality alone does not establish source fidelity.

## Faults and independent detection

Versioned YAML fault plans have a seed, validated selectors, and a canonical plan hash. Selection is reproducible. Runtime hooks mutate LLM responses or tool outcomes, persist a fault label before returning the mutation, and preserve immutable source trace rows. Replay observations capture the values actually seen by the agent.

`detectors.py` contains pure functions receiving a sanitized `RunView`. It cannot read storage, network, fault plans, or labels. `detection.py` adapts persisted observations and stores detector findings. Invalid calls, loops, timeout, malformed responses, HTTP errors, tool errors, and request budget violations have configurable thresholds. Replay divergences belong to replay; task failure belongs to the verifier/harness.

Loop detection considers emitted calls and associates executed events without counting the same call twice. When the first call in a detected loop has no execution, the detector now associates it with an earlier matching execution only if that observed signature has exactly one such execution. This is an observable association, not a claim of hidden causality. Multiple matching executions remain ambiguous and retain no tool-event location; exchange localization is preserved. Thresholds, counting, and labels are unchanged. The original M9 mislocalizations remain FP/FN in the archived report; follow-up measurements are separate and explicitly post-hoc.

## Evaluation and reporting

Five deterministic environments implement arithmetic, key/value operations, virtual files, fixed-corpus search, and ticket workflows. The frozen 200-task JSONL file has a version derived from its exact bytes. Verifiers check state or answers without LLM judging. Scripted trajectories test all tasks; the owner must also hand-solve 20 tasks before trusting success-rate claims.

The reference agent uses HTTPX and tools, with v1 as baseline and a seeded v2 regression that skips a batched tool execution. `evals/harness.py` orchestrates fresh environments for sources, repeated replays, version regression checks, and fault replays. Resume identities include phase, task, trial, version, source, repeat, and fault plan. Measured failures are retained; interrupted attempts remain audit history. Exact cohort configuration and code provenance are required for resume.

`evals/stats.py` owns metric arithmetic, Wilson intervals, conservative F1 bounds, fixed-seed paired bootstrap, and exact McNemar calculations. `report.py` validates cohort completeness and renders Markdown/JSON from those calculations; rates without denominators are rejected. Injected scoring requires unique kind/target/sequence matches. Manual presence labels are scored separately; absent labels yield undefined metrics. Clean false-positive rates are explicitly conditional on successful finding-free sources. Repeat determinism compares two replays, not the original execution. Unlocalized bad-result faults are reported as unscored.

The completed live cohort and its original measurements are immutable. Reports include source code SHA, task hash, model revision, versions, flags, and runtime environment. A consistent SQLite backup, trace exports, SHA256 manifest, and verified archive were copied off the rented GPU. No GPU is required for remaining human review, labeling, report generation, CI, or documentation. Any later behavioral fix needs its own provenance and measurements; it must not overwrite the original results as if it had been used during recording.

## Validation and release

CI uses clean checkouts on Python 3.10–3.12, installs the package, runs Ruff and the complete pytest suite, replays golden traces through the installed CLI, and verifies portable export bytes. Tests forbid real socket connections and use in-process ASGI/mock HTTP transports. Package installation may use PyPI; the checks require no live model, GPU, or secrets.

The README quickstart is also exercised in a fresh virtualenv. `v0.1.0` publication is withheld while owner task review and the 100 blind natural-run labels remain pending; passing automated checks do not substitute for those independent judgments. Known replay fidelity and localization limitations remain documented. The format freeze does not imply that pending scientific validation has happened.

Given the same trace, the same agent code, and an agent that is deterministic given its inputs, replay produces identical outputs every time. AgentReplay does not make agent code deterministic.
