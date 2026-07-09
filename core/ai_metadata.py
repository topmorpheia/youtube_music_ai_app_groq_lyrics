from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from .config import GROQ_API_KEY, GROQ_MAX_TOKENS, GROQ_TEMPERATURE, GROQ_TEXT_MODEL, TIKTOK_DEFAULT_PRIVACY_LEVEL
from .utils import normalize_tags, slugify


@dataclass
class VideoMetadata:
    youtube_title: str
    youtube_description: str
    youtube_tags: list[str] = field(default_factory=list)
    youtube_shorts_title: str = ""
    youtube_shorts_description: str = ""
    youtube_shorts_tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    pinned_comment: str = ""
    thumbnail_prompt: str = ""
    cover_prompt_16x9: str = ""
    cover_prompt_vertical: str = ""
    category_id: str = "10"  # Music
    privacy_status: str = "private"
    tiktok_caption: str = ""
    tiktok_privacy_level: str = "SELF_ONLY"
    tiktok_disable_duet: bool = False
    tiktok_disable_stitch: bool = False
    tiktok_disable_comment: bool = False
    tiktok_video_cover_timestamp_ms: int = 1000
    tiktok_brand_content_toggle: bool = False
    tiktok_brand_organic_toggle: bool = False
    tiktok_is_aigc: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STOPWORDS = {
    "a", "o", "os", "as", "um", "uma", "uns", "umas", "de", "da", "do", "das", "dos",
    "em", "no", "na", "nos", "nas", "por", "pra", "para", "com", "sem", "que", "e", "ou",
    "eu", "tu", "você", "voce", "ele", "ela", "nós", "nos", "eles", "elas", "me", "te", "se",
    "meu", "minha", "seu", "sua", "minhas", "meus", "seus", "suas", "é", "ser", "sou", "foi",
    "vai", "vou", "tá", "ta", "tô", "to", "não", "nao", "sim", "mais", "mas", "já", "ja",
    "the", "and", "you", "your", "for", "with", "from", "that", "this", "are", "was", "were",
    "of", "in", "on", "to", "my", "me", "it", "is", "be", "by", "or", "at",
}


YOUTUBE_DESCRIPTION_MAX_CHARS = 5000
TIKTOK_CAPTION_MAX_CHARS = 2200
TIKTOK_PRIVACY_OPTIONS = {
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
}


def required_title_prefix(song_title: str, artist_name: str) -> str:
    """Formato pedido: Nome do Artista - nome musica."""
    title = (song_title or "").strip() or "Nova Música"
    artist = (artist_name or "").strip() or "Artista"
    return f"{artist} - {title}"


def ensure_required_title(title: str, song_title: str, artist_name: str, suffix: str = "", max_chars: int = 100) -> str:
    prefix = required_title_prefix(song_title, artist_name)
    clean = re.sub(r"\s+", " ", (title or "").strip())
    if not clean.lower().startswith(prefix.lower()):
        clean = f"{prefix} {suffix}".strip()
    elif suffix and suffix.lower() not in clean.lower() and len(f"{clean} {suffix}") <= max_chars:
        clean = f"{clean} {suffix}".strip()
    if len(clean) > max_chars:
        clean = prefix[:max_chars]
    return clean


def append_lyrics_to_description(
    description: str,
    lyrics: str,
    max_chars: int = YOUTUBE_DESCRIPTION_MAX_CHARS,
) -> str:
    """Acrescenta a letra informada pelo usuário à descrição final do YouTube.

    O YouTube aceita descrições longas, mas a API corta em 5000 caracteres no
    upload. Esta função monta a seção da letra antes disso, preservando o máximo
    possível do texto enviado e evitando duplicação quando o usuário gerar de novo.
    """
    clean_lyrics = _clean_lyrics_for_description(lyrics)
    base = (description or "").strip()
    if not clean_lyrics:
        return base

    markers = ["\n\nLETRA DA MÚSICA", "\n\nLETRA", "\n\nLYRICS"]
    for marker in markers:
        idx = base.upper().find(marker.strip().upper())
        if idx != -1:
            base = base[:idx].rstrip()
            break

    header = "LETRA DA MÚSICA:"
    separator = "\n\n" if base else ""
    fixed_len = len(base) + len(separator) + len(header) + 1
    available = max_chars - fixed_len

    min_lyrics_space = min(1200, len(clean_lyrics))
    if available < min_lyrics_space and base:
        keep_base = max(0, max_chars - len(separator) - len(header) - 1 - min_lyrics_space - 42)
        base = base[:keep_base].rstrip()
        if base:
            base += "\n\n[Descrição resumida automaticamente para caber a letra.]"
        separator = "\n\n" if base else ""
        fixed_len = len(base) + len(separator) + len(header) + 1
        available = max_chars - fixed_len

    if available <= 0:
        return base[:max_chars].rstrip()

    lyrics_for_description = clean_lyrics
    if len(lyrics_for_description) > available:
        notice = "\n\n[Letra cortada automaticamente para respeitar o limite da descrição do YouTube.]"
        cutoff = max(0, available - len(notice))
        lyrics_for_description = lyrics_for_description[:cutoff].rstrip()
        if "\n" in lyrics_for_description and len(lyrics_for_description) > 300:
            lyrics_for_description = lyrics_for_description.rsplit("\n", 1)[0].rstrip()
        lyrics_for_description += notice

    return f"{base}{separator}{header}\n{lyrics_for_description}".strip()[:max_chars].rstrip()


