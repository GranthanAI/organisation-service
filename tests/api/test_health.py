"""/live, /ready (with fake probes), /metrics and the correlation id. No network."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_probes
from app.main import app


def fake_probes(**status: bool):
    def make(ok: bool):
        async def probe() -> bool:
            return ok
        return probe

    return lambda: {name: make(ok) for name, ok in status.items()}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_live_returns_200(client):
    r = await client.get("/live")
    assert r.status_code == 200
    assert r.json()["status"] == "UP"


async def test_ready_200_when_all_up(client):
    app.dependency_overrides[get_probes] = fake_probes(postgres=True, redis=True, kafka=True)
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json()["details"] == {"postgres": "UP", "redis": "UP", "kafka": "UP"}


@pytest.mark.parametrize("down", ["postgres", "redis", "kafka"])
async def test_ready_503_names_the_failed_dependency(client, down):
    status = {"postgres": True, "redis": True, "kafka": True, down: False}
    app.dependency_overrides[get_probes] = fake_probes(**status)
    r = await client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["failed"] == [down]
    assert body["details"][down] == "DOWN"


async def test_ready_treats_a_raising_probe_as_down(client):
    async def boom() -> bool:
        raise RuntimeError("connection refused")

    app.dependency_overrides[get_probes] = lambda: {"postgres": boom}
    r = await client.get("/ready")
    assert r.status_code == 503 and r.json()["failed"] == ["postgres"]


async def test_metrics_exposes_prometheus_text(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "python_info" in r.text


async def test_correlation_id_is_echoed(client):
    r = await client.get("/live", headers={"X-Correlation-ID": "abc-123"})
    assert r.headers["X-Correlation-ID"] == "abc-123"


async def test_correlation_id_is_generated_when_missing_or_unsafe(client):
    r = await client.get("/live")
    assert len(r.headers["X-Correlation-ID"]) == 36
    r2 = await client.get("/live", headers={"X-Correlation-ID": "bad value with spaces"})
    assert r2.headers["X-Correlation-ID"] != "bad value with spaces"
