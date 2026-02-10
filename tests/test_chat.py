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
