# VeriSWE: a verification-first coding-agent harness

VeriSWE turns a text-only foundation model into an autonomous software engineer. It takes a GitHub issue and a repository, then:

1. finds the relevant code
2. reproduces the bug
3. fixes it
4. has the harness prove the fix before any submission is accepted

Every run ends with a patch plus an evidence report showing:

- a reproduction that fails on the original code and passes on the fixed code
- regression tests compared against the original code
- the tokens, cost and time spent

> Same model. Different harness. **Evidence over claims.**

VeriSWE builds on [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) (MIT), from the Princeton/Stanford SWE-agent team. mini-swe-agent is the reference harness for SWE-bench bash-only and SWE-bench Pro. We kept its small, linear, provider-agnostic agent loop and added the harness engineering that it deliberately leaves out.

---

## Quick start (the standard evaluation flow)

```bash
export AI_API_KEY="<PROVIDED_API_KEY>"
make setup          # creates .venv and installs everything (uses uv if present, else pip)
make run            # launches the TUI: paste a GitHub issue URL or issue text and press Ctrl+S
make test           # offline test suite, no API key or network needed
```

You can also start `make run` non-interactively:

```bash
make run ISSUE=https://github.com/owner/repo/issues/123          # clones the repo, fetches the issue
make run ISSUE="<issue text>" REPO=/path/to/checked-out/repo
make run ISSUE=@issue.md REPO=/path/to/repo HEADLESS=1          # plain streaming output (CI / no TTY)
echo "<issue text>" | make run REPO=/path/to/repo                # stdin
make demo                                                        # solve the bundled demo bug end-to-end
```

When stdin is not a terminal, VeriSWE runs headless automatically.

Each run writes to `runs/<timestamp>-<repo>/`:

| File | Contents |
|---|---|
| `patch.diff` | Final patch against the starting commit. It is also left applied in the repository. |
| `report.md` | Status, the verification evidence table, history, token and cost stats, and the patch. |
| `result.json` | Machine-readable summary. |
| `trajectory.json` | Full agent trajectory with every message, command and output. Secrets are redacted. |
| `evidence/` | The reproduction script and JUnit XML files from the harness test runs. |

## Model configuration

The model is declared in [`config/model.yaml`](config/model.yaml). The credential is only ever read from `AI_API_KEY`. It is never written to disk, and it is removed from the agent's shell environment so a command such as `env` cannot leak it.

| Setting | Config key | Env override |
|---|---|---|
| Model (the one the organisers prescribe) | `model_name` | `AI_MODEL` |
| Provider | `provider: auto` | `AI_PROVIDER` |
| OpenAI-compatible endpoint (vLLM, Ollama, proxies) | `base_url` | `AI_BASE_URL` |
| Action format | `action_mode: auto` | `AI_TEXT_MODE=1/0` |

- **Provider auto-detection** from the key prefix:
  - `sk-ant-` → Anthropic
  - `AIza` → Gemini
  - `sk-or-` → OpenRouter
  - `gsk_` → Groq
  - `xai-` → xAI
  - anything else → OpenAI-compatible

  An explicit `provider/model` prefix or `AI_BASE_URL` takes precedence over detection. When a `base_url` is set and no model is configured, VeriSWE uses the first model the endpoint lists.
- **Capability probe.** One tiny call at startup checks the credential and native tool calling. Models without working tool calls fall back to plain-text actions (`mswea_bash_command` blocks), so weak or open models still work.
- **Reproducibility.** Decoding uses `temperature: 0`, and parameters a model does not support are dropped automatically. The harness makes no other random choices.

## Architecture

