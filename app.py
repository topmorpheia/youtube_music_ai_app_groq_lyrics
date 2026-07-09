from __future__ import annotations

import hmac
import os
import shutil
from datetime import datetime, time
from ipaddress import ip_address, ip_network
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from core.ai_metadata import (
    append_lyrics_to_description,
    create_local_thumbnail,
    ensure_required_title,
    generate_metadata,
)
from core.config import (
    APP_ALLOWED_IPS,
    APP_AUTH_ENABLED,
    APP_IP_STRICT,
    APP_PASSWORD,
    APP_USERNAME,
    DEFAULT_TIMEZONE,
    OUTPUTS_DIR,
    SHORTS_MAX_DURATION_SECONDS,
    SHORTS_VIDEO_HEIGHT,
    SHORTS_VIDEO_WIDTH,
    SUPPORTED_AUDIO,
    SUPPORTED_IMAGES,
    SCHEDULE_FILE,
    SCHEDULER_PING_TOKEN,
    TIKTOK_DEFAULT_PRIVACY_LEVEL,
    TIKTOK_QUEUE_POLL_SECONDS,
    TIKTOK_SCHEDULER_ENABLED,
    TIKTOK_TOKEN_FILE,
    UPLOADS_DIR,
)
from core.media import optimize_cover, optimize_thumbnail, optimize_vertical_cover, render_vertical_video, render_youtube_video
from core.scheduler import (
    add_or_replace_scheduled_post,
    cancel_scheduled_post,
    delete_scheduled_post,
    load_queue,
    process_due_posts,
    record_tiktok_submission,
    retry_scheduled_post,
    run_queue_maintenance,
    schedule_post_now,
    start_tiktok_queue_worker,
    sync_tiktok_publish_statuses,
    worker_is_running,
)
from core.tiktok_upload import (
    describe_tiktok_token_source,
    fetch_tiktok_publish_status,
    query_creator_info,
    upload_video_to_tiktok_inbox,
)
from core.utils import ensure_dirs, media_duration_seconds, new_job_id, read_json, save_uploaded_file, write_json
from core.youtube_upload import upload_video

ensure_dirs(UPLOADS_DIR, OUTPUTS_DIR)

st.set_page_config(page_title="YouTube Music AI", page_icon="🎵", layout="wide")

st.markdown(
    """
    <style>
    .ym-card {
        border: 1px solid rgba(128,128,128,.25);
        border-radius: 16px;
        padding: 1rem 1.1rem;
        background: rgba(128,128,128,.06);
        margin-bottom: .75rem;
    }
    .ym-muted { opacity: .78; font-size: .92rem; }
    .ym-badge {
        display: inline-block;
        padding: .18rem .55rem;
        border-radius: 999px;
        font-size: .78rem;
        font-weight: 700;
        border: 1px solid rgba(128,128,128,.25);
        margin-right: .25rem;
    }
    .ym-badge-ok { background: rgba(46, 204, 113, .16); }
    .ym-badge-warn { background: rgba(241, 196, 15, .16); }
    .ym-badge-err { background: rgba(231, 76, 60, .16); }
    .ym-badge-info { background: rgba(52, 152, 219, .16); }
    </style>
    """,
    unsafe_allow_html=True,
)

def _tags_from_text(value: str) -> list[str]:
    return [tag.strip().strip("#") for tag in value.split(",") if tag.strip()]


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _now_local() -> datetime:
    return datetime.now(_tz())


def _combine_schedule_date_time(schedule_date, schedule_time) -> str:
    dt = datetime.combine(schedule_date, schedule_time).replace(tzinfo=_tz())
    return dt.isoformat(timespec="seconds")


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path(__file__).resolve().parent))
    except ValueError:
        return str(path)


def _path_is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _resolved_existing_path(value: object) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        return path.resolve()
    except Exception:  # noqa: BLE001
        return None


def _active_queue_refs_for_current_package(result: dict[str, object]) -> list[dict[str, object]]:
    """Retorna itens da fila que ainda precisam dos arquivos deste pacote."""
    job_id = _safe_str(result.get("job_id")).strip()
    current_paths = {
        path
        for path in (
            _resolved_existing_path(result.get("youtube_video")),
            _resolved_existing_path(result.get("shorts_tiktok_video")),
            _resolved_existing_path(result.get("manifest")),
        )
        if path is not None
    }
    active_statuses = {"scheduled", "processing", "error"}
    refs: list[dict[str, object]] = []
    for item in load_queue(SCHEDULE_FILE):
        if item.get("status") not in active_statuses:
            continue
        item_job_id = _safe_str(item.get("job_id")).strip()
        item_path = _resolved_existing_path(item.get("video_path"))
        same_job = bool(job_id and item_job_id == job_id)
        same_file = bool(item_path and item_path in current_paths)
        if same_job or same_file:
            refs.append(item)
    return refs


def _delete_current_package_files(result: dict[str, object]) -> list[str]:
    """Apaga somente a pasta do pacote atual dentro de outputs/."""
    deleted: list[str] = []
    job_dir_raw = _safe_str(result.get("job_dir")).strip()
    job_dir = Path(job_dir_raw) if job_dir_raw else None

    if job_dir and job_dir.exists():
        if not _path_is_inside(job_dir, OUTPUTS_DIR) or job_dir.resolve() == OUTPUTS_DIR.resolve():
            raise RuntimeError(f"Caminho recusado para limpeza: {_display_path(job_dir)}")
        shutil.rmtree(job_dir)
        deleted.append(_display_path(job_dir))
        return deleted

    # Fallback defensivo: se a pasta do job não existir, apaga apenas arquivos conhecidos
    # do pacote atual, desde que estejam dentro de outputs/.
    for field in (
        "youtube_video",
        "shorts_tiktok_video",
        "cover_16x9",
        "cover_vertical_9x16",
        "thumbnail",
        "vertical_preview",
        "manifest",
    ):
        path = Path(_safe_str(result.get(field)).strip()) if _safe_str(result.get(field)).strip() else None
        if not path or not path.exists():
            continue
        if path.is_file() and _path_is_inside(path, OUTPUTS_DIR):
            path.unlink(missing_ok=True)
            deleted.append(_display_path(path))
    return deleted


def _reset_current_package_state() -> None:
    st.session_state.result = None
    st.session_state.pop("review_section", None)
    st.session_state.pop("last_tiktok_publish_id", None)
    st.session_state.pop("last_tiktok_status", None)
    st.session_state["uploader_reset_nonce"] = int(st.session_state.get("uploader_reset_nonce", 0)) + 1


def _render_cleanup_current_package(result: dict[str, object]) -> None:
    st.divider()
    st.subheader("6. Limpeza local após finalizar os envios")
    st.caption(
        "Use este botão depois que você terminar de enviar/agendar nas plataformas desejadas. "
        "Ele apaga a pasta local deste pacote em `outputs/`, limpa a prévia atual e recarrega o app para começar outro envio. "
        "Credenciais, tokens, configurações da sidebar e a fila geral não são apagados."
    )

    active_refs = _active_queue_refs_for_current_package(result)
    if active_refs:
        ids = ", ".join(_safe_str(item.get("schedule_id"), "sem ID") for item in active_refs[:5])
        st.warning(
            "Este pacote ainda tem item TikTok pendente/erro/processando na fila. "
            "Não apague os arquivos locais antes da publicação pela fila, senão o worker não terá o vídeo para enviar. "
            f"Itens relacionados: {ids}."
        )

    if st.button(
        "🧹 Apagar vídeos locais deste pacote e iniciar novo envio",
        key="cleanup_current_package_and_restart",
        type="secondary",
        disabled=bool(active_refs),
    ):
        try:
            deleted = _delete_current_package_files(result)
            _reset_current_package_state()
            st.session_state["cleanup_feedback"] = (
                "Pacote local apagado e tela recarregada para um novo envio. "
                + (f"Itens removidos: {', '.join(deleted)}." if deleted else "Nenhum arquivo local existente foi encontrado.")
            )
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Não consegui limpar os arquivos locais deste pacote: {exc}")


