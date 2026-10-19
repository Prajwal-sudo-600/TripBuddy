"""
LLM Interface for TripBuddy Agent.
Provides a provider-agnostic LLM interface with Google Gemini implementation.
Handles rate-limiting (429/quota) gracefully without crashing, and supports
inter-call delays via LLM_MIN_DELAY_SECONDS.
"""

from copy import deepcopy
import json
import time
from typing import Any, Dict, List, Optional, Union

from .config import get_config


class LLMRateLimitError(Exception):
    """Raised when an LLM API rate limit or quota exhaustion (429) occurs."""
    pass


class LLMError(Exception):
    """Raised when an unrecoverable LLM API error occurs."""
    pass


class ToolCallList(list):
    """List of tool calls that carries raw provider response parts if available."""
    def __init__(self, items, raw_parts=None):
        super().__init__(items)
        self.raw_parts = raw_parts


def _clean_json_schema(schema: Any) -> Any:
    """
    Cleans and normalizes JSON Schema into standard OpenAPI 3.0 schema
    compatible with Google Gemini API:
    - Removes '$defs', 'title', 'default' metadata
    - Resolves 'anyOf' with null into single concrete types
    - Ensures top-level object type
    """
    if not isinstance(schema, dict):
        return schema

    cleaned = {}
    for key, val in schema.items():
        if key in ("title", "$defs", "default"):
            continue

        if key == "anyOf" and isinstance(val, list):
            # Select non-null types
            non_null_types = [
                item.get("type")
                for item in val
                if isinstance(item, dict) and item.get("type") != "null"
            ]
            if non_null_types:
                cleaned["type"] = non_null_types[0]
            continue

        if isinstance(val, dict):
            cleaned[key] = _clean_json_schema(val)
        elif isinstance(val, list):
            cleaned[key] = [
                _clean_json_schema(item) if isinstance(item, dict) else item
                for item in val
            ]
        else:
            cleaned[key] = val

    if "type" not in cleaned and "properties" in cleaned:
        cleaned["type"] = "object"

    return cleaned


