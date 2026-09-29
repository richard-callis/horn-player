"""Local text-to-speech with Piper, so announcements ride the talkback path (no extra Protect rights)."""
import asyncio
import hashlib
import os
from pathlib import Path

VOICE = os.environ.get("PIPER_VOICE", "/app/voices/en_US-lessac-medium.onnx")
PIPER = os.environ.get("PIPER_BIN", "piper")


async def synthesize(text, cache_dir: Path):
    """Render text to a WAV file (cached by voice + text) and return its path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / (hashlib.sha256(f"{VOICE}\0{text}".encode()).hexdigest()[:24] + ".wav")
    if out.exists():
        return str(out)
    tmp = out.with_suffix(".tmp.wav")
    proc = await asyncio.create_subprocess_exec(
        PIPER, "-m", VOICE, "-f", str(tmp),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate(text.encode())
    if proc.returncode or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"piper failed: {err.decode(errors='replace')[-300:]}")
    tmp.rename(out)
    return str(out)
