"""
Tests for TripBuddy Agent HTTP Service (FastAPI on port 8100).
Verifies:
- GET /health returns status "ok" and current default MCP_URL.
- GET /config returns configuration without revealing API keys.
- PUT /config dynamically changes default MCP_URL in memory only.
- POST /run executes task with FakeLLM, tracks llm_calls, and formats response.
- POST /run handles rate-limit errors by returning status "error" with HTTP 200 and partial trace.
- POST /run enforces single-task concurrency and returns HTTP 409 when busy.
"""

import asyncio
import time
from typing import Any, Dict, List
import httpx
from httpx import ASGITransport
import pytest

from tripbuddy.agent.config import get_default_mcp_url, set_default_mcp_url
from tripbuddy.agent.llm import LLMRateLimitError
from tripbuddy.agent.service import app, set_test_llm
from tripbuddy.tests.test_agent_flow import FakeLLM, mcp_server_fixture


@pytest.fixture(autouse=True)
def reset_service_state():
    """Resets service in-memory state before and after each test."""
    original_url = get_default_mcp_url()
    set_test_llm(None)
    app.state.llm = None
    yield
    set_default_mcp_url(original_url)
    set_test_llm(None)
    app.state.llm = None


@pytest.mark.asyncio
async def test_health_endpoint():
    """GET /health returns status 'ok' and default mcp_url."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "mcp_url" in data
        assert data["mcp_url"] == get_default_mcp_url()


@pytest.mark.asyncio
async def test_config_get_endpoint():
    """GET /config returns current config and never reveals the LLM API key."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/config")
        assert r.status_code == 200
        data = r.json()
        assert "mcp_url" in data
        assert "hardened_prompt" in data
        assert "llm_provider" in data
        assert "llm_model" in data
        # Ensure API key is never exposed
        assert "llm_api_key" not in data
        assert "api_key" not in data


@pytest.mark.asyncio
async def test_config_put_endpoint():
    """PUT /config updates default MCP_URL in memory only without restarting."""
    new_url = "http://test-proxy:9999/mcp"
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.put("/config", json={"mcp_url": new_url})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["mcp_url"] == new_url

        # Check that GET /health and GET /config reflect the updated in-memory default
        h_resp = await client.get("/health")
        assert h_resp.json()["mcp_url"] == new_url

        c_resp = await client.get("/config")
        assert c_resp.json()["mcp_url"] == new_url


@pytest.mark.asyncio
async def test_run_success_with_fake_llm(mcp_server_fixture):
    """POST /run executes successfully and returns expected fields and llm_calls count."""
    mcp_url = mcp_server_fixture["mcp_url"]

    scripted = [
        [{"name": "search_hotels", "args": {"city": "Goa", "max_price_per_night": 4000}}],
        [{
            "name": "book_hotel",
            "args": {
                "hotel_id": 1,
                "check_in": "2026-12-12",
                "check_out": "2026-12-14",
                "guest_name": "Priya Sharma",
            },
        }],
        "Successfully booked Seaside Palm Grove for Priya Sharma.",
    ]
    fake_llm = FakeLLM(scripted)
    set_test_llm(fake_llm)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/run",
            json={
                "task": "Find a hotel in Goa under Rs 4000 and book it",
                "mcp_url": mcp_url,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["error"] is None
        assert data["final_answer"] == "Successfully booked Seaside Palm Grove for Priya Sharma."
        assert data["mcp_url_used"] == mcp_url
        assert data["llm_calls"] == 3
        assert len(data["steps"]) == 3
        assert "trace" in data
        assert data["trace"]["final_answer"] == "Successfully booked Seaside Palm Grove for Priya Sharma."


@pytest.mark.asyncio
async def test_run_rate_limit_error_returns_status_error_with_http_200(mcp_server_fixture):
    """
    POST /run with simulated rate limit error returns HTTP 200 with status 'error',
    clear error message, partial trace, and correct llm_calls count.
    """
    mcp_url = mcp_server_fixture["mcp_url"]

    fake_llm = FakeLLM([
        LLMRateLimitError("429 Resource has been exhausted (quota exceeded)")
    ])
    set_test_llm(fake_llm)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/run",
            json={
                "task": "Perform a task that hits rate limit",
                "mcp_url": mcp_url,
            },
        )
        # Must be HTTP 200 so test harness can read the error payload
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "error"
        assert data["error"] is not None
        assert "429" in data["error"] or "exhausted" in data["error"]
        assert "rate limit" in data["final_answer"].lower() or "429" in data["final_answer"]
        assert data["llm_calls"] == 1
        assert "trace" in data
        assert data["trace"]["error"] is not None


@pytest.mark.asyncio
async def test_run_409_concurrency_behaviour(mcp_server_fixture):
    """
    POST /run enforces single-task execution. If a second task is sent while the first
    is still running, the service must return HTTP 409 Conflict.
    """
    mcp_url = mcp_server_fixture["mcp_url"]

    class SlowFakeLLM:
        def __init__(self):
            self.call_count = 0

        def chat(self, messages, tools):
            self.call_count += 1
            # Simulate slow LLM generation
            time.sleep(0.4)
            return "Slow task finished."

    slow_llm = SlowFakeLLM()
    set_test_llm(slow_llm)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Launch first task
        task1_coro = client.post(
            "/run",
            json={"task": "Slow task 1", "mcp_url": mcp_url},
        )

        # Allow task1 to start and acquire lock
        await asyncio.sleep(0.1)

        # Launch concurrent task 2 while task 1 is running
        task2_coro = client.post(
            "/run",
            json={"task": "Concurrent task 2", "mcp_url": mcp_url},
        )

        resp1, resp2 = await asyncio.gather(task1_coro, task2_coro)

        # Task 1 must succeed with 200
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "ok"
        assert resp1.json()["final_answer"] == "Slow task finished."

        # Task 2 must be rejected with 409 Conflict
        assert resp2.status_code == 409
        assert "already running" in resp2.json()["detail"].lower()

        # After task 1 finishes, subsequent requests must succeed without 409
        resp3 = await client.post(
            "/run",
            json={"task": "Subsequent task 3", "mcp_url": mcp_url},
        )
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "ok"
