"""Tests for the IPython kernel manager."""

import pytest

from support_console.kernel import (
    KernelSession,
    KernelError,
    KernelNotStartedError,
    ExecutionTimeout,
)


@pytest.fixture
async def kernel():
    """Start a kernel session for testing."""
    k = KernelSession()
    await k.start()
    yield k
    await k.shutdown()


@pytest.mark.asyncio
async def test_start_and_shutdown():
    """Kernel starts and shuts down cleanly."""
    k = KernelSession()
    output = await k.start()
    assert k.is_alive
    assert isinstance(output, str)
    await k.shutdown()
    assert not k.is_alive


@pytest.mark.asyncio
async def test_start_with_startup_code():
    """Startup code runs during kernel start."""
    k = KernelSession(startup_code='print("hello from startup")')
    output = await k.start()
    assert "hello from startup" in output
    await k.shutdown()


@pytest.mark.asyncio
async def test_execute_simple(kernel):
    """Execute simple code and get stdout."""
    result = await kernel.execute('print("hello world")')
    assert result["status"] == "ok"
    assert "hello world" in result["stdout"]


@pytest.mark.asyncio
async def test_execute_expression(kernel):
    """Execute an expression and get the result."""
    result = await kernel.execute("2 + 3")
    assert result["status"] == "ok"
    assert "5" in result["result"]


@pytest.mark.asyncio
async def test_execute_error(kernel):
    """Execute code that raises an exception."""
    result = await kernel.execute("1 / 0")
    assert result["status"] == "error"
    assert result["error"] is not None
    assert result["error"]["ename"] == "ZeroDivisionError"


@pytest.mark.asyncio
async def test_execute_state_persistence(kernel):
    """Variables persist between executions."""
    await kernel.execute("x = 42")
    result = await kernel.execute("print(x)")
    assert "42" in result["stdout"]


@pytest.mark.asyncio
async def test_execute_multiline(kernel):
    """Multiline code executes correctly."""
    code = """
def greet(name):
    return f"Hello, {name}!"

result = greet("World")
print(result)
"""
    result = await kernel.execute(code)
    assert result["status"] == "ok"
    assert "Hello, World!" in result["stdout"]


@pytest.mark.asyncio
async def test_execute_timeout(kernel):
    """Execution times out for long-running code."""
    with pytest.raises(ExecutionTimeout):
        await kernel.execute("import time; time.sleep(60)", timeout=1.0)


@pytest.mark.asyncio
async def test_interrupt(kernel):
    """Interrupt stops a running execution."""
    import asyncio

    # Start a long-running task
    task = asyncio.create_task(
        kernel.execute("import time; time.sleep(60)", timeout=30)
    )
    # Give it a moment to start
    await asyncio.sleep(1)

    # Interrupt
    success = await kernel.interrupt()
    assert success

    # The task should complete (with an error from the interrupt)
    result = await task
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_execution_count(kernel):
    """Execution count increments."""
    assert kernel.execution_count == 0
    await kernel.execute("1 + 1")
    assert kernel.execution_count == 1
    await kernel.execute("2 + 2")
    assert kernel.execution_count == 2


@pytest.mark.asyncio
async def test_status(kernel):
    """Status returns expected fields."""
    status = kernel.status()
    assert "session_id" in status
    assert status["alive"] is True
    assert status["busy"] is False
    assert status["uptime"] is not None
    assert status["uptime"] >= 0
    assert status["execution_count"] == 0


@pytest.mark.asyncio
async def test_context_manager():
    """KernelSession works as an async context manager."""
    async with KernelSession(startup_code='x = 99') as kernel:
        result = await kernel.execute("print(x)")
        assert "99" in result["stdout"]
        assert kernel.is_alive

    assert not kernel.is_alive


@pytest.mark.asyncio
async def test_restart(kernel):
    """Restart clears state and re-runs startup."""
    await kernel.execute("y = 123")
    result = await kernel.execute("print(y)")
    assert "123" in result["stdout"]

    await kernel.restart(run_startup=False)
    assert kernel.is_alive

    # State should be cleared
    result = await kernel.execute("print(y)")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_operations_before_start():
    """Operations raise KernelNotStartedError if kernel not started."""
    k = KernelSession()
    with pytest.raises(KernelNotStartedError):
        await k.execute("1 + 1")
    with pytest.raises(KernelNotStartedError):
        await k.interrupt()


@pytest.mark.asyncio
async def test_session_id():
    """Each kernel gets a unique session ID."""
    k1 = KernelSession()
    k2 = KernelSession()
    assert k1.session_id != k2.session_id
    assert len(k1.session_id) == 12


@pytest.mark.asyncio
async def test_stderr_capture(kernel):
    """Stderr output is captured separately."""
    result = await kernel.execute('import sys; print("error msg", file=sys.stderr)')
    assert "error msg" in result["stderr"]


@pytest.mark.asyncio
async def test_complete_basic(kernel):
    """Complete returns matches from Jedi via the kernel."""
    await kernel.execute("import os")
    result = await kernel.complete("os.pa", cursor_pos=5)
    assert result["status"] == "ok"
    assert "path" in result["matches"]


@pytest.mark.asyncio
async def test_complete_empty(kernel):
    """Complete with nonsense returns empty matches."""
    result = await kernel.complete("xyzzynonexistent.qqq", cursor_pos=20)
    assert result["status"] == "ok"
    assert result["matches"] == []


@pytest.mark.asyncio
async def test_complete_before_start():
    """Complete raises KernelNotStartedError if kernel not started."""
    k = KernelSession()
    with pytest.raises(KernelNotStartedError):
        await k.complete("os.pa", cursor_pos=5)


@pytest.mark.asyncio
async def test_complete_when_busy(kernel):
    """Complete returns empty matches when kernel is busy executing."""
    kernel._busy = True
    try:
        result = await kernel.complete("os.pa", cursor_pos=5)
        assert result["status"] == "ok"
        assert result["matches"] == []
        assert result["cursor_start"] == 5
        assert result["cursor_end"] == 5
    finally:
        kernel._busy = False