def call_gemini(
    messages: List[Dict[str, Any]],
    tools: List[Any],
    model: str,
    api_key: str,
) -> Union[str, List[Dict[str, Any]]]:
    """
    Call Google Gemini API using google-genai SDK.
    Returns either string (final answer) or list of tool calls:
    [{"name": "...", "args": {...}}].
    Raises LLMRateLimitError on 429/quota exhaustion.
    Raises LLMError on other failures.
    """
    if not api_key:
        raise LLMError(
            "LLM_API_KEY is not configured. Please set LLM_API_KEY in your .env file."
        )

    try:
        from google import genai
        from google.genai import types
        from google.genai.errors import APIError
    except ImportError as e:
        raise LLMError(
            f"Failed to import google-genai SDK. Please ensure dependencies are installed: {e}"
        )

    client = genai.Client(api_key=api_key)

    # 1. Prepare Tools
    gemini_tools = []
    if tools:
        declarations = []
        for t in tools:
            # Handle both MCP Tool objects and plain dictionaries
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            desc = getattr(t, "description", None) or (t.get("description") if isinstance(t, dict) else "")
            schema = getattr(t, "inputSchema", None) or (t.get("parameters") or t.get("inputSchema") if isinstance(t, dict) else {})
            
            cleaned_params = _clean_json_schema(deepcopy(schema)) if schema else {"type": "object", "properties": {}}

            declarations.append(
                types.FunctionDeclaration(
                    name=name,
                    description=desc,
                    parameters=cleaned_params,
                )
            )

        if declarations:
            gemini_tools.append(types.Tool(function_declarations=declarations))

    # 2. Extract System Instruction and build Contents
    system_instruction: Optional[str] = None
    contents: List[types.Content] = []

    # Messages may contain:
    # {"role": "system", "content": "..."}
    # {"role": "user", "content": "..."}
    # {"role": "assistant", "content": "..."}
    # {"role": "assistant", "tool_calls": [{"name": ..., "args": ...}]}
    # {"role": "tool", "name": ..., "content": ...}

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role")

        if role == "system":
            system_instruction = msg.get("content", "")
            i += 1
            continue

        if role == "user":
            user_content = msg.get("content", "")
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=str(user_content))],
                )
            )
            i += 1
            continue

        if role in ("assistant", "model"):
            tool_calls = msg.get("tool_calls")
            raw_parts = getattr(tool_calls, "raw_parts", None) or msg.get("_raw_parts")
            if raw_parts:
                contents.append(types.Content(role="model", parts=list(raw_parts)))
            elif tool_calls:
                parts = []
                for tc in tool_calls:
                    call_name = tc.get("name")
                    call_args = tc.get("args") or {}
                    thought_sig = tc.get("thought_signature")
                    if thought_sig is not None:
                        parts.append(
                            types.Part(
                                function_call=types.FunctionCall(name=call_name, args=call_args),
                                thought_signature=thought_sig,
                            )
                        )
                    else:
                        parts.append(
                            types.Part.from_function_call(name=call_name, args=call_args)
                        )
                contents.append(types.Content(role="model", parts=parts))
            else:
                text_content = msg.get("content", "")
                contents.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=str(text_content))],
                    )
                )
            i += 1
            continue

        if role == "tool":
            # Collect all contiguous tool responses into a single 'user' turn
            tool_response_parts = []
            while i < len(messages) and messages[i].get("role") == "tool":
                t_msg = messages[i]
                t_name = t_msg.get("name", "")
                t_content = t_msg.get("content")
                # Wrap response in dict as required by Gemini function response
                if isinstance(t_content, dict):
                    resp_dict = t_content
                else:
                    resp_dict = {"result": t_content}

                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=t_name,
                        response=resp_dict,
                    )
                )
                i += 1

            contents.append(
                types.Content(role="user", parts=tool_response_parts)
            )
            continue

        # Catch-all for other roles
        i += 1

    # 3. Build Config
    config_args: Dict[str, Any] = {}
    if system_instruction:
        config_args["system_instruction"] = system_instruction
    if gemini_tools:
        config_args["tools"] = gemini_tools

    config = types.GenerateContentConfig(**config_args)

    # 4. Invoke LLM with Rate Limit Handling & Transient Retry
    max_retries = 3
    response = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            break
        except Exception as e:
            err_msg = str(e)
            code = getattr(e, "code", None)
            # Check for 429 or quota exhaustion indicators
            is_rate_limit = (
                code == 429
                or "429" in err_msg
                or "RESOURCE_EXHAUSTED" in err_msg
                or "quota" in err_msg.lower()
                or "rate limit" in err_msg.lower()
            )
            if is_rate_limit:
                raise LLMRateLimitError(
                    f"Google Gemini rate limit / quota exceeded (429): {err_msg}"
                ) from e

            # If 503 UNAVAILABLE and retries remain, wait and retry
            is_transient = code == 503 or "503" in err_msg or "UNAVAILABLE" in err_msg
            if is_transient and attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue

            raise LLMError(f"Google Gemini API error: {err_msg}") from e

    # 5. Extract Tool Calls or Text
    if response.function_calls:
        candidate_parts = (
            response.candidates[0].content.parts
            if response.candidates and response.candidates[0].content
            else None
        )
        tool_calls = []
        for fc in response.function_calls:
            args_dict = dict(fc.args) if fc.args else {}
            tool_calls.append({"name": fc.name, "args": args_dict})
        return ToolCallList(tool_calls, raw_parts=candidate_parts)

    if response.text is not None:
        return response.text

    return ""


# Provider registry for future extensions
PROVIDERS = {
    "gemini": call_gemini,
    "google": call_gemini,
}


class LLMInterface:
    """
    Provider-agnostic interface with one method:
    chat(messages, tools) -> either text, or a list of tool calls (name + JSON arguments).
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        min_delay_seconds: Optional[float] = None,
    ):
        cfg = get_config()
        self.provider = (provider or cfg.llm_provider).lower()
        self.model = model or cfg.llm_model
        self.api_key = api_key or cfg.llm_api_key
        self.min_delay_seconds = (
            min_delay_seconds
            if min_delay_seconds is not None
            else cfg.llm_min_delay_seconds
        )
        self._last_call_time = 0.0

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any],
    ) -> Union[str, List[Dict[str, Any]]]:
        """
        Send messages and available tools to the LLM.
        Returns either:
        - str: final answer text
        - list[dict]: list of requested tool calls [{"name": "...", "args": {...}}]
        """
        # Optional delay between calls to avoid hitting tight rate limits
        if self.min_delay_seconds > 0 and self._last_call_time > 0:
            elapsed = time.time() - self._last_call_time
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)

        provider_fn = PROVIDERS.get(self.provider)
        if not provider_fn:
            raise LLMError(
                f"Unsupported LLM provider: '{self.provider}'. Available: {list(PROVIDERS.keys())}"
            )

        result = provider_fn(
            messages=messages,
            tools=tools,
            model=self.model,
            api_key=self.api_key,
        )
        self._last_call_time = time.time()
        return result
