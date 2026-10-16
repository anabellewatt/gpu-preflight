"""Thin clients for the two external APIs: GitHub REST and OSV.dev.

Every upstream problem is raised as UpstreamError so the service layer can
decide whether to fail the request or degrade gracefully.

Layout of this file:
  - UpstreamError        the one exception type for "an external API let us down"
  - _request()           shared HTTP helper: timeout, one retry, rate-limit detection, logging
  - GitHubClient         repo metadata, contributors, dependency manifest
  - OSVClient            batch vulnerability lookup for pinned packages
  - parse_manifest()     turns requirements.txt / package.json text into (name, version) pairs
  - SimulatedOutageOSV   fake OSV that always fails, for the live demo failure drill
"""
import asyncio
import json
import logging
import os
import re
import time

import httpx

GITHUB_API = "https://api.github.com"
OSV_API = "https://api.osv.dev/v1"
# Per-request timeout. 8s is long enough for a slow GitHub response but short
# enough that a hung upstream doesn't leave the user staring at a spinner.
TIMEOUT = httpx.Timeout(8.0)
# Same logger name as main.py, so all request logs share one format.
log = logging.getLogger("preflight")


class UpstreamError(Exception):
    """An external API (GitHub or OSV.dev) failed, timed out, or refused us.

    Attributes:
        service:     human-readable name of the API ("GitHub" / "OSV.dev"),
                     returned to the UI so it can say *which* dependency broke.
        message:     user-facing explanation.
        status:      HTTP status our own API should return if this error is fatal
                     (502 bad gateway by default, 504 timeout, 429 rate limit, 404).
        retry_after: seconds until the caller can retry (only for rate limits).

    main.py turns this into a JSON error response; service.py catches it for
    OSV so a vulnerability-lookup failure doesn't sink the whole report.
    """

    def __init__(self, service: str, message: str, status: int = 502, retry_after: int | None = None):
        super().__init__(message)
        self.service = service
        self.message = message
        self.status = status
        self.retry_after = retry_after


async def _request(client: httpx.AsyncClient, service: str, method: str, url: str, **kw) -> httpx.Response:
    """One retry on timeouts / 5xx. Rate limits are surfaced, never retried.

    Why this policy:
      - Timeouts and 5xx are often transient, so one quick retry is cheap and
        usually fixes it. More retries would just make the user wait longer.
      - Rate limits (403/429) won't clear in half a second, and retrying burns
        more quota, so we stop immediately and tell the user when to come back.
      - 4xx (e.g. 404) is returned as-is: the caller decides what it means
        (a missing manifest file is normal; a missing repo is an error).

    Returns the httpx.Response for anything that isn't a rate limit or a
    second consecutive failure. Raises UpstreamError otherwise.
    """
    for attempt in range(2):  # attempt 0 = first try, attempt 1 = the one retry
        started = time.perf_counter()
        try:
            resp = await client.request(method, url, timeout=TIMEOUT, **kw)
        except httpx.TimeoutException:
            log.warning("%-7s %s %s -> timeout (attempt %d)", service, method, httpx.URL(url).path, attempt + 1)
            if attempt == 0:
                continue  # try once more
            raise UpstreamError(service, f"{service} timed out", status=504)
        except httpx.HTTPError:
            # DNS failure, connection refused, TLS error, etc. Not worth retrying.
            raise UpstreamError(service, f"Could not reach {service}")
        # One log line per upstream call: service, method, path, status, latency.
        log.info("%-7s %s %s -> %d (%dms)", service, method, resp.request.url.path,
                 resp.status_code, (time.perf_counter() - started) * 1000)

        if resp.status_code in (403, 429) and _is_rate_limited(resp):
            # GitHub sends either Retry-After (secondary limits, in seconds) or
            # X-RateLimit-Reset (primary limit, as an epoch timestamp).
            reset = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset")
            raise UpstreamError(service, f"{service} rate limit reached", status=429,
                                retry_after=_seconds_until(reset))
        if resp.status_code >= 500 and attempt == 0:
            await asyncio.sleep(0.5)  # short pause before the single retry
            continue
        return resp
    # Only reached if the retry also got a 5xx.
    raise UpstreamError(service, f"{service} is unavailable")


