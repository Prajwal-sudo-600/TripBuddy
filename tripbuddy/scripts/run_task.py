#!/usr/bin/env python3
"""
CLI script to dispatch a task to the TripBuddy Agent Service (POST /run).
Usage:
    python scripts/run_task.py "<task description>" [--service-url http://localhost:8100] [--mcp-url ...]
"""

import argparse
import json
import sys
from typing import Any, Dict, List
import httpx


def format_tool_call(tool_call: Dict[str, Any]) -> str:
    name = tool_call.get("name", "unknown")
    args = tool_call.get("args") or {}
    args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{name}({args_str})"


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a task to TripBuddy Agent Service.")
    parser.add_argument("task", help="The task description for the agent")
    parser.add_argument(
        "--service-url",
        default="http://localhost:8100",
        help="URL of the agent service (default: http://localhost:8100)",
    )
    parser.add_argument(
        "--mcp-url",
        default=None,
        help="Optional MCP URL override",
    )
    args = parser.parse_args()

    payload: Dict[str, Any] = {"task": args.task}
    if args.mcp_url:
        payload["mcp_url"] = args.mcp_url

    endpoint = f"{args.service_url.rstrip('/')}/run"

    try:
        # Service has a 300s internal timeout, allow 320s client-side timeout
        response = httpx.post(endpoint, json=payload, timeout=320.0)
    except Exception as exc:
        print(f"Error connecting to agent service at {endpoint}: {exc}", file=sys.stderr)
        sys.exit(1)

    if response.status_code == 409:
        print(f"Status: error (HTTP 409 Conflict - A task is already running)", file=sys.stderr)
        sys.exit(1)

    if response.status_code != 200:
        print(f"HTTP Error {response.status_code}: {response.text}", file=sys.stderr)
        sys.exit(1)

    data = response.json()
    status = data.get("status", "unknown")
    final_answer = data.get("final_answer", "")
    llm_calls = data.get("llm_calls", 0)
    error = data.get("error")

    # Extract all tools called in chronological order from steps
    tools_called: List[str] = []
    for step in data.get("steps", []):
        if step.get("type") == "tool_calls":
            for tc in step.get("tool_calls", []):
                tools_called.append(format_tool_call(tc))

    print(f"Status: {status}")
    if error:
        print(f"Error: {error}")

    print("Tools called in order:")
    if tools_called:
        for idx, tool_repr in enumerate(tools_called, 1):
            print(f"  {idx}. {tool_repr}")
    else:
        print("  (none)")

    print("\nFinal answer:")
    print(final_answer)

    print(f"\nLLM calls: {llm_calls}")


if __name__ == "__main__":
    main()
