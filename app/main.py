"""FastAPI entry point: HTTP routes, shared HTTP client, and error-to-response mapping.

Kept deliberately thin. All the logic lives in service.py; this file only
wires clients together and turns exceptions into the right status codes:
  InvalidInput   -> 400
  UpstreamError  -> e.status (404 / 429 / 502 / 504), plus Retry-After for rate limits

Run locally with:  uvicorn app.main:app --reload
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

from .clients import GitHubClient, OSVClient, SimulatedOutageOSV, UpstreamError
from .service import InvalidInput, check_repo

STATIC = Path(__file__).parent / "static"
# Read once at startup. Gates the ?simulate= failure drill so it can't be
# triggered by anyone on a normal deployment.
DEMO_MODE = os.getenv("DEMO_MODE") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("preflight")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create one shared httpx client at startup and close it at shutdown.

    Sharing it gives connection pooling (reused TCP/TLS connections to GitHub
    and OSV) instead of a fresh handshake on every request.
    follow_redirects=True matters: GitHub answers a renamed repo with a 301
    to the new location, and we want to follow it rather than report 404.
    """
    app.state.http = httpx.AsyncClient(follow_redirects=True)
    yield  # the app serves requests while paused here
    await app.state.http.aclose()


app = FastAPI(title="GPU Pre-flight", lifespan=lifespan)


@app.get("/")
def index():
    """Serve the single-page frontend (plain HTML + JS, no build step)."""
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    """Liveness probe for the container / load balancer. Never calls upstream APIs."""
    return {"ok": True}


@app.get("/api/check")
async def api_check(repo: str = Query(..., max_length=200), simulate: str | None = None):
    """Run a pre-flight check. ?repo=owner/name or a github.com URL.

    `max_length=200` makes FastAPI reject oversized input with a 422 before
    our code runs. `simulate=osv_down` only works when DEMO_MODE=1.
    """
    http = app.state.http
    # Failure drill for live demos. Ignored unless the server was started with DEMO_MODE=1.
    simulating = DEMO_MODE and simulate == "osv_down"
    osv = SimulatedOutageOSV(http) if simulating else OSVClient(http)
    log.info("check   repo=%r%s", repo, " (simulating OSV outage)" if simulating else "")
    try:
        # Skip the cache while simulating, otherwise a cached healthy report
        # would hide the outage we're trying to demonstrate.
        return await check_repo(repo, GitHubClient(http), osv, use_cache=not simulating)
    except InvalidInput as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except UpstreamError as e:
        # The frontend reads error/service/retry_after to show a useful message.
        # Retry-After is also set as a real HTTP header for non-browser clients.
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        return JSONResponse(
            {"error": e.message, "service": e.service, "retry_after": e.retry_after},
            status_code=e.status, headers=headers,
        )
