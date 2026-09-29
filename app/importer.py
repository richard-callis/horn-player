"""Fetch a clip from a link: a direct audio file, or any page yt-dlp understands (YouTube, pages
embedding <audio>, ...). Sites behind Cloudflare bot protection (myinstants and friends) refuse
server-side downloads; the UI tells people to download those on their phone and upload instead."""
import asyncio
import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import audio

MAX_BYTES = int(os.environ.get("MAX_IMPORT_MB", "50")) * 1024 * 1024
MAX_SECONDS = int(os.environ.get("MAX_IMPORT_SECONDS", "600"))   # longest clip we'll import
TIMEOUT = 180
YTDLP = os.environ.get("YTDLP_BIN", "yt-dlp")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"


class ImportFailed(Exception):
    pass


def public_host(url):
    """Whether every address the URL's host resolves to is public."""
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        raise ImportFailed(f"can't resolve {host}")
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def _check_public(url, allow_private):
    if not url.startswith(("http://", "https://")):
        raise ImportFailed("only http(s) links are supported")
    if not allow_private and not await asyncio.to_thread(public_host, url):
        raise ImportFailed("links to private or internal addresses aren't allowed")


async def _direct(url, dest: Path, allow_private):
    """Download url if it is itself audio/video. Returns False if it's a web page instead.
    Redirects are followed by hand so every hop gets the public-address check."""
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}) as c:
        for _ in range(5):
            await _check_public(url, allow_private)
            async with c.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code == 403 and "cloudflare" in r.headers.get("server", "").lower():
                    raise ImportFailed("that site blocks downloads from servers; download the sound "
                                       "on your phone and upload it instead")
                if not r.is_success:
                    raise ImportFailed(f"the link returned HTTP {r.status_code}")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if not (ctype.startswith(("audio/", "video/")) or ctype == "application/octet-stream"):
                    return False
                size = 0
                with dest.open("wb") as out:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ImportFailed(f"the file is larger than {MAX_BYTES >> 20} MB")
                        out.write(chunk)
                return True
        raise ImportFailed("too many redirects")


async def _ytdlp(url, workdir: Path):
    """Let yt-dlp find the audio on a page. Returns the downloaded file."""
    proc = await asyncio.create_subprocess_exec(
        YTDLP, "--no-playlist", "--no-progress", "--quiet", "--no-warnings",
        "-f", "bestaudio/best", "--max-filesize", str(MAX_BYTES),
        "--match-filter", f"duration <=? {MAX_SECONDS}",
        "-o", str(workdir / "dl.%(ext)s"), "--", url,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except asyncio.TimeoutError:
        raise ImportFailed(f"the download took longer than {TIMEOUT} s") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    files = [p for p in workdir.glob("dl.*") if not p.name.endswith(".part")]
    if proc.returncode or not files:
        msg = err.decode(errors="replace").strip().splitlines()
        msg = msg[-1] if msg else ""
        if "Unsupported URL" in msg:
            raise ImportFailed("couldn't find any audio on that page")
        if "does not pass filter" in msg:
            raise ImportFailed(f"that's longer than {MAX_SECONDS // 60} minutes")
        if "403" in msg or "Cloudflare" in msg:
            raise ImportFailed("that site blocks downloads from servers; download the sound on "
                               "your phone and upload it instead")
        raise ImportFailed(msg.removeprefix("ERROR: ")[:200] or "download failed")
    return files[0]


async def fetch_clip(url, dest: Path, workdir: Path, allow_private=False):
    """Download the audio behind url and store it at dest (.m4a)."""
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        await _check_public(url, allow_private)
        raw = workdir / "direct"
        got = raw if await _direct(url, raw, allow_private) else await _ytdlp(url, workdir)
        try:
            await audio.to_m4a(got, dest, max_seconds=MAX_SECONDS)
        except audio.SourceError as e:
            raise ImportFailed(f"couldn't read the download as audio ({e})"[:200]) from None
    finally:
        for p in workdir.glob("*"):
            p.unlink(missing_ok=True)
        workdir.rmdir()
