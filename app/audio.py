"""ffmpeg transcoding to the horn's talkback format: AAC-LC in ADTS, 24 kHz mono."""
import asyncio
import collections
import json

FRAME_DT = 1024 / 24000.0  # seconds of audio per AAC-LC frame at 24 kHz

# Stereo -> mono. A plain L+R sum cancels anything mixed out of phase between the channels,
# which on wide mixes takes vocals/choir with it; shifting R by 90 degrees first avoids that.
DOWNMIX = {
    "quad": "channelsplit=channel_layout=stereo[l][r];[r]aphaseshift=shift=0.5[rs];"
            "[l][rs]amix=inputs=2:normalize=0,volume=0.5",
    "mid": "pan=mono|c0=0.5*c0+0.5*c1",
    "left": "pan=mono|c0=c0",
}


class SourceError(RuntimeError):
    """ffmpeg could not read or decode the source."""


class ConvertTimeout(SourceError):
    pass


# Local files (uploads) are only ever real audio/video containers. Pinning the demuxer list stops a
# renamed ffconcat/HLS playlist from making ffmpeg read other files over and over.
LOCAL_INPUT = ["-protocol_whitelist", "file",
               "-format_whitelist", "mp3,aac,ogg,flac,wav,w64,caf,aiff,amr,mov,mp4,m4a,3gp,matroska,webm,asf"]
CONVERT_TIMEOUT = 120   # seconds


def is_url(src):
    return src.startswith(("http://", "https://"))


def ffmpeg_cmd(src, downmix="quad", seek=0.0, lead_silence_ms=900):
    fc = f"[0:a]aformat=channel_layouts=stereo,{DOWNMIX[downmix]}"
    if lead_silence_ms:
        fc += f",adelay={lead_silence_ms}:all=1"
    if not is_url(src):
        # Trailing silence: the horn only plays what's buffered once more audio pushes it out,
        # so without this the last word of a clip gets cut off.
        fc += ",apad=pad_dur=1"
    fc += "[out]"
    # One thread: a file transcodes faster than real time at first, and a multi-threaded burst
    # can exhaust the pod's CPU quota and stall the frame pacing alongside it.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1"]
    if is_url(src):
        # Keep URL sources to plain HTTP(S) so a playlist response can't redirect ffmpeg to file:// etc.
        # -rw_timeout (microseconds) makes a stalled connection fail instead of hanging forever.
        cmd += ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-rw_timeout", "15000000",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    else:
        cmd += LOCAL_INPUT
        if seek > 0:
            cmd += ["-ss", f"{seek:.2f}"]
    cmd += ["-i", src, "-filter_complex", fc, "-map", "[out]", "-c:a", "aac", "-profile:a", "aac_low",
            "-ar", "24000", "-ac", "1", "-b:a", "32k", "-filter_complex_threads", "1", "-f", "adts", "pipe:1"]
    return cmd


def split_adts(buf):
    """Split complete ADTS frames off the front of buf; return (frames, remainder)."""
    frames, i, n = [], 0, len(buf)
    while i + 7 <= n:
        if buf[i] != 0xFF or (buf[i + 1] & 0xF0) != 0xF0:
            i += 1
            continue
        flen = ((buf[i + 3] & 0x03) << 11) | (buf[i + 4] << 3) | (buf[i + 5] >> 5)
        if flen < 7:
            i += 1
            continue
        if i + flen > n:
            break
        frames.append(buf[i:i + flen])
        i += flen
    return frames, buf[i:]


async def _drain(stream, tail):
    """Keep reading ffmpeg's stderr so a chatty stream can't fill the pipe and stall it."""
    while line := await stream.readline():
        tail.append(line.decode(errors="replace").rstrip())


async def adts_frames(src, downmix="quad", seek=0.0, lead_silence_ms=900):
    """Yield ADTS frames from ffmpeg as they are produced; the ffmpeg process dies with the generator."""
    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd(src, downmix, seek, lead_silence_ms),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    tail = collections.deque(maxlen=5)
    drain = asyncio.create_task(_drain(proc.stderr, tail))
    buf = b""
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            frames, buf = split_adts(buf + chunk)
            for f in frames:
                yield f
        await proc.wait()
        await drain
        if proc.returncode:
            raise SourceError(f"can't play {src}: {' '.join(tail)[:300]}")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        drain.cancel()


async def to_m4a(src, dst):
    """Transcode any audio ffmpeg can read (e.g. .caf voice recordings) to AAC in .m4a."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *LOCAL_INPUT, "-i", str(src),
        "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "ipod", str(dst),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), CONVERT_TIMEOUT)
    except asyncio.TimeoutError:
        raise ConvertTimeout(f"conversion took longer than {CONVERT_TIMEOUT} s") from None
    finally:
        if proc.returncode is None:       # timed out or cancelled: don't leave ffmpeg running
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise SourceError(err.decode(errors="replace").strip()[-200:] or "not an audio file")


async def probe_title(path):
    """Best-effort 'Artist - Title' from a file's tags, falling back to None."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-hide_banner", "-loglevel", "error", "-show_entries", "format_tags=artist,title",
        "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        tags = {k.lower(): v for k, v in json.loads(out).get("format", {}).get("tags", {}).items()}
    except ValueError:
        return None
    parts = [tags.get("artist"), tags.get("title")]
    return " - ".join(p for p in parts if p) or None