def _is_rate_limited(resp: httpx.Response) -> bool:
    """True if the response is a rate limit rather than a permissions problem.

    GitHub uses 403 for both "you're rate limited" and "forbidden", so a 403
    only counts as a rate limit when the remaining-quota header is "0".
    """
    return resp.status_code == 429 or resp.headers.get("x-ratelimit-remaining") == "0"


def _seconds_until(value: str | None) -> int | None:
    """Normalise a rate-limit header into "seconds from now".

    Handles both formats: a plain number of seconds (Retry-After) and a Unix
    epoch timestamp (X-RateLimit-Reset). Returns None if the header is missing
    or not a number, in which case the UI just omits the "try again in" hint.
    """
    if not value or not value.isdigit():
        return None
    n = int(value)
    if n > 1_000_000_000:  # GitHub sends an epoch timestamp
        return max(0, n - int(time.time()))
    return n


# ---------------------------------------------------------------- GitHub
class GitHubClient:
    """Read-only access to the three GitHub REST endpoints we need.

    The httpx.AsyncClient is shared (created once in main.py's lifespan) so
    connections are pooled across requests instead of reopened every time.
    """

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        # Optional token: unauthenticated calls get 60 requests/hour, a token gets 5,000.
        token = os.getenv("GITHUB_TOKEN")
        # GitHub rejects requests without a User-Agent; the Accept header pins the API format.
        self.headers = {"Accept": "application/vnd.github+json", "User-Agent": "repo-health-check"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def _get(self, path: str, accept: str | None = None, **kw) -> httpx.Response:
        """GET a GitHub API path, optionally overriding the Accept header for this call only."""
        headers = {**self.headers, "Accept": accept} if accept else self.headers
        return await _request(self.client, "GitHub", "GET", f"{GITHUB_API}{path}", headers=headers, **kw)

    async def repo(self, owner: str, name: str) -> dict:
        """Repo metadata: full_name, stars, license, pushed_at, archived, etc.

        This is the one call that must succeed: a 404 here means there is
        nothing to report, so it is raised (and becomes a 404 for the user).
        GitHub returns 404 rather than 403 for private repos, hence the wording.
        """
        resp = await self._get(f"/repos/{owner}/{name}")
        if resp.status_code == 404:
            raise UpstreamError("GitHub", "Repository not found (or it is private)", status=404)
        if resp.status_code != 200:
            raise UpstreamError("GitHub", f"GitHub returned {resp.status_code}")
        return resp.json()

    async def contributors(self, owner: str, name: str) -> list[dict]:
        """Contributors sorted by commit count (GitHub sorts them for us).

        Only the first page of 100 is fetched: enough for the bus factor,
        because the people who matter for it are at the top. The UI shows
        "100+" when the page is full.
        """
        resp = await self._get(f"/repos/{owner}/{name}/contributors", params={"per_page": 100})
        if resp.status_code == 204:  # empty repo
            return []
        if resp.status_code != 200:
            raise UpstreamError("GitHub", f"GitHub returned {resp.status_code} for contributors")
        return resp.json()

    async def manifest(self, owner: str, name: str) -> tuple[str, str] | None:
        """Return (filename, text) for the first dependency manifest found.

        Checks the repo root only, Python first then Node. The "raw" Accept
        header makes GitHub return the file contents directly instead of a
        base64-encoded JSON wrapper. A 404 just means "not this file", so we
        move on to the next one; None means the repo has neither.
        """
        for filename in ("requirements.txt", "package.json"):
            resp = await self._get(f"/repos/{owner}/{name}/contents/{filename}",
                                   accept="application/vnd.github.raw+json")
            if resp.status_code == 200:
                return filename, resp.text
        return None


# ---------------------------------------------------------------- OSV.dev
class OSVClient:
    """Google's open vulnerability database (osv.dev). No auth, no key."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def query_batch(self, packages: list[dict]) -> list[list[str]]:
        """Return a list of vulnerability ids per package (same order).

        Uses /querybatch so every package is checked in ONE request rather
        than one request per dependency. OSV returns results in the same order
        as the queries, which is how the caller zips them back to packages.

        Input:  [{"name": "requests", "version": "2.19.0", "ecosystem": "PyPI"}, ...]
        Output: [["GHSA-x", "PYSEC-y"], [], ...]   (empty list = no known vulns)
        """
        if not packages:
            return []  # nothing pinned, so skip the network call entirely
        queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]}, "version": p["version"]}
                   for p in packages]
        resp = await _request(self.client, "OSV.dev", "POST", f"{OSV_API}/querybatch",
                              json={"queries": queries})
        if resp.status_code != 200:
            raise UpstreamError("OSV.dev", f"OSV.dev returned {resp.status_code}")
        results = resp.json().get("results", [])
        # A package with no vulnerabilities comes back as {} (no "vulns" key).
        return [[v["id"] for v in r.get("vulns", [])] for r in results]


# ---------------------------------------------------------------- manifest parsing
# requirements.txt: matches "name==version", allowing extras like "pkg[cuda]==1.2".
# Group 1 = package name, group 2 = exact version. Anything without "==" won't match.
_REQ_PIN = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(?:\[.*\])?\s*==\s*([A-Za-z0-9_.\-]+)")
# package.json: strips range prefixes (^ ~ >= v) and grabs the first x.y.z number.
# So "^4.18.2" is checked as 4.18.2, i.e. the lowest version the range allows.
_NPM_VERSION = re.compile(r"^[\^~>=v ]*(\d+(?:\.\d+){0,2})")


def parse_manifest(filename: str, text: str, limit: int = 50) -> dict:
    """Return {"pinned": [...], "total": n}.

    Only pinned (name, version) pairs are sent to OSV, because OSV needs a
    concrete version to give a meaningful answer. The total is kept so the
    UI can show how reproducible the install is: unpinned deps mean the
    environment you build on a GPU pod today may differ from tomorrow's.

    `limit` caps how many packages are sent to OSV, keeping the request small.
    """
    pinned: list[dict] = []
    total = 0
    if filename == "requirements.txt":
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()  # drop inline comments
            # Skip blanks, pip options (-r other.txt, -e ., --index-url) and
            # direct URL installs: none of these is a package we can look up.
            if not line or line.startswith(("-", "git+", "http")):
                continue
            total += 1  # counts every real dependency, pinned or not
            m = _REQ_PIN.match(line)
            if m:
                pinned.append({"name": m[1], "version": m[2], "ecosystem": "PyPI"})
    elif filename == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return {"pinned": [], "total": 0}  # malformed JSON: treat as no deps
        # Only runtime "dependencies"; devDependencies don't ship to the pod.
        deps = data.get("dependencies") or {}
        total = len(deps)
        for name, spec in deps.items():
            # spec can be "^1.2.3", "latest", "github:user/repo", etc.
            # Only specs that start with a version number are kept.
            m = _NPM_VERSION.match(str(spec))
            if m:
                pinned.append({"name": name, "version": m[1], "ecosystem": "npm"})
    return {"pinned": pinned[:limit], "total": total}


class SimulatedOutageOSV(OSVClient):
    """Stands in for OSV.dev during a live demo (DEMO_MODE=1 + ?simulate=osv_down).

    Same interface as OSVClient, so service.py can't tell the difference.
    That's the point: it exercises the real graceful-degradation path.
    """

    async def query_batch(self, packages: list[dict]) -> list[list[str]]:
        log.warning("OSV.dev POST /v1/querybatch -> simulated outage")
        raise UpstreamError("OSV.dev", "OSV.dev is unavailable (simulated)", status=503)
