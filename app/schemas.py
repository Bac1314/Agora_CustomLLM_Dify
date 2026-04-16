from pydantic import BaseModel, HttpUrl
from typing import Any, Dict, List, Optional, Union


class TextContent(BaseModel):
    type: str = "text"
    text: str


class ImageContent(BaseModel):
    type: str = "image"
    image_url: HttpUrl


class AudioContent(BaseModel):
    type: str = "input_audio"
    input_audio: Dict[str, str]


class ToolFunction(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    strict: bool = False


class Tool(BaseModel):
    type: str = "function"
    function: ToolFunction


class ToolChoice(BaseModel):
    type: str = "function"
    function: Optional[Dict[str, Any]] = None


class ResponseFormat(BaseModel):
    type: str = "json_schema"
    json_schema: Optional[Dict[str, Any]] = None


class SystemMessage(BaseModel):
    role: str = "system"
    content: Union[str, List[str]]


class UserMessage(BaseModel):
    role: str = "user"
    content: Union[str, List[Union[TextContent, ImageContent, AudioContent]]]
    # Agora ConvoAI extra metadata fields
    turn_id: Optional[int] = None
    timestamp: Optional[int] = None
    metadata: Optional[Dict[str, Union[str, int, float]]] = None


class AssistantMessage(BaseModel):
    role: str = "assistant"
    content: Optional[Union[str, List[TextContent]]] = None
    audio: Optional[Dict[str, str]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ToolMessage(BaseModel):
    role: str = "tool"
    content: Union[str, List[str]]
    tool_call_id: str


AnyMessage = Union[SystemMessage, UserMessage, AssistantMessage, ToolMessage]


class ChatCompletionRequest(BaseModel):
    # Agora ConvoAI flattens its `params` block into the request root, so these
    # arrive as top-level fields.  They are also echoed inside `metadata` if the
    # caller puts them there, and `context` is kept for backward compatibility.
    app_id: Optional[str] = None
    channel_name: Optional[str] = None
    user_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    messages: List[AnyMessage]
    response_format: Optional[ResponseFormat] = None
    modalities: List[str] = ["text"]
    audio: Optional[Dict[str, str]] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, ToolChoice]] = "auto"
    parallel_tool_calls: bool = True
    stream: bool = True
    stream_options: Optional[Dict[str, Any]] = None
