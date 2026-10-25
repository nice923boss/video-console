"""Audio tracks: a clip's `track` puts it on the voice rows or on a music track it shares; split keeps it, export
mutes it with its row, a sound dropped on a track lands there unless the track already plays at that time, and
合併音軌 packs music tracks that never play together onto one row."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from test_server import App, Client, app, client, needs_ffmpeg, project  # noqa: F401  (server fixtures)
from test_timeline_keys import JS, VID, make_project, plan, run_js, with_video
from test_voice import wav_bytes
from vc import edits, localjobs
from vc.ops import apply_ops
from vc.util import AppError

T1 = "t_track0001"


def add(p: dict, aid: str, start: float, length: float = 2.0, **extra: object) -> dict:
    apply_ops(p, [{"op": "audio.add", "item": {"id": aid, "takeId": VID, "start": start, "in": 0, "out": length,
                                               **extra}}], {})
    return p


def test_track_takes_voice_or_an_id_and_split_keeps_it() -> None:
    p = add(add(with_video(make_project()), "a_aaaaaaaa", 0, track="voice"), "a_bbbbbbbb", 1, track=T1)
    add(p, "a_cccccccc", 4)
    assert [a.get("track") for a in p["audio"]] == ["voice", T1, None]
    with pytest.raises(AppError):
        add(p, "a_dddddddd", 0, track="Music 1")
    for t, aid in ((2, "a_bbbbbbbb"), (5, "a_cccccccc")):
        apply_ops(p, edits.plan(p, {"cmd": "split", "t": t, "type": "audio", "id": aid})["ops"], {})
    # Both halves stay on one row: the named track, or the first half's own track (its id).
    assert [(a["start"], a.get("track")) for a in p["audio"] if a.get("track") != "voice"] == [
        (1, T1), (2.0, T1), (4, None), (5.0, "a_cccccccc")]


def test_track_busy_only_where_a_clip_on_it_plays() -> None:
    p = add(add(with_video(make_project()), "a_aaaaaaaa", 0, track=T1), "a_bbbbbbbb", 4)
    assert edits.track_busy(p, T1, 1.0, 1.0)
    assert not edits.track_busy(p, T1, 2.0, 1.0)  # starts where the clip ends
    assert edits.track_busy(p, "a_bbbbbbbb", 3.0, 1.5)  # a clip without `track` is its own track
    assert not edits.track_busy(p, "a_bbbbbbbb", 1.0, 1.0)
    assert not edits.track_busy(p, "voice", 0.0, 10.0)  # the voice rows pack themselves


@pytest.mark.parametrize(("track", "tracks", "plays"), [
    ("voice", {}, True), ("voice", {"voiceMuted": True}, False), ("voice", {"musicMuted": True}, True),
    (T1, {"voiceMuted": True}, True), (T1, {"musicMuted": True}, False)])
def test_export_mutes_a_clip_with_its_row(tmp_path: Path, track: str, tracks: dict, plays: bool) -> None:
    p = add(with_video(make_project()), "a_aaaaaaaa", 0, track=track)
    p["tracks"] = tracks
    assert (["-i", f"media/takes/{VID}.mp4"] in plan(p, tmp_path)["inputs"]) is plays


def test_upload_refuses_a_bad_track(client: Client, project: dict) -> None:  # noqa: F811
    url = f"/api/projects/{project['id']}/upload?name=s.wav&track=1&audioTrack=Music%201"
    status, _, body = client.request("POST", url, wav_bytes(0.2))
    assert status == 400 and "音軌代號不正確" in body.decode("utf-8")


@needs_ffmpeg
def test_dropped_sound_lands_on_its_track_unless_it_plays_there(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    for start, track in ((0, T1), (1, T1), (2, T1), (0, "voice")):
        job = client.json("POST", f"/api/projects/{pid}/upload?name=s.wav&track=1&start={start}&audioTrack={track}",
                          wav_bytes(1.5))["job"]
        done = client.wait_job(job["id"])
        assert done["status"] == "done", done["error"]
    audio = client.json("GET", f"/api/projects/{pid}")["project"]["audio"]
    # The one at 1 s would play over the first (0 to 1.5 s), so it gets a track of its own.
    assert sorted(((a["start"], a.get("track")) for a in audio), key=str) == [
        (0.0, T1), (0.0, "voice"), (1.0, None), (2.0, T1)]


@needs_ffmpeg
def test_dropped_sound_picks_its_track_under_the_project_lock(app: App, client: Client, project: dict,  # noqa: F811
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # Two imports run at once (local:ffmpeg takes 2): checking the track and adding the clip must not interleave.
    pid = project["id"]
    held = []

    def spy(*args: object) -> bool:
        lock = app.store.locked(pid)
        free = []
        probe = threading.Thread(target=lambda: free.append(lock.acquire(blocking=False) and (lock.release() or True)))
        probe.start()
        probe.join()
        held.append(not free[0])
        return edits.track_busy(*args)

    monkeypatch.setattr(localjobs, "track_busy", spy)
    job = client.json("POST", f"/api/projects/{pid}/upload?name=s.wav&track=1&audioTrack={T1}", wav_bytes(0.5))["job"]
    assert client.wait_job(job["id"])["status"] == "done"
    assert held == [True]


@needs_ffmpeg
def test_import_gain_sets_the_clip_volume_and_the_library_default(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    for query in ("gain=1.9953&track=1", "gain=9&track=0", "track=1"):
        job = client.json("POST", f"/api/projects/{pid}/upload?name=s.wav&{query}", wav_bytes(0.5))["job"]
        done = client.wait_job(job["id"])
        assert done["status"] == "done", done["error"]
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    # +6 dB lands on the clip and the library item; past 4 (+12 dB) it is held at 4; no choice stays at 1.
    assert sorted(m.get("gain", 0) for m in saved["media"].values() if m["kind"] == "audio") == [0, 1.9953, 4.0]
    assert sorted(a["volume"] for a in saved["audio"]) == [1.0, 1.9953]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_merge_tracks_packs_tracks_that_never_play_together() -> None:
    media = {"m_snd00000": {"id": "m_snd00000", "kind": "audio", "duration": 10.0}}
    clip = lambda aid, start, length, **extra: {"id": aid, "mediaId": "m_snd00000", "start": start, "in": 0,  # noqa: E731
                                                "out": length, **extra}
    audio = [clip("a_one00000", 0, 2), clip("a_voice0000", 0, 5, track="voice"), clip("a_two00000", 1, 2),
             clip("a_four0000", 2.5, 1, track=T1), clip("a_three000", 3, 1), clip("a_five0000", 6, 1, track=T1)]
    got = run_js(JS / "lib" / "sequence.js", "console.log(JSON.stringify(m.mergeTracks(data)));",
                 {"media": media, "audio": audio})
    # T1 (2.5-3.5 and 6-7) fits beside the first track (0-2); the clip at 3-4 then clashes there and joins the second.
    assert got["tracks"] == 2
    assert [(o["id"], o["patch"]["track"]) for o in got["ops"]] == [
        ("a_four0000", "a_one00000"), ("a_five0000", "a_one00000"), ("a_three000", "a_two00000")]
    p = with_video(make_project())
    p["media"].update(media)
    apply_ops(p, [{"op": "audio.add", "item": a} for a in audio] + got["ops"], {})  # the server takes the ops
    got = run_js(JS / "lib" / "sequence.js", "console.log(JSON.stringify(m.mergeTracks(data)));", p)
    assert got == {"ops": [], "tracks": 2}  # already as packed as it gets


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_merge_tracks_keeps_background_music_apart() -> None:
    """AU-02: a sound effect never joins the 背景音樂 row (it would dip under voice), nor the music a 一般 row."""
    media = {"m_snd00000": {"id": "m_snd00000", "kind": "audio", "duration": 10.0}}
    audio = [{"id": aid, "mediaId": "m_snd00000", "start": start, "in": 0, "out": 1}
             for aid, start in (("a_bgm00000", 0), ("a_sfx00000", 2), ("a_sfx11111", 4), ("a_bgm11111", 6))]
    got = run_js(JS / "lib" / "sequence.js", "console.log(JSON.stringify(m.mergeTracks(data)));",
                 {"media": media, "audio": audio, "tracks": {"bgmRows": ["a_bgm00000", "a_bgm11111"]}})
    assert got["tracks"] == 2
    assert [(o["id"], o["patch"]["track"]) for o in got["ops"]] == [
        ("a_sfx11111", "a_sfx00000"), ("a_bgm11111", "a_bgm00000")]
