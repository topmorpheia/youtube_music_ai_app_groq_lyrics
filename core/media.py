from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from .config import VIDEO_CRF, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH
from .utils import media_duration_seconds, require_ffmpeg, run_command


def copy_media(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    return destination


def optimize_cover(input_path: Path, output_path: Path, width: int = 1920, height: int = 1080) -> Path:
    """Prepara a arte do vídeo em tela cheia 16:9, sem duplicar imagem.

    Versões anteriores transformavam a capa em um quadrado e depois colocavam esse quadrado
    sobre um fundo desfocado. Agora a capa/arte é recortada para preencher 1920x1080,
    gerando um vídeo visualmente parecido com uma thumbnail em tela cheia.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = _safe_even(width)
    height = _safe_even(height)
    with Image.open(input_path) as img:
        img = img.convert("RGB")
        img = _cover_fill(img, width, height)
        img = ImageEnhance.Contrast(img).enhance(1.04)
        img = ImageEnhance.Color(img).enhance(1.04)
        img.save(output_path, quality=93, optimize=True)
    return output_path


def optimize_vertical_cover(input_path: Path, output_path: Path, width: int = 1080, height: int = 1920) -> Path:
    """Prepara arte vertical 9:16 para Shorts/TikTok em tela cheia."""
    return optimize_cover(input_path, output_path, width=width, height=height)


def optimize_thumbnail(input_path: Path, output_path: Path, width: int = 1280, height: int = 720) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(input_path) as img:
        img = img.convert("RGB")
        cropped = _cover_fill(img, width, height)
        cropped = ImageEnhance.Contrast(cropped).enhance(1.05)
        cropped = ImageEnhance.Color(cropped).enhance(1.04)
        cropped.save(output_path, quality=92, optimize=True)
    return output_path


def _safe_even(value: int | float) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


def _cover_fill(img: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / img.width, height / img.height)
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    resized = img.resize(new_size, Image.Resampling.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def render_youtube_video(
    audio_path: Path,
    cover_path: Path,
    output_path: Path,
    effect: str = "cinematic_zoom",
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    fps: int = VIDEO_FPS,
    crf: int = VIDEO_CRF,
    duration_limit_seconds: float | None = None,
) -> Path:
    """Renderiza vídeo 16:9 em tela cheia com áudio, sem legendas e sem segunda imagem.

    A imagem enviada é usada como quadro inteiro do vídeo. O efeito padrão é um
    Ken Burns leve, para dar movimento sem pesar a memória do PC.
    """
    require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_duration = media_duration_seconds(audio_path)
    if duration_limit_seconds is not None and duration_limit_seconds > 0:
        duration = min(source_duration, float(duration_limit_seconds))
    else:
        duration = source_duration

    width = _safe_even(width)
    height = _safe_even(height)
    fps = max(1, int(fps))
    crf = int(crf)
    total_frames = max(1, int(duration * fps))

    base = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"

    if effect == "none":
        zoom_expr = "1"
        look = "eq=contrast=1.02:saturation=1.03:brightness=0"
    elif effect == "subtle_pulse":
        zoom_expr = "1.025+0.012*sin(on/28)"
        look = "eq=contrast=1.06:saturation=1.10:brightness=0.005,vignette=PI/6"
    else:
        # Efeito padrão: zoom cinematográfico lento em tela cheia.
        zoom_expr = f"min(1.08,1+0.08*on/{total_frames})"
        look = "eq=contrast=1.07:saturation=1.10:brightness=0.005,vignette=PI/6"

    filter_complex = (
        f"[0:v]{base},"
        f"zoompan=z='{zoom_expr}':d={total_frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={width}x{height}:fps={fps},"
        f"trim=duration={duration:.3f},{look},format=yuv420p[v]"
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(cover_path),
            "-i",
            str(audio_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return output_path


def render_vertical_video(
    audio_path: Path,
    cover_path: Path,
    output_path: Path,
    effect: str = "cinematic_zoom",
    width: int = 1080,
    height: int = 1920,
    fps: int = VIDEO_FPS,
    crf: int = VIDEO_CRF,
    duration_limit_seconds: float | None = None,
) -> Path:
    """Renderiza vídeo vertical 9:16 para YouTube Shorts, TikTok e Reels.

    Quando `duration_limit_seconds` é informado, o áudio/vídeo vertical é cortado
    nesse limite. Isso mantém o arquivo dentro do teto de duração do Shorts.
    """
    return render_youtube_video(
        audio_path=audio_path,
        cover_path=cover_path,
        output_path=output_path,
        effect=effect,
        width=width,
        height=height,
        fps=fps,
        crf=crf,
        duration_limit_seconds=duration_limit_seconds,
    )


def make_social_preview(input_path: Path, output_path: Path, width: int = 1280, height: int = 720) -> Path:
    """Gera prévia/thumbnail 16:9 em tela cheia, sem duplicar a capa."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(input_path) as img:
        img = img.convert("RGB")
        frame = _cover_fill(img, width, height)
        frame = ImageEnhance.Contrast(frame).enhance(1.05)
        frame = ImageEnhance.Color(frame).enhance(1.05)
        frame.save(output_path, quality=92, optimize=True)
    return output_path
