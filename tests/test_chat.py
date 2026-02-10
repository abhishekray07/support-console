"""Tests for the chat module (tool implementations and helpers)."""

import os
import pytest

from support_console.chat import (
    _resolve_sandboxed,
    _tool_read_file,
    _tool_grep,
    _tool_glob_search,
    _extract_code_blocks,
    _is_likely_binary,
    ChatEngine,
)


class TestPathSandboxing:
    """Tests for path sandboxing logic."""

    def test_valid_path(self, sample_app_root):
        """Paths within the app root resolve correctly."""
        result = _resolve_sandboxed(
            os.path.join(sample_app_root, "config.py"), sample_app_root
        )
        assert result.exists()

    def test_escape_blocked(self, sample_app_root):
        """Paths escaping the sandbox are rejected."""
        with pytest.raises(ValueError, match="Access denied"):
            _resolve_sandboxed("/etc/passwd", sample_app_root)

    def test_dotdot_escape_blocked(self, sample_app_root):
        """Relative path traversal is blocked."""
        with pytest.raises(ValueError, match="Access denied"):
            _resolve_sandboxed(
                os.path.join(sample_app_root, "..", "..", "etc", "passwd"),
                sample_app_root,
            )


class TestToolReadFile:
    """Tests for the read_file tool."""

    def test_read_existing_file(self, sample_app_root):
        """Reads a file successfully."""
        result = _tool_read_file(
            os.path.join(sample_app_root, "config.py"), sample_app_root
        )
        assert "DATABASE_URL" in result

    def test_read_nonexistent(self, sample_app_root):
        """Returns error for missing files."""
        result = _tool_read_file(
            os.path.join(sample_app_root, "nope.py"), sample_app_root
        )
        assert "not found" in result.lower() or "error" in result.lower()

    def test_read_outside_sandbox(self, sample_app_root):
        """Reading outside sandbox raises ValueError."""
        with pytest.raises(ValueError, match="Access denied"):
            _tool_read_file("/etc/passwd", sample_app_root)

    def test_read_large_file_truncated(self, sample_app_root):
        """Large files are truncated."""
        large_file = os.path.join(sample_app_root, "large.py")
        with open(large_file, "w") as f:
            f.write("x" * 200_000)

        result = _tool_read_file(large_file, sample_app_root)
        assert "Truncated" in result


class TestToolGrep:
    """Tests for the grep tool."""

    def test_grep_finds_matches(self, sample_app_root):
        """Grep finds matching lines."""
        result = _tool_grep("class.*Model", sample_app_root, "*.py", sample_app_root)
        assert "User" in result
        assert "Asset" in result

    def test_grep_no_matches(self, sample_app_root):
        """Grep returns message when no matches found."""
        result = _tool_grep("NONEXISTENT_PATTERN_XYZ", sample_app_root, "*.py", sample_app_root)
        assert "No matches" in result

    def test_grep_invalid_regex(self, sample_app_root):
        """Grep handles invalid regex gracefully."""
        result = _tool_grep("[invalid", sample_app_root, None, sample_app_root)
        assert "invalid regex" in result.lower() or "error" in result.lower()

    def test_grep_single_file(self, sample_app_root):
        """Grep can search a single file."""
        path = os.path.join(sample_app_root, "models", "user.py")
        result = _tool_grep("email", path, None, sample_app_root)
        assert "email" in result

    def test_grep_outside_sandbox(self, sample_app_root):
        """Grep outside sandbox raises ValueError."""
        with pytest.raises(ValueError, match="Access denied"):
            _tool_grep("root", "/etc", None, sample_app_root)


