from datetime import datetime

from app.audio import FRAME_DT, ffmpeg_cmd, split_adts
from app.scheduler import is_active


def sched(**kw):
    s = {"enabled": 1, "days": "0123456", "start_time": "17:00", "end_time": "21:00",
         "start_date": None, "end_date": None}
    s.update(kw)
    return s


def test_window_same_day():
    assert is_active(sched(), datetime(2026, 10, 31, 17, 0))
    assert is_active(sched(), datetime(2026, 10, 31, 20, 59))
    assert not is_active(sched(), datetime(2026, 10, 31, 21, 0))
    assert not is_active(sched(), datetime(2026, 10, 31, 16, 59))


def test_window_overnight_belongs_to_start_day():
    s = sched(start_time="22:00", end_time="02:00", days="4")  # Friday nights only
    assert is_active(s, datetime(2026, 10, 30, 23, 0))         # Fri 23:00
    assert is_active(s, datetime(2026, 10, 31, 1, 0))          # Sat 01:00, still Friday's window
    assert not is_active(s, datetime(2026, 10, 31, 23, 0))     # Sat 23:00
    assert not is_active(s, datetime(2026, 10, 30, 12, 0))


def test_date_range_and_disabled():
    s = sched(start_date="2026-10-31", end_date="2026-10-31")
    assert is_active(s, datetime(2026, 10, 31, 18, 0))
    assert not is_active(s, datetime(2026, 11, 1, 18, 0))
    assert not is_active(sched(enabled=0), datetime(2026, 10, 31, 18, 0))


def adts_frame(payload_len):
    flen = 7 + payload_len
    hdr = bytes([0xFF, 0xF1, 0x60, 0x40 | (flen >> 11), (flen >> 3) & 0xFF, ((flen & 7) << 5) | 0x1F, 0xFC])
    return hdr + b"\x00" * payload_len


def test_split_adts_keeps_partial_frame():
    a, b = adts_frame(10), adts_frame(20)
    frames, rest = split_adts(b"junk" + a + b[:5])
    assert frames == [a]
    frames, rest = split_adts(rest + b[5:])
    assert frames == [b] and rest == b""


def test_ffmpeg_cmd_url_is_protocol_restricted_and_file_seeks():
    url = ffmpeg_cmd("https://example.com/s")
    assert "-protocol_whitelist" in url and "-ss" not in url
    f = ffmpeg_cmd("/data/music/a.mp3", seek=12.5)
    assert f[f.index("-ss") + 1] == "12.50"
    assert abs(FRAME_DT - 0.042667) < 1e-5


def test_mono_sources_skip_the_downmix():
    assert "pan=" not in " ".join(ffmpeg_cmd("/data/x.wav", mono=True))
    assert "pan=mono|c0=0.65*c0+0.35*c1" in " ".join(ffmpeg_cmd("/data/x.wav"))
    assert "aphaseshift" not in " ".join(ffmpeg_cmd("/data/x.wav"))


def test_channels_detects_mono_and_stereo(tmp_path):
    import asyncio
    import subprocess
    from app.audio import channels
    for n in (1, 2):
        p = tmp_path / f"c{n}.wav"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=0.2",
                        "-ac", str(n), str(p)], check=True)
        assert asyncio.run(channels(p)) == n
