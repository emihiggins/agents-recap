"""Defaults and optional ~/.agent-recap/config.toml overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("AGENT_RECAP_HOME", HOME / ".agent-recap"))

CLAUDE_DIR = HOME / ".claude"
CURSOR_USER = HOME / "Library/Application Support/Cursor/User"
VSCODE_USER = HOME / "Library/Application Support/Code/User"
CURSOR_PLANS = HOME / ".cursor/plans"

# Sessions rooted in these prefixes are throwaway noise, not real projects.
NOISE_PREFIXES = ("/tmp", "/private/tmp", "/private/var/folders", "/var/folders")

EMBED_DIM = 768


@dataclass
class Config:
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:8b"
    embed_model: str = "nomic-embed-text"
    claude_model: str = "claude-haiku-4-5-20251001"
    days: int = 7
    limit: int = 12
    max_age_days: int = 90
    batch_size: int = 6
    excerpt_chars: int = 1200
    sources: list[str] = field(default_factory=lambda: ["claude-code", "cursor", "vscode"])

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "store.db"

    @property
    def html_path(self) -> Path:
        return DATA_DIR / "recap.html"


def load() -> Config:
    cfg = Config()
    path = DATA_DIR / "config.toml"
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return cfg
        known = {f.name for f in fields(Config)}
        for key, value in raw.items():
            if key in known:
                setattr(cfg, key, value)
    return cfg


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def is_noise(project_path: str | None) -> bool:
    if not project_path:
        return False
    return project_path.startswith(NOISE_PREFIXES)
