"""Tests for the CLI entry point."""

import os
import stat
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

from support_console.startup_template import render_startup


class TestStartupScriptArg:
    """Tests for the --startup-script CLI argument."""

    def test_missing_file_exits_with_error(self):
        """--startup-script with nonexistent path exits with code 1."""
        result = subprocess.run(
            [sys.executable, "-m", "support_console", "run",
             "--startup-script", "/nonexistent/path.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1
        assert "startup script not found" in result.stderr

    def test_valid_script_wraps_in_template(self, tmp_path):
        """Valid startup script is read, wrapped via render_startup, and passed to kernel."""
        script = tmp_path / "startup.py"
        script.write_text('print("hello from startup")', encoding="utf-8")

        # Verify the wrapping logic produces correct output
        startup_code = render_startup('print("hello from startup")')
        assert 'print("hello from startup")' in startup_code
        assert "Initializing Support Console kernel..." in startup_code
        assert "Support Console ready." in startup_code

        # Verify the CLI doesn't exit with "not found" or "cannot read" errors
        result = subprocess.run(
            [sys.executable, "-m", "support_console", "run",
             "--startup-script", str(script)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # It will fail trying to start uvicorn/kernel, but NOT with our error messages
        assert "startup script not found" not in result.stderr
        assert "cannot read startup script" not in result.stderr

    def test_script_with_braces_preserved(self, tmp_path):
        """Script containing braces (dicts, f-strings) passes through correctly."""
        code = (
            'd = {"key": "value"}\n'
            'print(f"result: {d}")\n'
            's = {1, 2, 3}\n'
        )
        script = tmp_path / "startup.py"
        script.write_text(code, encoding="utf-8")

        # Verify braces are preserved through render_startup (no .format() mangling)
        startup_code = render_startup(code)
        assert '{"key": "value"}' in startup_code
        assert 'f"result: {d}"' in startup_code
        assert "{1, 2, 3}" in startup_code

        # Verify CLI doesn't crash on brace-containing scripts
        result = subprocess.run(
            [sys.executable, "-m", "support_console", "run",
             "--startup-script", str(script)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert "startup script not found" not in result.stderr
        assert "cannot read startup script" not in result.stderr

    @pytest.mark.skipif(os.name == "nt", reason="chmod not reliable on Windows")
    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_permission_error_exits_with_error(self, tmp_path):
        """Unreadable startup script exits with clear error."""
        script = tmp_path / "startup.py"
        script.write_text("print('secret')", encoding="utf-8")
        script.chmod(0o000)

        try:
            result = subprocess.run(
                [sys.executable, "-m", "support_console", "run",
                 "--startup-script", str(script)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 1
            assert "cannot read startup script" in result.stderr
        finally:
            script.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_oversized_file_exits_with_error(self, tmp_path):
        """Startup script larger than 1MB exits with clear error."""
        script = tmp_path / "huge.py"
        script.write_text("x = 1\n" * 200_000, encoding="utf-8")  # ~1.2MB

        result = subprocess.run(
            [sys.executable, "-m", "support_console", "run",
             "--startup-script", str(script)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1
        assert "too large" in result.stderr

    def test_help_includes_startup_script(self):
        """--startup-script appears in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "support_console", "run", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "--startup-script" in result.stdout

    def test_startup_code_reaches_create_app(self, tmp_path, monkeypatch):
        """Verify startup_code is passed through to create_app (not silently dropped)."""
        script = tmp_path / "startup.py"
        script.write_text('x = 42', encoding="utf-8")

        captured_startup_code = {}

        def fake_create_app(**kwargs):
            captured_startup_code.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(sys, "argv", [
            "support-console", "run",
            "--startup-script", str(script),
            "--app-root", "/tmp", "--session-db", "/tmp/test.db",
        ])

        with patch("support_console.cli.render_startup", wraps=render_startup) as mock_render, \
             patch.dict("sys.modules", {"uvicorn": MagicMock()}), \
             patch("support_console.server.create_app", side_effect=fake_create_app):
            from support_console.cli import main
            main()

        # render_startup was called with our script content
        mock_render.assert_called_once_with("x = 42")

        # create_app received startup_code containing our script
        assert "startup_code" in captured_startup_code
        assert captured_startup_code["startup_code"] is not None
        assert "x = 42" in captured_startup_code["startup_code"]
        assert "Initializing Support Console kernel..." in captured_startup_code["startup_code"]