def _secret_or_env(name: str, default: object = "") -> object:
    value = os.getenv(name)
    if value not in {None, ""}:
        return value
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:  # noqa: BLE001
        pass
    return default


def _bool_setting(name: str, default: bool = False) -> bool:
    value = _secret_or_env(name, str(default).lower())
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "sim"}


def _csv_setting(name: str, default: list[str] | None = None) -> list[str]:
    default = default or []
    value = _secret_or_env(name, ",".join(default))
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _request_headers() -> dict[str, str]:
    context = getattr(st, "context", None)
    headers = getattr(context, "headers", {}) if context is not None else {}
    try:
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except Exception:  # noqa: BLE001
        return {}


def _client_ip() -> str:
    headers = _request_headers()
    for header_name in ("cf-connecting-ip", "true-client-ip", "x-real-ip", "x-forwarded-for"):
        value = headers.get(header_name, "")
        if value:
            return value.split(",")[0].strip()
    return ""


def _ip_is_allowed(client_ip: str, allowed_entries: list[str]) -> bool:
    if not allowed_entries:
        return True
    if not client_ip:
        return False
    for entry in allowed_entries:
        if client_ip == entry:
            return True
        try:
            if ip_address(client_ip) in ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _require_app_access() -> None:
    allowed_ips = _csv_setting("APP_ALLOWED_IPS", APP_ALLOWED_IPS)
    ip_strict = _bool_setting("APP_IP_STRICT", APP_IP_STRICT)
    client_ip = _client_ip()
    if allowed_ips and not _ip_is_allowed(client_ip, allowed_ips):
        if ip_strict:
            st.error("Acesso bloqueado: este IP não está autorizado para usar o app.")
            st.caption(f"IP detectado: `{client_ip or 'não identificado'}` | Permitidos: `{', '.join(allowed_ips)}`")
            st.stop()
        st.warning(
            "APP_ALLOWED_IPS está configurado, mas o IP do visitante não foi confirmado. "
            "Como APP_IP_STRICT=false, vou continuar exigindo login por senha."
        )

    configured_password = str(_secret_or_env("APP_PASSWORD", APP_PASSWORD) or "")
    configured_username = str(_secret_or_env("APP_USERNAME", APP_USERNAME) or APP_USERNAME or "admin")
    auth_enabled = _bool_setting("APP_AUTH_ENABLED", APP_AUTH_ENABLED) or bool(configured_password)
    if not auth_enabled:
        if not allowed_ips:
            st.warning(
                "Segurança do app ainda não está ativada. Antes de publicar, configure APP_PASSWORD "
                "e, se quiser, APP_ALLOWED_IPS=189.120.78.7."
            )
        return

    if not configured_password:
        st.error("APP_AUTH_ENABLED=true, mas APP_PASSWORD está vazio. Configure uma senha antes de usar o app publicado.")
        st.stop()

    if st.session_state.get("app_authenticated") is True:
        with st.sidebar:
            st.caption(f"🔐 Logado como `{configured_username}`")
            if client_ip:
                st.caption(f"IP detectado: `{client_ip}`")
            if st.button("Sair", key="logout_app"):
                st.session_state["app_authenticated"] = False
                st.rerun()
        return

    st.title("🔐 YouTube Music AI")
    st.caption("Entre para acessar o gerador, uploads e fila de publicação.")
    with st.form("login_form"):
        username = st.text_input("Usuário", value=configured_username)
        password = st.text_input("Senha", type="password")
        submitted = st.form_submit_button("Entrar", type="primary")
    if submitted:
        user_ok = hmac.compare_digest(username.strip(), configured_username)
        pass_ok = hmac.compare_digest(password, configured_password)
        if user_ok and pass_ok:
            st.session_state["app_authenticated"] = True
            st.rerun()
        st.error("Usuário ou senha inválidos.")
    st.stop()


SIDEBAR_SETTINGS_FILE = OUTPUTS_DIR / "ui_settings.json"
EFFECT_OPTIONS = ["cinematic_zoom", "subtle_pulse", "none"]
YOUTUBE_PRIVACY_OPTIONS = ["private", "unlisted", "public"]
DEFAULT_SIDEBAR_SETTINGS = {
    "effect": "none",
    "privacy": "public",
    "made_for_kids": False,
}


def _load_sidebar_settings() -> dict[str, object]:
    settings = DEFAULT_SIDEBAR_SETTINGS.copy()
    try:
        if SIDEBAR_SETTINGS_FILE.exists():
            saved = read_json(SIDEBAR_SETTINGS_FILE)
            if isinstance(saved, dict):
                effect = saved.get("effect")
                privacy = saved.get("privacy")
                if effect in EFFECT_OPTIONS:
                    settings["effect"] = effect
                if privacy in YOUTUBE_PRIVACY_OPTIONS:
                    settings["privacy"] = privacy
                if isinstance(saved.get("made_for_kids"), bool):
                    settings["made_for_kids"] = saved["made_for_kids"]
    except Exception:  # noqa: BLE001
        # Se o arquivo local estiver corrompido, mantém os defaults definidos no app.
        return DEFAULT_SIDEBAR_SETTINGS.copy()
    return settings


def _save_sidebar_settings(settings: dict[str, object]) -> None:
    sanitized = {
        "effect": settings.get("effect") if settings.get("effect") in EFFECT_OPTIONS else DEFAULT_SIDEBAR_SETTINGS["effect"],
        "privacy": settings.get("privacy") if settings.get("privacy") in YOUTUBE_PRIVACY_OPTIONS else DEFAULT_SIDEBAR_SETTINGS["privacy"],
        "made_for_kids": bool(settings.get("made_for_kids", DEFAULT_SIDEBAR_SETTINGS["made_for_kids"])),
    }
    try:
        current = read_json(SIDEBAR_SETTINGS_FILE) if SIDEBAR_SETTINGS_FILE.exists() else None
    except Exception:  # noqa: BLE001
        current = None
    if current != sanitized:
        write_json(SIDEBAR_SETTINGS_FILE, sanitized)


def _safe_str(value: object, default: str = "") -> str:
    return str(value if value is not None else default)