class TestToolGlobSearch:
    """Tests for the glob_search tool."""

    def test_glob_finds_python_files(self, sample_app_root):
        """Glob finds .py files."""
        result = _tool_glob_search("**/*.py", sample_app_root, sample_app_root)
        assert "config.py" in result
        assert "user.py" in result

    def test_glob_no_matches(self, sample_app_root):
        """Glob returns message when no matches found."""
        result = _tool_glob_search("**/*.rs", sample_app_root, sample_app_root)
        assert "No files found" in result

    def test_glob_outside_sandbox(self, sample_app_root):
        """Glob outside sandbox raises ValueError."""
        with pytest.raises(ValueError, match="Access denied"):
            _tool_glob_search("*", "/etc", sample_app_root)

    def test_glob_dotdot_pattern_rejected(self, sample_app_root):
        """Glob patterns containing '..' are rejected."""
        result = _tool_glob_search("../../etc/*", sample_app_root, sample_app_root)
        assert "invalid glob pattern" in result.lower()

    def test_glob_absolute_pattern_rejected(self, sample_app_root):
        """Glob patterns starting with '/' are rejected."""
        result = _tool_glob_search("/etc/passwd", sample_app_root, sample_app_root)
        assert "invalid glob pattern" in result.lower()


class TestGrepIncludePatternValidation:
    """Tests for grep include pattern validation."""

    def test_grep_dotdot_include_rejected(self, sample_app_root):
        """Grep include patterns containing '..' are rejected."""
        result = _tool_grep("test", sample_app_root, "../../etc/*", sample_app_root)
        assert "invalid include pattern" in result.lower()

    def test_grep_absolute_include_rejected(self, sample_app_root):
        """Grep include patterns starting with '/' are rejected."""
        result = _tool_grep("test", sample_app_root, "/etc/passwd", sample_app_root)
        assert "invalid include pattern" in result.lower()


class TestSymlinkSandboxing:
    """Tests for symlink-based sandbox escapes."""

    def test_symlink_escape_blocked(self, sample_app_root):
        """Symlinks pointing outside the sandbox are caught by resolve()."""
        import os
        symlink_path = os.path.join(sample_app_root, "evil_link")
        os.symlink("/etc/passwd", symlink_path)

        with pytest.raises(ValueError, match="Access denied"):
            _tool_read_file(symlink_path, sample_app_root)


class TestExtractCodeBlocks:
    """Tests for code block extraction."""

    def test_extract_python_block(self):
        """Extracts a Python code block."""
        text = "Here's a script:\n\n```python\nprint('hello')\n```\n\nDone."
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["language"] == "python"
        assert blocks[0]["code"] == "print('hello')"

    def test_extract_multiple_blocks(self):
        """Extracts multiple code blocks."""
        text = "```python\nx = 1\n```\n\n```sql\nSELECT *\n```"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 2
        assert blocks[0]["language"] == "python"
        assert blocks[1]["language"] == "sql"

    def test_extract_no_blocks(self):
        """Returns empty list when no code blocks."""
        blocks = _extract_code_blocks("Just plain text.")
        assert blocks == []

    def test_extract_unlabeled_block(self):
        """Handles code blocks without a language label."""
        text = "```\nsome code\n```"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["language"] == "text"


class TestIsLikelyBinary:
    """Tests for binary file detection."""

    def test_binary_extensions(self):
        from pathlib import Path
        assert _is_likely_binary(Path("file.pyc"))
        assert _is_likely_binary(Path("image.png"))
        assert _is_likely_binary(Path("archive.zip"))

    def test_text_extensions(self):
        from pathlib import Path
        assert not _is_likely_binary(Path("script.py"))
        assert not _is_likely_binary(Path("readme.md"))
        assert not _is_likely_binary(Path("config.json"))


