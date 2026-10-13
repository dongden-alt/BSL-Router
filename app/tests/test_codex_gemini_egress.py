"""
Regression test: codex models on Gemini egress lane must convert Responses SSE to OpenAI SSE.

Bug: codex/gpt-5.6-sol returned HTTP 200 with zero content and In:0 Out:0 because
gemini_egress_stream parsed raw Codex Responses-API SSE as OpenAI chat.completion.chunk SSE.

Fix: Route codex upstream bytes through codex_adapter.responses_sse_to_openai_sse(...).

Pure TestClient harness (no pytest-asyncio) mirroring test_fel_wiring.py pattern.
"""

from fastapi.testclient import TestClient
import app.main as main
import app.config_state as cs
import json
import httpx


# Fake Codex upstream SSE bytes (Responses API format)
FAKE_CODEX_RESPONSES_SSE = b"""event: response.created
data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.6-sol","status":"in_progress"}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"Hello"}

event: response.output_text.delta
data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":" world"}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_1","model":"gpt-5.6-sol","status":"completed","output":[],"usage":{"input_tokens":12,"output_tokens":5}}}
"""

# Fake OpenAI upstream SSE bytes (for regression guard)
FAKE_OPENAI_SSE = b"""data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}

data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":5,"total_tokens":17}}
"""


class _FakeCodexUpstream:
    """Mock httpx.Response that yields fake Codex Responses SSE bytes."""
    def __init__(self, sse_bytes: bytes, status_code: int = 200):
        self.sse_bytes = sse_bytes
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream"}

    async def aiter_raw(self, chunk_size=None):
        """Yield the entire SSE payload in one chunk."""
        yield self.sse_bytes

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    async def aclose(self):
        pass


class _RecordingCodexClient:
    """Replaces httpx.AsyncClient for codex requests; records payloads, returns fake Responses SSE."""
    def __init__(self):
        self.payloads = []
        self.urls = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        self.entered = True
        self.urls.append(str(request.url))
        if request.content:
            try:
                self.payloads.append(json.loads(request.content))
            except json.JSONDecodeError:
                self.payloads.append(None)
        # Return fake Codex Responses SSE
        return _FakeCodexUpstream(FAKE_CODEX_RESPONSES_SSE, status_code=200)


class _RecordingOpenAIClient:
    """Replaces httpx.AsyncClient for non-codex OpenAI requests; returns fake OpenAI SSE."""
    def __init__(self):
        self.payloads = []
        self.urls = []
        self.entered = False

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, stream=False):
        self.entered = True
        self.urls.append(str(request.url))
        if request.content:
            try:
                self.payloads.append(json.loads(request.content))
            except json.JSONDecodeError:
                self.payloads.append(None)
        # Return fake OpenAI SSE
        return _FakeCodexUpstream(FAKE_OPENAI_SSE, status_code=200)


def _base_cfg():
    """Minimal config with codex provider registered."""
    return {
        "tools": {"output_thinking_squeeze": False},
        "upstream_stream_buffer": {"enabled": False},
        "providers": {
            "codex": {
                "type": "custom",
                "format": "codex",
                "connections": [{"enabled": True}],
                "models": [{"id": "gpt-5.6-sol", "enabled": True}],
            },
            "openai": {
                "type": "custom",
                "format": "openai",
                "connections": [{"enabled": True, "api_key": "test", "base_url": "https://api.openai.com/v1"}],
                "models": [{"id": "gpt-4", "enabled": True}],
            },
        },
        "combos": [],
        "aliases": {},
        # The antigravity lane (client=antigravity) short-circuits to a native
        # Google fallback 401 frame when no mapping resolves (main.py ~L15470).
        # Map the source models to provider targets so the request reaches the
        # mocked upstream clients.
        "antigravity_integration": {
            "enabled": True,
            "mappings": {
                "codex--gpt-5.6-sol": "codex/gpt-5.6-sol",
                "gpt-4": "openai/gpt-4",
            },
        },
    }


