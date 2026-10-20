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
    llm_base_url: str = "http://localhost:11434"


def get_config(
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    llm_min_delay_seconds: Optional[float] = None,
    mcp_url: Optional[str] = None,
    max_steps: Optional[int] = None,
    hardened_prompt: Optional[bool] = None,
    llm_base_url: Optional[str] = None,
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
    base_url = llm_base_url if llm_base_url is not None else os.getenv("LLM_BASE_URL", "http://localhost:11434")
    
    if llm_min_delay_seconds is not None:
        min_delay = float(llm_min_delay_seconds)
    else:
        min_delay = _str_to_float(os.getenv("LLM_MIN_DELAY_SECONDS"), 0.0)

    url = mcp_url if mcp_url is not None else get_default_mcp_url()

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
        llm_base_url=base_url,
    )


# Module-level defaults for direct access
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434")
LLM_MIN_DELAY_SECONDS = _str_to_float(os.getenv("LLM_MIN_DELAY_SECONDS"), 0.0)
_default_mcp_url = os.getenv("MCP_URL", "http://localhost:8000/mcp")
MCP_URL = _default_mcp_url
MAX_STEPS = _str_to_int(os.getenv("MAX_STEPS"), 10)
HARDENED_PROMPT = _str_to_bool(os.getenv("HARDENED_PROMPT"), False)


def get_default_mcp_url() -> str:
    """Returns the in-memory default MCP URL."""
    return _default_mcp_url


def set_default_mcp_url(url: str) -> None:
    """Updates the in-memory default MCP URL without modifying .env."""
    global _default_mcp_url, MCP_URL
    _default_mcp_url = url
    MCP_URL = url

