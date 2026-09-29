"""Local text-to-speech with Piper, so announcements ride the talkback path (no extra Protect rights)."""
import asyncio
import hashlib
import os
import uuid
from pathlib import Path

VOICE = os.environ.get("PIPER_VOICE", "/app/voices/en_US-lessac-medium.onnx")
PIPER = os.environ.get("PIPER_BIN", "piper")
CACHE_KEEP = 200   # most recently used renders kept on disk


async def synthesize(text, cache_dir: Path):
    """Render text to a WAV file (cached by voice + text) and return its path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / (hashlib.sha256(f"{VOICE}\0{text}".encode()).hexdigest()[:24] + ".wav")
    if out.exists():
        out.touch()
        return str(out)
    tmp = cache_dir / f".{uuid.uuid4().hex}.tmp.wav"   # unique, so identical concurrent requests don't collide
    proc = await asyncio.create_subprocess_exec(
        PIPER, "-m", VOICE, "-f", str(tmp),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate(text.encode())
    if proc.returncode or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"piper failed: {err.decode(errors='replace')[-300:]}")
    tmp.replace(out)
    _prune(cache_dir)
    return str(out)


def _prune(cache_dir: Path):
    renders = sorted(cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in renders[CACHE_KEEP:]:
        old.unlink(missing_ok=True)
