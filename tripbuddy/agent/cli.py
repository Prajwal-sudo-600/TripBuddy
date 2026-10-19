"""
Command-Line Interface for TripBuddy Agent.
Usage:
    python -m agent.cli "your task here"
"""

import sys
from typing import Any, Dict

from .loop import run_task


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m agent.cli \"<task description>\"")
        sys.exit(1)

    task = sys.argv[1].strip()
    if not task:
        print("Error: Task description cannot be empty.")
        sys.exit(1)

    def on_tool_call(name: str, args: Dict[str, Any]) -> None:
        args_formatted = ", ".join(f"{k}={v!r}" for k, v in args.items())
        print(f"[Tool Call] {name}({args_formatted})")
        sys.stdout.flush()

    try:
        result = run_task(task=task, on_tool_call=on_tool_call)
    except Exception as e:
        print(f"\nExecution failed with error: {e}")
        sys.exit(1)

    print("\n[Final Answer]")
    print(result.final_answer)
    print(f"\nTrace saved to: {result.trace_path}")


if __name__ == "__main__":
    main()
