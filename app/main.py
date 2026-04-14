import asyncio
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import session_store as ss
from app.llm_client import get_client
from app.schemas import ChatCompletionRequest
from app.settings import get_settings
from app.stream_handler import stream_with_dify_tools
from app.tool_registry import registry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Load Dify tool registry from YAML
    registry.load(settings.tools_config)
    # Start session-store cleanup background task
    ss.store.start_cleanup()
    logger.info("Agora ConvoAI Custom LLM Wrapper started (port %d)", settings.app_port)
    yield
    ss.store.stop_cleanup()
    logger.info("Wrapper shutting down.")


app = FastAPI(
    title="Agora ConvoAI Custom LLM Wrapper",
    description=(
        "OpenAI-compatible /chat/completions proxy with async Dify tool dispatch "
        "and Agora RTM result delivery."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "tools": list(registry._tools.keys())}


@app.post("/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest, http_request: Request):
    if not request.stream:
        raise HTTPException(status_code=400, detail="Only streaming (stream=true) is supported.")

    client = get_client()

    async def generate():
        try:
            async for chunk in stream_with_dify_tools(request, client):
                yield chunk
        except asyncio.CancelledError:
            logger.info("Client disconnected, stream cancelled.")
            raise
        except Exception as e:
            tb = "".join(traceback.format_tb(e.__traceback__))
            logger.error("Stream error: %s\n%s", e, tb)
            raise

    return StreamingResponse(generate(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=False)
