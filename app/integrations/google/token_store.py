"""Secure-enough local token storage for a single-user Windows app.

Tokens live in `secrets/google_tokens.json` (gitignored) with best-effort
0600 permissions plus the in-memory access-token cache. Only refresh tokens
persist; access tokens are re-issued via refresh. Never log token values.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_FILE = Path("secrets") / "google_tokens.json"


def token_file_path(base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else Path.cwd()
    return root / TOKEN_FILE


def save_tokens(tokens: dict, path: Path | None = None) -> Path:
    """Persist the refresh token (and metadata, never secrets in logs)."""
    target = path if path is not None else token_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "refresh_token": tokens.get("refresh_token", ""),
        "scope": tokens.get("scope", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "obtained_at": int(time.time()),
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        logger.warning("Could not restrict token file permissions.")
    os.replace(tmp, target)
    return target


def load_tokens(path: Path | None = None) -> dict | None:
    target = path if path is not None else token_file_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Google token file is corrupt.")
        return None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None
    return data


def clear_tokens(path: Path | None = None) -> None:
    target = path if path is not None else token_file_path()
    try:
        target.unlink()
    except OSError:
        pass


def is_connected(path: Path | None = None) -> bool:
    return load_tokens(path) is not None


def granted_scopes(path: Path | None = None) -> set[str]:
    """Return the OAuth scopes stored with the refresh token (may be empty)."""
    stored = load_tokens(path)
    if stored is None:
        return set()
    raw = stored.get("scope", "")
    if not isinstance(raw, str):
        return set()
    return {part for part in raw.split() if part}
