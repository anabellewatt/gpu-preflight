"""Pre-flight check for an open-source AI repo before you deploy it to a GPU pod.

GitHub answers "who keeps this alive, and can I use it?"; OSV.dev answers
"is what it installs safe?". The service joins them into one verdict.

This module holds the business logic and knows nothing about HTTP or
FastAPI: it takes clients in, returns a plain dict out. That keeps it easy
to test and means the scoring rules all live in one place.
"""
import re
import time
from datetime import datetime, timezone

from .clients import GitHubClient, OSVClient, UpstreamError, parse_manifest

# Accepts "owner/name", "https://github.com/owner/name", with optional ".git" or trailing "/".
# Group 1 = owner (GitHub usernames: letters, digits, hyphens, max 39 chars).
# Group 2 = repo name (letters, digits, _ . -, max 100 chars). The "?" makes it
# non-greedy so a trailing ".git" is stripped rather than swallowed into the name.
# Anchored with ^...$ so anything else (other hosts, extra path segments,
# "../" tricks) is rejected before we ever call GitHub.
_REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9-]{1,39})/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$")

CACHE_TTL = 600  # seconds; saves GitHub quota when the same repo is checked twice
# In-memory cache: "owner/name" (lowercased) -> (time stored, report dict).
# Per-process and lost on restart, which is fine for a single-instance demo.
# With several instances you'd move this to Redis or similar.
_cache: dict[str, tuple[float, dict]] = {}


class InvalidInput(ValueError):
    """The user typed something that isn't a GitHub repo. main.py returns 400."""
    pass


def parse_repo(value: str) -> tuple[str, str]:
    """Turn user input into (owner, name), or raise InvalidInput.

    "." and ".." are rejected explicitly: the regex allows dots in names, but
    those two would turn into path traversal in the GitHub API URL.
    """
    value = (value or "").strip()
    m = _REPO_RE.match(value)
    if not m or m[2] in (".", ".."):
        raise InvalidInput("Enter a GitHub repo as owner/name or a github.com URL")
    return m[1], m[2]


def bus_factor(contributors: list[dict]) -> int:
    """Smallest number of people who together made at least half of all commits.

    Example: commits [90, 10] -> 1 (one person did 90%, so losing them is fatal).
             commits [5, 5, 5, 5] -> 2.
    Returns 0 for a repo with no contributors.
    """
    # Sort biggest contributor first, then add people up until we pass 50%.
    counts = sorted((c.get("contributions", 0) for c in contributors), reverse=True)
    total = sum(counts)
    if total == 0:
        return 0
    running = 0
    for i, n in enumerate(counts, start=1):
        running += n
        if running * 2 >= total:  # i.e. running >= total / 2, without floats
            return i
    return len(counts)  # not reachable in practice; keeps the type checker happy


def score(bf: int, vuln_count: int | None, days_since_push: int, archived: bool = False) -> tuple[int, str]:
    """Combine the signals into a 0-100 score and a label.

    Start at 100 and subtract penalties:
      - bus factor:  0 -> -40, 1 -> -30, 2 -> -15, 3+ -> 0
      - advisories:  -10 each, capped at -40
      - staleness:   no push in 90+ days -> -10, in 365+ days -> -20
    Archived repos short-circuit to 0, since nobody will fix anything.

    vuln_count is None when OSV was unavailable: we don't penalise what we
    couldn't check (the UI shows a banner instead).

    Labels: 75+ Healthy, 50-74 Needs attention, below 50 At risk.
    """
    if archived:
        return 0, "Archived"
    s = 100
    s -= {0: 40, 1: 30, 2: 15}.get(bf, 0)  # .get(..., 0): no penalty for 3+
    if vuln_count:  # falsy for both None and 0, so neither is penalised
        s -= min(40, vuln_count * 10)
    if days_since_push > 365:
        s -= 20
    elif days_since_push > 90:
        s -= 10
    s = max(0, s)
    label = "Healthy" if s >= 75 else "Needs attention" if s >= 50 else "At risk"
    return s, label


async def check_repo(raw: str, github: GitHubClient, osv: OSVClient, use_cache: bool = True) -> dict:
    """Run the full pre-flight check for one repo and return the report dict.

    Steps:
      1. Validate input (InvalidInput -> 400, before any network call).
      2. Return a cached report if we have a fresh one.
      3. GitHub: repo metadata, contributors, dependency manifest. Required,
         so UpstreamError propagates and main.py turns it into an error response.
      4. OSV: look up pinned dependencies. Optional: on failure we mark
         security as "unavailable" and still return the GitHub half.
      5. Score it, build the report, cache it (unless it's degraded).

    The clients are passed in (not created here) so main.py can swap in
    SimulatedOutageOSV for the demo, and tests can mock the HTTP layer.
    """
    owner, name = parse_repo(raw)
    key = f"{owner}/{name}".lower()  # GitHub names are case-insensitive
    hit = _cache.get(key)
    if use_cache and hit and time.time() - hit[0] < CACHE_TTL:
        return {**hit[1], "cached": True}  # copy, so the stored report isn't mutated

    # GitHub is required: without it there is nothing to report.
    # These run one after another for simplicity. They're independent, so
    # asyncio.gather() could run them concurrently to cut latency.
    repo = await github.repo(owner, name)
    contributors = await github.contributors(owner, name)
    manifest = await github.manifest(owner, name)

    # OSV is optional: if it fails we still return the GitHub half.
    # security["status"] is one of:
    #   "no_manifest"  no requirements.txt / package.json at the repo root
    #   "ok"           OSV answered (vulnerable may still be empty = all clear)
    #   "unavailable"  OSV failed; the UI shows a warning banner
    security: dict = {"status": "no_manifest", "manifest": None, "checked": 0, "total_deps": 0, "vulnerable": []}
    if manifest:
        filename, text = manifest
        parsed = parse_manifest(filename, text)
        packages = parsed["pinned"]
        security.update(manifest=filename, checked=len(packages), total_deps=parsed["total"], status="ok")
        try:
            vuln_ids = await osv.query_batch(packages)
            # zip() pairs each package with its vuln list (OSV preserves order).
            # Keep only packages that have at least one advisory; show up to 5 ids each.
            security["vulnerable"] = [
                {**pkg, "vulns": ids[:5], "count": len(ids)}
                for pkg, ids in zip(packages, vuln_ids) if ids
            ]
        except UpstreamError as e:
            security.update(status="unavailable", message=e.message)

    # GitHub timestamps end in "Z"; older Pythons' fromisoformat needs "+00:00".
    pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - pushed).days
    bf = bus_factor(contributors)
    # None (not 0) when OSV was down, so score() knows "unknown" vs "clean".
    vuln_count = sum(v["count"] for v in security["vulnerable"]) if security["status"] == "ok" else None
    health, label = score(bf, vuln_count, days, repo.get("archived", False))

    # The JSON shape the frontend (static/index.html) renders.
    report = {
        # Use GitHub's canonical name: if the repo was renamed, the redirect gives us the new one.
        "repo": repo["full_name"],
        "url": repo["html_url"],
        "description": repo.get("description"),
        "stars": repo.get("stargazers_count", 0),
        "days_since_push": days,
        # spdx_id is e.g. "MIT"; repos with no license have license = null.
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
    # Don't cache a degraded report: once OSV recovers, the next check should
    # get the full answer rather than 10 minutes of "unavailable".
    if use_cache and security["status"] != "unavailable":
        _cache[key] = (time.time(), report)
    return report
