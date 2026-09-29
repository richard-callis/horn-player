"""Read the current song title from a Shoutcast/Icecast stream's in-band ICY metadata."""
import re

import httpx

MAX_METAINT = 64 * 1024
_TITLE = re.compile(rb"StreamTitle='(.*?)';", re.S)


async def now_playing(url, timeout=10):
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as c:
        async with c.stream("GET", url, headers={"Icy-MetaData": "1"}) as r:
            try:
                metaint = int(r.headers.get("icy-metaint", 0))
            except ValueError:
                return None
            if not 0 < metaint <= MAX_METAINT:     # a hostile server could ask us to buffer anything
                return None
            buf = b""
            async for chunk in r.aiter_raw():
                buf += chunk
                if len(buf) > metaint:
                    length = buf[metaint] * 16
                    if len(buf) >= metaint + 1 + length:
                        m = _TITLE.search(buf[metaint + 1:metaint + 1 + length])
                        title = m and m.group(1).decode("utf-8", "replace").strip(" -")
                        return title or None
                if len(buf) > metaint + 4096 + 1:
                    return None
    return None
