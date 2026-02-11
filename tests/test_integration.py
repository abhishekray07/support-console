"""Tests for the Flask integration module."""

import pytest

from support_console.integration import SupportConsole
from support_console.startup_template import DEFAULT_STARTUP, FLASK_STARTUP, render_startup


class TestSupportConsole:
    """Tests for the SupportConsole class."""

    def test_init_no_args(self):
        """SupportConsole can be created with no arguments."""
        console = SupportConsole()
        assert console._flask_app is None
        assert console._db is None

    def test_init_with_config(self):
        """SupportConsole accepts config dict."""
        config = {
            "anthropic_api_key": "sk-test",
            "app_root": "/my/app",
            "session_db": "/tmp/sessions.db",
        }
        console = SupportConsole(config=config)
        assert console._config == config

    def test_build_startup_code(self):
        """Startup code is generated from Flask app and db."""
        # Use mock objects
        mock_app = type("MockApp", (), {"__module__": "app.factory"})()
        mock_db = type("MockDB", (), {})()

        console = SupportConsole(mock_app, mock_db)
        assert console._startup_code is not None
        assert "create_app" in console._startup_code
        assert "Support Console" in console._startup_code

    def test_build_startup_code_with_config_overrides(self):
        """Config overrides for startup code are respected."""
        mock_app = type("MockApp", (), {})()
        mock_db = type("MockDB", (), {})()

        console = SupportConsole(mock_app, mock_db, config={
            "app_factory_module": "myapp",
            "app_factory_func": "make_app",
            "db_module": "myapp.db",
            "db_var": "database",
            "models_module": "myapp.all_models",
        })
        assert "make_app" in console._startup_code
        assert "myapp.db" in console._startup_code

    def test_init_app_not_implemented(self):
        """Blueprint mode raises NotImplementedError."""
        console = SupportConsole()
        with pytest.raises(NotImplementedError):
            console.init_app(None)

    def test_build_startup_code_no_format_error(self):
        """Startup code generation doesn't crash on brace-containing output."""
        mock_app = type("MockApp", (), {"__module__": "app.factory"})()
        mock_db = type("MockDB", (), {})()

        # This would raise KeyError with .format() because FLASK_STARTUP
        # output contains brace literals like {db_var}
        console = SupportConsole(mock_app, mock_db)
        assert console._startup_code is not None
        assert "Initializing Support Console kernel..." in console._startup_code


class TestStartupTemplate:
    """Tests for startup template rendering."""

    def test_render_startup_with_braces(self):
        """render_startup handles code containing braces."""
        code = 'd = {"key": "value"}\nprint(d)'
        result = render_startup(code)
        assert 'd = {"key": "value"}' in result
        assert "Support Console" in result

    def test_render_startup_empty(self):
        """render_startup with no args produces the default template."""
        result = render_startup()
        assert "Initializing Support Console kernel..." in result
        assert "Support Console ready." in result

    def test_flask_startup_renders(self):
        """FLASK_STARTUP template renders with all variables."""
        result = FLASK_STARTUP.format(
            app_factory_module="app",
            app_factory_func="create_app",
            db_module="app.extensions",
            db_var="db",
            models_module="app.models",
        )
        assert "from app import create_app" in result
        assert "from app.extensions import db" in result
        assert "from app.models import *" in result
