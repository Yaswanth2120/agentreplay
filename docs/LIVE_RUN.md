# M9 live evaluation runbook

Status: tooling tested offline; **no GPU has been rented or real vLLM request run**.
M9 is not complete until real measurements, owner labeling/review, and off-machine
preservation are done. M10 CI/release work is separate.

## Machine and pinned software

Use one NVIDIA GPU with **48 GB VRAM** as the comfortable target, 32 GB host RAM,
and at least 100 GB disk for weights, CUDA packages, caches, and results. This is a
headroom estimate, not a measured minimum or throughput promise: the official model
metadata lists 8,190,735,360 BF16 parameters (~16.4 GB raw weights), before KV cache
and runtime allocations. A 24 GB machine may need tighter settings; do not choose
one on the assumption this untested configuration will fit.

The committed configuration pins:

- `Qwen/Qwen3-8B`, model/tokenizer revision `b968826d9c46dd6066d109eabc6255188de91218`.
- vLLM `0.34.0`, Python 3.12 recommended in the GPU environment.
- Hermes automatic tools; localhost:8000; BF16; 8192 context; one active sequence.
- 1024 output tokens and `enable_thinking: false`, recorded per request/config.
- Temperature 0.7, trial seeds 0/1/2; 200 tasks, v1 followed by v2.

These are chosen experiment settings, not claims of optimal sampling or measured
speed. No automatic upgrade, paid provisioning, or package installation happens in
our runner. Check the provider's installed NVIDIA driver/CUDA compatibility before
installing the pinned vLLM wheel; use a compatible CUDA-enabled Linux image.

