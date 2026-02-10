# support_console/file_store.py
"""In-memory file store for uploaded files.

Stores file data keyed by UUID with automatic TTL expiry,
memory cap enforcement, and consumption-based eviction.
"""

import time
import uuid
from dataclasses import dataclass

# Defaults
DEFAULT_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB
DEFAULT_TTL_SECONDS = 3600.0                   # 1 hour


@dataclass
class FileEntry:
    """A single uploaded file stored in memory."""
    id: str
    name: str
    media_type: str
    data: bytes
    created_at: float
    size: int


class FileStore:
    """Asyncio-safe in-memory file store with TTL and memory cap."""

    def __init__(
        self,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ):
        self._entries: dict[str, FileEntry] = {}
        self._max_total_bytes = max_total_bytes
        self._ttl_seconds = ttl_seconds
        self.total_bytes: int = 0

    def add(self, *, name: str, media_type: str, data: bytes) -> FileEntry:
        """Store a file. Raises MemoryError if the memory cap would be exceeded."""
        size = len(data)
        if self.total_bytes + size > self._max_total_bytes:
            raise MemoryError(
                f"File store memory cap exceeded "
                f"({self.total_bytes + size} > {self._max_total_bytes})"
            )

        entry = FileEntry(
            id=str(uuid.uuid4()),
            name=name,
            media_type=media_type,
            data=data,
            created_at=time.monotonic(),
            size=size,
        )
        self._entries[entry.id] = entry
        self.total_bytes += size
        return entry

    def get(self, file_id: str) -> FileEntry | None:
        """Look up a file by ID. Returns None if not found."""
        return self._entries.get(file_id)

    def pop(self, file_id: str) -> FileEntry | None:
        """Remove and return a file by ID. Returns None if not found."""
        entry = self._entries.pop(file_id, None)
        if entry is not None:
            self.total_bytes -= entry.size
        return entry

    def cleanup_expired(self) -> int:
        """Remove entries older than TTL. Returns count of removed entries."""
        now = time.monotonic()
        expired = [
            fid for fid, entry in self._entries.items()
            if now - entry.created_at > self._ttl_seconds
        ]
        for fid in expired:
            self.pop(fid)
        return len(expired)
