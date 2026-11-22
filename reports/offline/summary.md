```json
{
  "backend": "scripted",
  "date": "2026-11-22T18:19:22.008287+00:00",
  "evaluation_id": "e807ca8bb4d44eeeb1f746f1ad80be9a",
  "git_sha": "387a2c98aec2a778541b2d3442bb0129267fd31f",
  "model": "scripted-offline",
  "owner_review": "pending",
  "task_ids": [
    "calc-001",
    "calc-017",
    "calc-033",
    "calc-034",
    "kv-001",
    "kv-017",
    "kv-033",
    "kv-034",
    "files-001",
    "files-017",
    "files-033",
    "files-034",
    "search-001",
    "search-017",
    "search-033",
    "search-034",
    "tickets-001",
    "tickets-017",
    "tickets-033",
    "tickets-034"
  ],
  "tasks_version": "tasks-v1-sha256:a84d51fb1a95bff5587ec8a2b18543c9d15c41211bc1765a38e9483d6f095ae0",
  "temperature": 0.7,
  "trials": 3,
  "versions": [
    "v1",
    "v2"
  ],
  "vllm_flags": [],
  "vllm_version": "not-used-scripted"
}
```

# Evaluation summary

Total runs: 480.

| Metric | Value | Success count | n | 95% interval |
|---|---:|---:|---:|---|
| versions.v1.task_success | 1.000000 | 60 | 60 | [0.939828, 1.000000] |
| versions.v1.tool_call_validity | 1.000000 | 240 | 240 | [0.984246, 1.000000] |
| versions.v1.loop | 0.000000 | 0 | 60 | [0.000000, 0.060172] |
| versions.v1.timeout | 0.000000 | 0 | 60 | [0.000000, 0.060172] |
| versions.v1.mean_steps | 3.200000 | — | 60 | undefined |
| versions.v2.task_success | 0.550000 | 33 | 60 | [0.424919, 0.669063] |
| versions.v2.tool_call_validity | 1.000000 | 240 | 240 | [0.984246, 1.000000] |
| versions.v2.loop | 0.000000 | 0 | 60 | [0.000000, 0.060172] |
| versions.v2.timeout | 0.000000 | 0 | 60 | [0.000000, 0.060172] |
| versions.v2.mean_steps | 3.200000 | — | 60 | undefined |
| replay_determinism | 1.000000 | 120 | 120 | [0.968981, 1.000000] |
| clean_false_positives.any | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| clean_false_positives.per_kind.budget_exceeded | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| clean_false_positives.per_kind.invalid_tool_call | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| clean_false_positives.per_kind.llm_error | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| clean_false_positives.per_kind.loop | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| clean_false_positives.per_kind.malformed_response | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| clean_false_positives.per_kind.timeout | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| clean_false_positives.per_kind.tool_error | 0.000000 | 0 | 186 | [0.000000, 0.020235] |
| regression_divergence | 0.450000 | 27 | 60 | [0.330937, 0.575081] |
| regression_kinds.replay_miss | 0.450000 | 27 | 60 | [0.330937, 0.575081] |
| regression_kinds.tool_replay_miss | 0.000000 | 0 | 60 | [0.000000, 0.060172] |
| regression_kinds.replay_leftover | 0.450000 | 27 | 60 | [0.330937, 0.575081] |
| regression_kinds.replay_order | 0.000000 | 0 | 60 | [0.000000, 0.060172] |
| injected.per_kind.invalid_tool_call.precision | undefined | 0 | 0 | undefined |
| injected.per_kind.invalid_tool_call.recall | undefined | 0 | 0 | undefined |
| injected.per_kind.invalid_tool_call.f1 | undefined | — | 0 | undefined |
| injected.per_kind.loop.precision | undefined | 0 | 0 | undefined |
| injected.per_kind.loop.recall | undefined | 0 | 0 | undefined |
| injected.per_kind.loop.f1 | undefined | — | 0 | undefined |
| injected.per_kind.timeout.precision | undefined | 0 | 0 | undefined |
| injected.per_kind.timeout.recall | undefined | 0 | 0 | undefined |
| injected.per_kind.timeout.f1 | undefined | — | 0 | undefined |
| injected.per_kind.malformed_response.precision | undefined | 0 | 0 | undefined |
| injected.per_kind.malformed_response.recall | undefined | 0 | 0 | undefined |
| injected.per_kind.malformed_response.f1 | undefined | — | 0 | undefined |
| injected.per_kind.llm_error.precision | 1.000000 | 60 | 60 | [0.939828, 1.000000] |
| injected.per_kind.llm_error.recall | 1.000000 | 60 | 60 | [0.939828, 1.000000] |
| injected.per_kind.llm_error.f1 | 1.000000 | — | 60 | [0.922738, 1.000000] |
| injected.per_kind.tool_error.precision | undefined | 0 | 0 | undefined |
| injected.per_kind.tool_error.recall | undefined | 0 | 0 | undefined |
| injected.per_kind.tool_error.f1 | undefined | — | 0 | undefined |
| injected.per_kind.budget_exceeded.precision | undefined | 0 | 0 | undefined |
| injected.per_kind.budget_exceeded.recall | undefined | 0 | 0 | undefined |
| injected.per_kind.budget_exceeded.f1 | undefined | — | 0 | undefined |
| manual.per_kind.loop.precision | undefined | 0 | 0 | undefined |
| manual.per_kind.loop.recall | undefined | 0 | 0 | undefined |
| manual.per_kind.loop.f1 | undefined | — | 0 | undefined |
| manual.per_kind.invalid_tool_call.precision | undefined | 0 | 0 | undefined |
| manual.per_kind.invalid_tool_call.recall | undefined | 0 | 0 | undefined |
| manual.per_kind.invalid_tool_call.f1 | undefined | — | 0 | undefined |
| manual.per_kind.timeout.precision | undefined | 0 | 0 | undefined |
| manual.per_kind.timeout.recall | undefined | 0 | 0 | undefined |
| manual.per_kind.timeout.f1 | undefined | — | 0 | undefined |
| paired.success_difference | -0.450000 | — | 60 | [-0.566667, -0.332917] |