def _clean_lyrics_for_description(lyrics: str) -> str:
    text = (lyrics or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    output: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip():
            blank_count = 0
            output.append(line.strip())
        else:
            blank_count += 1
            if blank_count <= 1:
                output.append("")
    return "\n".join(output).strip()


def generate_metadata(
    song_title: str,
    artist_name: str,
    lyrics: str,
    mood: str,
    language: str,
    extra_context: str = "",
) -> VideoMetadata:
    """Gera metadados via Groq. Sem chave ou em caso de erro, usa fallback local."""
    if GROQ_API_KEY:
        try:
            return _generate_with_groq(song_title, artist_name, lyrics, mood, language, extra_context)
        except Exception as exc:  # noqa: BLE001
            print(f"Groq falhou; usando fallback local: {exc}")
    return fallback_metadata(song_title, artist_name, lyrics, mood, language, extra_context)


def _build_prompt(
    song_title: str,
    artist_name: str,
    lyrics: str,
    mood: str,
    language: str,
    extra_context: str,
) -> str:
    trimmed_lyrics = lyrics[:9000]
    exact_title = required_title_prefix(song_title, artist_name)
    return f"""
Você é um estrategista de YouTube Music/SEO, YouTube Shorts e TikTok para lançamentos musicais.
Gere metadados para três publicações: vídeo YouTube 16:9, YouTube Shorts 9:16 e TikTok 9:16.

OBJETIVO:
- Maximizar clareza, CTR, descoberta orgânica e intenção de busca.
- Criar parâmetros prontos para postar, mas ainda editáveis pelo usuário.

REGRAS IMPORTANTES:
- O título de YouTube e Shorts SEMPRE deve começar exatamente com: {exact_title}
- O caption do TikTok também deve começar com: {exact_title}
- Não coloque a letra completa no JSON; o app irá anexar a letra informada pelo usuário automaticamente no final da descrição do YouTube quando marcado.
- Não reproduza versos completos dentro da parte criada pela IA.
- Não invente dados falsos, feats, gravadoras, datas ou links.
- Não use linguagem apelativa demais, enganosa ou clickbait falso.
- YouTube title: máximo 100 caracteres.
- Shorts title: máximo 100 caracteres e inclua #Shorts se couber.
- TikTok caption: máximo 2200 caracteres, com hashtags relevantes, sem exagerar.
- Tags do YouTube/Shorts: no máximo 20, curtas, sem #.
- Hashtags: no máximo 8, com #.
- Categoria: use 10 para música.
- TikTok privacy_level deve ser um destes: PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, SELF_ONLY. Use SELF_ONLY se estiver em dúvida.
- Use tiktok_is_aigc=false, exceto se o contexto deixar claro que o vídeo é conteúdo gerado por IA.
- Retorne SOMENTE JSON válido, sem markdown e sem comentários fora do JSON.

DADOS DA MÚSICA:
Título informado: {song_title or "não informado"}
Artista/canal: {artist_name or "não informado"}
Idioma desejado dos metadados: {language or "pt-BR"}
Vibe/estilo: {mood or "emocional, musical"}
Contexto extra: {extra_context or "nenhum"}
Letra/resumo fornecido pelo usuário:
{trimmed_lyrics or "não fornecido"}

FORMATO EXATO:
{{
  "youtube_title": "{exact_title} | Música Oficial",
  "youtube_description": "...",
  "youtube_tags": ["..."],
  "youtube_shorts_title": "{exact_title} #Shorts",
  "youtube_shorts_description": "...",
  "youtube_shorts_tags": ["..."],
  "hashtags": ["#..."],
  "keywords": ["..."],
  "pinned_comment": "...",
  "thumbnail_prompt": "Prompt visual em inglês para thumbnail 16:9 sem texto; deve combinar com a música.",
  "cover_prompt_16x9": "Prompt visual em inglês para arte 16:9 sem texto.",
  "cover_prompt_vertical": "Prompt visual em inglês para arte vertical 9:16 sem texto para Shorts/TikTok.",
  "category_id": "10",
  "privacy_status": "private",
  "tiktok_caption": "{exact_title} ... #musica",
  "tiktok_privacy_level": "SELF_ONLY",
  "tiktok_disable_duet": false,
  "tiktok_disable_stitch": false,
  "tiktok_disable_comment": false,
  "tiktok_video_cover_timestamp_ms": 1000,
  "tiktok_brand_content_toggle": false,
  "tiktok_brand_organic_toggle": false,
  "tiktok_is_aigc": false
}}
""".strip()


def _generate_with_groq(
    song_title: str,
    artist_name: str,
    lyrics: str,
    mood: str,
    language: str,
    extra_context: str,
) -> VideoMetadata:
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    prompt = _build_prompt(song_title, artist_name, lyrics, mood, language, extra_context)
    system_message = (
        "Você responde como especialista em YouTube Music, Shorts, TikTok, SEO, copywriting e lançamento musical. "
        "Sempre retorne JSON válido e nunca reproduza letras protegidas em trechos longos."
    )

    try:
        completion = client.chat.completions.create(
            model=GROQ_TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=GROQ_TEMPERATURE,
            max_tokens=GROQ_MAX_TOKENS,
        )
    except Exception:
        completion = client.chat.completions.create(
            model=GROQ_TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_message + " Retorne apenas JSON puro."},
                {"role": "user", "content": prompt},
            ],
            temperature=GROQ_TEMPERATURE,
            max_tokens=GROQ_MAX_TOKENS,
        )

    raw = completion.choices[0].message.content or "{}"
    data = _safe_json_loads(raw)
    return _metadata_from_dict(data, song_title, artist_name, lyrics, mood, language, extra_context)


