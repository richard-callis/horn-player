"""Fetch a clip from a link: a direct audio file, or any page yt-dlp understands (YouTube, pages
embedding <audio>, ...). Sites behind Cloudflare bot protection (myinstants and friends) refuse
server-side downloads; the UI tells people to download those on their phone and upload instead.

yt-dlp is only used to *resolve* a page to a media URL (`-j`); the bytes are always fetched by
_download, which checks every redirect hop against private addresses and enforces the size and
time caps. Formats yt-dlp can only fetch in fragments (HLS/DASH) are refused rather than handed to
yt-dlp's own downloader. This is still best effort against SSRF: DNS can change between the check
and the connection, and while resolving, yt-dlp makes its own unchecked GETs (e.g. an internal URL
embedded in a page). Those are blind: at most ~200 characters of error text come back. Acceptable
because only authenticated users can import."""
import asyncio
import ipaddress
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import audio

MAX_BYTES = int(os.environ.get("MAX_IMPORT_MB", "50")) * 1024 * 1024
MAX_SECONDS = int(os.environ.get("MAX_IMPORT_SECONDS", "600"))   # longest clip we'll import
TIMEOUT = 180                                                     # per import, all steps
# Default to the yt-dlp installed next to this interpreter (the venv/bin or /usr/local/bin).
YTDLP = os.environ.get("YTDLP_BIN") or str(Path(sys.executable).with_name("yt-dlp"))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
BLOCKED = "that site blocks downloads from servers; download the sound on your phone and upload it instead"

_one_at_a_time = asyncio.Semaphore(1)   # yt-dlp + ffmpeg per import; keep memory for playback


class ImportFailed(Exception):
    pass


class ImportBusy(ImportFailed):
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


async def _download(url, dest: Path, allow_private, headers=None, require_media=True):
    """Fetch url into dest. Returns False (and writes nothing) if it turns out to be a web page,
    or refuses a plain download, and require_media is set, so yt-dlp can have a go instead.
    Redirects are followed by hand so every hop is checked."""
    try:
        return await _download_checked(url, dest, allow_private, headers, require_media)
    except httpx.HTTPError as e:
        raise ImportFailed(f"couldn't fetch the link ({type(e).__name__})") from None


async def _download_checked(url, dest, allow_private, headers, require_media):
    hdrs = {"User-Agent": UA, **(headers or {})}
    async with httpx.AsyncClient(timeout=30, headers=hdrs) as c:
        for _ in range(5):
            await _check_public(url, allow_private)
            async with c.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code == 403 and "cloudflare" in r.headers.get("server", "").lower():
                    raise ImportFailed(BLOCKED)
                if not r.is_success:
                    if require_media:
                        return False          # maybe yt-dlp can still extract it
                    raise ImportFailed(f"the link returned HTTP {r.status_code}")
                if any(k.lower().startswith("icy-") for k in r.headers):
                    raise ImportFailed("that's a live radio stream; add it under Library → Stations instead")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if require_media and not (ctype.startswith(("audio/", "video/"))
                                          or ctype == "application/octet-stream"):
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


async def _resolve(url, workdir: Path):
    """Ask yt-dlp which media URL a page points at, without downloading anything."""
    proc = await asyncio.create_subprocess_exec(
        YTDLP, "--ignore-config", "--no-cache-dir", "--no-plugin-dirs", "--no-playlist",
        "--quiet", "--no-warnings", "-j", "-f", "bestaudio/best",
        "--match-filter", f"!is_live & duration <=? {MAX_SECONDS} & filesize_approx <=? {MAX_BYTES}",
        "--", url,
        cwd=workdir, env={"PATH": os.environ.get("PATH", ""), "HOME": str(workdir)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    finally:
        if proc.returncode is None:     # cancelled (e.g. by the overall timeout)
            proc.kill()
            await proc.wait()
    lines = err.decode(errors="replace").strip().splitlines()
    msg = lines[-1] if lines else ""
    if proc.returncode or not out.strip():
        if "Unsupported URL" in msg or not msg:
            raise ImportFailed("couldn't find any audio on that page")
        if "does not pass filter" in msg:
            raise ImportFailed(f"that's a live stream or longer than {MAX_SECONDS // 60} minutes; "
                               "add it as a station instead")
        if "403" in msg or "Cloudflare" in msg:
            raise ImportFailed(BLOCKED)
        raise ImportFailed(msg.removeprefix("ERROR: ")[:200])
    info = json.loads(out.splitlines()[0])
    if not info.get("url") or info.get("protocol", "https") not in ("http", "https"):
        raise ImportFailed("that audio is only available as a segmented stream, which can't be imported")
    return info["url"], info.get("http_headers") or {}


async def _fetch(url, dest: Path, workdir: Path, allow_private):
    await _check_public(url, allow_private)
    raw = workdir / "media"
    if not await _download(url, raw, allow_private):
        media_url, headers = await _resolve(url, workdir)
        await _download(media_url, raw, allow_private, headers=headers, require_media=False)
    try:
        await audio.to_m4a(raw, dest, max_seconds=MAX_SECONDS)
    except audio.SourceError as e:
        raise ImportFailed(f"couldn't read the download as audio ({e})"[:200]) from None


async def fetch_clip(url, dest: Path, workdir: Path, allow_private=False):
    """Download the audio behind url and store it at dest (.m4a). One import at a time."""
    if _one_at_a_time.locked():
        raise ImportBusy("another import is running; try again in a moment")
    async with _one_at_a_time:
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            async with asyncio.timeout(TIMEOUT):
                await _fetch(url, dest, workdir, allow_private)
        except TimeoutError:
            raise ImportFailed(f"the import took longer than {TIMEOUT} s") from None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def sweep(data_dir: Path):
    """Remove work directories left behind by a restart mid-import."""
    for d in data_dir.glob(".import-*"):
        shutil.rmtree(d, ignore_errors=True)
