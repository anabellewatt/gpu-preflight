"""Pre-flight check for an open-source AI repo before you deploy it to a GPU pod.

GitHub answers "who keeps this alive, and can I use it?"; OSV.dev answers
"is what it installs safe?". The service joins them into one verdict.
"""
import re
import time
from datetime import datetime, timezone

from .clients import GitHubClient, OSVClient, UpstreamError, parse_manifest

_REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9-]{1,39})/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$")

CACHE_TTL = 600  # seconds; saves GitHub quota when the same repo is checked twice
_cache: dict[str, tuple[float, dict]] = {}


class InvalidInput(ValueError):
    pass


def parse_repo(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    m = _REPO_RE.match(value)
    if not m or m[2] in (".", ".."):
        raise InvalidInput("Enter a GitHub repo as owner/name or a github.com URL")
    return m[1], m[2]


def bus_factor(contributors: list[dict]) -> int:
    """Smallest number of people who together made at least half of all commits."""
    counts = sorted((c.get("contributions", 0) for c in contributors), reverse=True)
    total = sum(counts)
    if total == 0:
        return 0
    running = 0
    for i, n in enumerate(counts, start=1):
        running += n
        if running * 2 >= total:
            return i
    return len(counts)


def score(bf: int, vuln_count: int | None, days_since_push: int, archived: bool = False) -> tuple[int, str]:
    if archived:
        return 0, "Archived"
    s = 100
    s -= {0: 40, 1: 30, 2: 15}.get(bf, 0)
    if vuln_count:
        s -= min(40, vuln_count * 10)
    if days_since_push > 365:
        s -= 20
    elif days_since_push > 90:
        s -= 10
    s = max(0, s)
    label = "Healthy" if s >= 75 else "Needs attention" if s >= 50 else "At risk"
    return s, label


async def check_repo(raw: str, github: GitHubClient, osv: OSVClient, use_cache: bool = True) -> dict:
    owner, name = parse_repo(raw)
    key = f"{owner}/{name}".lower()
    hit = _cache.get(key)
    if use_cache and hit and time.time() - hit[0] < CACHE_TTL:
        return {**hit[1], "cached": True}

    # GitHub is required: without it there is nothing to report.
    repo = await github.repo(owner, name)
    contributors = await github.contributors(owner, name)
    manifest = await github.manifest(owner, name)

    # OSV is optional: if it fails we still return the GitHub half.
    security: dict = {"status": "no_manifest", "manifest": None, "checked": 0, "total_deps": 0, "vulnerable": []}
    if manifest:
        filename, text = manifest
        parsed = parse_manifest(filename, text)
        packages = parsed["pinned"]
        security.update(manifest=filename, checked=len(packages), total_deps=parsed["total"], status="ok")
        try:
            vuln_ids = await osv.query_batch(packages)
            security["vulnerable"] = [
                {**pkg, "vulns": ids[:5], "count": len(ids)}
                for pkg, ids in zip(packages, vuln_ids) if ids
            ]
        except UpstreamError as e:
            security.update(status="unavailable", message=e.message)

    pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - pushed).days
    bf = bus_factor(contributors)
    vuln_count = sum(v["count"] for v in security["vulnerable"]) if security["status"] == "ok" else None
    health, label = score(bf, vuln_count, days, repo.get("archived", False))

    report = {
        "repo": repo["full_name"],
        "url": repo["html_url"],
        "description": repo.get("description"),
        "stars": repo.get("stargazers_count", 0),
        "days_since_push": days,
        "license": (repo.get("license") or {}).get("spdx_id") or "None",
        "open_issues": repo.get("open_issues_count", 0),
        "archived": repo.get("archived", False),
        "maintainers": {
            "bus_factor": bf,
            "contributors": len(contributors),
            "top": [{"login": c.get("login"), "commits": c.get("contributions", 0)} for c in contributors[:5]],
        },
        "security": security,
        "score": health,
        "label": label,
        "cached": False,
    }
    if use_cache and security["status"] != "unavailable":  # don't cache a degraded report
        _cache[key] = (time.time(), report)
    return report
