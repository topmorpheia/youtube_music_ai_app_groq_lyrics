from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .config import (
    CREDENTIALS_DIR,
    ROOT_DIR,
    TIKTOK_ACCESS_TOKEN,
    TIKTOK_CLIENT_KEY,
    TIKTOK_CLIENT_SECRET,
    TIKTOK_REFRESH_TOKEN,
    TIKTOK_TOKEN_FILE,
)

TIKTOK_API_BASE = "https://open.tiktokapis.com"
DIRECT_POST_ENDPOINT = f"{TIKTOK_API_BASE}/v2/post/publish/video/init/"
INBOX_UPLOAD_ENDPOINT = f"{TIKTOK_API_BASE}/v2/post/publish/inbox/video/init/"
CREATOR_INFO_ENDPOINT = f"{TIKTOK_API_BASE}/v2/post/publish/creator_info/query/"
STATUS_FETCH_ENDPOINT = f"{TIKTOK_API_BASE}/v2/post/publish/status/fetch/"
TOKEN_ENDPOINT = f"{TIKTOK_API_BASE}/v2/oauth/token/"

MIN_CHUNK_SIZE = 5_000_000
MAX_CHUNK_SIZE = 64_000_000
# O exemplo funcional enviado usava chunks fixos de ~10 MB. Esse tamanho também evita
# sobras finais menores que 5 MB quando o último chunk é mesclado com o restante.
DEFAULT_CHUNK_SIZE = 10_000_000
MAX_FINAL_CHUNK_SIZE = 128_000_000
TOKEN_REFRESH_SKEW_SECONDS = 300


class TikTokUploadError(RuntimeError):
    """Erro retornado pela API do TikTok ou pelo envio do arquivo."""


@dataclass(frozen=True)
class _TokenSource:
    access_token: str
    refresh_token: str
    token_file: Path | None
    label: str
    explicit: bool = False


def _candidate_token_files() -> list[Path]:
    """Arquivos aceitos para o token TikTok, em ordem de preferência."""
    candidates = [
        Path(os.getenv("TIKTOK_TOKEN_FILE", str(TIKTOK_TOKEN_FILE))),
        CREDENTIALS_DIR / ".tiktok_token.json",
        CREDENTIALS_DIR / "tiktok_token.json",
        ROOT_DIR / ".tiktok_token.json",
    ]
    output: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = ROOT_DIR / candidate
        resolved = str(candidate.resolve())
        if resolved not in seen:
            seen.add(resolved)
            output.append(candidate)
    return output


