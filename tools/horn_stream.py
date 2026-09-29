#!/usr/bin/env python3
"""Stream music to a UniFi Protect AI Horn over Protect's (private) talkback websocket.

Credentials come from the environment (UNIFI_USER / UNIFI_PASS) or a KEY=VALUE
file given with --creds-file, never from argv. Use a dedicated local admin
account on the console.

Examples:
  horn_stream.py --source ./music --shuffle --loop
  horn_stream.py --source https://example.com/halloween-radio.mp3
  horn_stream.py --list
"""
import argparse
import os
import random
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3
import websocket

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FRAME_DT = 1024 / 24000.0  # one AAC-LC frame at 24 kHz
LEAD = 0.40                # run this far ahead of real time to keep the speaker's jitter buffer full
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma", ".webm"}

stop = False


def on_signal(*_):
    global stop
    stop = True


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_creds(path):
    env = {k: os.environ.get(k) for k in ("UNIFI_HOST", "UNIFI_USER", "UNIFI_PASS")}
    if path:
        for line in Path(path).read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k in env:
                env[k] = v.strip().strip("'\"")
    if not env["UNIFI_USER"] or not env["UNIFI_PASS"]:
        sys.exit("set UNIFI_USER and UNIFI_PASS in the environment or --creds-file")
    return env["UNIFI_HOST"], env["UNIFI_USER"], env["UNIFI_PASS"]


class Protect:
    def __init__(self, host, user, pw):
        self.host, self.user, self.pw = host, user, pw
        self.api = f"https://{host}/proxy/protect/api"
        self.s = None

    def login(self):
        self.s = requests.Session()
        self.s.verify = False
        r = self.s.post(f"https://{self.host}/api/auth/login",
                        json={"username": self.user, "password": self.pw}, timeout=15)
        r.raise_for_status()
        csrf = r.headers.get("X-Updated-Csrf-Token") or r.headers.get("X-CSRF-Token")
        if csrf:
            self.s.headers["X-CSRF-Token"] = csrf

    @property
    def token(self):
        return self.s.cookies.get("TOKEN")

    def speakers(self):
        r = self.s.get(f"{self.api}/bootstrap", timeout=20)
        if not r.ok or "json" not in r.headers.get("Content-Type", ""):
            sys.exit(f"bootstrap failed: HTTP {r.status_code} {r.headers.get('Content-Type')} "
                     f"{r.text[:300]!r}")
        return r.json().get("speakers", [])

    def set_volume(self, speaker_id, vol):
        self.s.patch(f"{self.api}/speakers/{speaker_id}", json={"volume": vol}, timeout=15).raise_for_status()

    def connect(self, speaker_id):
        ws = websocket.create_connection(
            f"wss://{self.host}/proxy/protect/ws/talkback?speaker={speaker_id}",
            header=[f"Cookie: TOKEN={self.token}"],
            sslopt={"cert_reqs": ssl.CERT_NONE},
            origin=f"https://{self.host}", timeout=15)
        time.sleep(0.4)  # let the speaker arm
        check_closed(ws)
        return ws


def check_closed(ws):
    """Protect rejects talkback by closing the socket right after the handshake; surface that."""
    ws.settimeout(0.001)
    try:
        op, data = ws.recv_data(control_frame=True)
    except websocket.WebSocketTimeoutException:
        return
    except websocket.WebSocketConnectionClosedException:
        raise ConnectionError("talkback socket closed by server")
    finally:
        ws.settimeout(15)
    if op == websocket.ABNF.OPCODE_CLOSE:
        code = int.from_bytes(data[:2], "big") if len(data) >= 2 else None
        if code == 4403:
            sys.exit("talkback refused (4403): this account lacks Protect permission to use the speaker")
        raise ConnectionError(f"talkback socket closed by server (code {code})")


def playlist(source, shuffle, loop):
    if source.startswith(("http://", "https://")):
        while True:
            yield source
            if not loop:
                return
    p = Path(source)
    files = sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXT) if p.is_dir() else [p]
    if not files:
        sys.exit(f"no audio files in {source}")
    while True:
        order = files[:]
        if shuffle:
            random.shuffle(order)
        yield from (str(f) for f in order)
        if not loop:
            return


