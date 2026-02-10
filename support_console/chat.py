"""Claude chat backend using Anthropic SDK with streaming.

Provides a ChatEngine that wraps the Anthropic messages API with:
- Streaming text responses for real-time display
- Tool-use loop for source code exploration (read_file, grep, glob_search)
- Python code block extraction from responses
- Path sandboxing to prevent reading files outside the app root
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import AsyncGenerator

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MAX_FILE_SIZE = 100 * 1024          # 100 KB per file read
MAX_GREP_MATCHES = 50               # max grep result lines
MAX_GREP_CONTEXT_LINES = 2          # lines of context around each match
MAX_GLOB_RESULTS = 100              # max files returned by glob
MAX_TOOL_LOOPS = 15                 # guard against infinite tool-use loops
MAX_TOKENS = 4096                   # max tokens per Claude response

# ---------------------------------------------------------------------------
# Tool definitions sent to the Anthropic API
# ---------------------------------------------------------------------------
# Mapping from user-facing config names to internal tool names.
# Supports both spec names ("Read", "Grep", "Glob") and internal names.
_TOOL_NAME_ALIASES: dict[str, str] = {
    "Read": "read_file",
    "Grep": "grep",
    "Glob": "glob_search",
    "read_file": "read_file",
    "grep": "grep",
    "glob_search": "glob_search",
}

ALL_TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file. Returns the full file text up to "
            "100 KB. Use this to inspect source code, configs, and data files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to read.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search file contents using a regex pattern. Returns matching "
            "lines with file paths and line numbers (max 50 matches). "
            "Useful for finding usages, definitions, and patterns in code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or single file to search in.",
                },
                "include": {
                    "type": "string",
                    "description": (
                        "Optional glob pattern to filter which files to search "
                        "(e.g. '*.py', '*.html'). Defaults to all files."
                    ),
                },
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "glob_search",
        "description": (
            "Find files whose names match a glob pattern. Returns up to 100 "
            "matching file paths. Useful for discovering project structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern such as '**/*.py' or 'models/*.py'.",
                },
                "path": {
                    "type": "string",
                    "description": "Base directory to search from.",
                },
            },
            "required": ["pattern", "path"],
        },
    },
]

# ---------------------------------------------------------------------------
# Default system prompt
# ---------------------------------------------------------------------------
DEFAULT_SYSTEM_PROMPT = """\
You are a support engineering assistant. You help investigate and fix
production issues by generating Python scripts.

The Flask app source code is at {app_root}.
Models are in {app_root}/app/models/.
Use read_file, grep, and glob_search to look at the code when you need
to understand the codebase before writing scripts.

When you generate a script, output it as a fenced Python code block:

```python
# your code here
```

The user will run it in an IPython kernel with Flask app context already
loaded (db, app, and all models are available).

Do NOT include app context boilerplate -- the kernel already has it.
Just write the query/fix directly.

Rules:
- Start with investigation (read-only queries).
- For mutations, explain what will change and the blast radius.
- Use .count() or LIMIT before bulk operations.
- Always handle exceptions gracefully in generated scripts.
- Prefer explicit column selection over SELECT * for large tables."""


# ---------------------------------------------------------------------------
# Path sandboxing
# ---------------------------------------------------------------------------

def _resolve_sandboxed(path_str: str, allowed_root: str) -> Path:
    """Resolve *path_str* and ensure it lives under *allowed_root*.

    Raises ValueError if the resolved path escapes the sandbox.
    """
    root = Path(allowed_root).resolve()
    target = Path(path_str).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Access denied: {path_str!r} is outside the allowed root {allowed_root!r}"
        )
    return target


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

_BINARY_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".eot", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".db", ".sqlite", ".sqlite3", ".pdf", ".doc", ".docx",
    ".npy", ".npz", ".h5", ".hdf5",
})


def _is_likely_binary(path: Path) -> bool:
    """Quick heuristic to skip binary files during grep."""
    return path.suffix.lower() in _BINARY_SUFFIXES


def _try_relative(path: Path, root: str) -> str:
    """Return the path relative to root if possible, else absolute."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _tool_read_file(path: str, app_root: str) -> str:
    """Read a file, enforcing sandbox and size limits."""
    resolved = _resolve_sandboxed(path, app_root)

    if not resolved.exists():
        return f"Error: file not found: {resolved}"
    if not resolved.is_file():
        return f"Error: not a regular file: {resolved}"

    size = resolved.stat().st_size
    if size > MAX_FILE_SIZE:
        content = resolved.read_bytes()[:MAX_FILE_SIZE].decode("utf-8", errors="replace")
        return (
            f"[Truncated: file is {size:,} bytes, showing first {MAX_FILE_SIZE:,}]\n"
            f"{content}"
        )

    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {resolved}: {exc}"


