"""FastAPI web server for Support Console.

Provides:
- WebSocket endpoint for Claude chat with streaming
- REST endpoints for IPython kernel execution and management
- REST endpoints for session persistence
- Static file serving for the web UI
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# WebSocket safety limits
WS_MAX_MESSAGE_SIZE = 1024 * 1024  # 1 MB max per message
WS_RATE_LIMIT_WINDOW = 5.0        # seconds
WS_RATE_LIMIT_MAX = 20            # max messages per window

from support_console.chat import ChatEngine
from support_console.kernel import KernelSession, KernelError, ExecutionTimeout
from support_console.sessions import SessionStore
from support_console.startup_template import DEFAULT_STARTUP, FLASK_STARTUP

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    code: str
    timeout: float = 30.0

    @property
    def clamped_timeout(self) -> float:
        """Return timeout clamped to a safe range (1-300 seconds)."""
        return max(1.0, min(self.timeout, 300.0))


class SessionCreateRequest(BaseModel):
    title: str = ""
    metadata: dict | None = None


class SessionSaveRequest(BaseModel):
    chat_messages: list | None = None
    notebook_cells: list | None = None
    title: str | None = None
    metadata: dict | None = None


# ---------------------------------------------------------------------------
# Application state (managed via lifespan)
# ---------------------------------------------------------------------------

class AppState:
    """Container for shared application state."""

    def __init__(
        self,
        app_root: str,
        session_db: str,
        api_key: str | None,
        system_prompt: str | None,
        startup_code: str,
        allowed_tools: list[str] | None = None,
    ):
        self.app_root = app_root
        self.session_db = session_db
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.startup_code = startup_code
        self.allowed_tools = allowed_tools

        self.kernel: Optional[KernelSession] = None
        self.sessions: Optional[SessionStore] = None
        self.chat_engine: Optional[ChatEngine] = None


def create_app(
    app_root: str = "/app/server",
    session_db: str = "/data/sessions.db",
    api_key: str | None = None,
    system_prompt: str | None = None,
    startup_code: str | None = None,
    allowed_tools: list[str] | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Parameters
    ----------
    app_root:
        Path to the Flask app source code (for Claude's file tools).
    session_db:
        Path to the SQLite database file for session persistence.
    api_key:
        Anthropic API key.  Falls back to ANTHROPIC_API_KEY env var.
    system_prompt:
        Optional override for Claude's system prompt.
    startup_code:
        Python code to run in the IPython kernel on startup.
        Defaults to a basic "Support Console ready" message.
    allowed_tools:
        Optional list of tools Claude can use.  Accepts spec names
        ("Read", "Grep", "Glob") or internal names ("read_file", etc.).
        Defaults to all tools.
    """
    if startup_code is None:
        startup_code = DEFAULT_STARTUP.format(custom_startup="")

    state = AppState(
        app_root=app_root,
        session_db=session_db,
        api_key=api_key,
        system_prompt=system_prompt,
        startup_code=startup_code,
        allowed_tools=allowed_tools,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Start kernel and session store on startup; clean up on shutdown."""
        logger.info("Starting Support Console...")

        # Session store
        state.sessions = SessionStore(state.session_db)
        await state.sessions.initialize()

        # Chat engine
        try:
            state.chat_engine = ChatEngine(
                api_key=state.api_key,
                app_root=state.app_root,
                system_prompt=state.system_prompt,
                allowed_tools=state.allowed_tools,
            )
        except ValueError as exc:
            logger.warning("Chat engine not available: %s", exc)

        # IPython kernel
        state.kernel = KernelSession(startup_code=state.startup_code)
        try:
            output = await state.kernel.start()
            logger.info("Kernel started. Startup output:\n%s", output)
        except KernelError as exc:
            logger.warning("Kernel failed to start: %s", exc)

        yield

        # Cleanup
        if state.kernel and state.kernel.is_alive:
            await state.kernel.shutdown()
        if state.sessions:
            await state.sessions.close()

        logger.info("Support Console stopped.")

    app = FastAPI(
        title="Support Console",
        description="AI-powered debug shell for Flask + SQLAlchemy apps",
        lifespan=lifespan,
    )

    # Store state on the app for access in route handlers
    app.state.console = state

    # -----------------------------------------------------------------
    # Chat WebSocket
    # -----------------------------------------------------------------

    @app.websocket("/api/chat")
    async def chat_ws(ws: WebSocket):
        """WebSocket endpoint for Claude chat with streaming.

        Client sends JSON messages: {"message": "user text", "messages": [...history...]}
        Server sends JSON events: {"type": "text"|"tool_use"|"tool_result"|"code_block"|"done"|"error", ...}
        """
        await ws.accept()
        engine = app.state.console.chat_engine

        if engine is None:
            await ws.send_json({"type": "error", "error": "Chat engine not configured (missing API key?)"})
            await ws.close()
            return

        # Simple rate limiter state for this connection
        msg_timestamps: list[float] = []

        try:
            while True:
                raw = await ws.receive_text()

                # Guard: message size
                if len(raw) > WS_MAX_MESSAGE_SIZE:
                    await ws.send_json({"type": "error", "error": "Message too large (max 1 MB)"})
                    continue

                # Guard: rate limit
                now = time.monotonic()
                msg_timestamps = [t for t in msg_timestamps if now - t < WS_RATE_LIMIT_WINDOW]
                msg_timestamps.append(now)
                if len(msg_timestamps) > WS_RATE_LIMIT_MAX:
                    await ws.send_json({"type": "error", "error": "Rate limit exceeded. Please slow down."})
                    continue

                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    await ws.send_json({"type": "error", "error": "Invalid JSON"})
                    continue

                user_message = data.get("message", "")
                history = data.get("messages", [])

                if not user_message:
                    await ws.send_json({"type": "error", "error": "Empty message"})
                    continue

                # Build messages list from history + new message
                messages = list(history)
                messages.append({"role": "user", "content": user_message})

                # Set up cancellation
                cancel_event = asyncio.Event()

                async def _stream_to_ws():
                    async for event in engine.chat_stream(messages, cancel_event=cancel_event):
                        await ws.send_json(event)

                stream_task = asyncio.create_task(_stream_to_ws())

                try:
                    # Listen for cancel while streaming
                    while not stream_task.done():
                        receive_task = asyncio.create_task(ws.receive_text())
                        done_tasks, pending_tasks = await asyncio.wait(
                            {stream_task, receive_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if receive_task in done_tasks:
                            try:
                                raw_msg = receive_task.result()
                                msg_data = json.loads(raw_msg)
                                if msg_data.get("type") == "cancel":
                                    cancel_event.set()
                                    await stream_task
                                    break
                            except (json.JSONDecodeError, WebSocketDisconnect):
                                cancel_event.set()
                                await stream_task
                                break
                        else:
                            # Stream finished first; cancel pending receive
                            receive_task.cancel()
                            try:
                                await receive_task
                            except asyncio.CancelledError:
                                pass

                    # Check for exceptions from stream task
                    if stream_task.done() and not stream_task.cancelled():
                        exc = stream_task.exception()
                        if exc:
                            raise exc

                except WebSocketDisconnect:
                    cancel_event.set()
                    if not stream_task.done():
                        stream_task.cancel()
                        try:
                            await stream_task
                        except asyncio.CancelledError:
                            pass
                    raise  # Re-raise to be caught by outer handler

                # Send back updated messages (even on cancel, for history consistency).
                # Wrap in try/except because the socket may have disconnected
                # (e.g., cancel triggered by disconnect).
                try:
                    await ws.send_json({
                        "type": "history_update",
                        "messages": messages,
                    })
                except (WebSocketDisconnect, Exception):
                    pass

        except WebSocketDisconnect:
            logger.info("Chat WebSocket disconnected")
        except Exception as exc:
            logger.exception("Chat WebSocket error")
            try:
                await ws.send_json({"type": "error", "error": str(exc)})
                await ws.close(code=1011)
            except Exception:
                pass

    # -----------------------------------------------------------------
    # Kernel endpoints
    # -----------------------------------------------------------------

    @app.post("/api/kernel/execute")
    async def kernel_execute(req: ExecuteRequest):
        """Execute code in the IPython kernel."""
        kernel = app.state.console.kernel
        if kernel is None or not kernel.is_alive:
            raise HTTPException(status_code=503, detail="Kernel not available")

        try:
            result = await kernel.execute(req.code, timeout=req.clamped_timeout)
            return result
        except ExecutionTimeout:
            raise HTTPException(status_code=408, detail="Execution timed out")
        except KernelError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/kernel/interrupt")
    async def kernel_interrupt():
        """Interrupt the currently running kernel execution."""
        kernel = app.state.console.kernel
        if kernel is None or not kernel.is_alive:
            raise HTTPException(status_code=503, detail="Kernel not available")

        success = await kernel.interrupt()
        return {"success": success}

    @app.get("/api/kernel/status")
    async def kernel_status():
        """Get kernel health status."""
        kernel = app.state.console.kernel
        if kernel is None:
            return {"alive": False, "busy": False}
        return kernel.status()

    @app.post("/api/kernel/restart")
    async def kernel_restart():
        """Restart the IPython kernel."""
        kernel = app.state.console.kernel
        if kernel is None:
            raise HTTPException(status_code=503, detail="Kernel not available")

        try:
            output = await kernel.restart()
            return {"output": output, "status": kernel.status()}
        except KernelError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # -----------------------------------------------------------------
    # Session endpoints
    # -----------------------------------------------------------------

    @app.get("/api/sessions")
    async def list_sessions():
        """List all saved sessions."""
        store = app.state.console.sessions
        if store is None:
            raise HTTPException(status_code=503, detail="Session store not available")
        return await store.list_sessions()

    @app.post("/api/sessions")
    async def create_session(req: SessionCreateRequest):
        """Create a new session."""
        store = app.state.console.sessions
        if store is None:
            raise HTTPException(status_code=503, detail="Session store not available")
        return await store.create_session(title=req.title, metadata=req.metadata)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        """Load a session by ID."""
        store = app.state.console.sessions
        if store is None:
            raise HTTPException(status_code=503, detail="Session store not available")

        session = await store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    @app.put("/api/sessions/{session_id}")
    async def save_session(session_id: str, req: SessionSaveRequest):
        """Update a session."""
        store = app.state.console.sessions
        if store is None:
            raise HTTPException(status_code=503, detail="Session store not available")

        try:
            await store.save_session(
                session_id,
                chat_messages=req.chat_messages,
                notebook_cells=req.notebook_cells,
                title=req.title,
                metadata=req.metadata,
            )
            return {"ok": True}
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        """Delete a session."""
        store = app.state.console.sessions
        if store is None:
            raise HTTPException(status_code=503, detail="Session store not available")

        await store.delete_session(session_id)
        return {"ok": True}

    # -----------------------------------------------------------------
    # Static file serving
    # -----------------------------------------------------------------

    @app.get("/")
    async def index():
        """Serve the main web UI."""
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse("<h1>Support Console</h1><p>Static files not found.</p>")

    # Mount static files for JS/CSS
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
