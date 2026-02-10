"""CLI entry point for support-console."""

import argparse
import logging
import sys
from pathlib import Path

from support_console.startup_template import DEFAULT_STARTUP

logger = logging.getLogger(__name__)


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
        help="Path to a Python script to run when the kernel starts",
    )

    args = parser.parse_args()

    if args.command == "run":
        # Validate startup script early, before heavy imports
        startup_code = None
        if args.startup_script:
            path = Path(args.startup_script).resolve()
            if not path.is_file():
                print(f"Error: startup script not found: {path}", file=sys.stderr)
                sys.exit(1)
            if path.stat().st_size > 1_048_576:
                print(f"Error: startup script too large (>1MB): {path}", file=sys.stderr)
                sys.exit(1)
            try:
                custom_code = path.read_text(encoding="utf-8")
            except (PermissionError, UnicodeDecodeError) as exc:
                print(f"Error: cannot read startup script: {exc}", file=sys.stderr)
                sys.exit(1)
            logger.info("Loading startup script: %s", path)
            startup_code = DEFAULT_STARTUP.replace("{custom_startup}", custom_code)

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
