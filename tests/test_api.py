import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import service
from app.main import app

GH = "https://api.github.com/repos/acme/widget"
OSV = "https://api.osv.dev/v1/querybatch"

REPO = {"full_name": "acme/widget", "html_url": "https://github.com/acme/widget",
        "description": "A widget", "stargazers_count": 10, "pushed_at": "2099-01-01T00:00:00Z",
        "license": {"spdx_id": "MIT"}, "archived": False}
CONTRIBUTORS = [{"login": "alice", "contributions": 90}, {"login": "bob", "contributions": 10}]
REQS = "requests==2.19.0\nflask>=2.0\n"


@pytest.fixture(autouse=True)
def clear_cache():
    service._cache.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def mock_github(router):
    router.get(GH).respond(json=REPO)
    router.get(f"{GH}/contributors").respond(json=CONTRIBUTORS)
    router.get(f"{GH}/contents/requirements.txt").respond(text=REQS)


@respx.mock
def test_happy_path_combines_both_apis(client):
    mock_github(respx)
    osv = respx.post(OSV).respond(json={"results": [{"vulns": [{"id": "GHSA-1"}, {"id": "GHSA-2"}]}]})

    r = client.get("/api/check", params={"repo": "https://github.com/acme/widget"})

    assert r.status_code == 200
    body = r.json()
    assert body["maintainers"]["bus_factor"] == 1          # alice wrote 90%
    assert body["security"]["checked"] == 1                 # only the pinned dep is sent
    assert body["security"]["total_deps"] == 2              # flask>=2.0 counted but unpinned
    assert body["license"] == "MIT"
    assert body["security"]["vulnerable"][0]["name"] == "requests"
    assert body["score"] == 50                              # 100 - 30 (bus factor 1) - 20 (2 advisories)
    assert body["label"] == "Needs attention"
    assert osv.calls.last.request.content.count(b"requests") == 1


@respx.mock
def test_osv_down_degrades_gracefully(client):
    mock_github(respx)
    respx.post(OSV).respond(status_code=503)

    r = client.get("/api/check", params={"repo": "acme/widget"})

    assert r.status_code == 200
    assert r.json()["security"]["status"] == "unavailable"
    assert r.json()["maintainers"]["bus_factor"] == 1


@respx.mock
@pytest.mark.parametrize("demo_mode, osv_status", [(True, "unavailable"), (False, "ok")])
def test_simulate_osv_down_requires_demo_mode(client, monkeypatch, demo_mode, osv_status):
    monkeypatch.setattr("app.main.DEMO_MODE", demo_mode)
    mock_github(respx)
    respx.post(OSV).respond(json={"results": [{}]})

    r = client.get("/api/check", params={"repo": "acme/widget", "simulate": "osv_down"})

    assert r.status_code == 200
    assert r.json()["security"]["status"] == osv_status


@respx.mock
def test_github_rate_limit_returns_429(client):
    respx.get(GH).respond(status_code=403, headers={"x-ratelimit-remaining": "0", "retry-after": "60"})

    r = client.get("/api/check", params={"repo": "acme/widget"})

    assert r.status_code == 429
    assert r.json()["service"] == "GitHub"
    assert r.headers["retry-after"] == "60"


@respx.mock
def test_repo_not_found(client):
    respx.get(GH).respond(status_code=404)
    r = client.get("/api/check", params={"repo": "acme/widget"})
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["", "not a repo", "../../etc/passwd", "https://evil.com/a/b"])
def test_invalid_input_rejected_before_any_upstream_call(client, bad):
    with respx.mock(assert_all_called=False) as router:
        r = client.get("/api/check", params={"repo": bad})
        assert r.status_code in (400, 422)
        assert router.calls.call_count == 0


def test_bus_factor():
    assert service.bus_factor([]) == 0
    assert service.bus_factor([{"contributions": 5}] * 4) == 2

@respx.mock
def test_follows_renamed_repo_redirect(client):
    new = "https://api.github.com/repositories/42"
    respx.get(GH).respond(status_code=301, headers={"location": new})
    respx.get(new).respond(json={**REPO, "full_name": "acme-org/widget"})
    respx.get(f"{GH}/contributors").respond(json=CONTRIBUTORS)
    respx.get(f"{GH}/contents/requirements.txt").respond(status_code=404)
    respx.get(f"{GH}/contents/package.json").respond(status_code=404)

    r = client.get("/api/check", params={"repo": "acme/widget"})

    assert r.status_code == 200
    assert r.json()["repo"] == "acme-org/widget"


@respx.mock
def test_archived_repo_is_flagged(client):
    respx.get(GH).respond(json={**REPO, "archived": True})
    respx.get(f"{GH}/contributors").respond(json=CONTRIBUTORS)
    respx.get(f"{GH}/contents/requirements.txt").respond(status_code=404)
    respx.get(f"{GH}/contents/package.json").respond(status_code=404)

    body = client.get("/api/check", params={"repo": "acme/widget"}).json()

    assert body["label"] == "Archived"
    assert body["security"]["status"] == "no_manifest"


def test_manifest_parsing_counts_unpinned():
    text = "torch==2.3.0\nnumpy>=1.26  # comment\n-r other.txt\n\ngit+https://x/y.git\nsafetensors\n"
    parsed = service.parse_manifest("requirements.txt", text)
    assert [p["name"] for p in parsed["pinned"]] == ["torch"]
    assert parsed["total"] == 3


