"""CLI entry point for support-console."""

import argparse
import sys


def main():
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

    args = parser.parse_args()

    if args.command == "run":
        import uvicorn
        from support_console.server import create_app

        app = create_app(
            app_root=args.app_root,
            session_db=args.session_db,
        )
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
