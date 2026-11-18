# Evals

Golden sets live in `golden/`. `bob eval` lists them, `bob eval <name>` runs one, and a
nightly job runs every set that has cases. Each run is saved in `data/bob.sqlite`
(`eval_runs`); a drop of more than 3 points against the previous run sends a notification.

| Set | Kind | Metric | Starting target | State |
| --- | --- | --- | --- | --- |
| `tool_choice` | tool choice | accuracy | 0.9 | 40 prompts for the default plugins |

Tool-choice runs are dry: the eval records the first tool the model picks and runs nothing.

Not built yet: the mail triage set (needs 100 of your own labelled emails).

A retrieval set for a private collection goes in `golden/<name>_private.yaml`, which is not in
git: `kind: retrieval`, `collection: <id>`, `target: {recall_at_5: 0.9}`, and `cases:` with a
question and the chunk ids that answer it.

The read-only conformance test is separate: `bob conformance`.
