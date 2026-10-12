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
DEMO_MODE = os.getenv("DEMO_MODE") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("preflight")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(follow_redirects=True)
    yield
    await app.state.http.aclose()


app = FastAPI(title="GPU Pre-flight", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/check")
async def api_check(repo: str = Query(..., max_length=200), simulate: str | None = None):
    http = app.state.http
    # Failure drill for live demos. Ignored unless the server was started with DEMO_MODE=1.
    simulating = DEMO_MODE and simulate == "osv_down"
    osv = SimulatedOutageOSV(http) if simulating else OSVClient(http)
    log.info("check   repo=%r%s", repo, " (simulating OSV outage)" if simulating else "")
    try:
        return await check_repo(repo, GitHubClient(http), osv, use_cache=not simulating)
    except InvalidInput as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except UpstreamError as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        return JSONResponse(
            {"error": e.message, "service": e.service, "retry_after": e.retry_after},
            status_code=e.status, headers=headers,
        )
