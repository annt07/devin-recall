"""Local data folder (per machine, per repo), Devin data folders and the shared error log."""
from __future__ import annotations

import hashlib
import os
import traceback
from datetime import datetime
from pathlib import Path

_WINDOWS = os.name == "nt"
ERRORS_MAX_BYTES = 1_000_000


def local_base() -> Path:
    """RECALL_LOCAL_DIR, else LOCALAPPDATA (Windows), else XDG_DATA_HOME, else ~/.local/share."""
    env = os.environ.get("RECALL_LOCAL_DIR")
    if env:
        return Path(env)
    root = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            or str(Path.home() / ".local" / "share"))
    return Path(root) / "devin-recall"


def _env_config_bases() -> list[Path]:
    return [Path(os.environ[e]) for e in ("APPDATA", "XDG_CONFIG_HOME") if os.environ.get(e)]


def devin_config_bases() -> list[Path]:
    """Folders that may hold Devin's `devin/` config folder, in priority order: APPDATA,
    XDG_CONFIG_HOME, ~/Library/Application Support (macOS), ~/.config."""
    return [*_env_config_bases(), Path.home() / "Library" / "Application Support", Path.home() / ".config"]


def default_config_base() -> Path:
    """Where Devin data goes when no candidate exists yet: the first env folder, else ~/.config."""
    env = _env_config_bases()
    return env[0] if env else Path.home() / ".config"


def devin_base() -> Path:
    """RECALL_DEVIN_DIR, else the first candidate that has a `devin/` folder, else the default."""
    env = os.environ.get("RECALL_DEVIN_DIR")
    if env:
        return Path(env)
    for b in devin_config_bases():
        if (b / "devin").is_dir():
            return b
    return default_config_base()


def local_dir(repo_root) -> Path:
    key = str(Path(repo_root).resolve())
    if _WINDOWS:
        key = key.lower()
    return local_base() / hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def redact(text: str) -> str:
    """Replace secret-looking parts (the Gate's patterns) with <redacted>."""
    from recall.gate import SECRET_PATTERNS
    for _, rx in SECRET_PATTERNS:
        text = rx.sub("<redacted>", text)
    return text


def log_error(repo_root, exc_or_msg) -> None:
    """Append one redacted entry to errors.log (rotated at 1 MB). Never raises."""
    try:
        d = local_dir(repo_root) if repo_root else local_base()
        d.mkdir(parents=True, exist_ok=True)
        if isinstance(exc_or_msg, BaseException):
            text = "".join(traceback.format_exception(type(exc_or_msg), exc_or_msg, exc_or_msg.__traceback__))
        else:
            text = str(exc_or_msg)
        log = d / "errors.log"
        try:
            if log.stat().st_size >= ERRORS_MAX_BYTES:
                os.replace(log, log.with_name("errors.log.1"))
        except OSError:
            pass
        with open(log, "a", encoding="utf-8", newline="\n") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {redact(text.rstrip())}\n")
    except Exception:
        pass
