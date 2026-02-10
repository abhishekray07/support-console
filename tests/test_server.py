"""Tests for the FastAPI server endpoints."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

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


class TestUploadEndpoint:
    """Tests for the POST /api/upload endpoint."""

    async def test_upload_single_text_file(self, client):
        files = [("files", ("test.txt", b"hello world", "text/plain"))]
        resp = await client.post(
            "/api/upload",
            files=files,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["files"]) == 1
        assert data["files"][0]["name"] == "test.txt"
        assert data["files"][0]["size"] == 11

    async def test_upload_multiple_files(self, client):
        files = [
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbb", "text/plain")),
        ]
        resp = await client.post(
            "/api/upload",
            files=files,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["files"]) == 2

    async def test_upload_rejects_without_csrf_header(self, client):
        files = [("files", ("test.txt", b"hello", "text/plain"))]
        resp = await client.post("/api/upload", files=files)
        assert resp.status_code == 403

    async def test_upload_rejects_disallowed_type(self, client):
        files = [("files", ("evil.exe", b"MZ\x90\x00", "application/octet-stream"))]
        resp = await client.post(
            "/api/upload",
            files=files,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 415

    async def test_upload_rejects_oversized_file(self, client):
        big_data = b"x" * (10 * 1024 * 1024 + 1)
        files = [("files", ("big.txt", big_data, "text/plain"))]
        resp = await client.post(
            "/api/upload",
            files=files,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 413

    async def test_upload_rejects_too_many_files(self, client):
        files = [("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(6)]
        resp = await client.post(
            "/api/upload",
            files=files,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 400

    async def test_upload_rejects_no_files(self, client):
        """FastAPI returns 422 when required File(...) field is missing."""
        resp = await client.post(
            "/api/upload",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 422


class TestFileIdResolution:
    """Tests for file ID resolution in the WebSocket handler path."""

    async def test_file_store_integration(self, app_with_client):
        """File store on AppState can store and pop entries."""
        app, client = app_with_client
        store = app.state.console.file_store

        entry = store.add(name="test.txt", media_type="text/plain", data=b"hello")
        assert store.get(entry.id) is not None

        popped = store.pop(entry.id)
        assert popped.name == "test.txt"
        assert store.get(entry.id) is None

    async def test_upload_then_retrieve(self, app_with_client):
        """Upload a file, then verify it's in the file store."""
        app, client = app_with_client

        files = [("files", ("test.txt", b"hello", "text/plain"))]
        resp = await client.post(
            "/api/upload",
            files=files,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        file_id = resp.json()["files"][0]["id"]

        # Verify it's in the store
        entry = app.state.console.file_store.get(file_id)
        assert entry is not None
        assert entry.data == b"hello"


class TestUploadAndChatIntegration:
    """Integration test: upload files, then verify they are in the store."""

    async def test_full_upload_flow(self, app_with_client):
        """Upload files via HTTP, verify store, then pop (simulating WS handler)."""
        app, client = app_with_client
        store = app.state.console.file_store

        # Upload
        files = [
            ("files", ("readme.md", b"# Hello\n", "text/markdown")),
            ("files", ("data.csv", b"a,b\n1,2\n", "text/csv")),
        ]
        resp = await client.post(
            "/api/upload",
            files=files,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        uploaded = resp.json()["files"]
        assert len(uploaded) == 2

        # Verify both are in store
        for f in uploaded:
            assert store.get(f["id"]) is not None

        # Simulate WS handler: pop files
        resolved = []
        for f in uploaded:
            entry = store.pop(f["id"])
            assert entry is not None
            resolved.append(entry)

        assert len(resolved) == 2
        assert resolved[0].name == "readme.md"
        assert resolved[1].name == "data.csv"

        # Verify they're gone from store
        for f in uploaded:
            assert store.get(f["id"]) is None
        assert store.total_bytes == 0
