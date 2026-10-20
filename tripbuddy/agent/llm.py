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


def call_ollama(
    messages: List[Dict[str, Any]],
    tools: List[Any],
    model: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 120.0,
    http_client: Optional[Any] = None,
) -> Union[str, List[Dict[str, Any]]]:
    """
    Call local Ollama server at base_url (default http://localhost:11434).
    Uses /api/chat with tool-calling support.
    Converts Ollama tool-calls into internal format [{"name": "...", "args": {...}}].
    Handles connection errors, 404 model not found, timeouts, and malformed JSON gracefully.
    """
    import httpx

    target_base = (base_url or "http://localhost:11434").rstrip("/")
    chat_url = f"{target_base}/api/chat"

    # 1. Prepare Tools for Ollama
    ollama_tools = []
    if tools:
        for t in tools:
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            desc = getattr(t, "description", None) or (t.get("description") if isinstance(t, dict) else "")
            schema = getattr(t, "inputSchema", None) or (
                t.get("parameters") or t.get("inputSchema") if isinstance(t, dict) else {}
            )
            cleaned_params = _clean_json_schema(deepcopy(schema)) if schema else {"type": "object", "properties": {}}

            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": cleaned_params,
                },
            })

    # 2. Format Messages for Ollama
    ollama_messages: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            ollama_messages.append({"role": "system", "content": str(msg.get("content", ""))})
        elif role == "user":
            ollama_messages.append({"role": "user", "content": str(msg.get("content", ""))})
        elif role in ("assistant", "model"):
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                formatted_tcs = []
                for tc in tool_calls:
                    formatted_tcs.append({
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": tc.get("args") or {},
                        }
                    })
                ollama_messages.append({
                    "role": "assistant",
                    "content": str(msg.get("content") or ""),
                    "tool_calls": formatted_tcs,
                })
            else:
                ollama_messages.append({
                    "role": "assistant",
                    "content": str(msg.get("content", "")),
                })
        elif role == "tool":
            content_val = msg.get("content")
            if not isinstance(content_val, str):
                content_str = json.dumps(content_val, default=str)
            else:
                content_str = content_val
            ollama_messages.append({
                "role": "tool",
                "content": content_str,
            })
        else:
            # Fallback for unexpected roles
            ollama_messages.append({
                "role": role or "user",
                "content": str(msg.get("content", "")),
            })

    payload: Dict[str, Any] = {
        "model": model,
        "messages": ollama_messages,
        "stream": False,
    }
    if ollama_tools:
        payload["tools"] = ollama_tools

    # 3. Send HTTP request with error handling
    try:
        if http_client is not None:
            response = http_client.post(chat_url, json=payload)
        else:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(chat_url, json=payload)
    except httpx.ConnectError as e:
        raise LLMError(
            f"Ollama connection refused. Server is not running at {target_base}: {e}"
        ) from e
    except httpx.TimeoutException as e:
        raise LLMError(
            f"Ollama request timed out after {timeout} seconds: {e}"
        ) from e
    except httpx.HTTPError as e:
        raise LLMError(
            f"Ollama HTTP connection error at {target_base}: {e}"
        ) from e
    except Exception as e:
        raise LLMError(f"Unexpected error communicating with Ollama: {e}") from e

    # 4. Handle HTTP Status codes
    if response.status_code == 404:
        raise LLMError(
            f"Ollama model '{model}' not found (HTTP 404). Please ensure the model is pulled using 'ollama pull {model}'."
        )
    if response.status_code != 200:
        raise LLMError(
            f"Ollama returned HTTP {response.status_code}: {response.text}"
        )

    # 5. Parse Ollama Response JSON
    try:
        data = response.json()
    except Exception as e:
        raise LLMError(f"Malformed response JSON from Ollama: {e}") from e

    message_obj = data.get("message") or {}
    tool_calls = message_obj.get("tool_calls")

    if tool_calls:
        parsed_tool_calls: List[Dict[str, Any]] = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            raw_args = fn.get("arguments", {})

            if isinstance(raw_args, str):
                try:
                    args_dict = json.loads(raw_args)
                except Exception as e:
                    raise LLMError(
                        f"Malformed tool-call JSON from Ollama for tool '{fn_name}': {raw_args}"
                    ) from e
            elif isinstance(raw_args, dict):
                args_dict = raw_args
            else:
                args_dict = {}

            parsed_tool_calls.append({"name": fn_name, "args": args_dict})

        return parsed_tool_calls

    return message_obj.get("content", "") or ""


# Provider registry for future extensions
PROVIDERS = {
    "gemini": call_gemini,
    "google": call_gemini,
    "ollama": call_ollama,
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
        base_url: Optional[str] = None,
        timeout: float = 120.0,
    ):
        cfg = get_config()
        self.provider = (provider or cfg.llm_provider).lower()
        self.model = model or cfg.llm_model
        self.api_key = api_key or cfg.llm_api_key
        self.base_url = base_url or cfg.llm_base_url
        self.timeout = timeout
        self.min_delay_seconds = (
            min_delay_seconds
            if min_delay_seconds is not None
            else cfg.llm_min_delay_seconds
        )
        self._last_call_time = 0.0
        self.call_count = 0

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any],
        http_client: Optional[Any] = None,
    ) -> Union[str, List[Dict[str, Any]]]:
        """
        Send messages and available tools to the LLM.
        Returns either:
        - str: final answer text
        - list[dict]: list of requested tool calls [{"name": "...", "args": {...}}]
        """
        self.call_count += 1
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

        if self.provider == "ollama":
            result = provider_fn(
                messages=messages,
                tools=tools,
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                http_client=http_client,
            )
        else:
            result = provider_fn(
                messages=messages,
                tools=tools,
                model=self.model,
                api_key=self.api_key,
            )

        self._last_call_time = time.time()
        return result

