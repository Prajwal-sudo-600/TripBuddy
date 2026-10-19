"""
Configuration management for TripBuddy AI Agent.
Loads settings from .env using python-dotenv with safe defaults.
"""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional
from dotenv import find_dotenv, load_dotenv

# Search for .env from current working directory or upwards
_dotenv_path = find_dotenv(usecwd=True)
if _dotenv_path:
    load_dotenv(_dotenv_path)
else:
    # Fallback to loading from project root or current dir
    load_dotenv()


def _str_to_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("true", "1", "yes", "y", "t", "on")


def _str_to_int(val: Optional[str], default: int) -> int:
    if val is None:
        return default
    try:
        return int(str(val).strip())
    except ValueError:
        return default


def _str_to_float(val: Optional[str], default: float) -> float:
    if val is None:
        return default
    try:
        return float(str(val).strip())
    except ValueError:
        return default


@dataclass
class AgentConfig:
    """Holds configuration for agent execution and LLM provider."""
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_min_delay_seconds: float
    mcp_url: str
    max_steps: int
    hardened_prompt: bool


def get_config(
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    llm_min_delay_seconds: Optional[float] = None,
    mcp_url: Optional[str] = None,
    max_steps: Optional[int] = None,
    hardened_prompt: Optional[bool] = None,
) -> AgentConfig:
    """
    Returns an AgentConfig instance loaded from environment variables and .env,
    allowing caller overrides.
    """
    # Reload in case .env was created or modified dynamically
    dotenv_file = find_dotenv(usecwd=True)
    if dotenv_file:
        load_dotenv(dotenv_file, override=False)

    provider = llm_provider if llm_provider is not None else os.getenv("LLM_PROVIDER", "gemini")
    model = llm_model if llm_model is not None else os.getenv("LLM_MODEL", "gemini-2.5-flash")
    api_key = llm_api_key if llm_api_key is not None else os.getenv("LLM_API_KEY", "")
    
    if llm_min_delay_seconds is not None:
        min_delay = float(llm_min_delay_seconds)
    else:
        min_delay = _str_to_float(os.getenv("LLM_MIN_DELAY_SECONDS"), 0.0)

    url = mcp_url if mcp_url is not None else os.getenv("MCP_URL", "http://localhost:8000/mcp")

    if max_steps is not None:
        steps = int(max_steps)
    else:
        steps = _str_to_int(os.getenv("MAX_STEPS"), 10)

    if hardened_prompt is not None:
        hardened = bool(hardened_prompt)
    else:
        hardened = _str_to_bool(os.getenv("HARDENED_PROMPT"), False)

    return AgentConfig(
        llm_provider=provider,
        llm_model=model,
        llm_api_key=api_key,
        llm_min_delay_seconds=min_delay,
        mcp_url=url,
        max_steps=steps,
        hardened_prompt=hardened,
    )


# Module-level defaults for direct access
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MIN_DELAY_SECONDS = _str_to_float(os.getenv("LLM_MIN_DELAY_SECONDS"), 0.0)
MCP_URL = os.getenv("MCP_URL", "http://localhost:8000/mcp")
MAX_STEPS = _str_to_int(os.getenv("MAX_STEPS"), 10)
HARDENED_PROMPT = _str_to_bool(os.getenv("HARDENED_PROMPT"), False)
