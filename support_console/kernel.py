"""IPython kernel manager using jupyter_client.

Manages an IPython kernel subprocess, injects Flask app context on startup,
and provides an async API for executing code and collecting results.
"""

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Optional

from jupyter_client import KernelManager
from jupyter_client.kernelspec import NoSuchKernel

logger = logging.getLogger(__name__)

# Sentinel used to detect when we have drained all IOPub messages for a request.
_EMPTY = object()


class KernelError(Exception):
    """Raised when a kernel operation fails."""


class KernelNotStartedError(KernelError):
    """Raised when attempting operations on a kernel that is not running."""


class ExecutionTimeout(KernelError):
    """Raised when code execution exceeds the allowed timeout."""


class KernelSession:
    """Manages an IPython kernel with Flask app context pre-loaded.

    All public methods are async-safe and designed to be called from
    an asyncio event loop (e.g. FastAPI request handlers).

    Usage::

        session = KernelSession(startup_code="x = 42")
        await session.start()
        result = await session.execute("print(x)")
        await session.shutdown()
    """

    def __init__(self, startup_code: str = ""):
        self._km: Optional[KernelManager] = None
        self._kc: Optional[Any] = None  # jupyter_client.KernelClient
        self._startup_code = startup_code
        self._busy = False
        self._session_id = uuid.uuid4().hex[:12]
        self._started_at: Optional[float] = None
        self._execution_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> str:
        """Start the IPython kernel and run startup code.

        Returns:
            Startup output text (stdout captured during startup code execution).

        Raises:
            KernelError: If the kernel fails to start.
        """
        if self._km is not None and await asyncio.to_thread(self._km.is_alive):
            logger.warning("Kernel %s already running, skipping start", self._session_id)
            return ""

        logger.info("Starting kernel session %s", self._session_id)

        try:
            km = KernelManager(kernel_name="python3")
            await asyncio.to_thread(km.start_kernel)
        except NoSuchKernel:
            raise KernelError(
                "python3 kernel not found. Ensure ipykernel is installed: "
                "pip install ipykernel"
            )
        except Exception as exc:
            raise KernelError(f"Failed to start kernel: {exc}") from exc

        kc = km.client()
        kc.start_channels()

        # Wait for the kernel to be ready by polling for a shell reply.
        try:
            await asyncio.to_thread(kc.wait_for_ready, timeout=30)
        except RuntimeError as exc:
            # Cleanup on failure.
            kc.stop_channels()
            await asyncio.to_thread(km.shutdown_kernel, now=True)
            raise KernelError(f"Kernel did not become ready: {exc}") from exc

        self._km = km
        self._kc = kc
        self._started_at = time.monotonic()

        # Run startup code if provided.
        startup_output = ""
        if self._startup_code:
            result = await self._execute_internal(self._startup_code, timeout=60.0)
            parts = []
            if result["stdout"]:
                parts.append(result["stdout"])
            if result["stderr"]:
                parts.append(result["stderr"])
            if result["status"] == "error" and result.get("error"):
                err = result["error"]
                tb = "\n".join(err.get("traceback_plain", err.get("traceback", [])))
                parts.append(tb)
            startup_output = "\n".join(parts)

        logger.info("Kernel %s started successfully", self._session_id)
        return startup_output

    async def shutdown(self) -> None:
        """Shutdown the kernel and release resources."""
        logger.info("Shutting down kernel %s", self._session_id)

        if self._kc is not None:
            try:
                self._kc.stop_channels()
            except Exception:
                logger.debug("Error stopping channels", exc_info=True)
            self._kc = None

        if self._km is not None:
            try:
                await asyncio.to_thread(self._km.shutdown_kernel, now=False)
            except Exception:
                logger.debug("Error during graceful shutdown, forcing", exc_info=True)
                try:
                    await asyncio.to_thread(self._km.shutdown_kernel, now=True)
                except Exception:
                    logger.warning("Forced shutdown also failed", exc_info=True)
            self._km = None

        self._busy = False
        self._started_at = None
        logger.info("Kernel %s shut down", self._session_id)

    async def restart(self, run_startup: bool = True) -> str:
        """Restart the kernel and optionally re-run startup code.

        Args:
            run_startup: Whether to re-execute the startup code after restart.

        Returns:
            Startup output text if run_startup is True, else empty string.

        Raises:
            KernelNotStartedError: If the kernel was never started.
        """
        self._ensure_started()
        logger.info("Restarting kernel %s", self._session_id)

        try:
            await asyncio.to_thread(self._km.restart_kernel, now=False)
        except Exception:
            logger.warning("Graceful restart failed, forcing", exc_info=True)
            await asyncio.to_thread(self._km.restart_kernel, now=True)

        # Reconnect channels.
        self._kc.stop_channels()
        self._kc = self._km.client()
        self._kc.start_channels()

        try:
            await asyncio.to_thread(self._kc.wait_for_ready, timeout=30)
        except RuntimeError as exc:
            raise KernelError(f"Kernel did not become ready after restart: {exc}") from exc

        self._busy = False
        self._execution_count = 0

        startup_output = ""
        if run_startup and self._startup_code:
            result = await self._execute_internal(self._startup_code, timeout=60.0)
            parts = []
            if result["stdout"]:
                parts.append(result["stdout"])
            if result["stderr"]:
                parts.append(result["stderr"])
            if result["status"] == "error" and result.get("error"):
                err = result["error"]
                tb = "\n".join(err.get("traceback_plain", err.get("traceback", [])))
                parts.append(tb)
            startup_output = "\n".join(parts)

        logger.info("Kernel %s restarted", self._session_id)
        return startup_output

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, code: str, timeout: float = 30.0) -> dict:
        """Execute code in the kernel and return structured results.

        Args:
            code: Python code to execute.
            timeout: Maximum seconds to wait for execution to complete.

        Returns:
            Dictionary with keys:
                - stdout (str): Captured standard output.
                - stderr (str): Captured standard error.
                - result (str): Text representation of the execution result
                    (the repr of the last expression, if any).
                - display_data (list[dict]): Rich display outputs (HTML, images, etc.).
                    Each entry has ``mime_type`` and ``data`` keys.
                - status (str): "ok" or "error".
                - error (dict | None): If status is "error", contains ``ename``,
                    ``evalue``, ``traceback``, and ``traceback_plain``.

        Raises:
            KernelNotStartedError: If the kernel is not running.
            ExecutionTimeout: If execution exceeds *timeout* seconds.
        """
        self._ensure_started()
        return await self._execute_internal(code, timeout)

    async def interrupt(self) -> bool:
        """Interrupt the currently running execution.

        Returns:
            True if the interrupt signal was sent successfully, False otherwise.
        """
        self._ensure_started()
        try:
            await asyncio.to_thread(self._km.interrupt_kernel)
            logger.info("Kernel %s interrupted", self._session_id)
            return True
        except Exception:
            logger.warning("Failed to interrupt kernel %s", self._session_id, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        """Check if the kernel process is alive."""
        if self._km is None:
            return False
        try:
            return self._km.is_alive()
        except Exception:
            return False

    @property
    def is_busy(self) -> bool:
        """Check if the kernel is currently executing code."""
        return self._busy

    @property
    def session_id(self) -> str:
        """Unique identifier for this kernel session."""
        return self._session_id

    @property
    def uptime(self) -> Optional[float]:
        """Seconds since the kernel was started, or None if not started."""
        if self._started_at is None:
            return None
        return time.monotonic() - self._started_at

    @property
    def execution_count(self) -> int:
        """Number of execute requests completed (excluding startup)."""
        return self._execution_count

    def status(self) -> dict:
        """Return a status summary dictionary."""
        return {
            "session_id": self._session_id,
            "alive": self.is_alive,
            "busy": self.is_busy,
            "uptime": self.uptime,
            "execution_count": self._execution_count,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Raise if the kernel has not been started."""
        if self._km is None or self._kc is None:
            raise KernelNotStartedError(
                "Kernel has not been started. Call start() first."
            )

    async def _execute_internal(self, code: str, timeout: float) -> dict:
        """Core execution logic.  Sends an execute_request, then drains IOPub
        messages until an ``idle`` status is received for the matching request.

        This method is used both for startup code and user-facing execute().
        """
        kc = self._kc
        self._busy = True

        try:
            # Send the execute request.  We use silent=False so we get
            # execute_result messages for expression values.
            msg_id: str = await asyncio.to_thread(
                kc.execute, code, silent=False, store_history=True
            )

            stdout_parts: list[str] = []
            stderr_parts: list[str] = []
            result_text: Optional[str] = None
            display_data: list[dict] = []
            error_info: Optional[dict] = None
            status = "ok"

            deadline = time.monotonic() + timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Attempt to interrupt the kernel so it doesn't keep running.
                    try:
                        await asyncio.to_thread(self._km.interrupt_kernel)
                    except Exception:
                        pass
                    raise ExecutionTimeout(
                        f"Execution timed out after {timeout:.1f}s"
                    )

                # Poll for the next IOPub message.  We use a short inner
                # timeout so we can check the overall deadline regularly.
                poll_timeout = min(remaining, 0.5)
                try:
                    msg = await asyncio.to_thread(
                        kc.get_iopub_msg, timeout=poll_timeout
                    )
                except Exception:
                    # get_iopub_msg raises Empty (queue.Empty) on timeout.
                    continue

                # Only process messages belonging to our request.
                parent_id = msg.get("parent_header", {}).get("msg_id")
                if parent_id != msg_id:
                    continue

                msg_type = msg.get("msg_type", "")
                content = msg.get("content", {})

                if msg_type == "stream":
                    text = content.get("text", "")
                    if content.get("name") == "stderr":
                        stderr_parts.append(text)
                    else:
                        stdout_parts.append(text)

                elif msg_type == "execute_result":
                    data = content.get("data", {})
                    # Prefer plain text for the result field.
                    result_text = data.get("text/plain", "")
                    # Also capture any rich representations as display_data.
                    for mime, value in data.items():
                        if mime != "text/plain":
                            display_data.append({
                                "mime_type": mime,
                                "data": value,
                            })

                elif msg_type == "display_data" or msg_type == "update_display_data":
                    data = content.get("data", {})
                    for mime, value in data.items():
                        display_data.append({
                            "mime_type": mime,
                            "data": value,
                        })

                elif msg_type == "error":
                    status = "error"
                    traceback_raw = content.get("traceback", [])
                    error_info = {
                        "ename": content.get("ename", ""),
                        "evalue": content.get("evalue", ""),
                        "traceback": traceback_raw,
                        "traceback_plain": _strip_ansi_list(traceback_raw),
                    }

                elif msg_type == "status":
                    if content.get("execution_state") == "idle":
                        break

            self._execution_count += 1

            return {
                "stdout": "".join(stdout_parts),
                "stderr": "".join(stderr_parts),
                "result": result_text or "",
                "display_data": display_data,
                "status": status,
                "error": error_info,
            }

        except ExecutionTimeout:
            raise
        except Exception as exc:
            logger.error("Execution failed in kernel %s: %s", self._session_id, exc, exc_info=True)
            return {
                "stdout": "",
                "stderr": "",
                "result": "",
                "display_data": [],
                "status": "error",
                "error": {
                    "ename": type(exc).__name__,
                    "evalue": str(exc),
                    "traceback": [],
                    "traceback_plain": [],
                },
            }
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "KernelSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.shutdown()


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_ESCAPE_RE.sub("", text)


def _strip_ansi_list(items: list[str]) -> list[str]:
    """Remove ANSI escape sequences from each string in a list."""
    return [_strip_ansi(s) for s in items]