def _safe_json_loads(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _metadata_from_dict(
    data: dict[str, Any],
    song_title: str,
    artist_name: str,
    lyrics: str,
    mood: str,
    language: str,
    extra_context: str,
) -> VideoMetadata:
    fallback = fallback_metadata(song_title, artist_name, lyrics, mood, language, extra_context)
    youtube_title = ensure_required_title(str(data.get("youtube_title") or fallback.youtube_title), song_title, artist_name, suffix="| Música Oficial")
    shorts_title = ensure_required_title(str(data.get("youtube_shorts_title") or fallback.youtube_shorts_title), song_title, artist_name, suffix="#Shorts")
    description = str(data.get("youtube_description") or fallback.youtube_description).strip()
    shorts_description = str(data.get("youtube_shorts_description") or fallback.youtube_shorts_description).strip()
    youtube_tags = normalize_tags(_ensure_list(data.get("youtube_tags")) or fallback.youtube_tags, limit=20)
    shorts_tags = normalize_tags(_ensure_list(data.get("youtube_shorts_tags")) or fallback.youtube_shorts_tags, limit=20)
    hashtags = _normalize_hashtags(_ensure_list(data.get("hashtags")) or fallback.hashtags, limit=8)
    keywords = normalize_tags(_ensure_list(data.get("keywords")) or fallback.keywords, limit=20)
    tiktok_caption = _ensure_tiktok_caption(str(data.get("tiktok_caption") or fallback.tiktok_caption), song_title, artist_name, hashtags)
    tiktok_privacy = str(data.get("tiktok_privacy_level") or fallback.tiktok_privacy_level or TIKTOK_DEFAULT_PRIVACY_LEVEL).strip().upper()
    if tiktok_privacy not in TIKTOK_PRIVACY_OPTIONS:
        tiktok_privacy = TIKTOK_DEFAULT_PRIVACY_LEVEL if TIKTOK_DEFAULT_PRIVACY_LEVEL in TIKTOK_PRIVACY_OPTIONS else "SELF_ONLY"
    return VideoMetadata(
        youtube_title=youtube_title,
        youtube_description=description[:YOUTUBE_DESCRIPTION_MAX_CHARS],
        youtube_tags=_cap_tags_total(youtube_tags),
        youtube_shorts_title=shorts_title,
        youtube_shorts_description=shorts_description[:YOUTUBE_DESCRIPTION_MAX_CHARS],
        youtube_shorts_tags=_cap_tags_total(shorts_tags),
        hashtags=hashtags,
        keywords=keywords,
        pinned_comment=str(data.get("pinned_comment") or fallback.pinned_comment).strip(),
        thumbnail_prompt=str(data.get("thumbnail_prompt") or fallback.thumbnail_prompt).strip(),
        cover_prompt_16x9=str(data.get("cover_prompt_16x9") or data.get("cover_prompt") or fallback.cover_prompt_16x9).strip(),
        cover_prompt_vertical=str(data.get("cover_prompt_vertical") or fallback.cover_prompt_vertical).strip(),
        category_id=str(data.get("category_id") or "10"),
        privacy_status=str(data.get("privacy_status") or "private"),
        tiktok_caption=tiktok_caption,
        tiktok_privacy_level=tiktok_privacy,
        tiktok_disable_duet=_as_bool(data.get("tiktok_disable_duet"), fallback.tiktok_disable_duet),
        tiktok_disable_stitch=_as_bool(data.get("tiktok_disable_stitch"), fallback.tiktok_disable_stitch),
        tiktok_disable_comment=_as_bool(data.get("tiktok_disable_comment"), fallback.tiktok_disable_comment),
        tiktok_video_cover_timestamp_ms=_as_int(data.get("tiktok_video_cover_timestamp_ms"), fallback.tiktok_video_cover_timestamp_ms),
        tiktok_brand_content_toggle=_as_bool(data.get("tiktok_brand_content_toggle"), fallback.tiktok_brand_content_toggle),
        tiktok_brand_organic_toggle=_as_bool(data.get("tiktok_brand_organic_toggle"), fallback.tiktok_brand_organic_toggle),
        tiktok_is_aigc=_as_bool(data.get("tiktok_is_aigc"), fallback.tiktok_is_aigc),
    )


def _ensure_tiktok_caption(caption: str, song_title: str, artist_name: str, hashtags: list[str]) -> str:
    prefix = required_title_prefix(song_title, artist_name)
    clean = re.sub(r"\s+", " ", (caption or "").strip())
    if not clean.lower().startswith(prefix.lower()):
        clean = f"{prefix} {clean}".strip()
    for tag in hashtags[:6]:
        if tag.lower() not in clean.lower() and len(clean) + len(tag) + 1 <= TIKTOK_CAPTION_MAX_CHARS:
            clean = f"{clean} {tag}"
    return clean[:TIKTOK_CAPTION_MAX_CHARS].rstrip()


def _as_bool(value: Any, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "sim", "y"}
    if value is None:
        return fallback
    return bool(value)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return fallback


def _ensure_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def fallback_metadata(
    song_title: str,
    artist_name: str,
    lyrics: str,
    mood: str,
    language: str,
    extra_context: str = "",
) -> VideoMetadata:
    title_base = song_title.strip() or "Nova Música"
    artist = artist_name.strip() or "Artista"
    exact_title = required_title_prefix(title_base, artist)
    keywords = extract_keywords(lyrics, song_title, mood, limit=18)
    youtube_title = ensure_required_title(f"{exact_title} | Música Oficial", title_base, artist, suffix="| Música Oficial")
    shorts_title = ensure_required_title(f"{exact_title} #Shorts", title_base, artist, suffix="#Shorts")
    hashtags = _normalize_hashtags([artist, title_base, "Musica", "Lançamento", "Shorts", "TikTok"], limit=8)
    tags = _cap_tags_total(
        normalize_tags(
            [title_base, artist, f"{title_base} {artist}", exact_title, "música oficial", "video oficial", "lyric video", *keywords],
            limit=20,
        )
    )
    shorts_tags = _cap_tags_total(
        normalize_tags([title_base, artist, exact_title, "shorts", "youtube shorts", "música", "lançamento", *keywords], limit=20)
    )
    description_lines = [
        f"Ouça {exact_title}.",
        "",
        "Deixe seu like, inscreva-se no canal e ative o sino para acompanhar os próximos lançamentos.",
        "",
        f"Vibe: {mood or 'musical, emocional'}.",
    ]
    if extra_context.strip():
        description_lines.extend(["", extra_context.strip()[:500]])
    description_lines.extend(["", " ".join(hashtags)])
    shorts_description = f"{exact_title} em versão Shorts.\n\n{' '.join(hashtags)} #Shorts"
    tiktok_caption = _ensure_tiktok_caption(
        f"{exact_title} — trecho oficial. {' '.join(hashtags[:5])}",
        title_base,
        artist,
        hashtags,
    )
    return VideoMetadata(
        youtube_title=youtube_title,
        youtube_description="\n".join(description_lines),
        youtube_tags=tags,
        youtube_shorts_title=shorts_title,
        youtube_shorts_description=shorts_description,
        youtube_shorts_tags=shorts_tags,
        hashtags=hashtags,
        keywords=keywords,
        pinned_comment="Qual parte da música mais combinou com você? Comenta aqui embaixo.",
        thumbnail_prompt=(
            "cinematic YouTube music thumbnail, no text, no logo, emotional lighting, "
            f"song mood {mood or 'emotional'}, concept inspired by {title_base}, high contrast, 16:9"
        ),
        cover_prompt_16x9=(
            "cinematic 16:9 music artwork, no text, no logo, professional album visual, "
            f"concept inspired by {title_base}, themes: {', '.join(keywords[:8])}"
        ),
        cover_prompt_vertical=(
            "cinematic vertical 9:16 music artwork for TikTok and YouTube Shorts, no text, no logo, "
            f"concept inspired by {title_base}, mood {mood or 'emotional'}, high contrast"
        ),
        tiktok_caption=tiktok_caption,
        tiktok_privacy_level=TIKTOK_DEFAULT_PRIVACY_LEVEL if TIKTOK_DEFAULT_PRIVACY_LEVEL in TIKTOK_PRIVACY_OPTIONS else "SELF_ONLY",
    )


def extract_keywords(lyrics: str, song_title: str = "", mood: str = "", limit: int = 20) -> list[str]:
    text = f"{song_title} {mood} {lyrics}".lower()
    tokens = re.findall(r"[\wÀ-ÿ']+", text)
    counts: dict[str, int] = {}
    for token in tokens:
        token = token.strip("'")
        if len(token) < 3 or token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ordered[:limit]]