class TestChatEngineInit:
    """Tests for ChatEngine initialization."""

    def test_missing_api_key(self):
        """ChatEngine raises ValueError without API key."""
        # Clear env var if set
        env_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with pytest.raises(ValueError, match="API key"):
                ChatEngine(api_key=None)
        finally:
            if env_key:
                os.environ["ANTHROPIC_API_KEY"] = env_key

    def test_init_with_api_key(self, sample_app_root):
        """ChatEngine initializes with an explicit API key."""
        engine = ChatEngine(api_key="sk-test-key", app_root=sample_app_root)
        assert engine._app_root == sample_app_root
        assert engine._model == "claude-sonnet-4-5-20250929"

    def test_custom_system_prompt(self, sample_app_root):
        """ChatEngine accepts a custom system prompt."""
        engine = ChatEngine(
            api_key="sk-test-key",
            app_root=sample_app_root,
            system_prompt="Custom prompt for {app_root}",
        )
        assert sample_app_root in engine._system_prompt

    def test_allowed_tools_spec_names(self, sample_app_root):
        """ChatEngine filters tools using spec-style names (Read, Grep, Glob)."""
        engine = ChatEngine(
            api_key="sk-test-key",
            app_root=sample_app_root,
            allowed_tools=["Read", "Grep"],
        )
        tool_names = [t["name"] for t in engine._tools]
        assert "read_file" in tool_names
        assert "grep" in tool_names
        assert "glob_search" not in tool_names

    def test_allowed_tools_internal_names(self, sample_app_root):
        """ChatEngine filters tools using internal names."""
        engine = ChatEngine(
            api_key="sk-test-key",
            app_root=sample_app_root,
            allowed_tools=["read_file"],
        )
        tool_names = [t["name"] for t in engine._tools]
        assert tool_names == ["read_file"]

    def test_allowed_tools_all(self, sample_app_root):
        """ChatEngine includes all tools when allowed_tools is None."""
        engine = ChatEngine(
            api_key="sk-test-key",
            app_root=sample_app_root,
        )
        assert len(engine._tools) == 3

    def test_allowed_tools_invalid_name(self, sample_app_root):
        """ChatEngine raises ValueError for unknown tool names."""
        with pytest.raises(ValueError, match="Unknown tool"):
            ChatEngine(
                api_key="sk-test-key",
                app_root=sample_app_root,
                allowed_tools=["Bash"],
            )


import base64
import time

from support_console.chat import _build_user_content
from support_console.file_store import FileEntry


class TestBuildUserContent:
    """Tests for building multi-part user content with file attachments."""

    def test_text_only(self):
        """Without files, returns plain string."""
        result = _build_user_content("hello", files=None)
        assert result == "hello"

    def test_with_image(self):
        """Image file produces image content block + text."""
        img = FileEntry(
            id="img1", name="shot.png", media_type="image/png",
            data=b"\x89PNG\r\n", created_at=time.monotonic(), size=6,
        )
        result = _build_user_content("describe this", files=[img])
        assert isinstance(result, list)
        assert result[0]["type"] == "image"
        assert result[0]["source"]["media_type"] == "image/png"
        assert result[0]["source"]["data"] == base64.standard_b64encode(b"\x89PNG\r\n").decode()
        # User text is last
        assert result[-1]["type"] == "text"
        assert result[-1]["text"] == "describe this"

    def test_with_text_file(self):
        """Text file produces text block with attached-file tags."""
        txt = FileEntry(
            id="txt1", name="app.log", media_type="text/plain",
            data=b"ERROR: something broke\n", created_at=time.monotonic(), size=22,
        )
        result = _build_user_content("what happened?", files=[txt])
        assert isinstance(result, list)
        # First block is the text file
        assert '<attached-file name="app.log">' in result[0]["text"]
        assert "ERROR: something broke" in result[0]["text"]
        assert "</attached-file>" in result[0]["text"]
        # Last block is user message
        assert result[-1]["text"] == "what happened?"

    def test_mixed_files(self):
        """Images come before text files, user message last."""
        img = FileEntry(
            id="img1", name="shot.png", media_type="image/png",
            data=b"\x89PNG", created_at=time.monotonic(), size=4,
        )
        txt = FileEntry(
            id="txt1", name="data.csv", media_type="text/csv",
            data=b"a,b\n1,2\n", created_at=time.monotonic(), size=8,
        )
        result = _build_user_content("analyze", files=[txt, img])
        # Image first
        assert result[0]["type"] == "image"
        # Text file second
        assert result[1]["type"] == "text"
        assert "data.csv" in result[1]["text"]
        # User message last
        assert result[-1]["text"] == "analyze"

    def test_large_text_truncated(self):
        """Text files over 500KB are truncated."""
        big = FileEntry(
            id="big1", name="huge.log", media_type="text/plain",
            data=b"x" * (600 * 1024), created_at=time.monotonic(), size=600 * 1024,
        )
        result = _build_user_content("read this", files=[big])
        text_block = result[0]["text"]
        assert "truncated" in text_block.lower()
