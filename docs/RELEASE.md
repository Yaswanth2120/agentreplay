# Release review and remaining owner work

The implementation and CI are in place. Final `v0.1.0` publication still requires the independent owner judgments mandated by AGENTS.md sections 11.2 and 11.4. No model inference or rented GPU is needed for these steps. Do not fill review rows or labels from automated test outcomes or detector predictions.

## Automated release follow-up

Run from the repository root with the package installed, using unused output paths:

```bash
python -m scripts.verify_release --source-db results/live-review.db --source-evaluation-id b29dda4b8d1f4b5bb52be3ad7c91ea27 --db results/release-verification.db --out reports/release-verification.json
```

This first backs up the source into a separate database, then runs the current reference agent against each archived source and reruns the source cohort's loop fault plans on successful finding-free v1 sources. It compares outcomes, task success, final answers, final states, state errors, and replay divergences. All inference responses and tool results come from the archive. Exit 0 requires source fidelity and no loop FP/FN; exit 1 preserves the generated report with failures. Existing outputs are never overwritten.

The generated JSON records both the source recording SHA and follow-up code SHA, denominators, confidence intervals, and derived run IDs. It is post-hoc development evidence, not independent validation or a new live-model benchmark. The original `reports/summary.*`, source measurements, archive, and fault labels remain unchanged. `agentreplay report` should not be used to present this verification-only subset as a complete new live cohort.

The committed [verification JSON](../reports/release-verification.json) records the completed follow-up: 1,200/1,200 source replays matched and 922 loop injections produced 922 TP, 0 FP, 0 FN. Code SHA and original recording SHA are recorded separately; no new live-model performance claim follows from these development checks.

## Owner: hand-solve 20 tasks

Use the fixed rows in [REVIEW.md](../src/agentreplay/evals/REVIEW.md). Read each instruction and tool contracts before inspecting verifier parameters. This command displays only the selected instructions and tool contracts:

```bash
python - <<'PY'
import json
import re
from pathlib import Path
from agentreplay.evals.envs import make_environment
from agentreplay.evals.tasks import load_tasks
review = Path('src/agentreplay/evals/REVIEW.md').read_text()
selected = set(re.findall(r'^\| ([a-z]+-\d{3}) \|', review, re.MULTILINE))
for task in load_tasks():
    if task.id in selected:
        print('\nTASK', task.id, '\n', task.instruction)
        print(json.dumps(make_environment(task.env, task.seed).tools(), indent=2))
PY
```

Record your actual steps, result, solvability judgment, verifier correctness judgment, name, and date in REVIEW.md. Inspect the verifier only after your solution. Report mismatches rather than silently marking them correct. A task/verifier correction requires a new evaluation version and a decision about which live measurements it invalidates.

## Owner: blind labels on 100 natural runs

The fixed [manual sample](../reports/live/manual-sample.json) contains 20 runs per environment, selected before owner judgments. Label the original natural runs, not the new post-fix replays. Use the working database; the immutable backup stays untouched. Start one run with:

```bash
agentreplay label --db results/live-review.db --run-id 20cfc5a9d6ae404aa916bbfbd8ab306d --labeler owner
```

The CLI displays the observed transcript without detector findings or verifier success and asks whether `loop`, `invalid_tool_call`, and `timeout` are present. Complete all three y/n judgments to save a run's labels. Do not inspect its findings first. Repeat for all IDs in the sample; the sample file itself contains no judgments.

After both reviews, preserve a new consistent database backup and generate a new report into a separate directory. Record which review and labels it includes; never replace the immutable original archive. The report's owner-review status also needs to be reconciled with actual completed review evidence before claiming validation. Fix any review findings, rerun the appropriate checks, set package version to `0.1.0` in both pyproject.toml and `src/agentreplay/__init__.py`, and create the final release tag only when the review is genuinely complete.
