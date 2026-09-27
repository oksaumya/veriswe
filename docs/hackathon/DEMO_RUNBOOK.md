# VeriSWE demo runbook

**Story:** VeriSWE is not an LLM wrapper. The model provides the reasoning. The harness provides controlled action, feedback, recovery, objective verification and measured telemetry.

The flow to show is **TASK → EXPLORE → ACT → TEST → FAILURE → RECOVER → VERIFY → REPORT**.

| Component | Role |
|---|---|
| Foundation model | Reasoning |
| Harness | Autonomy |
| Tools | Action |
| Repository | Environment |
| Tests | Feedback |
| Recovery | Resilience |
| Verification | Trust |
| Telemetry | Observability |

## 0. Preparation (once, about 1 minute)

```bash
git clone <repo> && cd <repo>
export AI_API_KEY="<key>"   # DeepSeek / Qwen key; the host is detected from the key itself
make setup
make test                  # 108 offline tests pass
```

## 1. Guaranteed recovery demo (offline, about 10 seconds, no API key)

```bash
make demo-recovery          # TUI;   add HEADLESS=1 for plain output
```

The model's decisions are replayed from a script. Everything else is real: the tools, the project's tests, the verification gate, telemetry and the report.

What the audience sees:

1. **TASK:** the issue "median() is wrong for even-length inputs". The agent declares `TASK TYPE: bug`.
2. **EXPLORE:** `search` and `view` locate `calc/stats.py`.
3. **ACT and TEST:** the agent writes `reproduce_issue.py`, and `check_repro` shows it **fails on the original code**. This is the evidence, established before any fix.
4. **FAILURE:** the agent submits a plausible but wrong fix. The harness gate **REJECTS** it: `test_median_odd` passed before and fails now, and the reproduction fails. The phase bar drops back to *Fix*.
5. **RECOVER:** the agent reads the gate's logs, runs `undo_edit`, and changes strategy.
6. **VERIFY:** `check_repro` returns `VERDICT: OK`. The gate re-runs everything independently and **ACCEPTS** in round 2.
7. **REPORT:** the **AGENT EXECUTION** box shows 2 verification rounds, 1 failure and the recovery events. Press `p` for the patch.
   The demo repo is tiny, so *Context reduced* is about 0 here. On real repositories, output truncation and observation masking cut
   the context substantially, and `telemetry_summary.json` shows exactly how much (`context_chars_before` / `sent` / `reduced`).

## 2. Live run with the real model (1–3 minutes)

```bash
make run                    # paste an issue URL or text, press Ctrl+S
# or
make run ISSUE=https://github.com/<owner>/<repo>/issues/<n>
make run TASK="Add input validation to parse_config()" REPO=/path/to/repo
```

Point out, while it runs:
- the phase bar
- the live Patch tab
- the Run panel (tokens and cache %)
- the Verification checklist filling in when the agent submits

## 3. The "we don't trust the model" moment

Show a run where there is no objective evidence. For example, pass a vague task, or an issue whose fix has no test. The result is **UNVERIFIED**: the patch is kept for inspection, but it is never presented as success. This is the core difference from a chat-based agent.

## 4. Artifacts to open after any run

```
runs/<timestamp>-<repo>/
├── report.md               status, AGENT EXECUTION box, evidence table, verification history, patch
├── patch.diff              the change (also left applied in the repository)
├── telemetry.jsonl         every event: model, tool calls, results, failures, recoveries, verification
├── telemetry_summary.json  counts, context chars before/sent/reduced, tokens
├── trajectory.json         the full conversation (secrets redacted)
└── evidence/               the reproduction script and JUnit XML files from the harness test runs
```

## 5. One-line answers for likely questions

- **"How do you know it's correct?"** The harness, not the model, proves it: something must fail on the original code and pass after the change, with no regressions against the original test results.
- **"What if the verifier breaks?"** That counts as a failed round with feedback. It is never accepted.
- **"Efficiency?"** See `context_chars_reduced` and the cache % in telemetry. Observation masking keeps the prompt prefix stable, so caching works.
- **"Why not multiple agents?"** One orchestrated loop with a strict external verifier gives the separation of concerns without extra coordination cost. See the final audit, section 5.
