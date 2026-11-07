from __future__ import annotations

import os
import stat
from pathlib import Path

from dotenv import load_dotenv


class MissingAPIKeyError(RuntimeError):
    """Raised when a live MinerU operation has no configured token."""


def load_environment(env_file: Path | None = None) -> None:
    """Load a local .env without overriding an explicitly exported variable."""

    if env_file is None:
        env_file = Path.cwd() / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def resolve_env_file(env_file: Path | None = None) -> Path:
    return (env_file or (Path.cwd() / ".env")).expanduser().resolve()


def write_environment(
    api_key: str,
    env_file: Path | None = None,
    base_url: str = "https://mineru.net",
) -> Path:
    """Create a new 0600 environment file without overwriting an old one."""

    value = api_key.strip()
    if not value:
        raise ValueError("api_key cannot be empty")
    if "\r" in value or "\n" in value:
        raise ValueError("api_key cannot contain line breaks")
    path = resolve_env_file(env_file)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing credential file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Local MinerU credential; keep this file private.\n"
        f"MINERU_API_KEY={value}\n"
        f"MINERU_BASE_URL={base_url.rstrip('/')}\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    path.chmod(0o600)
    return path


def describe_environment(env_file: Path | None = None) -> dict[str, object]:
    """Return safe setup diagnostics; never return the token value."""

    path = resolve_env_file(env_file)
    exported = bool(os.getenv("MINERU_API_KEY", "").strip())
    load_environment(path)
    configured = bool(os.getenv("MINERU_API_KEY", "").strip())
    mode: int | None = None
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    return {
        "env_file": str(path),
        "env_file_exists": path.exists(),
        "env_file_mode": f"{mode:04o}" if mode is not None else None,
        "env_file_permissions_ok": mode is None or mode & 0o077 == 0,
        "api_key_configured": configured,
        "api_key_source": "environment" if exported else ("env_file" if configured else "missing"),
        "base_url": get_base_url(path),
    }


def get_api_key(env_file: Path | None = None) -> str:
    load_environment(env_file)
    value = os.getenv("MINERU_API_KEY", "").strip()
    if not value:
        raise MissingAPIKeyError(
            "MINERU_API_KEY is not set. Copy .env.example to .env and set a freshly rotated token."
        )
    return value


def get_base_url(env_file: Path | None = None) -> str:
    load_environment(env_file)
    return os.getenv("MINERU_BASE_URL", "https://mineru.net").rstrip("/")