## Detector counts

| Source | Kind | TP | FP | FN | TN | Labeled runs |
|---|---|---:|---:|---:|---:|---:|
| injected | invalid_tool_call | 0 | 0 | 0 | — | — |
| injected | loop | 0 | 0 | 0 | — | — |
| injected | timeout | 0 | 0 | 0 | — | — |
| injected | malformed_response | 0 | 0 | 0 | — | — |
| injected | llm_error | 60 | 0 | 0 | — | — |
| injected | tool_error | 0 | 0 | 0 | — | — |
| injected | budget_exceeded | 0 | 0 | 0 | — | — |
| manual | loop | 0 | 0 | 0 | 0 | 0 |
| manual | invalid_tool_call | 0 | 0 | 0 | 0 | 0 |
| manual | timeout | 0 | 0 | 0 | 0 | 0 |

Exact McNemar p=1.49012e-08; n=60 pairs, discordant n=27, v1-only=27, v2-only=0.
Bootstrap resamples=10000, seed=1234.

Injection scoring n=60 runs; unscored faults=0.

## Interpretation

- Scripted offline results validate plumbing, not live model capability.
- Owner task hand-solve review is pending; no validated benchmark or significance claim is made.
- Wilson intervals use the displayed count unit; F1 uses conservative harmonic bounds from Bonferroni-adjusted Wilson intervals.
- Trial pairs are resampled as paired observations; repeated tasks/trials and replay repeats are correlated, so generalization is limited.
- Clean FP denominators include both repeats of successful, finding-free source runs. Determinism compares each source's two repeats.
- Task failure is run-level; bad-result faults without localized verification are counted as unscored, never invented detector true positives.
- Manual presence-label scores remain separate from injected sequence-label scores; missing denominators are shown as undefined.