def test_codex_gemini_egress_converts_responses_sse(monkeypatch):
    """
    Codex upstream bytes must flow through codex_adapter.responses_sse_to_openai_sse
    before the Gemini drain loop parses them.

    Assert:
    (a) streamed Gemini output contains candidates/parts frames with "Hello" and " world"
    (b) usage data flows (prompt_tokens=12, completion_tokens=5)
    (c) stream terminates normally (no hang)
    """
    upstream = _RecordingCodexClient()
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: upstream)
    cs.replace_config(_base_cfg())

    client = TestClient(main.app)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "test"}]}],
        "generationConfig": {"temperature": 0.7}
    }

    response = client.post(
        "/v1beta/models/codex--gpt-5.6-sol:streamGenerateContent?alt=sse&client=antigravity",
        json=payload,
        headers={"X-BSL-Codex-Token": "fake_token", "content-type": "application/json"}
    )

    assert response.status_code == 200, f"Unexpected status: {response.status_code}"
    assert "text/event-stream" in response.headers.get("content-type", "")

    # Parse streamed Gemini frames
    frames = []
    content_parts = []
    usage_found = False
    prompt_tokens = 0
    completion_tokens = 0

    for line in response.text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            data_str = line[6:]
            if data_str and data_str != "[DONE]":
                try:
                    frame = json.loads(data_str)
                    frames.append(frame)

                    # Antigravity lane wraps Gemini payloads as {"response": {...}};
                    # handle both wrapped and bare streamGenerateContent shapes.
                    node = frame.get("response", frame)

                    # Extract content from candidates/parts
                    if "candidates" in node:
                        for candidate in node["candidates"]:
                            if "content" in candidate and "parts" in candidate["content"]:
                                for part in candidate["content"]["parts"]:
                                    if "text" in part:
                                        content_parts.append(part["text"])

                    # Extract usage
                    if "usageMetadata" in node:
                        usage = node["usageMetadata"]
                        if "promptTokenCount" in usage:
                            prompt_tokens = usage["promptTokenCount"]
                        if "candidatesTokenCount" in usage:
                            completion_tokens = usage["candidatesTokenCount"]
                        usage_found = True
                except json.JSONDecodeError:
                    pass

    # Assert (a): content converted correctly
    full_content = "".join(content_parts)
    assert "Hello" in full_content, f"Expected 'Hello' in content, got: {full_content}"
    assert "world" in full_content, f"Expected 'world' in content, got: {full_content}"

    # Assert (b): usage data flows
    assert usage_found, f"Usage metadata not found in stream. Frames: {frames}"
    assert prompt_tokens == 12, f"Expected prompt_tokens=12, got {prompt_tokens}"
    assert completion_tokens == 5, f"Expected completion_tokens=5, got {completion_tokens}"

    # Assert (c): stream terminated (received frames, no hang)
    assert len(frames) > 0, "Stream produced no frames"


def test_non_codex_gemini_egress_unchanged(monkeypatch):
    """
    Regression guard: non-codex OpenAI-shaped upstream through Gemini lane
    must still stream correctly after the codex branch addition.
    """
    upstream = _RecordingOpenAIClient()
    monkeypatch.setattr(main, "_get_client_for_proxy", lambda *_args: upstream)
    cs.replace_config(_base_cfg())

    client = TestClient(main.app)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "test"}]}],
        "generationConfig": {"temperature": 0.7}
    }

    response = client.post(
        "/v1beta/models/gpt-4:streamGenerateContent?alt=sse&client=antigravity",
        json=payload,
        headers={"Authorization": "Bearer fake_token", "content-type": "application/json"}
    )

    assert response.status_code == 200

    # Parse streamed Gemini frames
    content_parts = []

    for line in response.text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            data_str = line[6:]
            if data_str and data_str != "[DONE]":
                try:
                    frame = json.loads(data_str)
                    node = frame.get("response", frame)
                    if "candidates" in node:
                        for candidate in node["candidates"]:
                            if "content" in candidate and "parts" in candidate["content"]:
                                for part in candidate["content"]["parts"]:
                                    if "text" in part:
                                        content_parts.append(part["text"])
                except json.JSONDecodeError:
                    pass

    # Non-codex providers should still stream correctly
    full_content = "".join(content_parts)
    assert "Hello" in full_content, f"Non-codex provider broken: {full_content}"
    assert "world" in full_content, f"Non-codex provider broken: {full_content}"
