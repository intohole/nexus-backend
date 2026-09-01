import tempfile
from pathlib import Path

import pytest

from nexus.config import ConfigFactory, load_project_config


def _write_yaml(root: Path, content: str) -> Path:
    path = root / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_relative_sqlite_url_anchored_to_config_dir():
    ConfigFactory.reset()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg_path = _write_yaml(root, "database:\n  url: \"sqlite+aiosqlite:///./data/app.db\"\n")
        cfg = load_project_config(cfg_path)
        expected = f"sqlite+aiosqlite:///{(root / 'data' / 'app.db').resolve()}"
        assert cfg.database.url == expected


def test_absolute_sqlite_url_kept_unchanged():
    ConfigFactory.reset()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        abs_path = (root / "data" / "app.db").resolve()
        cfg_path = _write_yaml(root, f"database:\n  url: \"sqlite+aiosqlite:///{abs_path}\"\n")
        cfg = load_project_config(cfg_path)
        assert cfg.database.url == f"sqlite+aiosqlite:///{abs_path}"


def test_non_sqlite_url_kept_unchanged():
    ConfigFactory.reset()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg_path = _write_yaml(root, "database:\n  url: \"postgresql://user:pass@host/db\"\n")
        cfg = load_project_config(cfg_path)
        assert cfg.database.url == "postgresql://user:pass@host/db"