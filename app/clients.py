"""Thin clients for the two external APIs: GitHub REST and OSV.dev.

Every upstream problem is raised as UpstreamError so the service layer can
decide whether to fail the request or degrade gracefully.
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
TIMEOUT = httpx.Timeout(8.0)
log = logging.getLogger("preflight")


class UpstreamError(Exception):
    def __init__(self, service: str, message: str, status: int = 502, retry_after: int | None = None):
        super().__init__(message)
        self.service = service
        self.message = message
        self.status = status
        self.retry_after = retry_after


async def _request(client: httpx.AsyncClient, service: str, method: str, url: str, **kw) -> httpx.Response:
    """One retry on timeouts / 5xx. Rate limits are surfaced, never retried."""
    for attempt in range(2):
        started = time.perf_counter()
        try:
            resp = await client.request(method, url, timeout=TIMEOUT, **kw)
        except httpx.TimeoutException:
            log.warning("%-7s %s %s -> timeout (attempt %d)", service, method, httpx.URL(url).path, attempt + 1)
            if attempt == 0:
                continue
            raise UpstreamError(service, f"{service} timed out", status=504)
        except httpx.HTTPError:
            raise UpstreamError(service, f"Could not reach {service}")
        log.info("%-7s %s %s -> %d (%dms)", service, method, resp.request.url.path,
                 resp.status_code, (time.perf_counter() - started) * 1000)

        if resp.status_code in (403, 429) and _is_rate_limited(resp):
            reset = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset")
            raise UpstreamError(service, f"{service} rate limit reached", status=429,
                                retry_after=_seconds_until(reset))
        if resp.status_code >= 500 and attempt == 0:
            await asyncio.sleep(0.5)
            continue
        return resp
    raise UpstreamError(service, f"{service} is unavailable")


def _is_rate_limited(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or resp.headers.get("x-ratelimit-remaining") == "0"


def _seconds_until(value: str | None) -> int | None:
    if not value or not value.isdigit():
        return None
    n = int(value)
    if n > 1_000_000_000:  # GitHub sends an epoch timestamp
        return max(0, n - int(time.time()))
    return n


# ---------------------------------------------------------------- GitHub
class GitHubClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        token = os.getenv("GITHUB_TOKEN")
        self.headers = {"Accept": "application/vnd.github+json", "User-Agent": "repo-health-check"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def _get(self, path: str, accept: str | None = None, **kw) -> httpx.Response:
        headers = {**self.headers, "Accept": accept} if accept else self.headers
        return await _request(self.client, "GitHub", "GET", f"{GITHUB_API}{path}", headers=headers, **kw)

    async def repo(self, owner: str, name: str) -> dict:
        resp = await self._get(f"/repos/{owner}/{name}")
        if resp.status_code == 404:
            raise UpstreamError("GitHub", "Repository not found (or it is private)", status=404)
        if resp.status_code != 200:
            raise UpstreamError("GitHub", f"GitHub returned {resp.status_code}")
        return resp.json()

    async def contributors(self, owner: str, name: str) -> list[dict]:
        resp = await self._get(f"/repos/{owner}/{name}/contributors", params={"per_page": 100})
        if resp.status_code == 204:  # empty repo
            return []
        if resp.status_code != 200:
            raise UpstreamError("GitHub", f"GitHub returned {resp.status_code} for contributors")
        return resp.json()

    async def manifest(self, owner: str, name: str) -> tuple[str, str] | None:
        """Return (filename, text) for the first dependency manifest found."""
        for filename in ("requirements.txt", "package.json"):
            resp = await self._get(f"/repos/{owner}/{name}/contents/{filename}",
                                   accept="application/vnd.github.raw+json")
            if resp.status_code == 200:
                return filename, resp.text
        return None


# ---------------------------------------------------------------- OSV.dev
class OSVClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def query_batch(self, packages: list[dict]) -> list[list[str]]:
        """Return a list of vulnerability ids per package (same order)."""
        if not packages:
            return []
        queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]}, "version": p["version"]}
                   for p in packages]
        resp = await _request(self.client, "OSV.dev", "POST", f"{OSV_API}/querybatch",
                              json={"queries": queries})
        if resp.status_code != 200:
            raise UpstreamError("OSV.dev", f"OSV.dev returned {resp.status_code}")
        results = resp.json().get("results", [])
        return [[v["id"] for v in r.get("vulns", [])] for r in results]


# ---------------------------------------------------------------- manifest parsing
_REQ_PIN = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(?:\[.*\])?\s*==\s*([A-Za-z0-9_.\-]+)")
_NPM_VERSION = re.compile(r"^[\^~>=v ]*(\d+(?:\.\d+){0,2})")


def parse_manifest(filename: str, text: str, limit: int = 50) -> dict:
    """Return {"pinned": [...], "total": n}.

    Only pinned (name, version) pairs are sent to OSV, because OSV needs a
    concrete version to give a meaningful answer. The total is kept so the
    UI can show how reproducible the install is: unpinned deps mean the
    environment you build on a GPU pod today may differ from tomorrow's.
    """
    pinned: list[dict] = []
    total = 0
    if filename == "requirements.txt":
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "git+", "http")):
                continue
            total += 1
            m = _REQ_PIN.match(line)
            if m:
                pinned.append({"name": m[1], "version": m[2], "ecosystem": "PyPI"})
    elif filename == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return {"pinned": [], "total": 0}
        deps = data.get("dependencies") or {}
        total = len(deps)
        for name, spec in deps.items():
            m = _NPM_VERSION.match(str(spec))
            if m:
                pinned.append({"name": name, "version": m[1], "ecosystem": "npm"})
    return {"pinned": pinned[:limit], "total": total}


class SimulatedOutageOSV(OSVClient):
    """Stands in for OSV.dev during a live demo (DEMO_MODE=1 + ?simulate=osv_down)."""

    async def query_batch(self, packages: list[dict]) -> list[list[str]]:
        log.warning("OSV.dev POST /v1/querybatch -> simulated outage")
        raise UpstreamError("OSV.dev", "OSV.dev is unavailable (simulated)", status=503)
