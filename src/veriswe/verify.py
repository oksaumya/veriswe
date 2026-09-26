"""Verification gate: the harness itself checks the agent's patch before accepting a submission.

Checks (all evidence is recorded for the report):
  1. non-empty diff
  2. syntax of every changed source file
  3. reproduction script: must FAIL on the original code and PASS on the patched code
  4. targeted regression tests: tests near the changed code, compared against the original code so that
     pre-existing failures are not blamed on the patch
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from veriswe.environment import scrubbed_environ
from veriswe.workspace import Workspace

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "build", "dist", ".veriswe", ".eggs", "site-packages"}
MAX_TEST_FILES = 8
LOG_TAIL = 3000


def tail(text: str, n: int = LOG_TAIL) -> str:
    text = text or ""
    return text if len(text) <= n else "[...]\n" + text[-n:]


@dataclass
class Check:
    name: str
    status: str  # pass | fail | warn | skip
    summary: str
    log: str = ""
    evidence: list[str] = field(default_factory=list)
    """Objective proofs this check produced (e.g. tests that fail on the original code and pass after)."""
    data: dict = field(default_factory=dict)


@dataclass
class VerificationResult:
    round: int
    diff: str
    checks: list[Check] = field(default_factory=list)
    passed: bool = False
    feedback: str = ""
    score: tuple = ()
    seconds: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = list(self.score)
        return d

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)


def detect_python(repo: Path) -> str:
    if env_py := os.getenv("VERISWE_PYTHON"):
        return os.path.abspath(env_py) if os.sep in env_py else env_py
    for cand in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        if (repo / cand).exists():
            return str(repo / cand)
    return "python" if shutil.which("python") else "python3"


def _run(cmd: list[str] | str, cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str, bool]:
    """Returns (returncode, combined output, timed_out)."""
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            shell=isinstance(cmd, str),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            env={**scrubbed_environ(), "PAGER": "cat", "CI": "1", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
            start_new_session=True,
        )
        return p.returncode, p.stdout, False
    except subprocess.TimeoutExpired as e:
        out = e.output.decode("utf-8", "replace") if isinstance(e.output, bytes) else (e.output or "")
        return -9, out + f"\n[timed out after {timeout}s]", True


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    parts = set(Path(path).parts)
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        or bool(parts & {"tests", "test", "testing", "__tests__"})
    )


class Verifier:
    def __init__(self, ws: Workspace, *, test_timeout: int = 600, repro_timeout: int = 180):
        self.ws = ws
        self.test_timeout = test_timeout
        self.repro_timeout = repro_timeout
        self._test_files_cache: list[Path] | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def repo(self) -> Path:
        return self.ws.repo

    def _on_original(self, patch: str, fn):
        """Run fn() with the patch temporarily reversed; always re-apply."""
        self.ws.reverse(patch)
        try:
            return fn()
        finally:
            try:
                self.ws.apply(patch)
            except Exception:
                # Last resort: rebuild from the saved patch file
                (self.ws.scratch / "emergency.patch").write_text(patch)
                raise

    def _all_test_files(self) -> list[Path]:
        if self._test_files_cache is None:
            found = []
            for root, dirs, files in os.walk(self.repo):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
                for f in files:
                    if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")):
                        found.append(Path(root) / f)
                if len(found) > 20000:
                    break
            self._test_files_cache = found
        return self._test_files_cache

    # ------------------------------------------------------------------ checks
    def check_syntax(self, changed: list[str]) -> Check:
        errors = []
        for rel in changed:
            p = self.repo / rel
            if not p.is_file():
                continue
            try:
                if p.suffix == ".py":
                    compile(p.read_text(errors="replace"), str(p), "exec")
                elif p.suffix == ".json":
                    json.loads(p.read_text())
            except SyntaxError as e:
                errors.append(f"{rel}:{e.lineno}: SyntaxError: {e.msg}")
            except ValueError as e:
                errors.append(f"{rel}: {e}")
        if errors:
            return Check("syntax", "fail", f"{len(errors)} file(s) do not parse", "\n".join(errors))
        return Check("syntax", "pass", f"{len(changed)} changed file(s) parse")

    def check_repro(self, patch: str, python: str) -> tuple[Check, Check | None]:
        """Returns (after-check, before-check)."""
        if not self.ws.repro_path.exists():
            return Check("repro_after", "skip", "no reproduce_issue.py written"), None
        cmd = [python, str(self.ws.repro_path)]
        rc_a, out_a, to_a = _run(cmd, self.repo, self.repro_timeout)
        after = Check(
            "repro_after",
            "pass" if rc_a == 0 else "fail",
            "reproduction passes on patched code" if rc_a == 0 else f"reproduction FAILS on patched code (rc={rc_a})",
            tail(out_a),
        )
        rc_b, out_b, to_b = self._on_original(patch, lambda: _run(cmd, self.repo, self.repro_timeout))
        before = Check(
            "repro_before",
            "pass" if rc_b != 0 else "warn",
            "reproduction fails on original code (bug reproduced)"
            if rc_b != 0
            else "reproduction also PASSES on the original code: it does not reproduce the issue",
            tail(out_b),
        )
        return after, before

    def select_python_tests(self, changed: list[str]) -> list[str]:
        chosen: list[str] = []

        def add(p: Path | str):
            rel = str(Path(p).resolve().relative_to(self.repo)) if Path(p).is_absolute() else str(p)
            if rel not in chosen and (self.repo / rel).is_file():
                chosen.append(rel)

        # 1) test files the agent touched
        for rel in changed:
            if rel.endswith(".py") and _is_test_file(rel):
                add(rel)
        src = [rel for rel in changed if rel.endswith(".py") and not _is_test_file(rel)]
        stems = {Path(rel).stem for rel in src if Path(rel).stem not in {"__init__", "setup", "conftest"}}
        if not stems:
            return chosen[:MAX_TEST_FILES]
        all_tests = self._all_test_files()
        # 2) name matches: test_<stem>.py, <stem>_test.py, test_<stem>_*.py
        for t in all_tests:
            n = t.stem
            if any(n in (f"test_{s}", f"{s}_test") or n.startswith(f"test_{s}_") for s in stems):
                add(t)
        # 3) tests importing the changed module (dotted path suffix match), if still few
        if len(chosen) < 3:
            mods = set()
            for rel in src:
                parts = list(Path(rel).with_suffix("").parts)
                if parts and parts[0] in {"src", "lib"}:
                    parts = parts[1:]
                if len(parts) >= 2:
                    mods.add(".".join(parts[-2:]))
            pats = [re.compile(rf"(from|import)\s+[\w.]*{re.escape(m)}\b") for m in mods]
            for t in all_tests:
                if len(chosen) >= MAX_TEST_FILES:
                    break
                try:
                    txt = t.read_text(errors="ignore")
                except OSError:
                    continue
                if any(p.search(txt) for p in pats):
                    add(t)
        # 4) nothing specific: fall back to tests importing the top-level package of the changed code
        if not chosen:
            pkgs = set()
            for rel in src:
                parts = list(Path(rel).parts)
                if parts and parts[0] in {"src", "lib"}:
                    parts = parts[1:]
                if len(parts) >= 2:
                    pkgs.add(parts[0])
            pats = [re.compile(rf"^\s*(from|import)\s+{re.escape(p)}\b", re.M) for p in pkgs]
            for t in sorted(all_tests, key=lambda p: len(p.parts)):
                if len(chosen) >= MAX_TEST_FILES:
                    break
                try:
                    txt = t.read_text(errors="ignore")
                except OSError:
                    continue
                if any(p.search(txt) for p in pats):
                    add(t)
            # 5) indirect imports: a test package's __init__/conftest/helper imports the code (e.g. `from . import lib`)
            if not chosen and pats:
                for d in sorted({t.parent for t in all_tests}, key=lambda p: len(p.parts)):
                    helpers = [h for h in d.glob("*.py") if not (h.name.startswith("test_") or h.name.endswith("_test.py"))]
                    if any(p.search(h.read_text(errors="ignore")) for h in helpers for p in pats):
                        for t in sorted(x for x in all_tests if x.parent == d):
                            if len(chosen) < MAX_TEST_FILES:
                                add(t)
        return chosen[:MAX_TEST_FILES]

    def _pytest(self, python: str, files: list[str], tag: str) -> tuple[dict[str, str], str, int]:
        """Run pytest on files. Returns ({test_id: outcome}, output, returncode)."""
        xml = self.ws.scratch / f"junit_{tag}.xml"
        xml.unlink(missing_ok=True)
        base = [python, "-m", "pytest", "-q", "-rf", "--no-header", "-p", "no:cacheprovider", f"--junitxml={xml}"]
        env = getattr(self, "_test_env", None)
        rc, out, _ = _run(base + files, self.repo, self.test_timeout, env)
        if rc == 4 and "unrecognized arguments" in out:  # repo addopts need plugins we lack
            rc, out, _ = _run(base + ["-o", "addopts="] + files, self.repo, self.test_timeout, env)
        results: dict[str, str] = {}
        if xml.exists():
            try:
                for tc in ET.parse(xml).getroot().iter("testcase"):
                    tid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
                    tags = {child.tag for child in tc}
                    results[tid] = "failed" if tags & {"failure", "error"} else ("skipped" if "skipped" in tags else "passed")
            except ET.ParseError:
                pass
        return results, out, rc

    def check_tests(self, patch: str, changed: list[str], python: str) -> Check:
        py_changed = [c for c in changed if c.endswith(".py")]
        if py_changed:
            files = self.select_python_tests(changed)
            if not files:
                return Check("tests", "skip", "no related test files found")
            rc, _, _ = _run([python, "-c", "import pytest"], self.repo, 60)
            if rc != 0:
                # The project's interpreter lacks pytest: fall back to VeriSWE's own interpreter (always has pytest)
                # with the repo importable. Before/after runs use the same interpreter, so the comparison stays fair.
                python = sys.executable
                src_paths = [str(self.repo / d) for d in ("src", "lib") if (self.repo / d).is_dir()]
                self._test_env = {"PYTHONPATH": os.pathsep.join([str(self.repo), *src_paths])}
            after, out_a, rc_a = self._pytest(python, files, "after")
            if not after:
                # collection error etc. - see whether it also happens on the original code
                _, out_b, rc_b = self._on_original(patch, lambda: self._pytest(python, self._existing(files), "before"))
                if rc_b == rc_a:
                    return Check("tests", "warn", f"test run errors both before and after the patch (rc={rc_a}); inconclusive", tail(out_a))
                return Check("tests", "fail", f"test run broke after the patch (rc={rc_a}, was {rc_b})", tail(out_a))
            # Always compare with the original code: regressions AND evidence (tests that go fail -> pass).
            # Test files the patch adds do not exist on the original code, so their tests count as "not passing before".
            before, _, _ = self._on_original(patch, lambda: self._pytest(python, self._existing(files), "before"))
            failed_after = {t for t, o in after.items() if o == "failed"}
            n_pass = sum(1 for o in after.values() if o == "passed")
            new_failures = sorted(t for t in failed_after if before.get(t) == "passed")
            new_tests_failing = sorted(t for t in failed_after if t not in before)
            preexisting = sorted(t for t in failed_after if before.get(t) == "failed")
            fail_to_pass = sorted(t for t, o in after.items() if o == "passed" and before.get(t) != "passed")
            pass_both = sum(1 for t, o in after.items() if o == "passed" and before.get(t) == "passed")
            data = {
                "files": files,
                "passed": n_pass,
                "failed": len(failed_after),
                "skipped": sum(1 for o in after.values() if o == "skipped"),
                "total": len(after),
                "pass_both": pass_both,
                "fail_to_pass": fail_to_pass,
            }
            if new_failures:
                return Check(
                    "tests",
                    "fail",
                    f"{len(new_failures)} test(s) that passed on the original code now FAIL: {', '.join(new_failures[:8])}",
                    tail(out_a),
                    data=data,
                )
            if new_tests_failing:
                return Check(
                    "tests",
                    "fail",
                    f"{len(new_tests_failing)} newly added test(s) fail: {', '.join(new_tests_failing[:8])}",
                    tail(out_a),
                    data=data,
                )
            summary = f"no regressions: {n_pass} related tests pass ({len(files)} files)"
            if fail_to_pass:
                summary += f"; {len(fail_to_pass)} test(s) fail on the original code and pass now"
            if preexisting:
                summary += f"; {len(preexisting)} were already failing before the patch"
            return Check(
                "tests",
                "pass",
                summary,
                tail(out_a, 1500),
                evidence=[f"test fails on original code, passes now: {t}" for t in fail_to_pass],
                data=data,
            )
        return self._generic_tests(patch, changed)

    def _existing(self, files: list[str]) -> list[str]:
        return [f for f in files if (self.repo / f).is_file()]

    def _generic_tests(self, patch: str, changed: list[str]) -> Check:
        """Non-Python projects: run the natural test command and compare with the original code by exit code."""
        cmd = None
        if any(c.endswith(".go") for c in changed) and shutil.which("go"):
            pkgs = sorted({"./" + str(Path(c).parent) for c in changed if c.endswith(".go")})
            cmd = ["go", "test", *pkgs]
        elif any(c.endswith(".rs") for c in changed) and shutil.which("cargo") and (self.repo / "Cargo.toml").exists():
            cmd = ["cargo", "test", "--quiet"]
        elif (self.repo / "package.json").exists() and shutil.which("npm"):
            try:
                scripts = json.loads((self.repo / "package.json").read_text()).get("scripts", {})
            except Exception:
                scripts = {}
            if "test" in scripts and (self.repo / "node_modules").exists():
                cmd = ["npm", "test", "--silent"]
        if not cmd:
            return Check("tests", "skip", "no test runner detected for the changed files")
        rc_a, out_a, _ = _run(cmd, self.repo, self.test_timeout)
        rc_b, _, _ = self._on_original(patch, lambda: _run(cmd, self.repo, self.test_timeout))
        if rc_a == 0:
            ev = [f"`{' '.join(cmd)}` fails on the original code and passes now"] if rc_b != 0 else []
            return Check("tests", "pass", f"`{' '.join(cmd)}` passes", tail(out_a, 1500), evidence=ev, data={"pass_both": int(rc_b == 0)})
        if rc_b != 0:
            return Check("tests", "warn", f"`{' '.join(cmd)}` fails before and after the patch; inconclusive", tail(out_a))
        return Check("tests", "fail", f"`{' '.join(cmd)}` passed on the original code but FAILS with the patch", tail(out_a))

    # ------------------------------------------------------------------ main entry
    def verify(self, round_no: int, *, task_type: str = "") -> VerificationResult:
        t0 = time.time()
        patch = self.ws.diff()
        res = VerificationResult(round=round_no, diff=patch)
        if not patch.strip():
            res.checks.append(Check("diff", "fail", "no changes to the repository"))
            res.feedback = (
                "HARNESS VERIFICATION FAILED: your submission contains no changes to the repository "
                "(reproduce_issue.py is excluded from the patch). Implement the fix in the source files, then submit again."
            )
            res.score = (0, 0, 0, 0, 0)
            res.seconds = time.time() - t0
            return res
        changed = self.ws.changed_files()
        python = detect_python(self.repo)
        res.checks.append(Check("diff", "pass", f"{len(changed)} file(s) changed: {', '.join(changed[:10])}"))
        syntax = self.check_syntax(changed)
        res.checks.append(syntax)
        try:
            repro_after, repro_before = self.check_repro(patch, python)
        except Exception as e:  # the gate must never crash the run
            repro_after, repro_before = Check("repro_after", "skip", f"harness error while running reproduction: {e}"), None
        res.checks.append(repro_after)
        if repro_before:
            res.checks.append(repro_before)
        if syntax.status == "fail":
            tests = Check("tests", "skip", "skipped: syntax errors")
        else:
            try:
                tests = self.check_tests(patch, changed, python)
            except Exception as e:
                tests = Check("tests", "warn", f"harness error while running tests: {type(e).__name__}: {e}")
        res.checks.append(tests)
        touched_tests = [c for c in changed if _is_test_file(c)]
        if touched_tests:
            res.checks.append(Check("test_files_modified", "warn", f"patch modifies/adds test files: {', '.join(touched_tests[:6])}"))

        # ---- objective evidence: something that FAILS on the original code and PASSES with the change
        evidence: list[str] = []
        strong_repro = bool(repro_before and repro_before.status == "pass" and repro_after.status == "pass")
        if strong_repro:
            evidence.append("reproduce_issue.py fails on the original code and passes with the change")
        evidence += tests.evidence
        if task_type == "refactor" and tests.status == "pass" and tests.data.get("pass_both", 0) > 0:
            evidence.append(f"refactor: {tests.data['pass_both']} related test(s) pass before and after (behaviour preserved)")
        res.checks.append(
            Check("evidence", "pass", "; ".join(evidence[:4]) + (f" (+{len(evidence) - 4} more)" if len(evidence) > 4 else ""))
            if evidence
            else Check("evidence", "fail", "no objective evidence that the change achieves the task")
        )

        ok_syntax = syntax.status == "pass"
        ok_tests = tests.status in ("pass", "skip", "warn")
        res.score = (int(ok_syntax), int(ok_tests), int(bool(evidence)), int(repro_after.status != "fail"), len(evidence))

        problems = []
        if not ok_syntax:
            problems.append(("Syntax errors in changed files", syntax))
        if repro_after.status == "fail":
            problems.append(("Your reproduce_issue.py still fails on the changed code", repro_after))
        if tests.status == "fail":
            problems.append(("Regression tests", tests))
        if not evidence:
            hint = (
                "No objective evidence that your change achieves the task. The harness does not accept a claim of "
                "success. Provide at least one of: (a) `reproduce_issue.py` in the repo root that FAILS on the original "
                "code and PASSES now (check with `check_repro`); (b) a test in the project's test suite that fails on "
                "the original code and passes now (features, test fixes); (c) for a declared refactor "
                "(TASK TYPE: refactor), related existing tests that pass before and after"
            )
            problems.append((hint, repro_before if repro_before and repro_before.status == "warn" else None))
        res.passed = not problems
        if problems:
            parts = ["HARNESS VERIFICATION FAILED - your submission was not accepted. Problems:"]
            for i, (msg, chk) in enumerate(problems, 1):
                parts.append(f"\n{i}. {msg}.")
                if chk is not None:
                    parts.append(f"   Result: {chk.summary}")
                    if chk.log:
                        parts.append(f"   Log (tail):\n{chk.log}")
            parts.append("\nFix these, re-check yourself, then submit again with `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`.")
            res.feedback = "\n".join(parts)
        res.seconds = time.time() - t0
        return res
