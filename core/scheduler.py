from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import DEFAULT_TIMEZONE, SCHEDULE_FILE, TIKTOK_QUEUE_POLL_SECONDS, TIKTOK_SCHEDULER_ENABLED
from .tiktok_upload import fetch_tiktok_publish_status, upload_video_to_tiktok_direct, upload_video_to_tiktok_inbox
from .utils import read_json, write_json
from .youtube_upload import upload_video


@dataclass
class ScheduleResult:
    job_id: str
    platform: str
    status: str
    message: str
    response: dict[str, Any] | None = None


_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()
_worker_lock = threading.Lock()

TERMINAL_STATUSES = {"posted", "canceled"}
ACTIVE_STATUSES = {"scheduled", "processing", "submitted", "error"}
TIKTOK_PROCESSING_STATUSES = {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "PROCESSING"}
TIKTOK_SUCCESS_STATUSES = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}
TIKTOK_FAILED_STATUSES = {"FAILED"}
MAX_HISTORY_EVENTS = 40


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _now() -> datetime:
    return datetime.now(_tz())


def _iso_now() -> str:
    return _now().isoformat(timespec="seconds")


def _new_schedule_id() -> str:
    return uuid.uuid4().hex[:12]


def load_queue(queue_path: Path = SCHEDULE_FILE) -> list[dict[str, Any]]:
    if not queue_path.exists():
        return []
    try:
        data = read_json(queue_path)
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def save_queue(queue_path: Path, queue: list[dict[str, Any]]) -> None:
    write_json(queue_path, queue)