```
            ┌──────────── make run ─────────────┐
 issue ───▶ │ intake: URL | text | @file | stdin │──▶ repo (path or git clone) ──▶ Workspace
            └────────────────────────────────────┘        (git base snapshot, excludes)
                                  │
                                  ▼
        ┌────────────────────── VeriAgent (mini-swe-agent loop) ─────────────────────┐
        │ model.query(masked history) ─▶ bash action ─▶ guards ─▶ LocalEnv (stdin=null)│
        │        ▲                                          │                          │
        │        └────── observation (+ loop nudges) ◀──────┘                          │
        │ helper tools on PATH: view · search · str_replace (lint-gated) · undo_edit   │
        │ submit ─▶ VERIFICATION GATE ─▶ accepted ─▶ patch + report                    │
        │              └─ rejected ─▶ logs fed back to the agent (up to N rounds)      │
        │ limits hit ─▶ auto-submit the best verified checkpoint                       │
        └──────────────────────────────────────────────────────────────────────────────┘
```

| Module | Role |
|---|---|
| `src/veriswe/agent.py` | `VeriAgent`, a subclass of mini's `DefaultAgent`. Adds guards, masking, token accounting, the verification gate, checkpoint restore and autosubmit. |
| `src/veriswe/verify.py` | The harness-run verification gate. |
| `src/veriswe/tools/` | Helper CLIs the model uses from bash: `view`, `search`, `str_replace`, `undo_edit`. |
| `src/veriswe/context.py` | Cache-friendly observation masking and emergency compaction on context overflow. |
| `src/veriswe/guards.py` | Blocked commands and the loop/stuck detector. |
| `src/veriswe/model_config.py` | Provider detection, `AI_API_KEY` wiring, capability probe. |
| `src/veriswe/intake.py`, `workspace.py` | Issue and repo intake, and git plumbing (clean diffs without touching the user's index). |
| `src/veriswe/tui.py`, `cli.py` | Textual TUI and headless streaming CLI. |
| `config/agent.yaml` | All prompts and limits (Jinja2), in a single file. |

## What we added on top of mini-swe-agent, and why

Each addition targets a failure mode documented in recent coding-agent research.

1. **Verification gate (the largest lever).** When the agent says it is done, the harness checks the work itself and does not rely on the model's claim.
   - The patch must be non-empty and every changed file must still parse.
   - `reproduce_issue.py` is run on the patched code, where it must pass, and on the original code with the patch temporarily reverted, where it must fail. A reproduction that passes on the original code is rejected as weak evidence.
   - Targeted regression tests are selected from the changed modules: tests the agent touched, tests matching by name, and tests that import the module. They run with JUnit XML output, and only tests that passed before the patch and fail after it count against the patch. Pre-existing failures are not blamed on the agent.
   - On failure, the agent receives the exact logs and keeps working, for up to `max_verification_rounds` rounds.

   Motivation: "stopping without verifying" is the most common agent failure. LangChain gained +13.7pp on Terminal-Bench 2 mostly from a pre-completion verification step.
2. **Best-checkpoint restore and autosubmit.** Every verification attempt is scored. If later work makes things worse, or a step, time or context limit is reached, VeriSWE submits the best patch it has instead of nothing or a regression. This targets the "correct edit later overwritten" failure mode.
3. **Lint-gated `str_replace` editing.** SEARCH/REPLACE blocks must match exactly one location. Uniform indentation mistakes are auto-corrected. Ambiguous or missing matches return a helpful hint with the closest lines. Edits that break syntax (Python, JSON, JS, YAML, TOML) are reverted automatically. Every edit echoes the resulting snippet with line numbers, and `undo_edit` is available. In SWE-agent's ablations, lint-on-edit gave +3pp and removing the edit tool cost −7.7pp.
4. **Windowed `view` and capped `search`.** Files are shown in 100-line windows, which SWE-agent found to beat both whole files and 30-line windows. Search uses ripgrep with a grep fallback and bounded output.
5. **Context efficiency.**
   - Observation masking keeps the last N tool outputs verbatim and replaces older ones with one-line stubs. Masking happens in chunks so the prompt prefix stays stable for provider prompt caching; Anthropic cache-control markers are set automatically.
   - Long outputs are truncated to head and tail.
   - A context-window error triggers emergency compaction instead of a crash.
   - Observation masking has been measured at about −50% cost at an equal or better solve rate.
6. **Guards.**
   - Interactive programs (vim, less and similar) are blocked, and stdin is closed so nothing can hang or steal the TUI's terminal.
   - Destructive or history-leaking git commands are blocked: stash, reset --hard, checkout of other refs, `log --all` and remote history. The agent solves the issue from the code rather than from future commits, which audits have flagged as a benchmark-gaming pattern.
   - Destructive recursive `rm` is blocked.
   - A loop detector nudges the agent, with escalating warnings, when it repeats itself or keeps failing edits on the same file.
7. **Workflow prompt.** The prompt follows explore → reproduce → minimal root-cause fix → verify with the reproduction and nearby tests → edge cases → submit. It includes persistence rules and efficiency rules, following Anthropic's and OpenAI's published SWE-bench scaffolds.
8. **Model and API robustness.** These features are for an unknown model, and each one fixed a failure we hit in live testing.
   - **Tool-call failures.** When a provider rejects a malformed tool call (for example Groq's "Failed to parse tool call arguments"), VeriSWE turns it into feedback for the model instead of retrying the same 400 request with backoff. After repeated failures it switches the running conversation to plain-text actions.
   - **Multiple commands per reply.** In text mode, if a model emits several command blocks, the first one runs and the model is told the rest were skipped. The reply is not thrown away.
   - **Edit-marker tolerance.** SEARCH/REPLACE markers are accepted with 5–9 marker characters, because models miscount them.
   - **Rate limits.** Rate-limited calls wait for the provider's suggested Retry-After time rather than blind exponential backoff, and they are shown as compact status events.
   - **Oversized requests.** If one request is larger than the context window or the key's per-minute token budget (for example "Limit 8000, Requested 8554"), VeriSWE does not retry it forever. It compacts the history in escalating levels and continues.
   - **Mid-run crashes.** If the API dies mid-run, the harness still verifies and submits the best patch so far.
   - **Retired default models.** If no model is configured and the provider default has been retired, the best available chat model for the key is auto-selected. An explicitly configured model is never substituted; if it is missing, the error lists the models the endpoint serves.
9. **Robust intake and UX.** Issues can be given as GitHub URLs (including comments), raw text, files or stdin, and repos as a path or git URL. Issue fetching falls back from the REST API to the `gh` CLI to the issue web page, so an exhausted API rate limit does not block a run. Output is a live TUI or headless stream. Every run produces an evidence report.

## Tests

`make test` runs 48 offline tests in under 30 seconds, with no network or API key:

- **Unit tests:** provider detection, config and env overrides, a no-secrets-in-config check, guards, the loop detector, masking, intake, and workspace diffs and their round trip.
- **Tool tests:** `str_replace` uniqueness, lint revert and undo; `view`; `search`.
- **End-to-end agent runs with a scripted model**, each on a copy of a fixture repo with a planted bug:
  - the happy path is verified
  - the gate rejects an empty patch and then a regressing patch
  - a missing reproduction is requested
  - a step limit triggers autosubmit of the best patch
  - the best checkpoint is restored
  - blocked commands do not run
  - the API key never reaches the shell or the trajectory

## Limits and knobs

`config/agent.yaml` holds the limits:

| Setting | Default |
|---|---|
| `step_limit` | 120 |
| `wall_time_limit_seconds` | 3000 |
| `max_verification_rounds` | 3 |
| `keep_last_observations` | 10 |
| `test_timeout` | 600s |
| Per-command timeout | 180s |

`STEP_LIMIT=` can also be passed to `make run`.

## Credits and license

MIT. Built on [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) © Kilian Lieret, Carlos E. Jimenez et al. (see `LICENSE.md` and `README.mini-swe-agent.md`). The upstream `minisweagent` package is kept intact under `src/minisweagent` so it can be updated easily.