# Stereo -> mono for the horn. "mid" (L+R) cancels anything mixed out of phase between the
# channels, which on wide mixes can take vocals/choir with it; "blend" weights them 65/35 instead;
# "left" just drops the right channel.
DOWNMIX = {
    "blend": "pan=mono|c0=0.65*c0+0.35*c1",
    "mid": "pan=mono|c0=0.5*c0+0.5*c1",
    "left": "pan=mono|c0=c0",
}


def adts_frames(src, gain_db, downmix="blend"):
    """Transcode src with ffmpeg and yield AAC-LC ADTS frames (24 kHz mono) as they are produced."""
    fc = f"[0:a]aformat=channel_layouts=stereo,{DOWNMIX[downmix]},adelay=900:all=1"
    if gain_db:
        fc += f",volume={gain_db}dB"
    fc += "[out]"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if src.startswith(("http://", "https://")):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    cmd += ["-i", src, "-filter_complex", fc, "-map", "[out]", "-c:a", "aac", "-profile:a", "aac_low",
            "-ar", "24000", "-ac", "1", "-b:a", "32k", "-f", "adts", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            i, n = 0, len(buf)
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
                yield buf[i:i + flen]
                i += flen
            buf = buf[i:]
    finally:
        proc.kill()
        proc.wait()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="Protect console address (default: UNIFI_HOST from env or --creds-file)")
    ap.add_argument("--creds-file")
    ap.add_argument("--speaker", help="speaker name (substring) or id; default: the only speaker")
    ap.add_argument("--source", help="audio file, directory of files, or stream URL")
    ap.add_argument("--shuffle", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--volume", type=int, help="set speaker volume 0-100 before playing")
    ap.add_argument("--gain-db", type=float, default=0, help="software gain applied by ffmpeg")
    ap.add_argument("--downmix", choices=sorted(DOWNMIX), default="blend",
                    help="stereo-to-mono method (default blend; mid can cancel vocals on wide mixes)")
    ap.add_argument("--list", action="store_true", help="list speakers and exit")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    host, user, pw = load_creds(args.creds_file)
    host = args.host or host
    if not host:
        sys.exit("set UNIFI_HOST (env or --creds-file) or pass --host")
    pr = Protect(host, user, pw)
    pr.login()
    spk = pr.speakers()
    if args.list:
        for s in spk:
            tb = s.get("talkbackSettings", {})
            print(s["id"], s.get("mac"), repr(s.get("name")), "volume", s.get("volume"),
                  "state", s.get("state"), "fw", s.get("firmwareVersion"), "talkback", tb)
        return
    if not args.source:
        ap.error("--source is required")

    if args.speaker:
        spk = [s for s in spk if args.speaker in (s["id"], s.get("mac")) or
               args.speaker.lower() in (s.get("name") or "").lower()]
    if len(spk) != 1:
        sys.exit(f"need exactly one matching speaker, found {len(spk)}; use --list / --speaker")
    speaker = spk[0]
    log("speaker", speaker.get("name"), speaker["id"])
    if args.volume is not None:
        pr.set_volume(speaker["id"], max(0, min(100, args.volume)))

    ws = None
    for track in playlist(args.source, args.shuffle, args.loop):
        if stop:
            break
        log("playing", track)
        t0, n = None, 0
        for frame in adts_frames(track, args.gain_db, args.downmix):
            if stop:
                break
            if t0 is None:
                t0 = time.perf_counter()
            delay = t0 + n * FRAME_DT - LEAD - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -2:  # source stalled (e.g. radio rebuffer); resync the clock
                t0, n = time.perf_counter() + LEAD, 0
            for attempt in range(3):
                try:
                    if ws is None:
                        ws = pr.connect(speaker["id"])
                    ws.send_binary(frame)
                    if n % 50 == 0:
                        check_closed(ws)
                    break
                except Exception as e:
                    log("websocket error:", e, "- reconnecting")
                    try:
                        ws and ws.close()
                    except Exception:
                        pass
                    ws = None
                    time.sleep(1 + attempt)
                    try:
                        pr.login()
                    except Exception as le:
                        log("login failed:", le)
            n += 1
    if ws:
        time.sleep(0.3)
        ws.close()
    log("stopped")


if __name__ == "__main__":
    main()
