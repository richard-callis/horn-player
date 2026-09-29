# horn-player

Play internet radio, playlists, sound effects and spoken announcements on UniFi Protect
speakers (e.g. the AI Horn Speaker), with schedules and a small web UI.

Audio goes over Protect's **private** talkback websocket, the same path the Protect app uses
for push-to-talk. Ubiquiti can change it in any release.

## How it works

- `app/protect.py`: logs in to the Protect console, discovers speakers, opens
  `wss://<console>/proxy/protect/ws/talkback?speaker=<id>`. Protect closes the socket with
  code **4403** right after the handshake if the account may not use the speaker.
- `app/audio.py`: ffmpeg transcodes any file or stream to AAC-LC/ADTS, 24 kHz mono, one
  frame per websocket message, paced at 1024/24000 s per frame. Stereo is folded to mono
  with a 90° phase shift on one channel so out-of-phase vocals don't cancel.
- `app/player.py`: one engine per speaker. Clips and announcements interrupt the music,
  which then resumes (playlists at the same point in the track).
- `app/tts.py`: announcements are rendered locally with [Piper](https://github.com/rhasspy/piper)
  and streamed like a clip, so they need no extra Protect permissions. Protect's built-in
  voice is also available but needs rights to run automations.
- `app/scheduler.py`: time windows per speaker (days, times, optional date range; windows may
  cross midnight). A schedule only stops what it started.

## Protect account

Use a dedicated **local** account on the console that runs Protect. A custom role with
**Live** access to the speaker is enough for streaming and Piper announcements.

## Configuration

| Variable | Default | |
|---|---|---|
| `UNIFI_HOST` | — | Protect console (UNVR / UDM) address |
| `UNIFI_USER`, `UNIFI_PASS` | — | local Protect account |
| `DATA_DIR` | `/data` | SQLite DB, uploaded playlists and clips |
| `REQUIRE_AUTH` | `true` | reject requests without `AUTH_HEADER` |
| `AUTH_HEADER` | `X-authentik-username` | identity header set by the auth proxy |
| `DOWNMIX` | `quad` | `quad`, `mid` or `left` |
| `TZ` | UTC | timezone schedules are evaluated in |
| `MAX_UPLOAD_MB` / `MAX_REQUEST_MB` | `200` / `512` | per-file and per-request upload caps |
| `ALLOW_PRIVATE_STREAMS` | `false` | allow station URLs that resolve to private/internal addresses |

## Security notes

- The app trusts `AUTH_HEADER`, so it must only be reachable through a proxy that sets it and
  overwrites any client-supplied value (Traefik's forward-auth does, when the header is listed in
  `authResponseHeaders`). Anything that can reach the pod directly can claim any identity; without
  an enforcing NetworkPolicy that includes other pods in the cluster.
- Writes from another site are refused by an `Origin` check, since the auth proxy's session
  cookie would otherwise ride along.
- Station URLs are fetched server-side. Hosts resolving to private or internal addresses are
  refused when a station is added (best effort; redirects and later DNS changes aren't re-checked).

## Development

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest -q tests
./run-local.sh        # reads credentials from horn.env (UNIFI_HOST=…, UNIFI_USER=…, UNIFI_PASS=…)
```

`tools/horn_stream.py` is a standalone CLI that streams one file or URL to a speaker.