def _tool_grep(pattern: str, path: str, include: str | None, app_root: str) -> str:
    """Search file contents using regex, enforcing sandbox and limits."""
    resolved = _resolve_sandboxed(path, app_root)

    if not resolved.exists():
        return f"Error: path not found: {resolved}"

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regex: {exc}"

    # Collect files to search
    if resolved.is_file():
        files = [resolved]
    else:
        glob_pat = include or "*"
        # Reject glob patterns that could escape the sandbox
        if ".." in glob_pat or glob_pat.startswith("/"):
            return "Error: invalid include pattern (must not contain '..' or start with '/')"
        files = sorted(resolved.rglob(glob_pat))

    matches: list[str] = []
    files_searched = 0

    for fpath in files:
        if not fpath.is_file():
            continue
        if _is_likely_binary(fpath):
            continue

        files_searched += 1
        try:
            lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for line_no, line in enumerate(lines, start=1):
            if regex.search(line):
                rel = _try_relative(fpath, app_root)
                matches.append(f"{rel}:{line_no}: {line.rstrip()}")
                if len(matches) >= MAX_GREP_MATCHES:
                    break
        if len(matches) >= MAX_GREP_MATCHES:
            break

    if not matches:
        return f"No matches for pattern {pattern!r} in {files_searched} files searched."

    header = f"Found {len(matches)} match(es) across {files_searched} file(s)"
    if len(matches) >= MAX_GREP_MATCHES:
        header += f" (truncated at {MAX_GREP_MATCHES})"
    header += ":\n"

    return header + "\n".join(matches)


def _tool_glob_search(pattern: str, path: str, app_root: str) -> str:
    """Find files matching a glob pattern, enforcing sandbox and limits."""
    resolved = _resolve_sandboxed(path, app_root)

    if not resolved.exists():
        return f"Error: path not found: {resolved}"
    if not resolved.is_dir():
        return f"Error: not a directory: {resolved}"

    # Reject glob patterns that could escape the sandbox
    if ".." in pattern or pattern.startswith("/"):
        return "Error: invalid glob pattern (must not contain '..' or start with '/')"

    results = sorted(resolved.glob(pattern))
    file_results = [p for p in results if p.is_file()]

    if not file_results:
        return f"No files found matching {pattern!r} under {resolved}"

    truncated = len(file_results) > MAX_GLOB_RESULTS
    display = file_results[:MAX_GLOB_RESULTS]

    lines = [_try_relative(p, app_root) for p in display]
    header = f"Found {len(file_results)} file(s)"
    if truncated:
        header += f" (showing first {MAX_GLOB_RESULTS})"
    header += ":\n"

    return header + "\n".join(lines)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _extract_code_blocks(text: str) -> list[dict]:
    """Extract fenced code blocks from markdown text.

    Returns a list of dicts: {"language": str, "code": str}
    """
    pattern = re.compile(
        r"```(\w*)\s*\n(.*?)```",
        re.DOTALL,
    )
    blocks = []
    for match in pattern.finditer(text):
        lang = match.group(1) or "text"
        code = match.group(2)
        if code.endswith("\n"):
            code = code[:-1]
        blocks.append({"language": lang, "code": code})
    return blocks


def _serialize_content_blocks(blocks) -> list[dict]:
    """Convert anthropic content block objects into JSON-serializable dicts."""
    result = []
    for block in blocks:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return result


