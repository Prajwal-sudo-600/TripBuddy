"""
Unit tests for the Ollama provider in agent/llm.py.
Uses mock HTTP responses to verify:
1. Normal text answer returned by Ollama.
2. Tool call returned with parsed arguments dictionary.
3. Malformed tool-call JSON handled cleanly (raises LLMError).
4. Connection refused handled cleanly without crashing (raises LLMError).
5. Model not found (HTTP 404) handled with clear error message.
6. Timeout handled gracefully (raises LLMError).
"""

import json
import httpx
import pytest

from tripbuddy.agent.llm import LLMError, LLMInterface, call_ollama


def test_ollama_text_answer():
    """Verify normal text answer parsing."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["model"] == "llama3.1:8b"
        assert len(data["messages"]) == 1
        return httpx.Response(
            status_code=200,
            json={
                "model": "llama3.1:8b",
                "message": {
                    "role": "assistant",
                    "content": "Hello from local Ollama!",
                },
                "done": True,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    result = call_ollama(
        messages=[{"role": "user", "content": "Hello"}],
        tools=[],
        model="llama3.1:8b",
        http_client=client,
    )
    assert result == "Hello from local Ollama!"


def test_ollama_tool_call():
    """Verify tool call conversion to [{"name": ..., "args": {...}}]."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert len(data["tools"]) == 1
        assert data["tools"][0]["function"]["name"] == "search_hotels"
        return httpx.Response(
            status_code=200,
            json={
                "model": "llama3.1:8b",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "search_hotels",
                                "arguments": {"city": "Goa", "max_price_per_night": 4000},
                            }
                        }
                    ],
                },
                "done": True,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    tools = [
        {
            "name": "search_hotels",
            "description": "Search hotels in Goa",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    result = call_ollama(
        messages=[{"role": "user", "content": "Find hotels in Goa"}],
        tools=tools,
        model="llama3.1:8b",
        http_client=client,
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "search_hotels"
    assert result[0]["args"] == {"city": "Goa", "max_price_per_night": 4000}


def test_ollama_tool_call_string_arguments():
    """Verify tool call arguments when returned as JSON-string."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "model": "llama3.1:8b",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_reviews",
                                "arguments": '{"hotel_id": 2, "limit": 5}',
                            }
                        }
                    ],
                },
                "done": True,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    result = call_ollama(
        messages=[{"role": "user", "content": "Read reviews"}],
        tools=[],
        model="llama3.1:8b",
        http_client=client,
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "read_reviews"
    assert result[0]["args"] == {"hotel_id": 2, "limit": 5}


def test_ollama_malformed_json_tool_call():
    """Verify malformed JSON in tool call arguments raises LLMError."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "model": "llama3.1:8b",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "search_hotels",
                                "arguments": "{not-valid-json: broken",
                            }
                        }
                    ],
                },
                "done": True,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with pytest.raises(LLMError) as exc_info:
        call_ollama(
            messages=[{"role": "user", "content": "Find hotels"}],
            tools=[],
            model="llama3.1:8b",
            http_client=client,
        )
    assert "Malformed tool-call JSON" in str(exc_info.value)


def test_ollama_connection_refused():
    """Verify connection error / Ollama not running raises clear LLMError."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused to 127.0.0.1:11434")

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with pytest.raises(LLMError) as exc_info:
        call_ollama(
            messages=[{"role": "user", "content": "Hello"}],
            tools=[],
            model="llama3.1:8b",
            http_client=client,
        )
    assert "Ollama connection refused" in str(exc_info.value)


def test_ollama_model_not_found_404():
    """Verify HTTP 404 model not found raises clear LLMError with advice."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=404,
            text='{"error": "model \'missing-model\' not found"}',
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with pytest.raises(LLMError) as exc_info:
        call_ollama(
            messages=[{"role": "user", "content": "Hello"}],
            tools=[],
            model="missing-model",
            http_client=client,
        )
    assert "not found (HTTP 404)" in str(exc_info.value)
    assert "ollama pull" in str(exc_info.value)


def test_ollama_timeout():
    """Verify timeout raises LLMError without crashing."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Read timed out")

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with pytest.raises(LLMError) as exc_info:
        call_ollama(
            messages=[{"role": "user", "content": "Hello"}],
            tools=[],
            model="llama3.1:8b",
            http_client=client,
        )
    assert "timed out" in str(exc_info.value)


def test_llm_interface_with_ollama():
    """Verify LLMInterface dispatches to call_ollama with configured base_url."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/api/chat"
        return httpx.Response(
            status_code=200,
            json={
                "model": "llama3.1:8b",
                "message": {
                    "role": "assistant",
                    "content": "Interface dispatch succeeded",
                },
                "done": True,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    interface = LLMInterface(
        provider="ollama",
        model="llama3.1:8b",
        base_url="http://localhost:11434",
    )
    result = interface.chat(
        messages=[{"role": "user", "content": "Test"}],
        tools=[],
        http_client=client,
    )
    assert result == "Interface dispatch succeeded"
