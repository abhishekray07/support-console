# Support Console

An AI-powered debug shell for Flask + SQLAlchemy applications. It provides a web-based interactive environment combining Claude AI for intelligent debugging, an embedded IPython kernel for live code execution, and session persistence.

## Features

- **Claude AI Chat**: Ask questions about your codebase, generate fix scripts, and get debugging help
- **Code Execution**: Run Python code directly against your Flask app via an embedded IPython kernel
- **Tool Use**: Claude can automatically read files, search code, and explore your project structure
- **File Uploads**: Attach images, logs, and code snippets as context for Claude
- **Session Persistence**: Save and restore chat histories and notebook cells via SQLite
- **Streaming**: Real-time response streaming over WebSocket

## Requirements

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)

## Installation

```bash
pip install -e .
```

For development (includes testing tools):

```bash
pip install -e ".[dev]"
```

## Usage

### CLI

```bash
export ANTHROPIC_API_KEY=sk-...

support-console run \
  --port 8888 \
  --app-root /path/to/flask/app \
  --session-db /tmp/sessions.db \
  --startup-script /path/to/setup.py
```

Then open `http://localhost:8888` in your browser.

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `8888` | HTTP port to listen on |
| `--host` | `0.0.0.0` | Bind address |
| `--app-root` | — | Path to Flask app source (used by Claude's file tools) |
| `--session-db` | — | SQLite database file for session storage |
| `--startup-script` | — | Python file executed in the kernel at startup |

### Python Integration

```python
from support_console import SupportConsole
from app import create_app
from app.extensions import db

app = create_app()
console = SupportConsole(app, db)
console.run(port=8888)
```

Advanced configuration:

```python
console = SupportConsole(
    flask_app,
    db,
    config={
        "anthropic_api_key": "sk-...",
        "app_root": "/app/server",
        "session_db": "/data/sessions.db",
        "system_prompt": "You are a helpful debugging assistant.",
        "app_factory_module": "app",
        "app_factory_func": "create_app",
        "db_module": "app.extensions",
        "db_var": "db",
        "models_module": "app.models",
        "allowed_tools": ["read_file", "grep", "glob_search"],
    }
)
```

## Project Structure

```
support_console/
├── __init__.py          # Package exports (SupportConsole)
├── __main__.py          # CLI entry point
├── cli.py               # Command-line interface
├── integration.py       # SupportConsole Flask integration class
├── server.py            # FastAPI application and endpoints
├── chat.py              # Claude chat engine with tool use
├── kernel.py            # IPython kernel session manager
├── sessions.py          # Session persistence (SQLite)
├── file_store.py        # In-memory file storage for uploads
├── startup_template.py  # IPython startup code template
└── static/              # Frontend assets (HTML, JS, CSS)
tests/                   # Test suite
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `WebSocket` | `/api/chat` | Streaming chat with Claude |
| `POST` | `/api/upload` | File upload |
| `POST` | `/api/kernel/execute` | Execute Python code |
| `POST` | `/api/kernel/complete` | Code completion |
| `POST` | `/api/kernel/interrupt` | Interrupt execution |
| `GET` | `/api/kernel/status` | Kernel health |
| `POST` | `/api/kernel/restart` | Restart kernel |
| `GET/POST` | `/api/sessions` | List or create sessions |
| `GET/PUT/DELETE` | `/api/sessions/{id}` | Load, save, or delete a session |

## Running Tests

```bash
pytest tests/ -v
```

## Security Notes

- Claude's file tools are sandboxed to `--app-root` and cannot traverse outside it
- File reads are limited to 100 KB per file
- File uploads are rate-limited (10 per 60 seconds per IP) and validated with magic byte detection
- WebSocket messages include CSRF protection via `X-Requested-With` header
