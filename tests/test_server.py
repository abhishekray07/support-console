"""Tests for the FastAPI server endpoints."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from support_console.server import create_app, AppState
from support_console.sessions import SessionStore


@pytest.fixture
async def app_with_client(tmp_db_path, sample_app_root):
    """Create a FastAPI app with mocked kernel, real session store, and async client.

    We create the app with patched constructors, then manually initialize
    the state since httpx.AsyncClient doesn't trigger ASGI lifespan.
    """
    with patch("support_console.server.KernelSession") as MockKernel, \
         patch("support_console.server.ChatEngine") as MockChat:

        # Mock kernel instance
        mock_kernel = MagicMock()
        mock_kernel.is_alive = True
        mock_kernel.is_busy = False
        mock_kernel.start = AsyncMock(return_value="Kernel ready")
        mock_kernel.shutdown = AsyncMock()
        mock_kernel.status = MagicMock(return_value={
            "session_id": "test123",
            "alive": True,
            "busy": False,
            "uptime": 10.0,
            "execution_count": 0,
        })
        mock_kernel.execute = AsyncMock(return_value={
            "stdout": "hello\n",
            "stderr": "",
            "result": "",
            "display_data": [],
            "status": "ok",
            "error": None,
        })
        mock_kernel.interrupt = AsyncMock(return_value=True)
        mock_kernel.restart = AsyncMock(return_value="Restarted")
        MockKernel.return_value = mock_kernel

        mock_chat = MagicMock()
        MockChat.return_value = mock_chat

        application = create_app(
            app_root=sample_app_root,
            session_db=tmp_db_path,
            api_key="sk-test-key",
        )

        # Manually initialize state since lifespan won't run
        state = application.state.console
        state.kernel = mock_kernel
        state.sessions = SessionStore(tmp_db_path)
        await state.sessions.initialize()
        state.chat_engine = mock_chat

        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            yield application, client

        await state.sessions.close()


@pytest.fixture
async def client(app_with_client):
    """Extract just the client from the combined fixture."""
    _, client = app_with_client
    return client


class TestKernelEndpoints:
    """Tests for kernel API endpoints."""

    async def test_kernel_status(self, client):
        resp = await client.get("/api/kernel/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["alive"] is True

    async def test_kernel_execute(self, client):
        resp = await client.post("/api/kernel/execute", json={"code": "print('hello')"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "hello" in data["stdout"]

    async def test_kernel_execute_with_timeout(self, client):
        resp = await client.post("/api/kernel/execute", json={"code": "1+1", "timeout": 5.0})
        assert resp.status_code == 200

    async def test_kernel_interrupt(self, client):
        resp = await client.post("/api/kernel/interrupt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    async def test_kernel_restart(self, client):
        resp = await client.post("/api/kernel/restart")
        assert resp.status_code == 200
        data = resp.json()
        assert "output" in data


class TestSessionEndpoints:
    """Tests for session API endpoints."""

    async def test_create_session(self, client):
        resp = await client.post("/api/sessions", json={"title": "Test"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Test"
        assert "id" in data

    async def test_list_sessions(self, client):
        await client.post("/api/sessions", json={"title": "A"})
        await client.post("/api/sessions", json={"title": "B"})

        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 2

    async def test_get_session(self, client):
        create_resp = await client.post("/api/sessions", json={"title": "Fetch Me"})
        session_id = create_resp.json()["id"]

        resp = await client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Fetch Me"

    async def test_get_session_not_found(self, client):
        resp = await client.get("/api/sessions/nonexistent")
        assert resp.status_code == 404

    async def test_save_session(self, client):
        create_resp = await client.post("/api/sessions", json={"title": "Update Me"})
        session_id = create_resp.json()["id"]

        resp = await client.put(
            f"/api/sessions/{session_id}",
            json={
                "title": "Updated",
                "chat_messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert resp.status_code == 200

        get_resp = await client.get(f"/api/sessions/{session_id}")
        assert get_resp.json()["title"] == "Updated"
        assert len(get_resp.json()["chat_messages"]) == 1

    async def test_save_session_not_found(self, client):
        resp = await client.put("/api/sessions/nonexistent", json={"title": "Nope"})
        assert resp.status_code == 404

    async def test_delete_session(self, client):
        create_resp = await client.post("/api/sessions", json={"title": "Delete Me"})
        session_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 200

        get_resp = await client.get(f"/api/sessions/{session_id}")
        assert get_resp.status_code == 404


class TestStaticFiles:
    """Tests for static file serving."""

    async def test_index_page(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.fixture
def ws_app(sample_app_root, tmp_db_path):
    """Create a FastAPI app for synchronous WebSocket testing.

    Uses Starlette TestClient which triggers ASGI lifespan, so we
    patch constructors to avoid real kernel/API connections.
    """
    with patch("support_console.server.KernelSession") as MockKernel, \
         patch("support_console.server.ChatEngine") as MockChat:

        mock_kernel = MagicMock()
        mock_kernel.is_alive = True
        mock_kernel.is_busy = False
        mock_kernel.start = AsyncMock(return_value="Kernel ready")
        mock_kernel.shutdown = AsyncMock()
        MockKernel.return_value = mock_kernel

        mock_chat = MagicMock()
        MockChat.return_value = mock_chat

        application = create_app(
            app_root=sample_app_root,
            session_db=tmp_db_path,
            api_key="sk-test-key",
        )

        yield application


class TestChatWebSocket:
    """Tests for the chat WebSocket endpoint."""

    def test_cancel_message_accepted(self, ws_app):
        """WebSocket accepts cancel messages and interrupts the stream."""
        # Mock chat_stream to stream slowly
        async def slow_stream(messages, cancel_event=None):
            for i in range(10):
                if cancel_event and cancel_event.is_set():
                    yield {"type": "done", "stop_reason": "cancelled", "message": None}
                    return
                yield {"type": "text", "content": f"chunk {i} "}
                await asyncio.sleep(0.1)
            yield {"type": "done", "stop_reason": "end_turn", "message": {}}

        with TestClient(ws_app) as client:
            # Assign mock AFTER TestClient enters (lifespan runs), so it
            # won't be overwritten by lifespan initialization.
            ws_app.state.console.chat_engine.chat_stream = slow_stream

            with client.websocket_connect("/api/chat") as ws:
                ws.send_json({"message": "hello", "messages": []})
                # Read first text chunk to confirm streaming started
                data = ws.receive_json()
                assert data["type"] == "text"

                # Send cancel
                ws.send_json({"type": "cancel"})

                # Should eventually get done or history_update
                events = []
                for _ in range(20):  # Safety limit to avoid hanging
                    ev = ws.receive_json()
                    events.append(ev)
                    if ev["type"] in ("done", "history_update"):
                        break

                types = [e["type"] for e in events]
                assert "done" in types or "history_update" in types

                # Verify the stream was actually cancelled (not just completed normally).
                # The done event should have stop_reason "cancelled", proving
                # the cancel_event was passed through and set.
                done_events = [e for e in events if e["type"] == "done"]
                assert any(
                    e.get("stop_reason") == "cancelled" for e in done_events
                ), f"Expected cancelled stop_reason, got events: {events}"