Primary sources checked on 2026-11-22:
[Qwen model metadata](https://huggingface.co/api/models/Qwen/Qwen3-8B),
[Qwen vLLM deployment guide](https://github.com/QwenLM/Qwen3/blob/main/docs/source/deployment/vllm.md),
[vLLM v0.34.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.34.0),
[vLLM GPU installation](https://docs.vllm.ai/en/stable/getting_started/installation/gpu.html).
The Qwen guide documents Hermes and the `enable_thinking` request switch. The
library does not add vLLM/PyTorch as dependencies; they belong to the external
model-serving environment described by the master spec.

## Before paying for the rental

Complete the separate 20-task hand-solve review in
`src/agentreplay/evals/REVIEW.md` while still offline if possible. That review and
later manual natural-run labels must be authored by the owner; scripts never
invent them. Reports retain a pending review notice until the review is reconciled.

Prepare the repository and verify the non-GPU plan locally:

```bash
PYTHON=.venv/bin/python scripts/run_live.sh plan
```

This prints the exact serving command without starting a model or using the
network. The feature branch is `feature/m9-live-runner`. Keep the actual evaluation
checkout at one exact commit throughout recording/resume. A commit change requires
a new cohort, even if it only changes documentation.

For a private repo, a Git bundle transfers code/history without copying GitHub
credentials onto the rental. On the local machine:

```bash
git bundle create /tmp/agentreplay-m9.bundle feature/m9-live-runner
scp /tmp/agentreplay-m9.bundle GPU_HOST:/workspace/agentreplay-m9.bundle
```

Replace `GPU_HOST` with the owner's SSH alias/host and adapt port/key options as
needed. On the GPU host:

```bash
cd /workspace
git clone -b feature/m9-live-runner agentreplay-m9.bundle agentreplay
cd agentreplay
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip install 'vllm==0.34.0'
nvidia-smi
```

## Start and verify the server

In terminal 1 on the GPU host, after creating `results/`:

```bash
mkdir -p results
PYTHON=.venv/bin/python scripts/run_live.sh serve
```

Leave this running and wait for its ready log. It captures installed package
versions, Python/OS, GPU name/UUID/VRAM/driver, PyTorch CUDA build, selected device
visibility, exact argv, and Git SHA. A receipt in `.agentreplay-live/server.json`
links those details to the running process. Do not manually alter that receipt.
`serve` and `run` must use the same Python executable. The runner checks Linux
`/proc` process arguments and `/v1/models`; it does not trust guessed vLLM flags.
Do not run a different server on the configured port.

In terminal 2:

```bash
make eval-live PYTHON=.venv/bin/python DB=results/live.db
```

Before full recording it captures a streamed tool-call smoke trace and checks SSE
reassembly, tool name, parsed arguments, and result 5. A parser/stream failure
stops the run for inspection. This is the first real chunk fixture; until it has
passed on hardware, real-vLLM streaming compatibility remains unverified.

The session ID is persisted before evaluation. The reference agent records all
600 v1 and 600 v2 task/trial pairs, then replays each twice, performs the v1→v2
regression comparison, and runs 12 independent fault variants against eligible
clean v1 traces. Fault sweeps and repeats use recorded traffic, not more model
calls. There is no promise all source traces will be clean or that every fault is
eligible: reports count actual injections and leave missing denominators undefined.

## Resume safely

Run the same `make eval-live ...` command with the same DB, config, code commit,
and session file. It skips every pair with committed result/observations, including
verified failures. It does not retry failures merely to improve success rates.
Interrupted/unmeasured attempts are retained as aborted history; replacements have
incremented attempt numbers. Reports use the latest logical attempt and list
superseded IDs. Each fault plan is a separate resumable slot.

A nonblocking file lock prevents two live runners from using the same canonical
DB path simultaneously. Resume rejects changed task hashes, model revisions,
request settings, versions, flags, or code. Hardware/package inventories are
retained in each new run and disclosed as distinct environments in reports.

If both versions' source recordings are already complete, finish replay work on a
CPU machine without keeping the GPU server alive:

```bash
PYTHONPATH=src .venv/bin/python -m agentreplay.cli eval run \
  --db results/live.db --config eval.yaml --resume EVALUATION_ID --replay-only
```

This prechecks every source pair before any execution and refuses incomplete
recordings. It never silently falls back to calling a model. The ordinary resume
command can still call upstream for missing source pairs; use `--replay-only` when
o model should be touched. Use the session JSON's evaluation ID.

## Preserve before stopping/destroying the machine

Do not copy just a live SQLite `.db` file while leaving its WAL behind. Our backup
uses SQLite's backup API, verifies integrity, and reads exports/reports from that
snapshot rather than a moving source DB. It can preserve an interrupted run;
its manifest explicitly marks incomplete report status.

On the GPU host, choose an unused output directory:

```bash
PYTHON=.venv/bin/python scripts/run_live.sh backup \
  --db results/live.db --out results/backup-01
```

This writes `results/backup-01/` and `results/backup-01.tar.gz`, then prints the
archive SHA256. Contents include the consistent database (all cohorts/history in
that DB), original recorded golden JSONL files, successful streaming smoke traces,
frozen tasks/corpus, review document, session/config metadata, generated reports
when complete, and a per-file SHA256 manifest. Existing outputs are never replaced.

On the local machine, copy off the archive and compare its SHA256 with the value
printed on the GPU host:

```bash
scp GPU_HOST:/workspace/agentreplay/results/backup-01.tar.gz ./backup-01.tar.gz
shasum -a 256 backup-01.tar.gz
mkdir -p restored
tar -xzf backup-01.tar.gz -C restored
PYTHON=.venv/bin/python scripts/run_live.sh verify --out restored/backup-01
```

Only after the archive hash matches, manifest verification succeeds, and the DB
and golden files are visibly present off the rental should the machine be
destroyed. Neither the runner nor backup command destroys or purchases machines.
Keep the original archive immutable; use a working DB copy for labels/resume:

```bash
cp restored/backup-01/results.db results/review.db
```

A restored session can point at the moved database if it contains the same cohort;
the runner updates its stored path. To resume on a replacement GPU, restore the
Git commit/config and use the included session file via `--state`, start a new
captured server receipt, and pass the restored DB. Changing model/code/settings
requires a new evaluation rather than mixing incompatible observations.

## Human labels and final report

On the CPU machine with the copied DB:

```bash
PYTHON=.venv/bin/python scripts/run_live.sh sample-labels \
  --db results/review.db --evaluation-id EVALUATION_ID > results/manual-sample.json
```

The fixed seed samples 20 natural live source runs from each environment (100
runs total), including successes and failures without inspecting findings. For
each selected ID, the owner runs:

```bash
PYTHONPATH=src .venv/bin/python -m agentreplay.cli label \
  --db results/review.db --run-id RUN_ID --labeler owner
```

Read the blind transcript and supply the three y/n judgments before looking at
detector output. Do not substitute automated labels. Then generate final reports:

```bash
make report PYTHON=.venv/bin/python DB=results/review.db OUT=reports
```

Verify actual counts: 200×3 source runs per version, 100 labeled natural runs,
review completion, and n for all displayed rates. Manual presence and injected
sequence-label metrics remain separate; unlocalized bad-result faults are
explicitly unscored for detector P/R. Back up the labeled working DB again with a
new output name. M9 acceptance requires these real reports and preserved data;
none are claimed complete by the offline preparation tests.

## Existing replay limitation found during preparation

Natural tool failures are currently surfaced by the reference agent as
`EnvironmentError: ...` while replay wraps the recorded error as
`ToolReplayedError: EnvironmentError: ...`. That changes the next request's tool
message and can produce a strict replay miss for an unchanged agent. An offline
check of v2 `tickets-017` reproduced source outcome `completed` followed by both
repeats ending `agent_error` with replay_miss/leftover findings. This pre-existing
agent error-normalization issue is flagged rather than changed in M9 config/script
work. Clean FP sources exclude runs with these original findings. Determinism
currently compares the two replays with each other; it does not prove equality
between a failed source recording and its replay. Interpret those results with
this limitation until agent error normalization is addressed in a separate fix.

## Vast A100 image preset

For the supplied instance, `examples/eval-vast-a100.yaml` records the image's
installed vLLM 0.32.0 and internal port 18000 instead of replacing its Python/CUDA
stack. Model/tokenizer revisions and experiment settings remain pinned. Pause the
template's unrelated default model service before starting the project server;
use supervisor for both long-running model and evaluation processes. The workspace
on this host is not a persistent volume, so an off-machine verified backup remains
required. This is an explicit pre-cohort deployment configuration change.
