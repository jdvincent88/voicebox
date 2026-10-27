"""Pre-rendered prompts in a cloned Voicebox voice.

Voicebox renders a whole clip before returning it, which is too slow for
live conversation turns but ideal for fixed phrases: the greeting and the
"please hold" before a transfer. Clips are rendered once at startup,
cached on disk, and served to Twilio's <Play>.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CLIPS = {
    "greeting": None,  # filled from business.greeting
    "hold": "One moment please, I'm connecting you now.",
}


async def render_clips(voicebox_url: str, profile: str, greeting: str, out_dir: Path) -> dict[str, Path]:
    """Render each clip with the named/id'd profile. Returns name -> wav path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = dict(CLIPS, greeting=greeting)
    rendered: dict[str, Path] = {}
    async with httpx.AsyncClient(base_url=voicebox_url, timeout=300.0) as http:
        try:
            r = await http.get("/profiles")
            r.raise_for_status()
        except httpx.HTTPError:
            logger.warning("Voicebox not reachable at %s; using phone TTS for all prompts", voicebox_url)
            return {}
        match = next(
            (p for p in r.json() if profile in (p["id"], p["name"])),
            None,
        )
        if match is None:
            logger.warning("Voicebox profile %r not found; using phone TTS", profile)
            return {}

        for name, text in texts.items():
            key = hashlib.sha256(f"{match['id']}|{text}".encode()).hexdigest()[:16]
            path = out_dir / f"{name}-{key}.wav"
            if not path.exists():
                logger.info("Rendering %s clip with Voicebox profile %s", name, match["name"])
                try:
                    resp = await http.post(
                        "/generate/stream",
                        json={"profile_id": match["id"], "text": text, "language": "en"},
                    )
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.exception("Voicebox failed to render %s", name)
                    continue
                path.write_bytes(resp.content)
            rendered[name] = path
    return rendered
