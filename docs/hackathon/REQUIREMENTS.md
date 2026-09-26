# VeriSWE: how each hackathon requirement is met

Each row names the requirement, where it is implemented, and the evidence that shows it working. Evidence is an automated test, a CI job, or a run artifact.

`make test` runs 106 offline tests. `make test-all` runs 633 tests, including the whole upstream mini-swe-agent suite. CI runs both on every push.

## 1. Problem statement: an autonomous coding-agent harness

| Requirement | Implementation | Evidence |
|---|---|---|
| Understand software-engineering tasks | Issue or task intake: GitHub URL, `owner/repo#N`, text, `@file`, stdin, `--task`. The agent declares a task type (bug, feature, test-fix, refactor, other). | `src/veriswe/intake.py`, `config/agent.yaml`; tests `test_read_issue_text_and_file`, `test_declared_refactor_is_verified_by_preserved_behaviour` |
| Navigate an existing repository | `view` (100-line windows), `search` (ripgrep/grep, capped output), shell | `src/veriswe/tools/`; `test_view_and_search` |
| Use tools intelligently | Lint-gated `str_replace`/`undo_edit`, `check_repro`, and guards that block unsafe or interactive commands | `src/veriswe/tools/`, `src/veriswe/guards.py`; `test_str_replace_unique_and_lint_revert`, `test_blocked_commands`, `test_check_repro_tool_matches_the_gate` |
| Manage context | Observation masking in cache-stable chunks, head/tail output truncation, and escalating compaction on overflow. Characters before, sent and reduced are measured every call. | `src/veriswe/context.py`, `agent.py`; `test_masking_keeps_recent_and_is_chunked`, `test_oversized_request_compacts_instead_of_dying`; `telemetry_summary.json` → `context_chars_reduced` |
| Orchestrate model interactions | Provider discovery from `AI_API_KEY`, a capability probe, native tool calls with a text fallback, and a switch to text mode mid-run | `src/veriswe/model_config.py`, `models.py`; `test_discovery_*`, `test_switches_to_text_mode_after_tool_parse_failures` |
| Recover from failures | Retries (Retry-After aware), fast stop on an exhausted quota, salvage of rejected tool calls, loop nudges, best-checkpoint restore, and verification feedback rounds | `models.py`, `agent.py`; `test_probe_survives_rate_limits`, `test_salvage_tool_call_rejected_by_server_parser`, `test_best_checkpoint_is_restored`, `test_offline_recovery_demo` |
| Correct, verified changes | A harness-run verification gate that requires objective evidence (below) | `src/veriswe/verify.py`; `test_gate_rejects_empty_and_regressing_patches`, `test_no_objective_evidence_is_never_verified` |
| Efficient use of resources | Masking plus prompt caching (cache hits counted), bounded outputs, a step and time budget, and fast failure on billing or quota errors | `telemetry_summary.json` (tokens, cached, context reduction); `test_deepseek_cache_hits_are_counted` |

## 2. Verification integrity (final audit §3.1–3.3)

- **The model's "done" is not trusted.** On submit, the harness runs its own checks: the patch is non-empty, changed files parse, the reproduction is run with the patch reverted and then applied, and targeted regression tests are compared against the original code.
- **VERIFIED requires objective evidence.** Evidence means something that **fails on the original code and passes with the change**:
  - a reproduction script (bugs)
  - a new or previously failing test (features, test fixes)
  - for a declared refactor, related tests that pass both before and after
- **UNVERIFIED otherwise.** The patch is preserved for inspection, and `report.md` states plainly that it must not be treated as a success. This rule is never relaxed in later rounds. Test: `test_no_objective_evidence_is_never_verified`.
- **A verifier crash is a failed round, not a submission.** The agent gets a `VERIFICATION ERROR` message and keeps working. Test: `test_verifier_crash_is_not_a_submission`.
- **General software-engineering tasks** are supported. Tests: `test_new_test_that_fails_before_counts_as_evidence` (feature/test-fix flow) and `test_declared_refactor_is_verified_by_preserved_behaviour`.

## 3. Observability (audit §3.5–3.6)

Every run writes `runs/<run>/telemetry.jsonl` (every event, secrets redacted) and `telemetry_summary.json`. The summary records:
- model calls and tool calls
- failures and recovery events, with a breakdown
- verification rounds and verification failures
- `context_chars_before`, `context_chars_sent` and `context_chars_reduced`
- tokens, including cached tokens
- the final status

The same summary appears as the **AGENT EXECUTION** box at the end of every run: in the TUI, in the CLI output and in `report.md`. Test: `test_telemetry_artifacts`.

## 4. Standardised Makefile evaluation

| Requirement | Implementation / evidence |
|---|---|
| `Makefile` at the repo root with `setup`, `run`, `test` and `clean` | `Makefile`. CI runs `make setup` and `make test` on Ubuntu, on macOS, on Debian without `python3-venv`, on a machine with only Python 3.9, on `python:3.12-slim`, plus `make test-all` |
| `make setup` installs everything | `scripts/setup.sh`: uses uv, or venv+pip, or bootstraps uv, which can fetch Python 3.12 itself |
| `make run` launches the harness or TUI | Textual TUI by default. Headless when there is no TTY or `HEADLESS=1` is set. `ISSUE=`, `TASK=` and `REPO=` pass-through |
| Key only via `AI_API_KEY`, nothing hard-coded | `export AI_API_KEY=…` or `make run AI_API_KEY=…`. The key is removed from the agent's shell and redacted from all artifacts. Tests: `test_committed_config_has_no_secrets`, `test_api_key_never_reaches_shell_or_trajectory`, `test_key_status_never_reveals_the_key` |
| Text-only model | Only chat-completion text and tool calls are used; no multimodal input |
| Model configuration clearly defined; prescribed model family used | `config/model.yaml`. It prefers DeepSeek/Qwen (the evaluation families) and never substitutes an explicitly configured model |
| Reproducibility | Fixed sampling per model family in `config/model.yaml`. The harness itself makes no random choices |
| Works without modifying source | Everything is driven by env vars and Make variables. A missing or invalid key or input produces a clear message, never a traceback |
