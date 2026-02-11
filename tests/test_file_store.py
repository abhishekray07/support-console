# tests/test_file_store.py
"""Tests for the file store module."""

import time
import pytest
from unittest.mock import patch

from support_console.file_store import FileEntry, FileStore
from support_console.file_store import sanitize_filename, validate_file, detect_media_type, ValidationError

# Allowed extensions/types for reference
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_TEXT_EXTENSIONS = {
    ".txt", ".log", ".csv", ".json", ".xml", ".yaml",
    ".py", ".js", ".ts", ".html", ".css", ".md",
    ".sh", ".sql", ".toml", ".ini", ".cfg", ".conf",
}


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


class TestSanitizeFilename:

    def test_simple_name(self):
        assert sanitize_filename("hello.txt") == "hello.txt"

    def test_strips_path(self):
        assert sanitize_filename("/usr/local/file.py") == "file.py"
        assert sanitize_filename("C:\\Users\\file.py") == "file.py"

    def test_strips_null_bytes(self):
        assert sanitize_filename("file\x00.txt") == "file.txt"

    def test_strips_control_chars(self):
        assert sanitize_filename("file\n\r\t.txt") == "file.txt"

    def test_truncates_long_names(self):
        name = "a" * 300 + ".txt"
        result = sanitize_filename(name)
        assert len(result) <= 255

    def test_empty_name_gets_default(self):
        result = sanitize_filename("")
        assert result == "unnamed"


class TestValidateFile:

    def test_valid_png(self, tmp_path):
        # Minimal valid PNG: signature + IHDR chunk (required for libmagic detection)
        import struct, zlib
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
        ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
        png_data = sig + ihdr
        # validate_file checks extension + magic bytes agreement
        validate_file(name="screenshot.png", data=png_data)  # should not raise

    def test_valid_text_file(self):
        validate_file(name="app.log", data=b"2024-01-01 INFO: started\n")

    def test_rejects_disallowed_extension(self):
        with pytest.raises(ValidationError, match="Unsupported file type"):
            validate_file(name="evil.exe", data=b"MZ\x90\x00")

    def test_rejects_oversized_file(self):
        with pytest.raises(ValidationError, match="exceeds"):
            validate_file(name="big.txt", data=b"x" * (10 * 1024 * 1024 + 1))

    def test_rejects_env_files(self):
        with pytest.raises(ValidationError, match="Unsupported file type"):
            validate_file(name="secrets.env", data=b"API_KEY=foo")

    def test_rejects_non_utf8_text(self):
        with pytest.raises(ValidationError, match="valid UTF-8"):
            validate_file(name="bad.txt", data=b"\xff\xfe" + b"\x80" * 100)

    def test_strips_null_from_text(self):
        # Should not raise — nulls are stripped
        validate_file(name="has_nulls.log", data=b"line1\x00line2\n")
