# tests/test_file_store.py
"""Tests for the file store module."""

import time
import pytest
from unittest.mock import patch

from support_console.file_store import FileEntry, FileStore


class TestFileEntry:
    """Tests for the FileEntry dataclass."""

    def test_create_entry(self):
        entry = FileEntry(
            id="abc123",
            name="test.png",
            media_type="image/png",
            data=b"\x89PNG\r\n",
            created_at=time.monotonic(),
            size=6,
        )
        assert entry.id == "abc123"
        assert entry.name == "test.png"
        assert entry.size == 6


class TestFileStore:
    """Tests for the in-memory file store."""

    def test_add_and_get(self):
        store = FileStore()
        entry = store.add(name="test.png", media_type="image/png", data=b"fakepng")
        assert entry.name == "test.png"
        assert store.get(entry.id) is entry

    def test_get_missing_returns_none(self):
        store = FileStore()
        assert store.get("nonexistent") is None

    def test_pop_removes_entry(self):
        store = FileStore()
        entry = store.add(name="test.png", media_type="image/png", data=b"data")
        popped = store.pop(entry.id)
        assert popped is entry
        assert store.get(entry.id) is None

    def test_pop_missing_returns_none(self):
        store = FileStore()
        assert store.pop("nonexistent") is None

    def test_total_bytes_tracked(self):
        store = FileStore()
        store.add(name="a.txt", media_type="text/plain", data=b"hello")
        store.add(name="b.txt", media_type="text/plain", data=b"world!")
        assert store.total_bytes == 11

    def test_total_bytes_decreases_on_pop(self):
        store = FileStore()
        entry = store.add(name="a.txt", media_type="text/plain", data=b"hello")
        store.pop(entry.id)
        assert store.total_bytes == 0

    def test_memory_cap_enforced(self):
        store = FileStore(max_total_bytes=100)
        store.add(name="a.txt", media_type="text/plain", data=b"x" * 60)
        with pytest.raises(MemoryError, match="File store memory cap"):
            store.add(name="b.txt", media_type="text/plain", data=b"x" * 60)

    def test_cleanup_expired(self):
        store = FileStore(ttl_seconds=1.0)
        entry = store.add(name="old.txt", media_type="text/plain", data=b"old")
        # Fake the created_at to be in the past
        entry.created_at = time.monotonic() - 3600
        removed = store.cleanup_expired()
        assert removed == 1
        assert store.get(entry.id) is None
        assert store.total_bytes == 0

    def test_cleanup_keeps_fresh(self):
        store = FileStore(ttl_seconds=3600.0)
        store.add(name="new.txt", media_type="text/plain", data=b"new")
        removed = store.cleanup_expired()
        assert removed == 0