def _tiktok_status_message(status_data: dict[str, object]) -> tuple[str, str]:
    status = _safe_str(status_data.get("status")).upper()
    fail_reason = _safe_str(status_data.get("fail_reason"))
    if status == "SEND_TO_USER_INBOX":
        return (
            "success",
            "TikTok confirmou SEND_TO_USER_INBOX: a notificação foi enviada para a Inbox/Caixa de entrada da conta que autorizou o token.",
        )
    if status == "PUBLISH_COMPLETE":
        return "success", "TikTok confirmou PUBLISH_COMPLETE: o post foi concluído pelo fluxo do TikTok."
    if status in {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"}:
        return "info", "TikTok ainda está processando o arquivo. Aguarde um pouco e consulte o status novamente."
    if status == "FAILED":
        suffix = f" Motivo: {fail_reason}." if fail_reason else ""
        return "error", "TikTok retornou FAILED para esse publish_id." + suffix
    if status:
        return "info", f"Status retornado pelo TikTok: {status}."
    return "info", "Status do TikTok recebido, mas sem campo `status` explícito."


def _render_tiktok_status(status_data: dict[str, object]) -> None:
    level, message = _tiktok_status_message(status_data)
    if level == "success":
        st.success(message)
    elif level == "error":
        st.error(message)
    else:
        st.info(message)

    uploaded_bytes = status_data.get("uploaded_bytes")
    downloaded_bytes = status_data.get("downloaded_bytes")
    public_ids = status_data.get("publicaly_available_post_id")
    if uploaded_bytes:
        st.caption(f"Bytes enviados processados pelo TikTok: {uploaded_bytes}.")
    if downloaded_bytes:
        st.caption(f"Bytes baixados/processados pelo TikTok: {downloaded_bytes}.")
    if public_ids:
        st.caption(f"Post IDs públicos retornados pelo TikTok: {public_ids}")
    st.json(status_data)


def _fetch_and_render_tiktok_status(publish_id: str, access_token: str | None = None) -> None:
    status_data = fetch_tiktok_publish_status(publish_id, access_token=access_token)
    st.session_state["last_tiktok_status"] = status_data
    _render_tiktok_status(status_data)


def _save_last_tiktok_publish(response: dict[str, object]) -> str:
    publish_id = _safe_str(response.get("publish_id")).strip()
    if publish_id:
        st.session_state["last_tiktok_publish_id"] = publish_id
    return publish_id


def _review_section_options(has_youtube: bool, has_vertical: bool) -> list[str]:
    section_names: list[str] = []
    if has_youtube:
        section_names.append("YouTube 16:9")
    if has_vertical:
        section_names.extend(["YouTube Shorts", "TikTok"])
    section_names.append("Prompts de imagem")
    return section_names


def _select_review_section(section_names: list[str]) -> str:
    current = st.session_state.get("review_section")
    if current not in section_names:
        st.session_state["review_section"] = section_names[0]
    return st.radio(
        "Aba de revisão",
        section_names,
        horizontal=True,
        label_visibility="collapsed",
        key="review_section",
    )


def _render_tiktok_status_controls(access_token: str | None = None) -> None:
    last_publish_id = _safe_str(st.session_state.get("last_tiktok_publish_id")).strip()
    if not last_publish_id:
        return

    with st.expander("Status do último envio TikTok", expanded=True):
        st.caption(f"publish_id: `{last_publish_id}`")
        if st.button("🔎 Consultar status desse publish_id no TikTok", key="tiktok_fetch_last_status"):
            try:
                _fetch_and_render_tiktok_status(last_publish_id, access_token=access_token)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Erro ao consultar status TikTok: {exc}")
        elif isinstance(st.session_state.get("last_tiktok_status"), dict):
            _render_tiktok_status(st.session_state["last_tiktok_status"])



def _status_badge(status: object) -> str:
    value = _safe_str(status, "?").lower()
    labels = {
        "scheduled": ("Agendado", "warn"),
        "processing": ("Enviando", "info"),
        "submitted": ("Aguardando TikTok", "info"),
        "posted": ("Publicado/confirmado", "ok"),
        "error": ("Erro", "err"),
        "canceled": ("Cancelado", "warn"),
    }
    label, kind = labels.get(value, (value or "?", "info"))
    return f'<span class="ym-badge ym-badge-{kind}">{label}</span>'


def _status_label(status: object) -> str:
    value = _safe_str(status, "?").lower()
    return {
        "scheduled": "⏰ Agendado",
        "processing": "🔄 Enviando",
        "submitted": "🛰️ Aguardando TikTok",
        "posted": "✅ Confirmado",
        "error": "❌ Erro",
        "canceled": "🚫 Cancelado",
    }.get(value, value or "?")


def _safe_dt(value: object) -> datetime | None:
    raw = _safe_str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt


def _human_time_delta(target: object) -> str:
    dt = _safe_dt(target)
    if not dt:
        return "sem data"
    delta = dt - _now_local()
    seconds = int(delta.total_seconds())
    past = seconds < 0
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        text = f"{days}d {hours}h"
    elif hours:
        text = f"{hours}h {minutes}min"
    else:
        text = f"{minutes}min"
    return f"há {text}" if past else f"em {text}"


def _queue_item_label(item: dict[str, object]) -> str:
    scheduled = _safe_str(item.get("scheduled_at"), "sem data")
    status = _status_label(item.get("status"))
    caption = _safe_str(item.get("caption"), "").replace("\n", " ")[:60]
    schedule_id = _safe_str(item.get("schedule_id"))
    return f"{status} | {scheduled} | {caption or item.get('job_id')} | {schedule_id}"


def _queue_rows(include_all: bool = True) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in load_queue(SCHEDULE_FILE):
        if item.get("platform") != "tiktok":
            continue
        status = _safe_str(item.get("status"), "")
        if not include_all and status in {"posted", "canceled"}:
            continue
        video_path = Path(_safe_str(item.get("video_path"))) if item.get("video_path") else None
        scheduled_at = item.get("scheduled_at", "")
        rows.append(
            {
                "status": _status_label(status),
                "agendado_para": scheduled_at,
                "quando": _human_time_delta(scheduled_at),
                "tiktok_status": item.get("tiktok_status", ""),
                "tentativas": item.get("attempts", 0),
                "publish_id": item.get("publish_id", ""),
                "arquivo_ok": bool(video_path and video_path.exists()),
                "última_ação": item.get("last_event", item.get("error", "")),
                "erro": item.get("error", ""),
                "caption": _safe_str(item.get("caption"), "")[:140],
                "id": item.get("schedule_id", ""),
            }
        )
    rows.sort(key=lambda row: str(row.get("agendado_para") or ""), reverse=False)
    return rows


def _queue_counts() -> dict[str, int]:
    counts = {"scheduled": 0, "processing": 0, "submitted": 0, "posted": 0, "error": 0, "canceled": 0}
    for item in load_queue(SCHEDULE_FILE):
        if item.get("platform") != "tiktok":
            continue
        status = _safe_str(item.get("status"), "").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def _next_tiktok_due_text() -> str:
    items = [item for item in load_queue(SCHEDULE_FILE) if item.get("platform") == "tiktok" and item.get("status") == "scheduled"]
    dts = [(item, _safe_dt(item.get("scheduled_at"))) for item in items]
    dts = [(item, dt) for item, dt in dts if dt]
    if not dts:
        return "Nenhum pendente"
    item, dt = min(dts, key=lambda pair: pair[1])
    return f"{dt.isoformat(timespec='minutes')} ({_human_time_delta(item.get('scheduled_at'))})"


def _run_queue_action(label: str, action, success_empty: str = "Nada para atualizar.") -> None:  # type: ignore[no-untyped-def]
    try:
        results = action()
        if not results:
            st.info(success_empty)
        for result in results:
            if result.status in {"posted", "submitted"}:
                st.success(result.message)
                if result.response and result.response.get("publish_id"):
                    st.session_state["last_tiktok_publish_id"] = result.response["publish_id"]
            elif result.status == "error":
                st.error(result.message)
            else:
                st.info(result.message)
    except Exception as exc:  # noqa: BLE001
        st.error(f"{label}: {exc}")


def _render_tiktok_diagnostics(access_token: str | None = None) -> None:
    st.markdown("**Diagnóstico TikTok**")
    try:
        diag = describe_tiktok_token_source(access_token=access_token)
        safe_diag = {k: v for k, v in diag.items() if "token" not in k.lower() or k in {"has_access_token", "has_refresh_token", "token_file", "token_file_exists"}}
        st.json(safe_diag)
        if not diag.get("client_key_configured") or not diag.get("client_secret_configured"):
            st.warning("Client Key/Secret não estão totalmente configurados. Sem isso, refresh automático pode falhar quando o token expirar.")
        if not diag.get("has_refresh_token"):
            st.warning("Não encontrei refresh_token. Quando o access token expirar, será necessário gerar outro token manualmente.")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Não consegui diagnosticar o token TikTok: {exc}")

    if st.button("🧪 Testar conexão e permissões da conta TikTok", key="dashboard_test_tiktok_creator"):
        try:
            info = query_creator_info(access_token=access_token)
            st.success("TikTok respondeu à consulta da conta. Confira as privacidades disponíveis abaixo.")
            st.json(info)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Falha no teste TikTok. Isso normalmente indica token expirado, escopo ausente ou app sem permissão: {exc}")


def _render_tiktok_queue_manager(expanded: bool = False, access_token: str | None = None) -> None:
    with st.expander("📡 Central TikTok: fila, status e logs", expanded=expanded):
        worker_label = "rodando" if worker_is_running() else "parado"
        if TIKTOK_SCHEDULER_ENABLED:
            st.caption(
                f"Worker local: `{worker_label}` | checagem a cada `{TIKTOK_QUEUE_POLL_SECONDS}s` | "
                f"próximo pendente: `{_next_tiktok_due_text()}` | fila: `{_display_path(SCHEDULE_FILE)}`"
            )
            ping_enabled = bool(_safe_str(_secret_or_env("SCHEDULER_PING_TOKEN", SCHEDULER_PING_TOKEN)).strip())
            if ping_enabled:
                st.caption(
                    "Ping externo habilitado: um monitor pode acessar a URL do app com `?queue_token=SEU_TOKEN` "
                    "para acordar a fila, processar vencidos e consultar status sem abrir a interface."
                )
            else:
                st.caption(
                    "Dica para Streamlit Cloud: configure `SCHEDULER_PING_TOKEN` e um monitor externo para acessar "
                    "`?queue_token=...` periodicamente. Isso reduz o risco de o app dormir no horário do TikTok."
                )
        else:
            st.warning("TIKTOK_SCHEDULER_ENABLED=false. A fila fica salva, mas o worker automático não roda.")

        counts = _queue_counts()
        metric_cols = st.columns(5)
        metric_cols[0].metric("Agendados", counts.get("scheduled", 0))
        metric_cols[1].metric("Enviando", counts.get("processing", 0))
        metric_cols[2].metric("Aguardando TikTok", counts.get("submitted", 0))
        metric_cols[3].metric("Confirmados", counts.get("posted", 0))
        metric_cols[4].metric("Erros", counts.get("error", 0))

        st.markdown(
            "<div class='ym-card ym-muted'>"
            "O app agora não marca TikTok como publicado só porque o upload foi aceito. "
            "Ele salva o <code>publish_id</code>, consulta o status e só considera confirmado quando o TikTok retorna "
            "<code>PUBLISH_COMPLETE</code> ou <code>SEND_TO_USER_INBOX</code>."
            "</div>",
            unsafe_allow_html=True,
        )

        col_a, col_b, col_c, col_d = st.columns(4)
        with col_a:
            if st.button("🔄 Atualizar status TikTok", key="sync_tiktok_statuses"):
                _run_queue_action("Erro ao atualizar status", lambda: sync_tiktok_publish_statuses(SCHEDULE_FILE))
                st.rerun()
        with col_b:
            if st.button("⚙️ Processar vencidos agora", key="process_tiktok_due_now"):
                _run_queue_action("Erro ao processar fila", lambda: process_due_posts(SCHEDULE_FILE), "Não há itens vencidos.")
                st.rerun()
        with col_c:
            if st.button("🩺 Rodada completa", key="run_queue_maintenance_now"):
                _run_queue_action("Erro na manutenção", lambda: run_queue_maintenance(SCHEDULE_FILE), "Fila conferida; nada novo.")
                st.rerun()
        with col_d:
            if TIKTOK_SCHEDULER_ENABLED and not worker_is_running() and st.button("▶️ Iniciar worker", key="start_tiktok_worker"):
                start_tiktok_queue_worker(SCHEDULE_FILE)
                st.rerun()

        rows = _queue_rows(include_all=True)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum agendamento TikTok na fila ainda.")

        queue = [item for item in load_queue(SCHEDULE_FILE) if item.get("platform") == "tiktok"]
        if queue:
            labels = {_queue_item_label(item): str(item.get("schedule_id")) for item in queue}
            selected_label = st.selectbox("Gerenciar item TikTok", list(labels), key="manage_tiktok_queue_item")
            selected_id = labels[selected_label]
            selected_item = next((item for item in queue if str(item.get("schedule_id")) == selected_id), {})
            st.markdown(_status_badge(selected_item.get("status")), unsafe_allow_html=True)
            st.caption(f"ID: `{selected_id}` | publish_id: `{selected_item.get('publish_id', '') or 'ainda não gerado'}`")

            events = selected_item.get("events") if isinstance(selected_item.get("events"), list) else []
            if events:
                st.markdown("**Linha do tempo**")
                for event in reversed(events[-8:]):
                    st.caption(f"{event.get('at', '')} — {event.get('message', '')}")

            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            with col_m1:
                if selected_item.get("status") in {"scheduled", "error"} and st.button("🚀 Publicar agora", key="force_tiktok_queue_item_now"):
                    if schedule_post_now(SCHEDULE_FILE, selected_id):
                        _run_queue_action("Erro ao publicar agora", lambda: process_due_posts(SCHEDULE_FILE))
                        st.rerun()
                    else:
                        st.warning("Não encontrei item publicável com esse ID.")
            with col_m2:
                if selected_item.get("status") == "error" and st.button("♻️ Tentar novamente", key="retry_tiktok_queue_item"):
                    if retry_scheduled_post(SCHEDULE_FILE, selected_id):
                        st.success("Item voltou para a fila.")
                        st.rerun()
                    else:
                        st.warning("Não encontrei item com erro para reprocessar.")
            with col_m3:
                if selected_item.get("status") in {"scheduled", "processing", "submitted", "error"} and st.button("Cancelar", key="cancel_tiktok_queue_item"):
                    if cancel_scheduled_post(SCHEDULE_FILE, selected_id):
                        st.success("Agendamento cancelado.")
                        st.rerun()
                    else:
                        st.warning("Não encontrei item cancelável com esse ID.")
            with col_m4:
                if selected_item.get("status") in {"posted", "canceled", "error"} and st.button("🗑️ Remover da lista", key="delete_tiktok_queue_item"):
                    if delete_scheduled_post(SCHEDULE_FILE, selected_id):
                        st.success("Item removido da fila visual.")
                        st.rerun()

            publish_id = _safe_str(selected_item.get("publish_id")).strip()
            if publish_id and st.button("🔎 Consultar este publish_id agora", key="fetch_selected_tiktok_status"):
                try:
                    status_data = fetch_tiktok_publish_status(publish_id, access_token=access_token)
                    st.session_state["last_tiktok_publish_id"] = publish_id
                    st.session_state["last_tiktok_status"] = status_data
                    sync_tiktok_publish_statuses(SCHEDULE_FILE)
                    _render_tiktok_status(status_data)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Erro ao consultar status TikTok: {exc}")

            with st.expander("Ver JSON completo do item selecionado", expanded=False):
                st.json(selected_item)

        with st.expander("Diagnóstico de autenticação TikTok", expanded=False):
            _render_tiktok_diagnostics(access_token=access_token)


def _queue_ping_requested() -> bool:
    try:
        token = st.query_params.get("queue_token", "")
    except Exception:  # noqa: BLE001
        return False
    expected = _safe_str(_secret_or_env("SCHEDULER_PING_TOKEN", SCHEDULER_PING_TOKEN)).strip()
    return bool(expected and token and hmac.compare_digest(_safe_str(token), expected))


def _handle_queue_ping_if_requested() -> None:
    if not _queue_ping_requested():
        return
    if TIKTOK_SCHEDULER_ENABLED:
        start_tiktok_queue_worker(SCHEDULE_FILE)
    results = run_queue_maintenance(SCHEDULE_FILE)
    st.write(
        {
            "ok": True,
            "processed": len(results),
            "worker_running": worker_is_running(),
            "checked_at": _now_local().isoformat(timespec="seconds"),
        }
    )
    st.stop()


def _maintenance_tick() -> None:
    if not TIKTOK_SCHEDULER_ENABLED:
        return
    start_tiktok_queue_worker(SCHEDULE_FILE)
    now = _now_local()
    last_raw = st.session_state.get("last_queue_maintenance_at")
    last_dt = _safe_dt(last_raw)
    if last_dt and (now - last_dt).total_seconds() < max(30, min(TIKTOK_QUEUE_POLL_SECONDS, 120)):
        return
    st.session_state["last_queue_maintenance_at"] = now.isoformat(timespec="seconds")
    try:
        run_queue_maintenance(SCHEDULE_FILE)
    except Exception as exc:  # noqa: BLE001
        st.session_state["queue_maintenance_error"] = str(exc)


@st.fragment
def _render_review_sections(
    result: dict[str, object],
    has_youtube: bool,
    has_vertical: bool,
    target_publish_at: str | None,
    privacy: str,
    made_for_kids: bool,
) -> None:
    meta = result["metadata"]
    section_names = _review_section_options(has_youtube, has_vertical)
    selected_section = _select_review_section(section_names)
    st.caption("A seleção acima fica preservada durante envios e consultas, evitando voltar automaticamente para a primeira aba.")

    if selected_section == "YouTube 16:9" and has_youtube:
        youtube_video_path = Path(result["youtube_video"])
        thumbnail_path = Path(result["thumbnail"])
        edited_title = st.text_input("Título final do YouTube", value=meta.get("youtube_title", "")[:100], key="youtube_title")
        edited_description = st.text_area("Descrição final do YouTube", value=meta.get("youtube_description", ""), height=260, key="youtube_desc")
        edited_tags_text = st.text_area(
            "Tags do YouTube, separadas por vírgula",
            value=", ".join(meta.get("youtube_tags", [])),
            height=110,
            key="youtube_tags",
        )
        pinned_comment = st.text_area("Comentário fixado sugerido", value=meta.get("pinned_comment", ""), height=90, key="pinned_comment")
        st.markdown("**Palavras-chave sugeridas:** " + ", ".join(meta.get("keywords", [])))
        st.markdown("**Hashtags sugeridas:** " + " ".join(meta.get("hashtags", [])))

        st.subheader("Publicar YouTube 16:9")
        youtube_publish_at = target_publish_at
        youtube_button_label = (
            "📅 Enviar agora e agendar liberação no YouTube"
            if youtube_publish_at
            else "📤 Enviar agora para o YouTube"
        )
        if youtube_publish_at:
            st.caption(
                f"Usando o horário informado no início: `{youtube_publish_at}`. "
                "O vídeo será enviado como privado com `status.publishAt`."
            )
        if st.button(youtube_button_label, key="post_youtube_submit"):
            upload_progress = st.progress(0, text="Iniciando upload...")

            def on_progress(value: float, label: str) -> None:
                upload_progress.progress(min(1.0, max(0.0, value)), text=label)

            try:
                response = upload_video(
                    video_path=youtube_video_path,
                    title=edited_title,
                    description=edited_description,
                    tags=_tags_from_text(edited_tags_text),
                    thumbnail_path=thumbnail_path,
                    privacy_status="private" if youtube_publish_at else privacy,
                    publish_at=youtube_publish_at,
                    category_id="10",
                    made_for_kids=made_for_kids,
                    progress_callback=on_progress,
                )
                if youtube_publish_at:
                    st.success(f"Agendado no YouTube para {youtube_publish_at}: {response['url']}")
                else:
                    st.success(f"Publicado/enviado com sucesso: {response['url']}")
                if response.get("thumbnail_error"):
                    st.warning("Vídeo enviado, mas a thumbnail não foi definida automaticamente: " + response["thumbnail_error"])
                if pinned_comment.strip():
                    st.info("Comentário fixado sugerido salvo acima. Fixar comentário automaticamente exige permissões/fluxo extra; copie e cole após publicar.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Erro ao enviar para o YouTube: {exc}")

    elif selected_section == "YouTube Shorts" and has_vertical:
        vertical_video_path = Path(result["shorts_tiktok_video"])
        shorts_title = st.text_input("Título final do Shorts", value=meta.get("youtube_shorts_title", "")[:100], key="shorts_title")
        shorts_description = st.text_area("Descrição final do Shorts", value=meta.get("youtube_shorts_description", ""), height=220, key="shorts_desc")
        shorts_tags_text = st.text_area(
            "Tags do Shorts, separadas por vírgula",
            value=", ".join(meta.get("youtube_shorts_tags", [])),
            height=100,
            key="shorts_tags",
        )
        st.caption("Shorts usa upload normal do YouTube com vídeo vertical 9:16 e metadados contendo #Shorts.")

        shorts_publish_at = target_publish_at
        shorts_button_label = (
            "📅 Enviar agora e agendar liberação do Shorts"
            if shorts_publish_at
            else "📲 Enviar agora para YouTube Shorts"
        )
        if shorts_publish_at:
            st.caption(
                f"Usando o horário informado no início: `{shorts_publish_at}`. "
                "O Shorts será enviado como privado com `status.publishAt`."
            )
        if st.button(shorts_button_label, key="post_shorts_submit"):
            upload_progress = st.progress(0, text="Iniciando upload do Shorts...")

            def on_progress(value: float, label: str) -> None:
                upload_progress.progress(min(1.0, max(0.0, value)), text=label)

            try:
                response = upload_video(
                    video_path=vertical_video_path,
                    title=shorts_title,
                    description=shorts_description,
                    tags=_tags_from_text(shorts_tags_text),
                    thumbnail_path=None,
                    privacy_status="private" if shorts_publish_at else privacy,
                    publish_at=shorts_publish_at,
                    category_id="10",
                    made_for_kids=made_for_kids,
                    progress_callback=on_progress,
                )
                if shorts_publish_at:
                    st.success(f"Shorts agendado no YouTube para {shorts_publish_at}: {response['url']}")
                else:
                    st.success(f"Shorts enviado com sucesso: {response['url']}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Erro ao enviar Shorts: {exc}")

    elif selected_section == "TikTok" and has_vertical:
        vertical_video_path = Path(result["shorts_tiktok_video"])
        st.warning(
            "A API oficial do TikTok usa User Access Token OAuth. "
            "Nesta versão, a seção TikTok usa somente Inbox/Draft, então o token precisa do escopo `video.upload`. "
            "Client Key sozinho não envia vídeo; rode `python tiktok_oauth_setup.py` para gerar/salvar o token."
        )
        st.caption(
            "Token automático: se este campo ficar vazio, o app usa `TIKTOK_ACCESS_TOKEN` do `.env` "
            f"ou o arquivo `{_display_path(TIKTOK_TOKEN_FILE)}`."
        )
        tiktok_access_token = st.text_input(
            "TikTok User Access Token opcional para substituir o token salvo",
            value="",
            type="password",
            help="Normalmente deixe vazio. Use este campo só para testar outro User Access Token temporariamente.",
            key="tiktok_token_override",
        )
        if st.button("Consultar opções da conta TikTok", key="tiktok_creator_info"):
            try:
                info = query_creator_info(access_token=tiktok_access_token or None)
                st.json(info)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Erro ao consultar conta TikTok: {exc}")

        tiktok_caption = st.text_area("Caption TikTok gerado pela IA", value=meta.get("tiktok_caption", ""), height=180, key="tiktok_caption")
        privacy_options = ["SELF_ONLY", "FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS", "PUBLIC_TO_EVERYONE"]
        default_privacy = meta.get("tiktok_privacy_level", TIKTOK_DEFAULT_PRIVACY_LEVEL)
        if default_privacy not in privacy_options:
            default_privacy = "SELF_ONLY"
        tiktok_privacy = st.selectbox("Privacidade TikTok", privacy_options, index=privacy_options.index(default_privacy), key="tiktok_privacy")
        col_t1, col_t2, col_t3 = st.columns(3)
        with col_t1:
            disable_duet = st.checkbox("Desativar Duet", value=bool(meta.get("tiktok_disable_duet", False)), key="tiktok_disable_duet")
        with col_t2:
            disable_stitch = st.checkbox("Desativar Stitch", value=bool(meta.get("tiktok_disable_stitch", False)), key="tiktok_disable_stitch")
        with col_t3:
            disable_comment = st.checkbox("Desativar comentários", value=bool(meta.get("tiktok_disable_comment", False)), key="tiktok_disable_comment")
        col_t4, col_t5, col_t6 = st.columns(3)
        with col_t4:
            cover_ts = st.number_input("Frame de capa TikTok (ms)", value=int(meta.get("tiktok_video_cover_timestamp_ms", 1000)), min_value=0, step=500, key="tiktok_cover_ts")
        with col_t5:
            brand_content = st.checkbox("Parceria paga", value=bool(meta.get("tiktok_brand_content_toggle", False)), key="tiktok_brand_content")
        with col_t6:
            brand_organic = st.checkbox("Promove negócio próprio", value=bool(meta.get("tiktok_brand_organic_toggle", False)), key="tiktok_brand_organic")
        is_aigc = st.checkbox("Marcar como conteúdo gerado por IA no TikTok", value=bool(meta.get("tiktok_is_aigc", False)), key="tiktok_is_aigc")

        tiktok_publish_at = target_publish_at
        st.markdown("**Modo TikTok ativo: Inbox/Draft**")
        st.caption(
            "Removi o Direct Post/agendamento automático desta tela. Agora o app usa somente o envio oficial para "
            "Inbox/Draft do TikTok (`video.upload`), para você finalizar ou agendar dentro do fluxo nativo do TikTok."
        )
        st.warning(
            "Importante: a API oficial de Inbox/Draft do TikTok não aceita caption/descrição no payload de upload. "
            "Por isso o TikTok pode exibir uma hashtag padrão do app, como `#tiktokuploadtest`, na tela inicial. "
            "O app não envia essa hashtag. A legenda correta fica pronta abaixo para copiar/colar ao concluir o post no TikTok."
        )
        caption_file_name = f"caption_tiktok_{result.get('job_id', 'video')}.txt"
        st.download_button(
            "⬇️ Baixar legenda TikTok pronta",
            data=tiktok_caption.encode("utf-8"),
            file_name=caption_file_name,
            mime="text/plain",
            key="download_tiktok_caption_txt",
        )
        with st.expander("Ver legenda pronta para copiar", expanded=True):
            st.code(tiktok_caption, language="text")
            st.caption(f"{len(tiktok_caption)} caracteres. Use esta legenda no TikTok ao abrir a notificação da Inbox/Draft.")

        def _build_tiktok_history_item() -> dict[str, object]:
            return {
                "job_id": result.get("job_id", ""),
                "platform": "tiktok",
                "scheduled_at": _now_local().isoformat(timespec="seconds"),
                "video_path": str(vertical_video_path),
                "caption": tiktok_caption,
                "privacy_level": tiktok_privacy,
                "disable_duet": bool(disable_duet),
                "disable_stitch": bool(disable_stitch),
                "disable_comment": bool(disable_comment),
                "video_cover_timestamp_ms": int(cover_ts),
                "brand_content_toggle": bool(brand_content),
                "brand_organic_toggle": bool(brand_organic),
                "is_aigc": bool(is_aigc),
                "tiktok_mode": "inbox_upload",
                "song_title": result.get("song_title", ""),
                "artist_name": result.get("artist_name", ""),
                "caption_delivery": "manual_copy_required_by_tiktok_inbox_api",
            }

        def _record_manual_tiktok_submission(response: dict[str, object]) -> None:
            try:
                item = _build_tiktok_history_item()
                item["job_id"] = f"{result.get('job_id', 'job')}-manual-inbox-{_now_local().strftime('%H%M%S')}"
                record_tiktok_submission(SCHEDULE_FILE, item, response)
            except Exception as history_exc:  # noqa: BLE001
                st.warning(f"Envio aceito, mas não consegui registrar no painel de histórico: {history_exc}")

        if tiktok_publish_at:
            st.info(
                f"Horário selecionado como referência: `{tiktok_publish_at}`. "
                "No modo Inbox/Draft, o TikTok não agenda pela API: ele envia a notificação para você abrir no TikTok, "
                "colar/conferir a legenda e escolher o agendamento nativo por lá."
            )

        button_label = "📨 Enviar para TikTok Inbox/Draft"
        if tiktok_publish_at:
            button_label = "📨 Enviar para Inbox/Draft e agendar no TikTok"
        if st.button(button_label, key="send_tiktok_inbox_only"):
            upload_progress = st.progress(0, text="Enviando para TikTok Inbox/Draft...")

            def on_progress(value: float, label: str) -> None:
                upload_progress.progress(min(1.0, max(0.0, value)), text=label)

            try:
                response = upload_video_to_tiktok_inbox(
                    video_path=vertical_video_path,
                    access_token=tiktok_access_token or None,
                    caption=tiktok_caption,
                    progress_callback=on_progress,
                )
                publish_id = _save_last_tiktok_publish(response)
                _record_manual_tiktok_submission(response)
                sync_tiktok_publish_statuses(SCHEDULE_FILE)
                st.success(f"Vídeo enviado para Inbox/Draft do TikTok. publish_id: {publish_id or response.get('publish_id')}")
                st.info(
                    "Abra a notificação/Caixa de entrada do TikTok para concluir. A legenda correta está acima para copiar; "
                    "se o TikTok mostrar `#tiktokuploadtest`, substitua pela legenda gerada pela IA."
                )
                if publish_id:
                    try:
                        _fetch_and_render_tiktok_status(publish_id, access_token=tiktok_access_token or None)
                    except Exception as status_exc:  # noqa: BLE001
                        st.warning(f"Upload aceito, mas não consegui consultar o status agora: {status_exc}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Erro no TikTok Inbox/Draft: {exc}")
        st.caption("Acompanhe fila, logs e erros na Central TikTok exibida no topo do app.")

    elif selected_section == "Prompts de imagem":
        st.text_area("Prompt thumbnail 16:9", value=meta.get("thumbnail_prompt", ""), height=120)
        st.text_area("Prompt imagem 16:9", value=meta.get("cover_prompt_16x9", ""), height=120)
        st.text_area("Prompt imagem vertical 9:16", value=meta.get("cover_prompt_vertical", ""), height=120)


_handle_queue_ping_if_requested()
_maintenance_tick()
_require_app_access()

st.title("🎵 YouTube Music AI")
st.caption("Gera vídeo 16:9, Shorts/TikTok 9:16, metadados por IA e publicação/agendamento.")
cleanup_feedback = st.session_state.pop("cleanup_feedback", None)
if cleanup_feedback:
    st.success(cleanup_feedback)

st.info(
    "Use apenas músicas, capas, thumbnails e letras que sejam suas, licenciadas ou que você tenha autorização para publicar. "
    "Se você incluir a letra na descrição, confirme que tem direito de publicar esse texto. "
    "O app otimiza metadados, mas não garante viralização."
)

queue_error = st.session_state.pop("queue_maintenance_error", None)
if queue_error:
    st.warning(f"A manutenção automática da fila encontrou um problema: {queue_error}")

_dashboard_counts = _queue_counts()
_dashboard_open = any(_dashboard_counts.get(key, 0) for key in ("scheduled", "processing", "submitted", "error"))
_render_tiktok_queue_manager(expanded=_dashboard_open)

sidebar_settings = _load_sidebar_settings()

with st.sidebar:
    st.header("Configuração")
    st.markdown(
        "1. Crie `.env` a partir de `.env.example`.\n"
        "2. Preencha `GROQ_API_KEY`.\n"
        "3. Para YouTube, salve `credentials/client_secret.json`.\n"
        "4. Para TikTok, rode `python tiktok_oauth_setup.py` ou preencha `TIKTOK_ACCESS_TOKEN` com um User Access Token autorizado."
    )
    effect = st.selectbox(
        "Efeito do vídeo",
        EFFECT_OPTIONS,
        index=EFFECT_OPTIONS.index(str(sidebar_settings["effect"])),
        help="A imagem preenche o quadro inteiro, tanto em 16:9 quanto em 9:16.",
        key="sidebar_effect",
    )
    privacy = st.selectbox(
        "Privacidade padrão no YouTube para envio imediato",
        YOUTUBE_PRIVACY_OPTIONS,
        index=YOUTUBE_PRIVACY_OPTIONS.index(str(sidebar_settings["privacy"])),
        key="sidebar_privacy",
    )
    made_for_kids = st.checkbox(
        "Conteúdo feito para crianças",
        value=bool(sidebar_settings["made_for_kids"]),
        key="sidebar_made_for_kids",
    )
    _save_sidebar_settings({"effect": effect, "privacy": privacy, "made_for_kids": made_for_kids})
    st.caption(
        f"Shorts/TikTok vertical será limitado a {_format_duration(SHORTS_MAX_DURATION_SECONDS)} "
        "para ficar abaixo do teto de Shorts e evitar erro por arredondamento de duração."
    )
    st.caption(
        "Agendamento: YouTube/Shorts usam `publishAt`. TikTok usa Inbox/Draft; o agendamento final é escolhido dentro do TikTok."
    )

uploader_reset_nonce = int(st.session_state.get("uploader_reset_nonce", 0))

st.subheader("1. Envie os arquivos")
col_a, col_b, col_c, col_d = st.columns(4)
with col_a:
    audio_file = st.file_uploader("Música/áudio", type=sorted([x.strip(".") for x in SUPPORTED_AUDIO]), key=f"audio_file_{uploader_reset_nonce}")
with col_b:
    cover_16x9_file = st.file_uploader(
        "Imagem 16:9 para YouTube",
        type=sorted([x.strip(".") for x in SUPPORTED_IMAGES]),
        help="Se preencher só este campo, o app gera apenas o vídeo normal do YouTube.",
        key=f"cover_16x9_file_{uploader_reset_nonce}",
    )
with col_c:
    cover_vertical_file = st.file_uploader(
        "Imagem vertical 9:16 para Shorts/TikTok",
        type=sorted([x.strip(".") for x in SUPPORTED_IMAGES]),
        help="Se preencher só este campo, o app gera apenas o vídeo vertical para YouTube Shorts/TikTok.",
        key=f"cover_vertical_file_{uploader_reset_nonce}",
    )
with col_d:
    thumbnail_file = st.file_uploader("Thumbnail YouTube opcional", type=sorted([x.strip(".") for x in SUPPORTED_IMAGES]), key=f"thumbnail_file_{uploader_reset_nonce}")

st.caption(
    "Agora as imagens são realmente opcionais por formato: 16:9 gera YouTube normal; 9:16 gera Shorts/TikTok; "
    "as duas juntas geram os dois vídeos. Envie pelo menos uma imagem."
)

st.subheader("2. Agendamento de publicação")
schedule_mode = st.selectbox(
    "Quando publicar/liberar nas plataformas?",
    ["Publicar imediatamente", "Agendar data e hora abaixo"],
    index=0,
    help=(
        "No YouTube/Shorts, o horário é enviado para a API como status.publishAt. "
        "No TikTok, o app usa somente Inbox/Draft: envia o vídeo para a caixa de entrada do TikTok, e você finaliza ou agenda no fluxo nativo."
    ),
    key="global_schedule_mode",
)
col_sched_date, col_sched_time = st.columns(2)
with col_sched_date:
    publish_schedule_date = st.date_input(
        "Data de publicação/liberação",
        value=_now_local().date(),
        key="global_publish_date",
    )
with col_sched_time:
    publish_schedule_time = st.time_input(
        "Hora de publicação/liberação",
        value=time(13, 0),
        key="global_publish_time",
    )

global_publish_at = None
if schedule_mode == "Agendar data e hora abaixo":
    global_publish_at = _combine_schedule_date_time(publish_schedule_date, publish_schedule_time)
    st.success(f"Agendamento selecionado: `{global_publish_at}` no fuso `{DEFAULT_TIMEZONE}`.")
    st.caption(
        "YouTube normal e YouTube Shorts serão enviados agora como privados e liberados publicamente nesse horário. "
        "Para TikTok, entre na seção TikTok após gerar o pacote e clique em agendar publicação automática via fila."
    )
    if datetime.fromisoformat(global_publish_at) <= _now_local():
        st.warning("Esse horário já passou no fuso configurado; YouTube pode publicar imediatamente e a fila TikTok processará assim que o worker rodar.")
else:
    st.caption(f"Fuso usado nos campos acima: `{DEFAULT_TIMEZONE}`. Escolha agendamento para enviar publishAt ao YouTube/Shorts.")

st.subheader("3. Dados da música")
col1, col2 = st.columns(2)
with col1:
    song_title = st.text_input("Nome da música", placeholder="Ex.: Seu Amor")
    artist_name = st.text_input("Nome do artista", placeholder="Ex.: MC/Artista", value="Rodrigo Morais")
with col2:
    mood = st.text_input("Vibe/estilo", placeholder="Ex.: trap romântico, sofrência, worship, funk consciente", value="Sertanejo")
    language = st.text_input("Idioma dos metadados", value="pt-BR")

st.caption("Regra aplicada automaticamente nos títulos: **Nome do Artista - nome música**.")

include_lyrics_in_description = st.checkbox(
    "Incluir a letra na descrição do YouTube 16:9",
    value=True,
    help="Quando marcado, o app anexa o texto informado no campo de letra ao final da descrição antes do upload.",
)

lyrics_txt = st.file_uploader("Arquivo .txt com a letra opcional", type=["txt"], key=f"lyrics_txt_{uploader_reset_nonce}")
lyrics_text = st.text_area(
    "Letra ou resumo da música",
    height=220,
    placeholder="Cole a letra completa. Se a opção acima estiver marcada, esse texto entra no final da descrição do YouTube.",
)
extra_context = st.text_area(
    "Contexto extra opcional",
    height=90,
    placeholder="Ex.: lançamento oficial, links, créditos, estilo do canal, público alvo...",
)

if lyrics_txt is not None and not lyrics_text.strip():
    try:
        lyrics_text = lyrics_txt.read().decode("utf-8", errors="ignore")
        st.success("Letra carregada do arquivo .txt.")
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Não consegui ler o arquivo de letra: {exc}")

st.subheader("4. Gerar vídeos e metadados")

if "result" not in st.session_state:
    st.session_state.result = None

if st.button("🚀 Gerar pacote conforme imagens enviadas", type="primary", disabled=audio_file is None):
    if audio_file is None:
        st.error("Envie um arquivo de áudio primeiro.")
    elif cover_16x9_file is None and cover_vertical_file is None:
        st.error("Envie pelo menos uma imagem: 16:9 para YouTube ou 9:16 para Shorts/TikTok.")
    else:
        generate_youtube = cover_16x9_file is not None
        generate_vertical = cover_vertical_file is not None

        title_for_job = song_title or Path(audio_file.name).stem
        job_id = new_job_id(title_for_job)
        job_dir = OUTPUTS_DIR / job_id
        assets_dir = job_dir / "assets"
        ensure_dirs(job_dir, assets_dir)

        progress = st.progress(0, text="Preparando arquivos...")
        audio_ext = Path(audio_file.name).suffix.lower() or ".mp3"
        audio_path = save_uploaded_file(audio_file, assets_dir / f"audio{audio_ext}")
        audio_duration_seconds = media_duration_seconds(audio_path)
        vertical_will_be_trimmed = generate_vertical and audio_duration_seconds > SHORTS_MAX_DURATION_SECONDS
        if vertical_will_be_trimmed:
            st.warning(
                "A música enviada tem "
                f"{_format_duration(audio_duration_seconds)}. O vídeo vertical Shorts/TikTok será cortado em "
                f"{_format_duration(SHORTS_MAX_DURATION_SECONDS)} para manter o arquivo dentro do formato Shorts. "
                "O vídeo 16:9 do YouTube continua com a música completa."
            )

        progress.progress(0.10, text="Gerando metadados por IA para os formatos selecionados...")
        metadata = generate_metadata(
            song_title=title_for_job,
            artist_name=artist_name,
            lyrics=lyrics_text,
            mood=mood,
            language=language,
            extra_context=extra_context,
        )
        metadata.youtube_title = ensure_required_title(metadata.youtube_title, title_for_job, artist_name, suffix="| Música Oficial")
        metadata.youtube_shorts_title = ensure_required_title(metadata.youtube_shorts_title, title_for_job, artist_name, suffix="#Shorts")
        if include_lyrics_in_description:
            metadata.youtube_description = append_lyrics_to_description(metadata.youtube_description, lyrics_text)
        metadata.privacy_status = privacy

        cover_16x9_path: Path | None = None
        thumbnail_path: Path | None = None
        youtube_video_path: Path | None = None
        cover_vertical_path: Path | None = None
        vertical_preview_path: Path | None = None
        vertical_video_path: Path | None = None

        if generate_youtube:
            progress.progress(0.25, text="Preparando imagem 16:9...")
            cover_16x9_path = assets_dir / "cover_16x9.jpg"
            raw_cover = save_uploaded_file(
                cover_16x9_file,
                assets_dir / f"uploaded_cover_16x9{Path(cover_16x9_file.name).suffix.lower()}",
            )
            optimize_cover(raw_cover, cover_16x9_path, width=1920, height=1080)

            progress.progress(0.38, text="Preparando thumbnail 16:9...")
            thumbnail_path = assets_dir / "thumbnail.jpg"
            if thumbnail_file is not None:
                raw_thumbnail = save_uploaded_file(
                    thumbnail_file,
                    assets_dir / f"uploaded_thumbnail{Path(thumbnail_file.name).suffix.lower()}",
                )
                optimize_thumbnail(raw_thumbnail, thumbnail_path)
            else:
                create_local_thumbnail(cover_16x9_path, thumbnail_path, title_for_job, artist_name)

            progress.progress(0.55 if generate_vertical else 0.65, text="Renderizando vídeo 16:9...")
            youtube_video_path = job_dir / "youtube_video_16x9.mp4"
            render_youtube_video(audio_path, cover_16x9_path, youtube_video_path, effect=effect)

        if generate_vertical:
            progress.progress(0.55 if generate_youtube else 0.30, text="Preparando imagem vertical 9:16...")
            cover_vertical_path = assets_dir / "cover_vertical_9x16.jpg"
            raw_vertical = save_uploaded_file(
                cover_vertical_file,
                assets_dir / f"uploaded_cover_9x16{Path(cover_vertical_file.name).suffix.lower()}",
            )
            optimize_vertical_cover(raw_vertical, cover_vertical_path, width=SHORTS_VIDEO_WIDTH, height=SHORTS_VIDEO_HEIGHT)

            vertical_preview_path = assets_dir / "shorts_tiktok_preview.jpg"
            create_local_thumbnail(
                cover_vertical_path,
                vertical_preview_path,
                title_for_job,
                artist_name,
                width=SHORTS_VIDEO_WIDTH,
                height=SHORTS_VIDEO_HEIGHT,
            )

            progress_text = "Renderizando vídeo vertical 9:16 para Shorts/TikTok..."
            if vertical_will_be_trimmed:
                progress_text = (
                    "Renderizando vídeo vertical 9:16 para Shorts/TikTok "
                    f"com corte em {_format_duration(SHORTS_MAX_DURATION_SECONDS)}..."
                )
            progress.progress(0.78, text=progress_text)
            vertical_video_path = job_dir / "shorts_tiktok_video_9x16.mp4"
            render_vertical_video(
                audio_path,
                cover_vertical_path,
                vertical_video_path,
                effect=effect,
                width=SHORTS_VIDEO_WIDTH,
                height=SHORTS_VIDEO_HEIGHT,
                duration_limit_seconds=SHORTS_MAX_DURATION_SECONDS,
            )

        progress.progress(0.94, text="Salvando manifesto...")
        manifest = {
            "job_id": job_id,
            "audio": str(audio_path),
            "cover_16x9": str(cover_16x9_path) if cover_16x9_path else "",
            "cover_vertical_9x16": str(cover_vertical_path) if cover_vertical_path else "",
            "thumbnail": str(thumbnail_path) if thumbnail_path else "",
            "vertical_preview": str(vertical_preview_path) if vertical_preview_path else "",
            "youtube_video": str(youtube_video_path) if youtube_video_path else "",
            "shorts_tiktok_video": str(vertical_video_path) if vertical_video_path else "",
            "generated_formats": {
                "youtube_16x9": bool(youtube_video_path),
                "shorts_tiktok_9x16": bool(vertical_video_path),
            },
            "audio_duration_seconds": round(audio_duration_seconds, 3),
            "shorts_max_duration_seconds": SHORTS_MAX_DURATION_SECONDS,
            "shorts_tiktok_was_trimmed": bool(vertical_will_be_trimmed),
            "metadata": metadata.to_dict(),
            "include_lyrics_in_description": include_lyrics_in_description,
            "target_publish_at": global_publish_at or "",
            "publish_schedule_enabled": bool(global_publish_at),
        }
        manifest_path = job_dir / "manifest.json"
        write_json(manifest_path, manifest)

        st.session_state.result = {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "youtube_video": str(youtube_video_path) if youtube_video_path else "",
            "shorts_tiktok_video": str(vertical_video_path) if vertical_video_path else "",
            "cover_16x9": str(cover_16x9_path) if cover_16x9_path else "",
            "cover_vertical_9x16": str(cover_vertical_path) if cover_vertical_path else "",
            "thumbnail": str(thumbnail_path) if thumbnail_path else "",
            "vertical_preview": str(vertical_preview_path) if vertical_preview_path else "",
            "manifest": str(manifest_path),
            "metadata": metadata.to_dict(),
            "audio_duration_seconds": round(audio_duration_seconds, 3),
            "shorts_max_duration_seconds": SHORTS_MAX_DURATION_SECONDS,
            "shorts_tiktok_was_trimmed": bool(vertical_will_be_trimmed),
            "include_lyrics_in_description": include_lyrics_in_description,
            "target_publish_at": global_publish_at or "",
            "publish_schedule_enabled": bool(global_publish_at),
            "song_title": title_for_job,
            "artist_name": artist_name,
        }
        progress.progress(1.0, text="Concluído.")
        st.success("Pacote gerado com sucesso.")

result = st.session_state.result

if result:
    st.divider()
    st.subheader("5. Revise antes de postar ou agendar")
    meta = result["metadata"]
    manifest_path = Path(result["manifest"])
    has_youtube = bool(result.get("youtube_video")) and Path(result["youtube_video"]).exists()
    has_vertical = bool(result.get("shorts_tiktok_video")) and Path(result["shorts_tiktok_video"]).exists()
    target_publish_at = result.get("target_publish_at") or None

    if target_publish_at:
        st.info(
            f"Agendamento ativo para este pacote: `{target_publish_at}`. "
            "Ao publicar no YouTube/Shorts, o app envia esse horário diretamente como `status.publishAt`."
        )
    else:
        st.caption("Este pacote está configurado para publicação imediata. Para agendar, selecione data/hora antes de gerar o pacote.")

    if result.get("shorts_tiktok_was_trimmed"):
        st.info(
            "O vídeo vertical Shorts/TikTok foi cortado automaticamente em "
            f"{_format_duration(float(result.get('shorts_max_duration_seconds', SHORTS_MAX_DURATION_SECONDS)))}. "
            "O vídeo 16:9 do YouTube, quando gerado, continua com a duração completa da música."
        )

    left, right = st.columns(2)
    with left:
        st.markdown("**Vídeo YouTube 16:9**")
        if has_youtube:
            youtube_video_path = Path(result["youtube_video"])
            thumbnail_path = Path(result["thumbnail"])
            st.video(str(youtube_video_path))
            st.download_button("⬇️ Baixar vídeo 16:9 MP4", youtube_video_path.read_bytes(), file_name="youtube_video_16x9.mp4")
            if thumbnail_path.exists():
                st.image(str(thumbnail_path), caption="Thumbnail 16:9", use_container_width=True)
                st.download_button("⬇️ Baixar thumbnail 16:9 JPG", thumbnail_path.read_bytes(), file_name="thumbnail_16x9.jpg")
        else:
            st.info("Formato 16:9 não foi gerado porque a imagem 16:9 não foi enviada.")
    with right:
        st.markdown("**Vídeo Shorts/TikTok 9:16**")
        if has_vertical:
            vertical_video_path = Path(result["shorts_tiktok_video"])
            vertical_preview_path = Path(result["vertical_preview"])
            st.video(str(vertical_video_path))
            st.download_button("⬇️ Baixar vídeo 9:16 MP4", vertical_video_path.read_bytes(), file_name="shorts_tiktok_video_9x16.mp4")
            if vertical_preview_path.exists():
                st.image(str(vertical_preview_path), caption="Imagem vertical 9:16", use_container_width=True)
                st.download_button("⬇️ Baixar imagem vertical JPG", vertical_preview_path.read_bytes(), file_name="shorts_tiktok_9x16.jpg")
        else:
            st.info("Formato 9:16 não foi gerado porque a imagem vertical não foi enviada.")

    st.download_button("⬇️ Baixar manifest JSON", manifest_path.read_bytes(), file_name="manifest.json")

    _render_review_sections(
        result=result,
        has_youtube=has_youtube,
        has_vertical=has_vertical,
        target_publish_at=target_publish_at,
        privacy=privacy,
        made_for_kids=made_for_kids,
    )
    _render_cleanup_current_package(result)
else:
    st.caption("Depois de gerar, os vídeos/imagens/metadados e opções de publicação aparecerão aqui para revisão.")
