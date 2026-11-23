# Task-set review — AI-assisted release exception

Status: COMPLETE AS AI-ASSISTED REVIEW on 2026-11-23, explicitly authorized by the owner. Independent human hand-solve review was not performed.

Reviewer: Codex AI, the same agent that implemented the project. Solutions were committed before per-task verifier-goal comparison; this is not independent benchmark validation. See `reviews/ai-task-solutions.json` and `reports/release/summary.json` in the repository root.

Sample: four random tasks per environment, selected with seed 20261122.
Generation seed: 20261122. Review the frozen task file before reading verifier params or detector output.

The AI reasoned from each instruction, initial state, and tool/corpus contracts, recorded its proposed solution, then compared it with the verifier. All 20 solutions were accepted and matched their goals. No task/corpus bytes were changed. This does not establish independent validity of success-rate claims.

| Task | Difficulty | AI solution / result | Solvable? | Verifier correct? | Reviewer / date |
|---|---|---|---|---|---|
| calc-037 | 3 | {"result": 9566} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| calc-033 | 3 | {"result": 3606} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| calc-016 | 1 | {"result": 25} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| calc-025 | 2 | {"result": 3097} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| kv-040 | 3 | {"item-0": {"quantity": 1, "tag": "blue"}, "item-1": {"quantity": 43, "tag": "red"}, "item-2": {"quantity": 51, "tag": "blue"}, "item-3": {"quantity": 79, "tag": "green"}, "item-4": {"quantity": 9, "tag": "green"}} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| kv-003 | 1 | {"baseline": 322887709, "item-0": {"quantity": 72, "tag": "green"}} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| kv-004 | 1 | {"baseline": 396749397, "item-0": {"quantity": 76, "tag": "red"}} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| kv-025 | 2 | {"item-0": {"quantity": 45, "tag": "green"}, "item-1": {"quantity": 39, "tag": "blue"}} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| files-002 | 1 | Write the requested part files with exact newlines; preserve notes/readme.txt. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| files-011 | 1 | Write the requested part files with exact newlines; preserve notes/readme.txt. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| files-028 | 2 | Write the requested part files with exact newlines; preserve notes/readme.txt. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| files-032 | 2 | Write the requested part files with exact newlines; preserve notes/readme.txt. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| search-032 | 2 | {"codes": ["C23348", "C37704", "C26295"]} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| search-015 | 1 | {"codes": ["C89466"]} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| search-024 | 2 | {"codes": ["C32551", "C68458", "C87803"]} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| search-020 | 2 | {"codes": ["C60358", "C43977", "C24994"]} | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| tickets-026 | 2 | Create sequential ticket IDs, assign and advance each to resolved; preserve T0001. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| tickets-034 | 3 | Create sequential ticket IDs, assign and advance each to resolved; preserve T0001. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| tickets-017 | 2 | Create sequential ticket IDs, assign and advance each to resolved; preserve T0001. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |
| tickets-031 | 2 | Create sequential ticket IDs, assign and advance each to resolved; preserve T0001. Full state in evidence JSON. | yes (AI) | matches AI solution | Codex AI / 2026-11-23 |

## Findings and corrections

No discrepancy found in the selected 20 AI solutions. The owner-authorized AI substitution is documented in AGENTS.md; independent human review remains unperformed, not silently marked complete. A correction to frozen tasks or corpus requires a new evaluation version.
