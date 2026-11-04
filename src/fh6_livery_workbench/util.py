from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timestamp14() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def local_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_slug(value: str, fallback: str = "livery", limit: int = 64) -> str:
    value = value.strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:limit].rstrip(" .") or fallback


def json_dump_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def relative_manifest(root: Path, files: Iterable[Path]) -> list[dict[str, Any]]:
    result = []
    for file in sorted(files, key=lambda p: str(p).lower()):
        result.append(
            {
                "path": file.relative_to(root).as_posix(),
                "size": file.stat().st_size,
                "sha256": sha256_file(file),
            }
        )
    return result


def app_root() -> Path:
    """Writable portable root next to the executable, or repository root in source mode."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else app_root()


def runtime_workspace() -> Path:
    root = app_root() / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root