def _append_event(item: dict[str, Any], event: str, message: str, **extra: Any) -> None:
    events = item.get("events")
    if not isinstance(events, list):
        events = []
    payload = {
        "at": _iso_now(),
        "event": event,
        "message": message,
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    events.append(payload)
    item["events"] = events[-MAX_HISTORY_EVENTS:]
    item["last_event"] = message
    item["last_event_at"] = payload["at"]


def add_or_replace_scheduled_post(queue_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    """Adiciona/atualiza um item da fila.

    A chave padrão é (job_id, platform), para evitar vários agendamentos duplicados
    do mesmo pacote/plataforma quando o usuário clica mais de uma vez.
    """
    queue = load_queue(queue_path)
    key = (item.get("job_id"), item.get("platform"))
    existing = next((old for old in queue if (old.get("job_id"), old.get("platform")) == key), None)
    queue = [old for old in queue if (old.get("job_id"), old.get("platform")) != key]
    item = dict(item)
    item.setdefault("schedule_id", (existing or {}).get("schedule_id") or _new_schedule_id())
    item.setdefault("status", "scheduled")
    item.setdefault("attempts", int((existing or {}).get("attempts", 0)))
    item.setdefault("created_at", (existing or {}).get("created_at") or _iso_now())
    item.setdefault("events", (existing or {}).get("events") or [])
    item["updated_at"] = _iso_now()
    _append_event(item, "scheduled", f"Agendado para {item.get('scheduled_at') or 'sem data'}.")
    queue.append(item)
    save_queue(queue_path, queue)
    return item


def record_tiktok_submission(queue_path: Path, item: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Registra um envio TikTok feito manualmente para aparecer no painel de status/histórico."""
    queue = load_queue(queue_path)
    item = dict(item)
    item["schedule_id"] = item.get("schedule_id") or _new_schedule_id()
    item["platform"] = "tiktok"
    item["status"] = "submitted"
    item["scheduled_at"] = item.get("scheduled_at") or _iso_now()
    item["submitted_at"] = _iso_now()
    item["publish_id"] = response.get("publish_id")
    item["response"] = response
    item["tiktok_status"] = "UPLOAD_SUBMITTED"
    item["attempts"] = int(item.get("attempts") or 1)
    item["created_at"] = item.get("created_at") or _iso_now()
    item["updated_at"] = _iso_now()
    item.setdefault("events", [])
    _append_event(item, "submitted", f"Envio manual registrado. publish_id={item.get('publish_id') or 'indisponível'}.")
    queue.append(item)
    save_queue(queue_path, queue)
    return item


def cancel_scheduled_post(queue_path: Path, schedule_id: str) -> bool:
    queue = load_queue(queue_path)
    changed = False
    for item in queue:
        if str(item.get("schedule_id")) == str(schedule_id):
            if item.get("status") in ACTIVE_STATUSES:
                item["status"] = "canceled"
                item["canceled_at"] = _iso_now()
                item["updated_at"] = item["canceled_at"]
                _append_event(item, "canceled", "Agendamento cancelado pelo usuário.")
                changed = True
            break
    if changed:
        save_queue(queue_path, queue)
    return changed


def delete_scheduled_post(queue_path: Path, schedule_id: str) -> bool:
    queue = load_queue(queue_path)
    new_queue = [item for item in queue if str(item.get("schedule_id")) != str(schedule_id)]
    if len(new_queue) == len(queue):
        return False
    save_queue(queue_path, new_queue)
    return True


def retry_scheduled_post(queue_path: Path, schedule_id: str) -> bool:
    queue = load_queue(queue_path)
    changed = False
    for item in queue:
        if str(item.get("schedule_id")) == str(schedule_id) and item.get("status") == "error":
            item["status"] = "scheduled"
            item.pop("error", None)
            item["updated_at"] = _iso_now()
            _append_event(item, "retry", "Item recolocado na fila para nova tentativa.")
            changed = True
            break
    if changed:
        save_queue(queue_path, queue)
    return changed


def schedule_post_now(queue_path: Path, schedule_id: str) -> bool:
    """Move um item scheduled/error para agora, para processar manualmente."""
    queue = load_queue(queue_path)
    changed = False
    for item in queue:
        if str(item.get("schedule_id")) == str(schedule_id) and item.get("status") in {"scheduled", "error"}:
            item["status"] = "scheduled"
            item["scheduled_at"] = _iso_now()
            item.pop("error", None)
            item["updated_at"] = _iso_now()
            _append_event(item, "manual_run", "Item movido para processamento imediato pelo usuário.")
            changed = True
            break
    if changed:
        save_queue(queue_path, queue)
    return changed


def reset_stale_processing_items(queue_path: Path, older_than_minutes: int = 120) -> int:
    queue = load_queue(queue_path)
    cutoff = _now() - timedelta(minutes=max(5, older_than_minutes))
    changed = 0
    for item in queue:
        if item.get("status") != "processing":
            continue
        try:
            last_attempt = datetime.fromisoformat(str(item.get("last_attempt_at") or ""))
            if last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=_tz())
        except ValueError:
            last_attempt = datetime.min.replace(tzinfo=_tz())
        if last_attempt <= cutoff:
            item["status"] = "scheduled"
            item["updated_at"] = _iso_now()
            item["note"] = "Reagendado automaticamente após processamento interrompido."
            _append_event(item, "stale_reset", "Processamento antigo foi destravado e voltou para a fila.")
            changed += 1
    if changed:
        save_queue(queue_path, queue)
    return changed


def due_items(queue: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=_tz())
    output: list[dict[str, Any]] = []
    for item in queue:
        if item.get("status") != "scheduled":
            continue
        scheduled_at = str(item.get("scheduled_at") or "").strip()
        if not scheduled_at:
            continue
        try:
            dt = datetime.fromisoformat(scheduled_at)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz())
        if dt <= current:
            output.append(item)
    return output


class _ExclusiveFileLock:
    """Lock simples por arquivo para reduzir risco de publicação duplicada."""

    def __init__(self, lock_path: Path, ttl_seconds: int = 1800):
        self.lock_path = lock_path
        self.ttl_seconds = ttl_seconds
        self.fd: int | None = None
        self.acquired = False

    def __enter__(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.lock_path.exists():
                age = time.time() - self.lock_path.stat().st_mtime
                if age > self.ttl_seconds:
                    self.lock_path.unlink(missing_ok=True)
            self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, f"pid={os.getpid()} at={datetime.utcnow().isoformat()}Z".encode("utf-8"))
            self.acquired = True
        except FileExistsError:
            self.acquired = False
        return self.acquired

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        if self.acquired:
            self.lock_path.unlink(missing_ok=True)


def _mark_item(queue_path: Path, schedule_id: str, updates: dict[str, Any], event: tuple[str, str] | None = None) -> None:
    queue = load_queue(queue_path)
    for item in queue:
        if str(item.get("schedule_id")) == str(schedule_id):
            item.update(updates)
            item["updated_at"] = _iso_now()
            if event:
                _append_event(item, event[0], event[1])
            break
    save_queue(queue_path, queue)


def _publish_item(item: dict[str, Any], progress_callback: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    platform = str(item.get("platform") or "")
    if platform in {"youtube", "youtube_shorts"}:
        return upload_video(
            video_path=Path(item["video_path"]),
            title=item.get("title", ""),
            description=item.get("description", ""),
            tags=list(item.get("tags") or []),
            thumbnail_path=Path(item["thumbnail_path"]) if item.get("thumbnail_path") else None,
            privacy_status=item.get("privacy_status", "private"),
            publish_at=item.get("publish_at") or None,
            category_id=item.get("category_id", "10"),
            made_for_kids=bool(item.get("made_for_kids", False)),
            progress_callback=progress_callback,
        )

    if platform == "tiktok":
        mode = item.get("tiktok_mode", "direct_post")
        if mode == "inbox_upload":
            return upload_video_to_tiktok_inbox(
                video_path=Path(item["video_path"]),
                access_token=item.get("access_token") or None,
                progress_callback=progress_callback,
            )
        return upload_video_to_tiktok_direct(
            video_path=Path(item["video_path"]),
            caption=item.get("caption", ""),
            privacy_level=item.get("privacy_level", "SELF_ONLY"),
            disable_duet=bool(item.get("disable_duet", False)),
            disable_stitch=bool(item.get("disable_stitch", False)),
            disable_comment=bool(item.get("disable_comment", False)),
            video_cover_timestamp_ms=int(item.get("video_cover_timestamp_ms", 1000)),
            brand_content_toggle=bool(item.get("brand_content_toggle", False)),
            brand_organic_toggle=bool(item.get("brand_organic_toggle", False)),
            is_aigc=bool(item.get("is_aigc", False)),
            access_token=item.get("access_token") or None,
            progress_callback=progress_callback,
        )

    raise ValueError(f"Plataforma desconhecida: {platform}")


def _normalize_tiktok_publish_status(status_data: dict[str, Any]) -> tuple[str, str, str]:
    status = str(status_data.get("status") or "").upper()
    fail_reason = str(status_data.get("fail_reason") or status_data.get("fail_reason_detail") or "").strip()
    if status in TIKTOK_SUCCESS_STATUSES:
        return "posted", status, "TikTok confirmou a publicação/entrega."
    if status in TIKTOK_FAILED_STATUSES:
        suffix = f" Motivo: {fail_reason}" if fail_reason else ""
        return "error", status, f"TikTok retornou falha no processamento.{suffix}"
    if status in TIKTOK_PROCESSING_STATUSES or not status:
        return "submitted", status or "AGUARDANDO_STATUS", "TikTok ainda está processando o envio."
    return "submitted", status, f"TikTok retornou status intermediário: {status}."


def sync_tiktok_publish_statuses(queue_path: Path = SCHEDULE_FILE) -> list[ScheduleResult]:
    """Atualiza itens TikTok que já têm publish_id, mas ainda não chegaram a estado final."""
    results: list[ScheduleResult] = []
    lock_path = queue_path.with_suffix(queue_path.suffix + ".status.lock")
    with _ExclusiveFileLock(lock_path, ttl_seconds=600) as acquired:
        if not acquired:
            return results

        queue = load_queue(queue_path)
        changed = False
        for item in queue:
            if item.get("platform") != "tiktok":
                continue
            if item.get("status") not in {"submitted", "processing"}:
                continue
            publish_id = str(item.get("publish_id") or "").strip()
            if not publish_id:
                continue
            try:
                status_data = fetch_tiktok_publish_status(publish_id, access_token=item.get("access_token") or None)
                queue_status, tiktok_status, message = _normalize_tiktok_publish_status(status_data)
                item["status"] = queue_status
                item["tiktok_status"] = tiktok_status
                item["status_response"] = status_data
                item["last_status_checked_at"] = _iso_now()
                if queue_status == "posted":
                    item["posted_at"] = item.get("posted_at") or _iso_now()
                    item.pop("error", None)
                elif queue_status == "error":
                    item["error"] = message
                    item["last_error_at"] = _iso_now()
                _append_event(item, "status_sync", message, tiktok_status=tiktok_status)
                results.append(ScheduleResult(str(item.get("job_id") or publish_id), "tiktok", queue_status, message, status_data))
                changed = True
            except Exception as exc:  # noqa: BLE001
                item["last_status_checked_at"] = _iso_now()
                item["status_check_error"] = str(exc)
                _append_event(item, "status_sync_error", f"Falha ao consultar status TikTok: {exc}")
                results.append(ScheduleResult(str(item.get("job_id") or publish_id), "tiktok", "error", str(exc), None))
                changed = True
        if changed:
            save_queue(queue_path, queue)
    return results


def process_due_posts(
    queue_path: Path = SCHEDULE_FILE,
    progress_callback: Callable[[float, str], None] | None = None,
) -> list[ScheduleResult]:
    results: list[ScheduleResult] = []
    lock_path = queue_path.with_suffix(queue_path.suffix + ".lock")
    with _ExclusiveFileLock(lock_path) as acquired:
        if not acquired:
            return results

        reset_stale_processing_items(queue_path)
        queue = load_queue(queue_path)
        due = due_items(queue)
        if not due:
            return results

        for item in due:
            schedule_id = str(item.get("schedule_id") or "")
            platform = str(item.get("platform") or "")
            job_id = str(item.get("job_id") or schedule_id)
            attempts = int(item.get("attempts") or 0) + 1
            _mark_item(
                queue_path,
                schedule_id,
                {
                    "status": "processing",
                    "last_attempt_at": _iso_now(),
                    "attempts": attempts,
                },
                event=("processing", f"Tentativa {attempts}: iniciando envio para {platform}."),
            )
            try:
                response = _publish_item(item, progress_callback=progress_callback)
                publish_id = response.get("publish_id") if isinstance(response, dict) else None
                if platform == "tiktok":
                    _mark_item(
                        queue_path,
                        schedule_id,
                        {
                            "status": "submitted",
                            "submitted_at": _iso_now(),
                            "response": response,
                            "publish_id": publish_id,
                            "tiktok_status": "UPLOAD_SUBMITTED",
                        },
                        event=("submitted", f"Vídeo enviado ao TikTok. publish_id={publish_id or 'indisponível'}."),
                    )
                    results.append(ScheduleResult(job_id, platform, "submitted", f"TikTok recebeu o upload. publish_id: {publish_id}", response))
                else:
                    message = str(response.get("url", "Publicado."))
                    _mark_item(
                        queue_path,
                        schedule_id,
                        {
                            "status": "posted",
                            "posted_at": _iso_now(),
                            "response": response,
                            "publish_id": publish_id,
                        },
                        event=("posted", message),
                    )
                    results.append(ScheduleResult(job_id, platform, "posted", message, response))
            except Exception as exc:  # noqa: BLE001
                _mark_item(
                    queue_path,
                    schedule_id,
                    {
                        "status": "error",
                        "error": str(exc),
                        "last_error_at": _iso_now(),
                    },
                    event=("error", str(exc)),
                )
                results.append(ScheduleResult(job_id, platform, "error", str(exc), None))

    # Após liberar o lock de upload, já tenta atualizar status dos uploads aceitos.
    results.extend(sync_tiktok_publish_statuses(queue_path))
    return results


def run_queue_maintenance(queue_path: Path = SCHEDULE_FILE) -> list[ScheduleResult]:
    """Executa uma rodada completa: destrava itens antigos, publica vencidos e consulta status pendentes."""
    results: list[ScheduleResult] = []
    reset_stale_processing_items(queue_path)
    results.extend(process_due_posts(queue_path))
    results.extend(sync_tiktok_publish_statuses(queue_path))
    return results


def _worker_loop(queue_path: Path, poll_seconds: int) -> None:
    while not _worker_stop.is_set():
        try:
            run_queue_maintenance(queue_path)
        except Exception:
            # Worker em background não deve derrubar o Streamlit.
            pass
        _worker_stop.wait(max(15, poll_seconds))


def start_tiktok_queue_worker(queue_path: Path = SCHEDULE_FILE, poll_seconds: int = TIKTOK_QUEUE_POLL_SECONDS) -> bool:
    """Inicia worker daemon local para processar a fila enquanto o app estiver rodando."""
    global _worker_thread
    if not TIKTOK_SCHEDULER_ENABLED:
        return False
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return True
        _worker_stop.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop,
            args=(queue_path, max(15, poll_seconds)),
            name="tiktok-schedule-worker",
            daemon=True,
        )
        _worker_thread.start()
        return True


def worker_is_running() -> bool:
    return bool(_worker_thread and _worker_thread.is_alive())
