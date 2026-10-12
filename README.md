# GPU Pre-flight ✈️

**Check an open-source AI repo before you spin up a GPU for it.**

A common RunPod workflow is: find a repo like ComfyUI or a text-generation UI, clone it onto a GPU pod, `pip install -r requirements.txt`, and run it. Before paying for GPU time and running someone else's code, you probably want quick answers to:

| Question | Answered by |
|---|---|
| Is anyone still maintaining this? What happens if one person disappears? | **GitHub REST API**: bus factor, contributors, last push, archived status |
| Am I allowed to use it for my project? | **GitHub REST API**: license |
| Will `pip install` give me what the authors tested? | **GitHub REST API**: share of dependencies that are pinned |
| Does it install packages with known vulnerabilities? | **OSV.dev API**: advisories for each pinned dependency |

Neither API answers the question alone. The backend joins them: it reads the dependency manifest **from GitHub**, then sends those packages **to OSV** in one batch call, and combines everything into a single score.

Live app: `https://<POD_ID>-8000.proxy.runpod.net` *(fill in after deploying)*

## Architecture

```mermaid
flowchart LR
    U[Browser<br/>index.html] -->|GET /api/check?repo=owner/name| B[FastAPI backend<br/>RunPod CPU Pod]
    B -->|1. repo, contributors,<br/>requirements.txt / package.json| G[(GitHub REST API)]
    B -->|2. one batch query<br/>for pinned deps| O[(OSV.dev API)]
    B -->|3. combined score + report| U
    B -.->|10-min in-memory cache| B
```

The frontend is a single static page served by the same FastAPI app, so there's one deploy and one URL. The GitHub token lives only in the Pod's environment variables and never reaches the browser.

## Request walkthrough

1. **Validate** (`service.parse_repo`): only `owner/name` or a `github.com` URL is accepted. Anything else returns `400` before any upstream call.
2. **Cache check**: a repeat check within 10 minutes is served from memory, saving GitHub quota.
3. **GitHub (required)**: repo metadata, top 100 contributors, and the first manifest found at the repo root.
4. **Parse**: count all dependencies, then keep only pinned ones (`pkg==1.2.3`, or npm `^1.2.3` → `1.2.3`), capped at 50. OSV needs a concrete version for a meaningful answer, and the pinned share is itself a reproducibility signal.
5. **OSV (optional)**: one `POST /v1/querybatch` for all packages.
6. **Score**: start at 100, subtract for bus factor ≤ 2, for advisories, and for stale pushes. Archived repos are flagged outright.

## Failure handling

| Situation | What the user sees | HTTP |
|---|---|---|
| Invalid input | "Enter a GitHub repo as owner/name…" | 400 |
| Repo missing or private | "Repository not found" | 404 |
| GitHub rate limit hit | Error with "try again in ~N min" (from `Retry-After` / `X-RateLimit-Reset`) | 429 |
| GitHub timeout / 5xx | Retried once, then a clear error | 504 / 502 |
| **OSV down** | **Report still shown** with a warning banner; vulnerabilities marked "not checked" | 200 |

GitHub is the source of truth for the workflow, so its failures fail the request. OSV failures **degrade gracefully**, because maintainer data is still useful on its own.

## Free-tier limits

| API | Key needed | Limit | How the app copes |
|---|---|---|---|
| GitHub REST | Optional `GITHUB_TOKEN` | 60 req/hr without a token, 5,000 req/hr with one | ≤ 4 calls per check, 10-min cache, rate limit surfaced to the user with a wait time |
| OSV.dev | None | No published quota | One batch request per check instead of one per package |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # optionally add GITHUB_TOKEN
export $(cat .env | xargs)
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000.

## Tests

```bash
pytest -q
```

Both external APIs are mocked with `respx`, so tests are fast and need no network. They cover:

- the full happy path combining both APIs
- manifest parsing: pinned vs unpinned, comments, `-r` includes and git URLs
- archived repos
- **OSV returning 503** → report still returned, security marked unavailable
- **GitHub rate limit** → 429 with `Retry-After`
- repo not found
- invalid input, including path-traversal attempts, makes **zero** upstream calls

## Deploy to RunPod (Pod)

1. In the RunPod console, deploy a **CPU Pod** (no GPU needed) from a Python or Ubuntu base template.
2. In the Pod settings, **expose HTTP port `8000`** and add an environment variable `GITHUB_TOKEN`.
3. Open the Pod's web terminal and run:
   ```bash
   git clone https://github.com/<you>/gpu-preflight.git && cd gpu-preflight
   pip install -r requirements.txt
   nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 > server.log 2>&1 &
   ```
4. Visit `https://<POD_ID>-8000.proxy.runpod.net`. Check `/health` returns `{"ok": true}`.

**Alternative:** build the included `Dockerfile`, push it to Docker Hub, and use the image in a custom RunPod Pod template with port 8000 exposed. This makes the deploy reproducible with no manual steps.

## Project layout

```
app/
  clients.py   # GitHub + OSV clients, retries, rate-limit detection, manifest parsing
  service.py   # the workflow: validate → cache → GitHub → OSV → score
  main.py      # FastAPI routes and error → HTTP mapping
  static/index.html  # the whole frontend: loading, error, partial and success states
tests/test_api.py
```

## What I'd improve with more time

- Parse `pyproject.toml` and lockfiles (`poetry.lock`, `uv.lock`) so unpinned and modern-packaged deps get checked too; many AI repos have moved there
- GPU-specific checks: flag pinned CUDA / `torch` versions and compare them with the CUDA versions available on RunPod base images
- A "Deploy to RunPod" button that pre-fills a Pod template for repos that pass
- Page through contributors for very large repos (currently top 100)
- Replace the in-memory cache with Redis so it survives restarts and scales across Pods
- Show OSV severity scores, not just advisory counts
- Run the GitHub calls concurrently

## AI usage

Built with help from Claude for scaffolding the code, tests and README. I reviewed every file, ran the tests, and deployed it myself. *(Edit this to match what you actually did.)*