def _normalize_hashtags(values: list[str], limit: int = 8) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value).strip().strip("#")
        value = slugify(value).replace("-", "")
        if not value:
            continue
        tag = f"#{value[:40]}"
        key = tag.lower()
        if key in seen:
            continue
        output.append(tag)
        seen.add(key)
        if len(output) >= limit:
            break
    return output


def _cap_tags_total(tags: list[str], max_chars: int = 480) -> list[str]:
    output: list[str] = []
    total = 0
    for tag in tags:
        addition = len(tag) + (1 if output else 0)
        if total + addition > max_chars:
            break
        output.append(tag)
        total += addition
    return output


def create_local_thumbnail(
    cover_path: Path,
    output_path: Path,
    title: str = "",
    artist: str = "",
    width: int = 1280,
    height: int = 720,
) -> Path:
    """Cria thumbnail em tela cheia, sem duplicar a imagem."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(cover_path) as img:
        img = img.convert("RGB")
        frame = _cover_fill(img, width, height)
        frame = ImageEnhance.Contrast(frame).enhance(1.06)
        frame = ImageEnhance.Color(frame).enhance(1.06)
        frame = ImageEnhance.Sharpness(frame).enhance(1.08)
        frame.save(output_path, quality=94, optimize=True)
    return output_path


def create_placeholder_cover(output_path: Path, title: str, mood: str = "", width: int = 1920, height: int = 1080) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base_seed = sum(ord(ch) for ch in (title + mood + str(width) + str(height))) or 123
    c1 = (30 + base_seed % 80, 35 + (base_seed * 3) % 70, 70 + (base_seed * 5) % 120)
    c2 = (100 + (base_seed * 7) % 120, 50 + (base_seed * 11) % 90, 130 + (base_seed * 13) % 90)
    img = Image.new("RGB", (width, height), c1)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(c1[i] * (1 - ratio) + c2[i] * ratio) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(22):
        radius = 90 + ((base_seed + i * 47) % 230)
        x = (base_seed * (i + 3) * 29) % width
        y = (base_seed * (i + 5) * 31) % height
        od.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 255, 255, 20 + i % 5 * 9))
    overlay = overlay.filter(ImageFilter.GaussianBlur(42))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    img.save(output_path, quality=94)
    return output_path


def _cover_fill(img: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / img.width, height / img.height)
    new_size = (int(img.width * scale), int(img.height * scale))
    resized = img.resize(new_size, Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))
