from __future__ import annotations

import hmac
import os
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
    load_queue,
    process_due_posts,
    retry_scheduled_post,
    start_tiktok_queue_worker,
    worker_is_running,
)
from core.tiktok_upload import (
    fetch_tiktok_publish_status,
    query_creator_info,
    upload_video_to_tiktok_direct,
    upload_video_to_tiktok_inbox,
)
from core.utils import ensure_dirs, media_duration_seconds, new_job_id, read_json, save_uploaded_file, write_json
from core.youtube_upload import upload_video

ensure_dirs(UPLOADS_DIR, OUTPUTS_DIR)

st.set_page_config(page_title="YouTube Music AI", page_icon="🎵", layout="wide")

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


def _queue_item_label(item: dict[str, object]) -> str:
    scheduled = _safe_str(item.get("scheduled_at"), "sem data")
    status = _safe_str(item.get("status"), "?")
    caption = _safe_str(item.get("caption"), "").replace("\n", " ")[:60]
    schedule_id = _safe_str(item.get("schedule_id"))
    return f"{scheduled} | {status} | {caption or item.get('job_id')} | {schedule_id}"


def _queue_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in load_queue(SCHEDULE_FILE):
        if item.get("platform") != "tiktok":
            continue
        rows.append(
            {
                "id": item.get("schedule_id", ""),
                "status": item.get("status", ""),
                "agendado_para": item.get("scheduled_at", ""),
                "modo": item.get("tiktok_mode", "direct_post"),
                "privacidade": item.get("privacy_level", ""),
                "tentativas": item.get("attempts", 0),
                "publish_id": item.get("publish_id", ""),
                "erro": item.get("error", ""),
                "caption": _safe_str(item.get("caption"), "")[:120],
            }
        )
    rows.sort(key=lambda row: str(row.get("agendado_para") or ""), reverse=False)
    return rows


