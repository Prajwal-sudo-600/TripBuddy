"""
TripBuddy Agent HTTP Service.
Wraps the TripBuddy agent in a FastAPI service on port 8100 so test harnesses
can send tasks and observe execution.
"""

import asyncio
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from .config import (
        AgentConfig,
        get_config,
        get_default_mcp_url,
        set_default_mcp_url,
    )
    from .loop import AgentResult, run_task
except ImportError:
    from tripbuddy.agent.config import (
        AgentConfig,
        get_config,
        get_default_mcp_url,
        set_default_mcp_url,
    )
    from tripbuddy.agent.loop import AgentResult, run_task


app = FastAPI(
    title="TripBuddy Agent Service",
    description="HTTP Service exposing TripBuddy AI Agent execution loop for testing and evaluation.",
    version="1.0.0",
)

# Concurrency lock: allow strictly one task execution at a time
_task_lock = asyncio.Lock()

# Optional module-level LLM override for tests
_override_llm: Optional[Any] = None


def set_test_llm(llm: Optional[Any]) -> None:
    """Set or clear an in-memory test LLM override."""
    global _override_llm
    _override_llm = llm


class RunRequest(BaseModel):
    task: str = Field(..., description="The travel booking or assistance task for the agent.")
    mcp_url: Optional[str] = Field(None, description="Optional MCP server URL override for this run.")


class ConfigUpdateRequest(BaseModel):
    mcp_url: str = Field(..., description="New default MCP server URL (in-memory only).")


@app.get("/health")
async def health() -> Dict[str, str]:
    """Health check returning status and current default MCP_URL."""
    return {
        "status": "ok",
        "mcp_url": get_default_mcp_url(),
    }


@app.get("/config")
async def get_configuration() -> Dict[str, Any]:
    """
    Returns current runtime configuration:
    default MCP_URL, HARDENED_PROMPT, LLM provider, and model name.
    API keys are never exposed.
    """
    cfg: AgentConfig = get_config()
    return {
        "mcp_url": get_default_mcp_url(),
        "hardened_prompt": cfg.hardened_prompt,
        "llm_provider": cfg.llm_provider,
        "llm_model": cfg.llm_model,
    }


@app.put("/config")
async def update_configuration(req: ConfigUpdateRequest) -> Dict[str, Any]:
    """
    Updates the default MCP_URL in memory only without altering .env.
    Enables switching between direct MCP and security proxy dynamically.
    """
    set_default_mcp_url(req.mcp_url)
    cfg: AgentConfig = get_config()
    return {
        "status": "ok",
        "mcp_url": get_default_mcp_url(),
        "hardened_prompt": cfg.hardened_prompt,
        "llm_provider": cfg.llm_provider,
        "llm_model": cfg.llm_model,
    }


@app.post("/run")
async def run_task_endpoint(req: RunRequest):
    """
    Executes a task using the TripBuddy agent.
    - If another task is currently executing, returns HTTP 409 Conflict.
    - Applies a 300-second execution timeout.
    - If mcp_url is missing, uses the default MCP_URL.
    - On rate-limit, quota, or execution error, returns HTTP 200 with status='error' and partial trace.
    """
    # Enforce strictly one task at a time (HTTP 409 if busy)
    if _task_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A task is already running. Concurrency limit is 1.",
        )

    try:
        # Non-blocking acquisition
        await asyncio.wait_for(_task_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A task is already running. Concurrency limit is 1.",
        )

    target_mcp_url = req.mcp_url if req.mcp_url is not None else get_default_mcp_url()
    llm_instance = getattr(app.state, "llm", None) or _override_llm

    try:
        try:
            # 300 second timeout for run_task execution
            result: AgentResult = await asyncio.wait_for(
                asyncio.to_thread(
                    run_task,
                    task=req.task,
                    mcp_url=target_mcp_url,
                    llm=llm_instance,
                ),
                timeout=300.0,
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "error",
                    "error": "Task execution timed out after 300 seconds",
                    "final_answer": "Error: Task execution timed out after 300 seconds",
                    "steps": [],
                    "trace": {"error": "Task execution timed out after 300 seconds"},
                    "mcp_url_used": target_mcp_url,
                    "llm_calls": 0,
                },
            )
        except Exception as exc:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "error",
                    "error": str(exc),
                    "final_answer": f"Error: Unexpected failure during task: {exc}",
                    "steps": [],
                    "trace": {"error": str(exc)},
                    "mcp_url_used": target_mcp_url,
                    "llm_calls": 0,
                },
            )

        # Check for errors recorded during agent loop (e.g. RateLimit or LLM errors)
        is_error = bool(result.trace.get("error")) or result.final_answer.startswith("Error:")
        error_msg = result.trace.get("error")
        if is_error and not error_msg:
            error_msg = result.final_answer

        return {
            "status": "error" if is_error else "ok",
            "error": error_msg if is_error else None,
            "final_answer": result.final_answer,
            "steps": result.steps,
            "trace": result.trace,
            "mcp_url_used": target_mcp_url,
            "llm_calls": result.llm_calls,
        }

    finally:
        _task_lock.release()