def _truncate(text: str, max_len: int = 8000) -> str:
    """Truncate text to *max_len* characters with a note if truncated."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n[... truncated at {max_len:,} characters]"


# ---------------------------------------------------------------------------
# ChatEngine
# ---------------------------------------------------------------------------

class ChatEngine:
    """Manages Claude chat with tool use for source code exploration.

    Usage::

        engine = ChatEngine(api_key="sk-...", app_root="/app/server")
        messages = [{"role": "user", "content": "Show me all user models"}]
        async for event in engine.chat_stream(messages):
            if event["type"] == "text":
                print(event["content"], end="", flush=True)
    """

    def __init__(
        self,
        api_key: str | None = None,
        app_root: str = "/app/server",
        system_prompt: str | None = None,
        model: str = "claude-sonnet-4-5-20250929",
        allowed_tools: list[str] | None = None,
    ):
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key is required. Pass api_key= or set "
                "ANTHROPIC_API_KEY."
            )
        self._client = anthropic.AsyncAnthropic(api_key=resolved_key)
        self._app_root = str(Path(app_root).resolve())
        self._model = model

        # Filter tools based on allowed_tools config
        if allowed_tools is not None:
            internal_names = set()
            for name in allowed_tools:
                internal = _TOOL_NAME_ALIASES.get(name)
                if internal is None:
                    raise ValueError(
                        f"Unknown tool {name!r}. Valid tools: "
                        f"{sorted(_TOOL_NAME_ALIASES.keys())}"
                    )
                internal_names.add(internal)
            self._tools = [t for t in ALL_TOOLS if t["name"] in internal_names]
        else:
            self._tools = list(ALL_TOOLS)

        template = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._system_prompt = template.format(app_root=self._app_root)

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _execute_tool(self, name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result as a string.

        All tool functions are synchronous (file I/O only) so they are
        called directly without awaiting.
        """
        try:
            if name == "read_file":
                return _tool_read_file(
                    path=tool_input["path"],
                    app_root=self._app_root,
                )
            elif name == "grep":
                return _tool_grep(
                    pattern=tool_input["pattern"],
                    path=tool_input["path"],
                    include=tool_input.get("include"),
                    app_root=self._app_root,
                )
            elif name == "glob_search":
                return _tool_glob_search(
                    pattern=tool_input["pattern"],
                    path=tool_input["path"],
                    app_root=self._app_root,
                )
            else:
                return f"Error: unknown tool {name!r}"
        except ValueError as exc:
            # Sandbox violations surface as ValueError
            return str(exc)
        except Exception as exc:
            logger.exception("Tool execution error for %s", name)
            return f"Error executing {name}: {exc}"

    # ------------------------------------------------------------------
    # Streaming chat with tool-use loop
    # ------------------------------------------------------------------

    async def chat_stream(
        self, messages: list[dict]
    ) -> AsyncGenerator[dict, None]:
        """Stream a chat response, handling the full tool-use loop.

        Yields event dicts:

        ============  ==========================================
        type          Fields
        ============  ==========================================
        text          content: str (incremental text delta)
        tool_use      name: str, input: dict
        tool_result   name: str, result: str (possibly truncated)
        code_block    language: str, code: str
        done          stop_reason: str, message: dict
        error         error: str
        ============  ==========================================

        The *messages* list is mutated in place: assistant and tool-result
        messages are appended so the caller can persist the full
        conversation history.
        """
        loop_count = 0

        while loop_count < MAX_TOOL_LOOPS:
            loop_count += 1

            # --- Stream one API turn ---------------------------------
            try:
                async with self._client.messages.stream(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=self._system_prompt,
                    tools=self._tools,
                    messages=messages,
                ) as stream:
                    async for event in stream:
                        # Yield text deltas for real-time display
                        if event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                yield {
                                    "type": "text",
                                    "content": event.delta.text,
                                }

                    response_message = await stream.get_final_message()

            except anthropic.APIStatusError as exc:
                yield {
                    "type": "error",
                    "error": (
                        f"Anthropic API error ({exc.status_code}): "
                        f"{exc.message}"
                    ),
                }
                return
            except anthropic.APIConnectionError as exc:
                yield {
                    "type": "error",
                    "error": f"Failed to connect to Anthropic API: {exc}",
                }
                return
            except Exception as exc:
                logger.exception("Unexpected error during streaming")
                yield {"type": "error", "error": f"Unexpected error: {exc}"}
                return

            # --- Process response blocks -----------------------------
            full_text = ""
            tool_uses: list[dict] = []

            for block in response_message.content:
                if block.type == "text":
                    full_text += block.text
                elif block.type == "tool_use":
                    tool_uses.append({
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            # Append the assistant message to conversation history
            assistant_msg = {
                "role": "assistant",
                "content": _serialize_content_blocks(response_message.content),
            }
            messages.append(assistant_msg)

            # --- If no tool calls, we are done -----------------------
            if response_message.stop_reason != "tool_use" or not tool_uses:
                for code_block in _extract_code_blocks(full_text):
                    yield {"type": "code_block", **code_block}

                yield {
                    "type": "done",
                    "stop_reason": response_message.stop_reason,
                    "message": assistant_msg,
                }
                return

            # --- Execute tools and feed results back -----------------
            tool_results: list[dict] = []

            for tool_call in tool_uses:
                yield {
                    "type": "tool_use",
                    "name": tool_call["name"],
                    "input": tool_call["input"],
                }

                result = await asyncio.to_thread(
                    self._execute_tool, tool_call["name"], tool_call["input"]
                )

                yield {
                    "type": "tool_result",
                    "name": tool_call["name"],
                    "result": _truncate(result, max_len=8000),
                }

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call["id"],
                    "content": _truncate(result),
                })

            # Append tool results as a user message and continue loop
            messages.append({"role": "user", "content": tool_results})

        # Exhausted the tool-use loop budget
        yield {
            "type": "error",
            "error": (
                f"Tool-use loop exceeded {MAX_TOOL_LOOPS} iterations. "
                "Claude may be stuck in a tool-calling loop."
            ),
        }
