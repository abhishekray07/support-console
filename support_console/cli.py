"""CLI entry point for support-console."""

import argparse
import logging
import sys
from pathlib import Path

from support_console.startup_template import render_startup

logger = logging.getLogger(__name__)

# Startup scripts beyond this size are almost certainly not hand-written
# configuration code.  The limit prevents accidentally passing large files
# (data dumps, serialized models) that would bloat kernel memory.
MAX_STARTUP_SCRIPT_SIZE = 1_048_576  # 1 MB


def _read_startup_script(raw_path: str) -> str:
    """Read and validate a startup script, returning wrapped startup code.

    Performs a single filesystem read inside a try/except to avoid TOCTOU
    races between existence/size checks and the actual read.
    """
    path = Path(raw_path).resolve()
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"Error: startup script not found: {path}", file=sys.stderr)
        sys.exit(1)
    except IsADirectoryError:
        print(f"Error: startup script is a directory: {path}", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Error: cannot read startup script (permission denied): {path}", file=sys.stderr)
        sys.exit(1)
    except UnicodeDecodeError as exc:
        print(f"Error: cannot read startup script (not valid UTF-8): {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error: cannot read startup script: {exc}", file=sys.stderr)
        sys.exit(1)

    if len(content.encode("utf-8")) > MAX_STARTUP_SCRIPT_SIZE:
        print(f"Error: startup script too large (>1MB): {path}", file=sys.stderr)
        sys.exit(1)

    logger.info("Loading startup script: %s", path)
    return render_startup(content)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        prog="support-console",
        description="AI-powered debug shell for Flask + SQLAlchemy apps",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Start the support console server")
    run_parser.add_argument("--port", type=int, default=8888, help="Port to listen on")
    run_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    run_parser.add_argument("--app-root", type=str, default="/app/server", help="Flask app source root")
    run_parser.add_argument("--session-db", type=str, default="/data/sessions.db", help="Session DB path")
    run_parser.add_argument(
        "--startup-script",
        type=str,
        default=None,
        help="Path to a .py file executed in the IPython kernel at boot (e.g. Flask app_context setup)",
    )

    args = parser.parse_args()

    if args.command == "run":
        # Validate startup script early, before heavy imports
        startup_code = None
        if args.startup_script:
            startup_code = _read_startup_script(args.startup_script)

        import uvicorn
        from support_console.server import create_app

        app = create_app(
            app_root=args.app_root,
            session_db=args.session_db,
            startup_code=startup_code,
        )
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
