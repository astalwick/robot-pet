"""Load character cards from config/personality/*.md."""

from __future__ import annotations

from pathlib import Path

from lib.log import setup_logging


log = setup_logging("robot-voice")

DEFAULT_PERSONALITIES_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "personality"
DEFAULT_PERSONALITY_NAME = "default"
DEFAULT_VOICE_ID = "Ct9jL3ofSaf3bjiuX3cL"
DEFAULT_PROSE = (
    "You are Bloop, a voice assistant on a small robot pet in Longueuil, Quebec. "
    "Keep a natural, friendly tone."
)


def load_personalities(directory: str | Path = DEFAULT_PERSONALITIES_DIR) -> dict[str, tuple[str, str]]:
    dir_path = Path(directory)
    if not dir_path.is_dir():
        log.warning("personality directory %s does not exist", dir_path)
        return {}

    personalities: dict[str, tuple[str, str]] = {}
    for path in sorted(dir_path.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        parsed = _parse_card(path)
        if parsed is None:
            continue
        personalities[path.stem] = parsed
    return personalities


def lookup_personality(
    name: str,
    personalities: dict[str, tuple[str, str]],
) -> tuple[str, str, str]:
    entry = personalities.get(name)
    if entry is not None:
        return name, entry[0], entry[1]
    fallback = personalities.get(DEFAULT_PERSONALITY_NAME)
    if fallback is not None:
        log.warning("unknown personality %r, falling back to %r", name, DEFAULT_PERSONALITY_NAME)
        return DEFAULT_PERSONALITY_NAME, fallback[0], fallback[1]
    log.warning("unknown personality %r and no default card found, using built-in default", name)
    return DEFAULT_PERSONALITY_NAME, DEFAULT_VOICE_ID, DEFAULT_PROSE


def _parse_card(path: Path) -> tuple[str, str] | None:
    text = path.read_text()
    if not text.startswith("---"):
        log.warning("personality card %s: missing frontmatter, skipping", path.name)
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        log.warning("personality card %s: invalid frontmatter, skipping", path.name)
        return None

    voice_id = _voice_id_from_frontmatter(parts[1])
    if not voice_id:
        log.warning("personality card %s: missing voice_id, skipping", path.name)
        return None

    prose = parts[2].lstrip("\n")
    return voice_id, prose


def _voice_id_from_frontmatter(frontmatter: str) -> str | None:
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if stripped.startswith("voice_id:"):
            value = stripped.split(":", 1)[1].strip()
            return value or None
    return None
