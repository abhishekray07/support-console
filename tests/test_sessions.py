"""Tests for the session persistence layer."""

import pytest

from support_console.sessions import SessionStore


@pytest.fixture
async def store(tmp_db_path):
    """Create and initialize a SessionStore for testing."""
    s = SessionStore(tmp_db_path)
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_initialize_creates_db(tmp_db_path):
    """initialize() creates the database file and table."""
    import os

    store = SessionStore(tmp_db_path)
    assert not os.path.exists(tmp_db_path)

    await store.initialize()
    assert os.path.exists(tmp_db_path)
    await store.close()


@pytest.mark.asyncio
async def test_initialize_creates_parent_dirs(tmp_dir):
    """initialize() creates parent directories if they don't exist."""
    import os

    nested_path = os.path.join(tmp_dir, "a", "b", "c", "sessions.db")
    store = SessionStore(nested_path)
    await store.initialize()
    assert os.path.exists(nested_path)
    await store.close()


@pytest.mark.asyncio
async def test_initialize_idempotent(store):
    """Calling initialize() multiple times is safe."""
    await store.initialize()  # already initialized in fixture
    await store.initialize()  # should be no-op


@pytest.mark.asyncio
async def test_create_session(store):
    """create_session returns a session dict with all expected fields."""
    session = await store.create_session(title="Test Session", metadata={"customer": "acme"})

    assert "id" in session
    assert session["title"] == "Test Session"
    assert session["chat_messages"] == []
    assert session["notebook_cells"] == []
    assert session["metadata"] == {"customer": "acme"}
    assert "created_at" in session


@pytest.mark.asyncio
async def test_get_session(store):
    """get_session returns the full session data."""
    created = await store.create_session(title="Get Test")
    loaded = await store.get_session(created["id"])

    assert loaded is not None
    assert loaded["id"] == created["id"]
    assert loaded["title"] == "Get Test"
    assert loaded["chat_messages"] == []


@pytest.mark.asyncio
async def test_get_session_not_found(store):
    """get_session returns None for non-existent IDs."""
    result = await store.get_session("nonexistent-id")
    assert result is None


@pytest.mark.asyncio
async def test_save_session_updates_fields(store):
    """save_session updates only the specified fields."""
    session = await store.create_session(title="Original")

    messages = [{"role": "user", "content": "hello"}]
    cells = [{"code": "print(1)", "output": "1"}]
    await store.save_session(session["id"], chat_messages=messages, notebook_cells=cells)

    loaded = await store.get_session(session["id"])
    assert loaded["chat_messages"] == messages
    assert loaded["notebook_cells"] == cells
    assert loaded["title"] == "Original"  # unchanged


@pytest.mark.asyncio
async def test_save_session_update_title(store):
    """save_session can update just the title."""
    session = await store.create_session(title="Original")
    await store.save_session(session["id"], title="Updated Title")

    loaded = await store.get_session(session["id"])
    assert loaded["title"] == "Updated Title"


@pytest.mark.asyncio
async def test_save_session_not_found(store):
    """save_session raises KeyError for non-existent session."""
    with pytest.raises(KeyError):
        await store.save_session("nonexistent-id", title="nope")


@pytest.mark.asyncio
async def test_save_session_no_fields(store):
    """save_session raises ValueError if no fields provided."""
    session = await store.create_session()
    with pytest.raises(ValueError):
        await store.save_session(session["id"])


@pytest.mark.asyncio
async def test_list_sessions(store):
    """list_sessions returns summaries ordered by creation time."""
    await store.create_session(title="First")
    await store.create_session(title="Second")
    await store.create_session(title="Third")

    sessions = await store.list_sessions()
    assert len(sessions) == 3
    # Most recent first
    assert sessions[0]["title"] == "Third"
    assert sessions[1]["title"] == "Second"
    assert sessions[2]["title"] == "First"

    # Summaries should not include chat_messages or notebook_cells
    assert "chat_messages" not in sessions[0]
    assert "notebook_cells" not in sessions[0]


@pytest.mark.asyncio
async def test_list_sessions_limit(store):
    """list_sessions respects the limit parameter."""
    for i in range(5):
        await store.create_session(title=f"Session {i}")

    sessions = await store.list_sessions(limit=3)
    assert len(sessions) == 3


@pytest.mark.asyncio
async def test_delete_session(store):
    """delete_session removes the session."""
    session = await store.create_session(title="To Delete")
    await store.delete_session(session["id"])

    loaded = await store.get_session(session["id"])
    assert loaded is None


@pytest.mark.asyncio
async def test_delete_session_idempotent(store):
    """delete_session is a no-op for non-existent IDs."""
    await store.delete_session("nonexistent-id")  # should not raise


@pytest.mark.asyncio
async def test_context_manager(tmp_db_path):
    """SessionStore works as an async context manager."""
    async with SessionStore(tmp_db_path) as store:
        session = await store.create_session(title="Context Manager Test")
        assert session["id"]

    # After exiting, the store is closed
    assert store._db is None


@pytest.mark.asyncio
async def test_operations_before_initialize(tmp_db_path):
    """Operations raise RuntimeError if store not initialized."""
    store = SessionStore(tmp_db_path)
    with pytest.raises(RuntimeError):
        await store.create_session()