def _render_tiktok_queue_manager(expanded: bool = False) -> None:
    with st.expander("Fila TikTok automática", expanded=expanded):
        if TIKTOK_SCHEDULER_ENABLED:
            running_text = "rodando" if worker_is_running() else "parado"
            st.caption(
                f"Worker local: `{running_text}` | checagem a cada `{TIKTOK_QUEUE_POLL_SECONDS}s` | arquivo: `{_display_path(SCHEDULE_FILE)}`"
            )
            if not worker_is_running():
                if st.button("▶️ Iniciar worker da fila", key="start_tiktok_worker"):
                    start_tiktok_queue_worker(SCHEDULE_FILE)
                    st.rerun()
        else:
            st.warning("TIKTOK_SCHEDULER_ENABLED=false. A fila fica salva, mas o worker automático não roda.")

        rows = _queue_rows()
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum agendamento TikTok na fila.")

        col_q1, col_q2 = st.columns(2)
        with col_q1:
            if st.button("⚙️ Processar vencidos agora", key="process_tiktok_due_now"):
                try:
                    results = process_due_posts(SCHEDULE_FILE)
                    if not results:
                        st.info("Não há itens vencidos para processar agora.")
                    for result in results:
                        if result.status == "posted":
                            st.success(result.message)
                            if result.response and result.response.get("publish_id"):
                                st.session_state["last_tiktok_publish_id"] = result.response["publish_id"]
                        else:
                            st.error(result.message)
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Erro ao processar fila: {exc}")
        with col_q2:
            st.caption("A fila publica via Direct Post quando o app/worker estiver rodando.")

        actionable = [item for item in load_queue(SCHEDULE_FILE) if item.get("platform") == "tiktok" and item.get("status") in {"scheduled", "error", "processing"}]
        if actionable:
            labels = {_queue_item_label(item): str(item.get("schedule_id")) for item in actionable}
            selected_label = st.selectbox("Gerenciar item da fila", list(labels), key="manage_tiktok_queue_item")
            selected_id = labels[selected_label]
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                if st.button("Cancelar item selecionado", key="cancel_tiktok_queue_item"):
                    if cancel_scheduled_post(SCHEDULE_FILE, selected_id):
                        st.success("Agendamento cancelado.")
                        st.rerun()
                    st.warning("Não encontrei item cancelável com esse ID.")
            with col_m2:
                selected_item = next((item for item in actionable if str(item.get("schedule_id")) == selected_id), {})
                if selected_item.get("status") == "error" and st.button("Tentar novamente", key="retry_tiktok_queue_item"):
                    if retry_scheduled_post(SCHEDULE_FILE, selected_id):
                        st.success("Item voltou para a fila.")
                        st.rerun()
                    st.warning("Não encontrei item com erro para reprocessar.")


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
            "Para publicar direto, o token precisa de `video.publish`; para enviar para Inbox/Draft, precisa de `video.upload`. "
            "Client Key sozinho não publica; rode `python tiktok_oauth_setup.py` para gerar/salvar o token."
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
        tiktok_mode = st.selectbox("Modo de envio TikTok", ["direct_post", "inbox_upload"], index=0, key="tiktok_mode")
        st.caption(
            "`direct_post` publica direto usando `video.publish`. `inbox_upload` envia para Inbox/Draft usando `video.upload` e ainda exige confirmação manual."
        )

        def _build_tiktok_queue_item() -> dict[str, object]:
            # Por segurança, não gravamos o token digitado na tela na fila. Para agendamento,
            # deixe o token persistido no .env ou em credentials/.tiktok_token.json.
            return {
                "job_id": result.get("job_id", ""),
                "platform": "tiktok",
                "scheduled_at": tiktok_publish_at,
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
                "tiktok_mode": "direct_post",
                "song_title": result.get("song_title", ""),
                "artist_name": result.get("artist_name", ""),
            }

        if tiktok_publish_at:
            st.info(
                f"Agendamento TikTok selecionado: `{tiktok_publish_at}`. "
                "A opção automática abaixo salva este vídeo em uma fila local e publica via Direct Post quando chegar o horário."
            )
            if tiktok_access_token.strip():
                st.warning(
                    "O token digitado na tela não será salvo na fila por segurança. "
                    "Para o agendamento automático funcionar, salve o token no `.env` ou em `credentials/.tiktok_token.json`."
                )
            if tiktok_mode != "direct_post":
                st.warning("Agendamento automático sem celular só funciona com Direct Post. Inbox/Draft continua manual.")

            col_sched_t1, col_sched_t2 = st.columns(2)
            with col_sched_t1:
                if st.button("📅 Agendar publicação automática via fila", key="schedule_tiktok_direct_queue", disabled=tiktok_mode != "direct_post"):
                    try:
                        scheduled_item = add_or_replace_scheduled_post(SCHEDULE_FILE, _build_tiktok_queue_item())
                        if TIKTOK_SCHEDULER_ENABLED:
                            start_tiktok_queue_worker(SCHEDULE_FILE)
                        st.success(
                            "Agendamento TikTok salvo na fila. "
                            f"ID: `{scheduled_item.get('schedule_id')}` | horário: `{scheduled_item.get('scheduled_at')}`"
                        )
                        st.info(
                            "Mantenha o app rodando no Streamlit/servidor no horário agendado. "
                            "Se o servidor estiver dormindo/desligado, o item será processado quando o app voltar a rodar."
                        )
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Erro ao salvar agendamento TikTok: {exc}")
            with col_sched_t2:
                if st.button("📨 Enviar para Inbox/Draft agora", key="send_tiktok_inbox_for_native_schedule"):
                    upload_progress = st.progress(0, text="Enviando para TikTok Inbox/Draft...")

                    def on_progress(value: float, label: str) -> None:
                        upload_progress.progress(min(1.0, max(0.0, value)), text=label)

                    try:
                        response = upload_video_to_tiktok_inbox(
                            video_path=vertical_video_path,
                            access_token=tiktok_access_token or None,
                            progress_callback=on_progress,
                        )
                        publish_id = _save_last_tiktok_publish(response)
                        st.success(f"Vídeo enviado para Inbox/Draft do TikTok. publish_id: {publish_id or response.get('publish_id')}")
                        st.info("Inbox/Draft ainda exige abrir a notificação do TikTok e concluir pelo fluxo nativo.")
                        if publish_id:
                            try:
                                _fetch_and_render_tiktok_status(publish_id, access_token=tiktok_access_token or None)
                            except Exception as status_exc:  # noqa: BLE001
                                st.warning(f"Upload aceito, mas não consegui consultar o status agora: {status_exc}")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Erro no TikTok Inbox/Draft: {exc}")
        else:
            if st.button("🎵 Postar/enviar agora para TikTok", key="post_tiktok_now"):
                upload_progress = st.progress(0, text="Iniciando TikTok...")

                def on_progress(value: float, label: str) -> None:
                    upload_progress.progress(min(1.0, max(0.0, value)), text=label)

                try:
                    if tiktok_mode == "inbox_upload":
                        response = upload_video_to_tiktok_inbox(
                            video_path=vertical_video_path,
                            access_token=tiktok_access_token or None,
                            progress_callback=on_progress,
                        )
                        publish_id = _save_last_tiktok_publish(response)
                        st.success(f"Vídeo enviado para Inbox/Draft do TikTok. publish_id: {publish_id or response.get('publish_id')}")
                        st.info("No modo Inbox/Draft, finalize a publicação dentro do app/site do TikTok.")
                    else:
                        response = upload_video_to_tiktok_direct(
                            video_path=vertical_video_path,
                            caption=tiktok_caption,
                            privacy_level=tiktok_privacy,
                            disable_duet=disable_duet,
                            disable_stitch=disable_stitch,
                            disable_comment=disable_comment,
                            video_cover_timestamp_ms=int(cover_ts),
                            brand_content_toggle=brand_content,
                            brand_organic_toggle=brand_organic,
                            is_aigc=is_aigc,
                            access_token=tiktok_access_token or None,
                            progress_callback=on_progress,
                        )
                        publish_id = _save_last_tiktok_publish(response)
                        st.success(f"TikTok recebeu a postagem. publish_id: {publish_id or response.get('publish_id')}")
                    if publish_id:
                        try:
                            _fetch_and_render_tiktok_status(publish_id, access_token=tiktok_access_token or None)
                        except Exception as status_exc:  # noqa: BLE001
                            st.warning(f"Envio aceito, mas não consegui consultar o status agora: {status_exc}")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Erro no TikTok: {exc}")

        _render_tiktok_status_controls(access_token=tiktok_access_token or None)
        _render_tiktok_queue_manager(expanded=bool(tiktok_publish_at))

    elif selected_section == "Prompts de imagem":
        st.text_area("Prompt thumbnail 16:9", value=meta.get("thumbnail_prompt", ""), height=120)
        st.text_area("Prompt imagem 16:9", value=meta.get("cover_prompt_16x9", ""), height=120)
        st.text_area("Prompt imagem vertical 9:16", value=meta.get("cover_prompt_vertical", ""), height=120)


