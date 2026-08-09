from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Union, Dict, Any

class MessageContentPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    text: Optional[str] = None
    image_url: Optional[Dict[str, Any]] = None
    document: Optional[Dict[str, Any]] = None
    source: Optional[Dict[str, Any]] = None  # Anthropic style document source

class ToolCallFunction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    arguments: str

class ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    type: str = "function"
    function: ToolCallFunction

class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: Optional[Union[str, List[Union[Dict[str, Any], MessageContentPart]]]] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    # extra="allow" preserves unknown fields (e.g. response_format, seed, reasoning_effort)
    # so they survive Pydantic validation and reach upstream providers.
    # Previously extra="ignore" silently dropped response_format/json_schema, breaking
    # structured-output clients that rely on it (DeepSeek, GPT-4o, GLM, etc).
    model_config = ConfigDict(extra="allow")
    model: str
    messages: List[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    # Explicit fields for commonly-used params (ensures model_dump includes them)
    response_format: Optional[Dict[str, Any]] = None
    seed: Optional[int] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    n: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    user: Optional[str] = None
    reasoning_effort: Optional[str] = None


class MitmProcessOwner(BaseModel):
    """Authoritative listener identity exposed by MITM lifecycle diagnostics."""
    pid: int
    name: str
    parent_pid: Optional[int] = None
    parent_chain: List[Dict[str, Any]] = Field(default_factory=list)
    is_bsl_mitm: bool = False


class MitmRuntimeStatus(BaseModel):
    """Reconciled passive-supervisor state for the configured MITM port."""
    state: str
    inspection_error: Optional[str] = None
    server: bool = False
    port_occupied: Optional[bool] = None
    owners: List[MitmProcessOwner] = Field(default_factory=list)
    conflict: Optional[bool] = None
    port: int = 443
    desired_running: bool = False
    tracked_pid: Optional[int] = None
    ownership_verified: bool = False
    ownership_lost: bool = False
    transition: Optional[str] = None
    observed_at: float
    lifecycle_events: List[Dict[str, Any]] = Field(default_factory=list)
