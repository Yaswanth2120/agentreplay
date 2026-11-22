# Live evidence and owner handoff

The canonical generated report is [../summary.md](../summary.md), with matching
JSON. Source recording used commit `92874a4`, the A100 image preset, and vLLM 0.32.0.
These are real model measurements; the report retains the pending owner-review
notice and does not claim a validated benchmark or statistical significance.

- `archive-receipt.json`: original generated archive path/hash and complete status.
- `data-manifest.json`: original per-file hashes for the immutable archive contents.
- `manual-sample.json`: generated blind stratified sample for the owner to label.

The raw archive is preserved locally at `results/final-01.tar.gz`; its verified
extracted DB/goldens are at `results/offbox/final-01/`. A working DB copy for labels
and reports is `results/live-review.db`. Large raw data stays outside Git. The
manifest describes the original archive, not this publication folder. Published
metrics were checked to equal the original snapshot report exactly; additional
interpretation notes explain known limitations.

Reproduce the canonical report from the saved working copy:

```bash
PYTHONPATH=src .venv/bin/python -m agentreplay.cli report \
  --db results/live-review.db --out reports \
  --evaluation-id b29dda4b8d1f4b5bb52be3ad7c91ea27
```

For each sampled ID, run the blind labeling CLI against `results/live-review.db`.
The owner must supply judgments; no automated labels or task-review attestations
have been inserted. Re-run report generation after those reviews, then preserve
the labeled DB in a new archive without overwriting the original evidence.

The Vast instance remains running at the owner's explicit request. Project model
and evaluation processes are stopped, and GPU memory was verified free. No stop or
destroy request was sent to the instance.