def _read_token_file(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _parse_timestamp(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            return int(cleaned)
        try:
            return int(datetime.fromisoformat(cleaned.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _expiry_timestamp(token_data: dict[str, Any]) -> int | None:
    expires_at = _parse_timestamp(token_data.get("expires_at") or token_data.get("access_token_expires_at"))
    if expires_at:
        return expires_at

    issued_at = _parse_timestamp(
        token_data.get("created_at")
        or token_data.get("saved_at")
        or token_data.get("updated_at")
        or token_data.get("issued_at")
    )
    expires_in = _parse_timestamp(token_data.get("expires_in"))
    if issued_at and expires_in:
        return issued_at + expires_in
    return None


def _token_needs_refresh(token_data: dict[str, Any]) -> bool:
    expires_at = _expiry_timestamp(token_data)
    if expires_at is None:
        # Tokens antigos gerados sem timestamp continuam utilizáveis; se a API negar,
        # tentamos refresh automaticamente no retry de autenticação.
        return False
    return expires_at <= int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS


def _decorate_token_data(token_data: dict[str, Any], previous_data: dict[str, Any] | None = None) -> dict[str, Any]:
    previous_data = previous_data or {}
    now = int(time.time())
    decorated = {**previous_data, **token_data}
    decorated["saved_at"] = now
    decorated["saved_at_iso"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    expires_in = _parse_timestamp(decorated.get("expires_in"))
    if expires_in:
        decorated["expires_at"] = now + expires_in
        decorated["expires_at_iso"] = datetime.fromtimestamp(now + expires_in, tz=timezone.utc).isoformat()

    refresh_expires_in = _parse_timestamp(decorated.get("refresh_expires_in"))
    if refresh_expires_in:
        decorated["refresh_expires_at"] = now + refresh_expires_in
        decorated["refresh_expires_at_iso"] = datetime.fromtimestamp(now + refresh_expires_in, tz=timezone.utc).isoformat()

    return decorated


def _write_token_file(path: Path, token_data: dict[str, Any], previous_data: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    decorated = _decorate_token_data(token_data, previous_data=previous_data)
    path.write_text(json.dumps(decorated, indent=2, ensure_ascii=False), encoding="utf-8")


def _client_key() -> str:
    return (os.getenv("TIKTOK_CLIENT_KEY") or os.getenv("TIKTOK_CLIENT_ID") or TIKTOK_CLIENT_KEY or "").strip()


def _client_secret() -> str:
    return (os.getenv("TIKTOK_CLIENT_SECRET") or TIKTOK_CLIENT_SECRET or "").strip()


def _refresh_access_token(token_file: Path | None = None, refresh_token: str | None = None) -> _TokenSource | None:
    """Renova o access token quando houver refresh_token e credenciais do app."""
    file_data: dict[str, Any] = {}
    selected_file = token_file
    if selected_file:
        file_data = _read_token_file(selected_file)
    else:
        for candidate in _candidate_token_files():
            data = _read_token_file(candidate)
            if data.get("refresh_token"):
                selected_file = candidate
                file_data = data
                break

    refresh_token = (refresh_token or file_data.get("refresh_token") or os.getenv("TIKTOK_REFRESH_TOKEN") or TIKTOK_REFRESH_TOKEN or "").strip()
    if not refresh_token:
        return None

    client_key = _client_key()
    client_secret = _client_secret()
    if not client_key or not client_secret:
        return None

    response = requests.post(
        TOKEN_ENDPOINT,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
        },
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=60,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TikTokUploadError(f"Falha ao renovar token TikTok: HTTP {response.status_code} {response.text[:500]}") from exc

    if response.status_code >= 400 or not payload.get("access_token"):
        safe_payload = {k: v for k, v in payload.items() if "token" not in k.lower()}
        raise TikTokUploadError(f"Falha ao renovar token TikTok: HTTP {response.status_code} {safe_payload}")

    if selected_file is None:
        selected_file = TIKTOK_TOKEN_FILE
    _write_token_file(selected_file, payload, previous_data=file_data)
    return _TokenSource(
        access_token=str(payload.get("access_token") or "").strip(),
        refresh_token=str(payload.get("refresh_token") or refresh_token or "").strip(),
        token_file=selected_file,
        label=f"token renovado em {selected_file}",
    )


def _resolve_token(access_token: str | None = None, *, refresh_if_needed: bool = True) -> _TokenSource:
    explicit = (access_token or "").strip()
    if explicit:
        return _TokenSource(explicit, "", None, "token informado na tela", explicit=True)

    env_token = (os.getenv("TIKTOK_ACCESS_TOKEN") or TIKTOK_ACCESS_TOKEN or "").strip()
    env_refresh = (os.getenv("TIKTOK_REFRESH_TOKEN") or TIKTOK_REFRESH_TOKEN or "").strip()
    if env_token:
        # Sem timestamp confiável no .env, usamos o access token atual e renovamos
        # automaticamente só se a API retornar erro de autenticação.
        return _TokenSource(env_token, env_refresh, None, "TIKTOK_ACCESS_TOKEN do .env")

    for token_file in _candidate_token_files():
        token_data = _read_token_file(token_file)
        file_token = str(token_data.get("access_token") or "").strip()
        file_refresh = str(token_data.get("refresh_token") or "").strip()
        if not file_token:
            continue
        if refresh_if_needed and file_refresh and _token_needs_refresh(token_data):
            refreshed = _refresh_access_token(token_file=token_file, refresh_token=file_refresh)
            if refreshed:
                return refreshed
        return _TokenSource(file_token, file_refresh, token_file, f"{token_file}")

    raise TikTokUploadError(
        "TikTok access token não configurado. O app procurou por: campo da tela, "
        "TIKTOK_ACCESS_TOKEN no .env, TIKTOK_TOKEN_FILE, credentials/.tiktok_token.json, "
        "credentials/tiktok_token.json e .tiktok_token.json. Rode `python tiktok_oauth_setup.py` "
        "para gerar um token com video.upload."
    )


def _get_access_token(access_token: str | None = None) -> str:
    return _resolve_token(access_token).access_token


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _raise_for_tiktok_error(payload: dict[str, Any]) -> None:
    error = payload.get("error") or {}
    code = str(error.get("code") or "ok")
    if code and code != "ok":
        message = error.get("message") or "Erro desconhecido retornado pelo TikTok."
        log_id = error.get("log_id") or error.get("logid") or ""
        suffix = f" | log_id={log_id}" if log_id else ""
        raise TikTokUploadError(f"TikTok API error: {code} - {message}{suffix}")


def _is_auth_error(status_code: int, payload: dict[str, Any]) -> bool:
    error = payload.get("error") or {}
    code = str(error.get("code") or "").lower()
    message = str(error.get("message") or "").lower()
    text = f"{code} {message}"
    if status_code in {401, 403} and "scope" not in text and "permission" not in text:
        return True
    return "token" in text and any(word in text for word in ("expired", "invalid", "unauthorized", "auth"))


def _post_json(url: str, access_token: str, body: dict[str, Any], *, allow_refresh: bool = True) -> dict[str, Any]:
    response = requests.post(url, headers=_headers(access_token), json=body, timeout=60)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TikTokUploadError(f"Resposta não-JSON do TikTok: HTTP {response.status_code} {response.text[:500]}") from exc

    if allow_refresh and _is_auth_error(response.status_code, payload):
        refreshed = _refresh_access_token()
        if refreshed and refreshed.access_token and refreshed.access_token != access_token:
            return _post_json(url, refreshed.access_token, body, allow_refresh=False)

    if response.status_code >= 400:
        _raise_for_tiktok_error(payload)
        raise TikTokUploadError(f"TikTok HTTP {response.status_code}: {payload}")
    _raise_for_tiktok_error(payload)
    return payload


def _calculate_chunking(video_size: int, preferred_chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[int, int]:
    """Calcula chunk_size/total_chunk_count aceitos pelo TikTok.

    A forma mais estável para vídeos comuns é o envio em 1 chunk quando o arquivo
    tem até 64 MB: chunk_size=video_size e total_chunk_count=1. Esse é o mesmo
    formato usado no exemplo funcional enviado e evita o erro `chunk size is invalid`
    que algumas contas/endpoints retornam ao tentar dividir vídeos pequenos.

    Para arquivos maiores que 64 MB, usa chunks regulares e deixa o último chunk
    absorver os bytes restantes, conforme a regra oficial do TikTok.
    """
    if video_size <= 0:
        raise TikTokUploadError("Arquivo de vídeo vazio.")

    # Caminho preferencial: TikTok aceita qualquer vídeo de até 64 MB em uma só
    # parte. Isso também cobre arquivos menores que 5 MB.
    if video_size <= MAX_CHUNK_SIZE:
        return video_size, 1

    chunk_size = max(MIN_CHUNK_SIZE, min(int(preferred_chunk_size or DEFAULT_CHUNK_SIZE), MAX_CHUNK_SIZE))

    # Pela documentação, total_chunk_count deve ser floor(video_size / chunk_size),
    # e o último chunk pode ser maior que chunk_size para carregar o restante.
    total = max(2, video_size // chunk_size)

    # Garante que o último chunk não ultrapasse 128 MB. Se ultrapassar, aumentamos
    # a quantidade de chunks, mantendo o tamanho regular dentro do limite.
    while total > 1:
        regular_bytes = (total - 1) * chunk_size
        final_chunk_size = video_size - regular_bytes
        if final_chunk_size <= MAX_FINAL_CHUNK_SIZE:
            break
        total += 1

    if total > 1000:
        chunk_size = math.ceil(video_size / 1000)
        chunk_size = max(MIN_CHUNK_SIZE, min(chunk_size, MAX_CHUNK_SIZE))
        total = max(2, video_size // chunk_size)
        if total > 1000:
            raise TikTokUploadError("Vídeo grande demais para o limite de chunks do TikTok.")

    return chunk_size, total


def _upload_file_chunks(
    upload_url: str,
    video_path: Path,
    chunk_size: int,
    total_chunk_count: int,
    progress_callback: Callable[[float, str], None] | None = None,
) -> None:
    video_size = video_path.stat().st_size
    sent = 0
    with video_path.open("rb") as fh:
        for chunk_index in range(total_chunk_count):
            is_last = chunk_index == total_chunk_count - 1
            bytes_to_read = video_size - sent if is_last else chunk_size
            data = fh.read(bytes_to_read)
            if not data:
                break
            start = sent
            end = start + len(data) - 1
            headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{video_size}",
            }
            response = requests.put(upload_url, headers=headers, data=data, timeout=300)
            if response.status_code >= 400:
                raise TikTokUploadError(
                    f"Falha ao enviar chunk {chunk_index + 1}/{total_chunk_count} para TikTok: "
                    f"HTTP {response.status_code} {response.text[:1000]}"
                )
            sent += len(data)
            if progress_callback:
                progress_callback(sent / video_size, f"Enviando vídeo para o TikTok ({chunk_index + 1}/{total_chunk_count})...")

    if sent != video_size:
        raise TikTokUploadError(f"Upload incompleto para o TikTok: enviado {sent} de {video_size} bytes.")


def fetch_tiktok_publish_status(publish_id: str, access_token: str | None = None) -> dict[str, Any]:
    """Consulta o status de um publish_id gerado pelo Inbox Upload.

    Retorna o objeto `data` da API quando disponível. Status importantes:
    PROCESSING_UPLOAD, SEND_TO_USER_INBOX, PUBLISH_COMPLETE e FAILED.
    """
    cleaned_publish_id = str(publish_id or "").strip()
    if not cleaned_publish_id:
        raise TikTokUploadError("Informe um publish_id para consultar o status no TikTok.")

    token = _get_access_token(access_token)
    payload = _post_json(STATUS_FETCH_ENDPOINT, token, {"publish_id": cleaned_publish_id})
    return payload.get("data") or payload


def query_creator_info(access_token: str | None = None) -> dict[str, Any]:
    """Consulta opções de privacidade/interações disponíveis para a conta autorizada."""
    token = _get_access_token(access_token)
    payload = _post_json(CREATOR_INFO_ENDPOINT, token, {})
    return payload.get("data") or payload


def upload_video_to_tiktok_direct(
    video_path: Path,
    caption: str,
    privacy_level: str = "SELF_ONLY",
    disable_duet: bool = False,
    disable_stitch: bool = False,
    disable_comment: bool = False,
    video_cover_timestamp_ms: int = 1000,
    brand_content_toggle: bool = False,
    brand_organic_toggle: bool = False,
    is_aigc: bool = False,
    access_token: str | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Publica diretamente no TikTok usando Content Posting API /video/init/ + FILE_UPLOAD.

    Requer token com escopo video.publish e app aprovado para Direct Post. Apps não auditados podem ficar
    restritos a publicações privadas/SELF_ONLY.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Vídeo não encontrado: {video_path}")

    token = _get_access_token(access_token)
    video_size = video_path.stat().st_size
    chunk_size, total_chunk_count = _calculate_chunking(video_size)
    post_info: dict[str, Any] = {
        "title": (caption or "")[:2200],
        "privacy_level": privacy_level or "SELF_ONLY",
        "disable_duet": bool(disable_duet),
        "disable_stitch": bool(disable_stitch),
        "disable_comment": bool(disable_comment),
        "video_cover_timestamp_ms": max(0, int(video_cover_timestamp_ms or 0)),
        "brand_content_toggle": bool(brand_content_toggle),
        "brand_organic_toggle": bool(brand_organic_toggle),
        "is_aigc": bool(is_aigc),
    }

    body = {
        "post_info": post_info,
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count,
        },
    }

    if progress_callback:
        progress_callback(0.02, "Inicializando postagem no TikTok...")
    try:
        init_payload = _post_json(DIRECT_POST_ENDPOINT, token, body)
    except TikTokUploadError as exc:
        raise TikTokUploadError(
            f"{exc} | parâmetros de upload: video_size={video_size}, "
            f"chunk_size={chunk_size}, total_chunk_count={total_chunk_count}"
        ) from exc
    data = init_payload.get("data") or {}
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if not upload_url:
        raise TikTokUploadError(f"TikTok não retornou upload_url: {init_payload}")

    if progress_callback:
        progress_callback(0.08, "Upload URL recebido; enviando arquivo...")
    _upload_file_chunks(upload_url, video_path, chunk_size, total_chunk_count, progress_callback=progress_callback)
    if progress_callback:
        progress_callback(1.0, "TikTok recebeu o vídeo.")

    return {
        "publish_id": publish_id,
        "raw_response": init_payload,
        "mode": "direct_post",
        "chunk_size": chunk_size,
        "total_chunk_count": total_chunk_count,
    }


def upload_video_to_tiktok_inbox(
    video_path: Path,
    access_token: str | None = None,
    caption: str | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Envia vídeo para Inbox/Draft do TikTok para finalizar/agendar dentro do TikTok.

    Requer escopo video.upload. Esse modo não publica automaticamente; o usuário precisa abrir a notificação
    no TikTok e concluir o post dentro da plataforma.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Vídeo não encontrado: {video_path}")

    token = _get_access_token(access_token)
    video_size = video_path.stat().st_size
    chunk_size, total_chunk_count = _calculate_chunking(video_size)
    body = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count,
        }
    }
    if progress_callback:
        progress_callback(0.02, "Inicializando envio para Inbox/Draft do TikTok...")
    try:
        init_payload = _post_json(INBOX_UPLOAD_ENDPOINT, token, body)
    except TikTokUploadError as exc:
        raise TikTokUploadError(
            f"{exc} | parâmetros de upload: video_size={video_size}, "
            f"chunk_size={chunk_size}, total_chunk_count={total_chunk_count}"
        ) from exc
    data = init_payload.get("data") or {}
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if not upload_url:
        raise TikTokUploadError(f"TikTok não retornou upload_url: {init_payload}")
    _upload_file_chunks(upload_url, video_path, chunk_size, total_chunk_count, progress_callback=progress_callback)
    if progress_callback:
        progress_callback(1.0, "Vídeo enviado para Inbox/Draft do TikTok.")
    return {
        "publish_id": publish_id,
        "raw_response": init_payload,
        "mode": "inbox_upload",
        "chunk_size": chunk_size,
        "total_chunk_count": total_chunk_count,
        "caption": (caption or "")[:2200],
        "caption_delivery": "manual_copy_required_by_tiktok_inbox_api",
        "caption_note": (
            "A Content Posting API de Inbox/Draft aceita apenas source_info no init; "
            "a legenda deve ser revisada/colada no TikTok ao concluir o post."
        ),
    }


def describe_tiktok_token_source(access_token: str | None = None) -> dict[str, Any]:
    """Retorna um diagnóstico seguro da fonte do token, sem expor valores sensíveis."""
    source = _resolve_token(access_token, refresh_if_needed=False)
    token_data: dict[str, Any] = {}
    if source.token_file:
        token_data = _read_token_file(source.token_file)
    expires_at = _expiry_timestamp(token_data) if token_data else None
    return {
        "source": source.label,
        "explicit_override": source.explicit,
        "has_access_token": bool(source.access_token),
        "has_refresh_token": bool(source.refresh_token or token_data.get("refresh_token")),
        "token_file": str(source.token_file) if source.token_file else "",
        "token_file_exists": bool(source.token_file and source.token_file.exists()),
        "expires_at": expires_at,
        "expires_at_iso": token_data.get("expires_at_iso", ""),
        "client_key_configured": bool(_client_key()),
        "client_secret_configured": bool(_client_secret()),
    }
