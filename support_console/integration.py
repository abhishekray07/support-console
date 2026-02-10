"""Flask integration for Support Console.

Provides the SupportConsole class for easy integration with any Flask + SQLAlchemy app.

Usage (standalone)::

    from support_console import SupportConsole
    app = create_app()
    console = SupportConsole(app, db)
    console.run(port=8888)

Usage (as Flask blueprint -- future)::

    console = SupportConsole(app, db)
    console.init_app(app, url_prefix="/support-console")
"""

import logging
from typing import Any

from support_console.startup_template import DEFAULT_STARTUP, FLASK_STARTUP

logger = logging.getLogger(__name__)


class SupportConsole:
    """AI-powered debug shell for Flask + SQLAlchemy apps.

    Parameters
    ----------
    flask_app:
        The Flask application instance, or a factory function.
    db:
        The SQLAlchemy ``db`` instance (e.g. ``flask_sqlalchemy.SQLAlchemy``).
    config:
        Optional configuration dict with keys:
        - anthropic_api_key: API key (or set ANTHROPIC_API_KEY env var)
        - app_root: Path to Flask app source (default: /app/server)
        - session_db: Path to SQLite DB (default: /data/sessions.db)
        - system_prompt: Override Claude's system prompt
        - startup_code: Override kernel startup code
    """

    def __init__(self, flask_app=None, db=None, config: dict | None = None):
        self._flask_app = flask_app
        self._db = db
        self._config = config or {}
        self._startup_code: str | None = None

        if flask_app is not None and db is not None:
            self._startup_code = self._build_startup_code(flask_app, db)

    def _build_startup_code(self, flask_app: Any, db: Any) -> str:
        """Generate IPython kernel startup code from the Flask app and db objects.

        Inspects the Flask app and db to determine import paths, then renders
        the startup template.
        """
        # Try to determine the factory function and module
        app_factory_module = "app"
        app_factory_func = "create_app"
        db_module = "app.extensions"
        db_var = "db"
        models_module = "app.models"

        # If we can inspect the Flask app, try to get actual module info
        if flask_app is not None:
            module = getattr(flask_app, "__module__", None)
            if module:
                app_factory_module = module.rsplit(".", 1)[0] if "." in module else module

        # Allow config overrides
        app_factory_module = self._config.get("app_factory_module", app_factory_module)
        app_factory_func = self._config.get("app_factory_func", app_factory_func)
        db_module = self._config.get("db_module", db_module)
        db_var = self._config.get("db_var", db_var)
        models_module = self._config.get("models_module", models_module)

        flask_startup = FLASK_STARTUP.format(
            app_factory_module=app_factory_module,
            app_factory_func=app_factory_func,
            db_module=db_module,
            db_var=db_var,
            models_module=models_module,
        )

        return DEFAULT_STARTUP.replace("{custom_startup}", flask_startup)

    def run(self, host: str = "0.0.0.0", port: int = 8888, **kwargs) -> None:
        """Start the support console server in standalone mode.

        This blocks the current process (runs uvicorn).

        Parameters
        ----------
        host:
            Host to bind to.  Defaults to 0.0.0.0.
        port:
            Port to listen on.  Defaults to 8888.
        **kwargs:
            Additional keyword arguments passed to ``uvicorn.run()``.
        """
        import uvicorn
        from support_console.server import create_app

        app = create_app(
            app_root=self._config.get("app_root", "/app/server"),
            session_db=self._config.get("session_db", "/data/sessions.db"),
            api_key=self._config.get("anthropic_api_key"),
            system_prompt=self._config.get("system_prompt"),
            startup_code=self._config.get("startup_code", self._startup_code),
            allowed_tools=self._config.get("allowed_tools"),
        )

        logger.info("Starting Support Console on %s:%d", host, port)
        uvicorn.run(app, host=host, port=port, **kwargs)

    def init_app(self, flask_app, url_prefix: str = "/support-console") -> None:
        """Register as a Flask blueprint (placeholder for future implementation).

        This would mount the Support Console UI within an existing Flask app.
        For now, use standalone mode via ``run()``.
        """
        raise NotImplementedError(
            "Blueprint mode is not yet implemented. "
            "Use standalone mode: console.run(port=8888)"
        )
