"""Shared test fixtures."""

import os
import tempfile

import pytest


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that's cleaned up after the test."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def tmp_db_path(tmp_dir):
    """Provide a path for a temporary SQLite database."""
    return os.path.join(tmp_dir, "test_sessions.db")


@pytest.fixture
def sample_app_root(tmp_dir):
    """Create a sample app directory structure for testing file tools."""
    app_root = os.path.join(tmp_dir, "app")
    models_dir = os.path.join(app_root, "models")
    os.makedirs(models_dir)

    # Create some sample files
    with open(os.path.join(app_root, "config.py"), "w") as f:
        f.write('DATABASE_URL = "postgresql://localhost/myapp"\nDEBUG = True\n')

    with open(os.path.join(models_dir, "user.py"), "w") as f:
        f.write(
            'from app.extensions import db\n\n'
            'class User(db.Model):\n'
            '    id = db.Column(db.Integer, primary_key=True)\n'
            '    email = db.Column(db.String(120), unique=True)\n'
            '    name = db.Column(db.String(80))\n'
        )

    with open(os.path.join(models_dir, "asset.py"), "w") as f:
        f.write(
            'from app.extensions import db\n\n'
            'class Asset(db.Model):\n'
            '    id = db.Column(db.Integer, primary_key=True)\n'
            '    name = db.Column(db.String(200))\n'
            '    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"))\n'
        )

    with open(os.path.join(models_dir, "__init__.py"), "w") as f:
        f.write('from .user import User\nfrom .asset import Asset\n')

    return app_root
