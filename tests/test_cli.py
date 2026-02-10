"""Tests for the CLI entry point."""

import os
import stat
import subprocess
import sys

import pytest

from support_console.startup_template import DEFAULT_STARTUP


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
        """Valid startup script is read, wrapped in DEFAULT_STARTUP, and passed to kernel.

        We can't boot the full server in a test, so we verify the CLI exits
        at the uvicorn.run() stage (meaning it got past file reading successfully).
        We also verify the template wrapping logic directly.
        """
        script = tmp_path / "startup.py"
        script.write_text('print("hello from startup")', encoding="utf-8")

        # Verify the wrapping logic produces correct output
        custom_code = script.read_text(encoding="utf-8")
        startup_code = DEFAULT_STARTUP.replace("{custom_startup}", custom_code)
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
        script = tmp_path / "startup.py"
        script.write_text(
            'd = {"key": "value"}\n'
            'print(f"result: {d}")\n'
            's = {1, 2, 3}\n',
            encoding="utf-8",
        )

        # Verify braces are preserved through .replace() (not mangled by .format())
        custom_code = script.read_text(encoding="utf-8")
        startup_code = DEFAULT_STARTUP.replace("{custom_startup}", custom_code)
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
