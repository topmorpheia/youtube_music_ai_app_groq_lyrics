from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def slugify(value: str, fallback: str = "musica") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9áàâãéèêíïóôõöúçñü]+", "-", value, flags=re.IGNORECASE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or fallback


def new_job_id(title: str = "musica") -> str:
    return f"{slugify(title)}-{uuid.uuid4().hex[:8]}"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_uploaded_file(uploaded_file: Any, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as fh:
        fh.write(uploaded_file.getbuffer())
    return destination


def require_ffmpeg() -> None:
    missing = [cmd for cmd in ("ffmpeg", "ffprobe") if shutil.which(cmd) is None]
    if missing:
        raise RuntimeError(
            "FFmpeg/FFprobe não encontrado no PATH. Instale o FFmpeg antes de renderizar vídeos. "
            f"Comandos ausentes: {', '.join(missing)}"
        )


def run_command(args: list[str]) -> None:
    process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process.returncode != 0:
        raise RuntimeError("Comando falhou:\n" + " ".join(args) + "\n\n" + process.stderr[-5000:])


def media_duration_seconds(path: Path) -> float:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr)
    return float(process.stdout.strip())


def normalize_tags(tags: list[str], limit: int = 20) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = str(tag).strip().strip("#")
        tag = re.sub(r"\s+", " ", tag)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        clean.append(tag[:80])
        seen.add(key)
        if len(clean) >= limit:
            break
    return clean