_require_app_access()
if TIKTOK_SCHEDULER_ENABLED:
    start_tiktok_queue_worker(SCHEDULE_FILE)

st.title("🎵 YouTube Music AI")
st.caption("Gera vídeo 16:9, Shorts/TikTok 9:16, metadados por IA e publicação/agendamento.")

st.info(
    "Use apenas músicas, capas, thumbnails e letras que sejam suas, licenciadas ou que você tenha autorização para publicar. "
    "Se você incluir a letra na descrição, confirme que tem direito de publicar esse texto. "
    "O app otimiza metadados, mas não garante viralização."
)

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
        "Agendamento: YouTube/Shorts usam `publishAt`. TikTok usa fila local + Direct Post quando você selecionar data/hora."
    )

st.subheader("1. Envie os arquivos")
col_a, col_b, col_c, col_d = st.columns(4)
with col_a:
    audio_file = st.file_uploader("Música/áudio", type=sorted([x.strip(".") for x in SUPPORTED_AUDIO]))
with col_b:
    cover_16x9_file = st.file_uploader(
        "Imagem 16:9 para YouTube",
        type=sorted([x.strip(".") for x in SUPPORTED_IMAGES]),
        help="Se preencher só este campo, o app gera apenas o vídeo normal do YouTube.",
    )
with col_c:
    cover_vertical_file = st.file_uploader(
        "Imagem vertical 9:16 para Shorts/TikTok",
        type=sorted([x.strip(".") for x in SUPPORTED_IMAGES]),
        help="Se preencher só este campo, o app gera apenas o vídeo vertical para YouTube Shorts/TikTok.",
    )
with col_d:
    thumbnail_file = st.file_uploader("Thumbnail YouTube opcional", type=sorted([x.strip(".") for x in SUPPORTED_IMAGES]))

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
        "No TikTok, o app salva uma fila local e publica via Direct Post quando chegar o horário."
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

lyrics_txt = st.file_uploader("Arquivo .txt com a letra opcional", type=["txt"])
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
else:
    st.caption("Depois de gerar, os vídeos/imagens/metadados e opções de publicação aparecerão aqui para revisão.")
