"""Config: reads/writes ~/.atessa/config (env-style KEY=VALUE, comments preserved)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

_FS_LOCK = threading.Lock()


def get_atessa_dir() -> Path:
    if env_home := os.environ.get("ATESSA_HOME"):
        return Path(env_home)
    if env_cfg := os.environ.get("ATESSA_CONFIG"):
        return Path(env_cfg).parent
    return Path.home() / ".atessa"


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding=encoding) as f:
            tmp_path = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def atomic_write_json(path: Path, data: dict | list, indent: int = 2) -> None:
    content = json.dumps(data, indent=indent) + "\n"
    atomic_write_text(path, content, encoding="utf-8")

ROLE_KEYS = {
    "default": "ATESSA_MODEL_DEFAULT",
    "vision": "ATESSA_MODEL_VISION",
    "ocr": "ATESSA_MODEL_OCR",
    "power": "ATESSA_MODEL_POWER",
    "image": "ATESSA_MODEL_IMAGE",
}
ROLE_DEFAULTS = {
    "default": "ling-3.0-flash",
    "vision": "gpt-5.6-luna",
    # Free and image-capable: text extraction should not burn credits.
    "ocr": "composer-2.5",
    "power": "kimi-k2.7-code",
    "image": "gpt-5.6-luna",
}
# Tried in order when the chosen OCR model is unavailable.
OCR_FALLBACKS = ("composer-2.5", "composer-2.5-fast", "gpt-5.6-luna")


class Config:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or os.environ.get("ATESSA_CONFIG") or (get_atessa_dir() / "config"))
        self.values: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        self.values = {}
        with _FS_LOCK:
            if self.path.is_file():
                try:
                    content = self.path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as error:
                    raise ValueError(f"cannot load config {self.path}: {error}") from error
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    self.values[k.strip()] = v.strip().strip('"').strip("'")

    @property
    def api_key(self) -> str:
        return (
            os.environ.get("ATESSA_API_KEY")
            or self.values.get("ATESSA_API_KEY")
            or self.values.get("WEBSEARCH_API_KEY")
            or ""
        )

    @property
    def base_url(self) -> str:
        return (
            os.environ.get("ATESSA_BASE_URL")
            or self.values.get("ATESSA_BASE_URL")
            or "https://atessa.top/v1"
        ).rstrip("/")

    def model_for(self, role: str) -> str:
        key = ROLE_KEYS[role]
        return os.environ.get(key) or self.values.get(key) or ROLE_DEFAULTS[role]

    def set_model(self, role: str, model: str) -> None:
        """Assign a model to a role and persist, preserving unrelated lines/comments."""
        key = ROLE_KEYS[role]
        if os.environ.get(key):
            raise ValueError(f"{role} route is overridden by the {key} environment variable")
        with _FS_LOCK:
            lines: list[str] = []
            if self.path.is_file():
                try:
                    lines = self.path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
            updated = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in line:
                    if line.split("=", 1)[0].strip() == key:
                        lines[i] = f"{key}={model}"
                        updated = True
                        break
            if not updated:
                lines.append(f"{key}={model}")
            content = "\n".join(lines) + "\n"
            atomic_write_text(self.path, content)
            self.values[key] = model
