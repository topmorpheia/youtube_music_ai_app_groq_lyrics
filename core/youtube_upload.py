from __future__ import annotations

from pathlib import Path
from typing import Callable

from .config import YOUTUBE_CLIENT_SECRETS, YOUTUBE_TOKEN_FILE

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


def get_youtube_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if YOUTUBE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not YOUTUBE_CLIENT_SECRETS.exists():
            raise FileNotFoundError(
                f"Arquivo OAuth não encontrado: {YOUTUBE_CLIENT_SECRETS}. "
                "Baixe o client_secret.json no Google Cloud e salve nesse caminho."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(YOUTUBE_CLIENT_SECRETS), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        YOUTUBE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        YOUTUBE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload_video(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    thumbnail_path: Path | None = None,
    privacy_status: str = "private",
    publish_at: str | None = None,
    category_id: str = "10",
    made_for_kids: bool = False,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict:
    from googleapiclient.http import MediaFileUpload

    if not video_path.exists():
        raise FileNotFoundError(f"Vídeo não encontrado: {video_path}")

    youtube = get_youtube_service()
    status_body = {
        "privacyStatus": "private" if publish_at else privacy_status,
        "selfDeclaredMadeForKids": bool(made_for_kids),
    }
    if publish_at:
        # Agendamento nativo do YouTube exige privacyStatus=private e status.publishAt em ISO 8601.
        status_body["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": str(category_id or "10"),
        },
        "status": status_body,
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progress_callback:
            progress_callback(float(status.progress()), "Enviando vídeo...")

    video_id = response["id"]
    thumbnail_result = None
    thumbnail_error = None
    if thumbnail_path and thumbnail_path.exists():
        try:
            thumb_media = MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg", resumable=False)
            thumbnail_result = youtube.thumbnails().set(videoId=video_id, media_body=thumb_media).execute()
        except Exception as exc:  # noqa: BLE001
            thumbnail_error = str(exc)

    if progress_callback:
        progress_callback(1.0, "Upload concluído.")

    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "raw_response": response,
        "thumbnail_result": thumbnail_result,
        "thumbnail_error": thumbnail_error,
        "publish_at": publish_at,
        "privacy_status": status_body.get("privacyStatus"),
    }
