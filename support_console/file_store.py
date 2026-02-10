# support_console/file_store.py
"""In-memory file store for uploaded files.

Stores file data keyed by UUID with automatic TTL expiry,
memory cap enforcement, and consumption-based eviction.
"""

import os
import re
import time
import uuid
from dataclasses import dataclass

import magic as libmagic

# Defaults
DEFAULT_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB
DEFAULT_TTL_SECONDS = 3600.0                   # 1 hour

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_TEXT_CONTENT = 500 * 1024      # 500 KB for text sent to Claude

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_TEXT_EXTENSIONS = {
    ".txt", ".log", ".csv", ".json", ".xml", ".yaml",
    ".py", ".js", ".ts", ".html", ".css", ".md",
    ".sh", ".sql", ".toml", ".ini", ".cfg", ".conf",
}
ALLOWED_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_TEXT_EXTENSIONS

# Exact set of image MIME types we accept — no prefix matching
_ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class ValidationError(ValueError):
    """Raised when a file fails validation."""
    pass


def sanitize_filename(name: str) -> str:
    """Sanitize an uploaded filename: basename only, no control chars, max 255."""
    # Extract basename (handles both / and \ separators)
    name = os.path.basename(name.replace("\\", "/"))
    # Strip null bytes and control characters
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    # Truncate
    if len(name) > 255:
        base, ext = os.path.splitext(name)
        name = base[: 255 - len(ext)] + ext
    return name or "unnamed"


def detect_media_type(data: bytes) -> str:
    """Detect the MIME type of a file using libmagic. Returns the detected MIME string."""
    return libmagic.from_buffer(data[:2048], mime=True)


def validate_file(*, name: str, data: bytes) -> None:
    """Validate an uploaded file. Raises ValidationError on failure."""
    # Size check
    if len(data) > MAX_FILE_SIZE:
        raise ValidationError(
            f"File '{name}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit"
        )

    # Extension check
    _, ext = os.path.splitext(name.lower())
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(f"Unsupported file type: '{ext}'")

    # Image validation: check magic bytes agree with exact MIME set
    if ext in ALLOWED_IMAGE_EXTENSIONS:
        detected = detect_media_type(data)
        if detected not in _ALLOWED_IMAGE_MIMES:
            raise ValidationError(
                f"File '{name}' has extension '{ext}' but detected type is '{detected}'"
            )

    # Text validation: must be valid UTF-8
    if ext in ALLOWED_TEXT_EXTENSIONS:
        try:
            data.replace(b"\x00", b"").decode("utf-8")
        except UnicodeDecodeError:
            raise ValidationError(f"File '{name}' is not valid UTF-8 text")


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
