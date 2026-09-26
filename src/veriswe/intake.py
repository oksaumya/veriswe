"""Turn whatever the evaluator hands us (issue URL, text, file, stdin) into a task + a local repo."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

ISSUE_URL_RE = re.compile(r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<num>\d+)")
REPO_URL_RE = re.compile(r"^(https?://|git@)[^\s]+$")


@dataclass
class Issue:
    title: str
    body: str
    url: str = ""
    repo_url: str = ""

    def as_task(self) -> str:
        head = f"# {self.title}\n\n" if self.title else ""
        src = f"\n\n(Source: {self.url})" if self.url else ""
        return f"{head}{self.body.strip()}{src}"


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "veriswe"}
    if tok := os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_github_issue(url: str, max_comments: int = 10) -> Issue:
    """REST API -> `gh` CLI -> scrape the issue page (survives unauthenticated API rate limits)."""
    m = ISSUE_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a GitHub issue URL: {url}")
    errors = []
    for fetch in (_fetch_via_api, _fetch_via_gh, _fetch_via_html):
        try:
            issue = fetch(m["owner"], m["repo"], m["num"], max_comments)
            if issue and (issue.title or issue.body):
                issue.url, issue.repo_url = url, f"https://github.com/{m['owner']}/{m['repo']}"
                return issue
        except Exception as e:
            errors.append(f"{fetch.__name__}: {e}")
    raise RuntimeError(f"Could not fetch {url}: " + "; ".join(errors))


def _fetch_via_gh(owner: str, repo: str, num: str, max_comments: int) -> Issue | None:
    import json
    import shutil

    if not shutil.which("gh"):
        return None
    out = subprocess.run(
        ["gh", "issue", "view", num, "-R", f"{owner}/{repo}", "--json", "title,body,comments"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout  # fmt: skip
    d = json.loads(out)
    body = d.get("body") or ""
    comments = [f"**{c.get('author', {}).get('login', '?')}**: {c.get('body') or ''}" for c in d.get("comments", [])[:max_comments]]
    if comments:
        body += "\n\n## Discussion\n\n" + "\n\n---\n\n".join(comments)
    return Issue(title=d.get("title", ""), body=body)


def _fetch_via_html(owner: str, repo: str, num: str, max_comments: int) -> Issue | None:
    import json

    r = requests.get(
        f"https://github.com/{owner}/{repo}/issues/{num}", headers={"User-Agent": "Mozilla/5.0 (veriswe)"}, timeout=30
    )
    r.raise_for_status()
    for m in re.finditer(r'<script type="application/json" data-target="react-app.embeddedData">(.*?)</script>', r.text, re.S):
        try:
            data = json.loads(m.group(1))
            issue = data["payload"]["preloadedQueries"][0]["result"]["data"]["repository"]["issue"]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        body = issue.get("body") or ""
        comments = []
        for edge in (issue.get("frontTimelineItems") or {}).get("edges", []):
            node = edge.get("node") or {}
            if node.get("body") and len(comments) < max_comments:
                author = (node.get("author") or {}).get("login", "?")
                comments.append(f"**{author}**: {node['body']}")
        if comments:
            body += "\n\n## Discussion\n\n" + "\n\n---\n\n".join(comments)
        return Issue(title=issue.get("title", ""), body=body)
    return None


def _fetch_via_api(owner: str, repo: str, num: str, max_comments: int) -> Issue:
    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{num}"
    r = requests.get(api, headers=_gh_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    body = data.get("body") or ""
    if data.get("comments"):
        try:
            cr = requests.get(api + "/comments", headers=_gh_headers(), timeout=30, params={"per_page": max_comments})
            cr.raise_for_status()
            comments = [f"**{c['user']['login']}**: {c.get('body') or ''}" for c in cr.json()[:max_comments]]
            if comments:
                body += "\n\n## Discussion\n\n" + "\n\n---\n\n".join(comments)
        except Exception:
            pass
    return Issue(title=data.get("title", ""), body=body)


def read_issue(spec: str) -> Issue:
    """spec can be: a GitHub issue URL, '@path/to/file', '-' (stdin), a path to an existing file, or raw text."""
    spec = spec.strip()
    if ISSUE_URL_RE.fullmatch(spec) or (ISSUE_URL_RE.match(spec) and "\n" not in spec):
        return fetch_github_issue(spec)
    if spec == "-":
        return Issue(title="", body=sys.stdin.read())
    if spec.startswith("@"):
        return Issue(title="", body=Path(spec[1:]).expanduser().read_text())
    if "\n" not in spec and len(spec) < 4096:
        p = Path(spec).expanduser()
        try:
            if p.is_file():
                return Issue(title="", body=p.read_text())
        except OSError:
            pass
    # Raw text that *mentions* an issue URL: keep the text, remember the repo.
    m = ISSUE_URL_RE.search(spec)
    return Issue(title="", body=spec, repo_url=f"https://github.com/{m['owner']}/{m['repo']}" if m else "")


def resolve_repo(repo_spec: str | None, issue: Issue, workspace_root: Path) -> Path:
    """Return a local path to the target repository, cloning it if a URL was given."""
    spec = (repo_spec or "").strip()
    if spec and not REPO_URL_RE.match(spec):
        p = Path(spec).expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"Repository path does not exist: {p}")
        return p
    url = spec or issue.repo_url
    if not url:
        raise ValueError("No repository given. Pass REPO=<path-or-git-url> (or use a GitHub issue URL).")
    name = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    dest = workspace_root / name
    if dest.exists() and (dest / ".git").exists():
        return dest
    workspace_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", url, str(dest)], check=True)
    return dest
