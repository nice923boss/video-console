"""Pre-export check (EX-08): what the exported video would get wrong, by the same rules as the export."""
from __future__ import annotations

from pathlib import Path

from test_timeline_keys import make_project, with_video
from vc.exportcheck import export_check


def touch(root: Path, rel: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(b"x")


def test_a_clean_project_has_nothing_to_report(tmp_path: Path) -> None:
    p = with_video(make_project(), 4.0)  # two 4 s shots: an 8 s video
    touch(tmp_path, p["takes"]["t_vid00000"]["file"])
    c = export_check(p, tmp_path)
    assert c["duration"] == 8.0
    assert (c["empty"], c["missing"], c["disabled"], c["missingAudio"], c["missingOverlays"]) == ([], [], [], [], [])
    assert (c["audioPastEnd"], c["captionsPastEnd"], c["videoHidden"]) == (0, 0, False)


def test_shots_are_listed_by_their_shot_list_number(tmp_path: Path) -> None:
    p = make_project()
    assert export_check(p, tmp_path)["empty"] == [1, 2]
    p = with_video(make_project(), 4.0)  # two 4 s shots: an 8 s video
    p["shots"][1]["disabled"] = True
    c = export_check(p, tmp_path)  # the take's file was never written
    assert c["missing"] == [1, 2] and c["disabled"] == [2] and c["empty"] == []


def test_missing_files_and_items_past_the_end(tmp_path: Path) -> None:
    p = with_video(make_project(), 4.0)  # two 4 s shots: an 8 s video
    touch(tmp_path, p["takes"]["t_vid00000"]["file"])
    p["media"] = {
        "m_music000": {"id": "m_music000", "kind": "audio", "file": "media/import/song.mp3", "duration": 30.0},
        "m_quiet000": {"id": "m_quiet000", "kind": "audio", "file": "media/import/quiet.mp3", "duration": 2.0},
        "m_pic00000": {"id": "m_pic00000", "kind": "image", "file": "media/import/logo.png"},
    }
    p["audio"] = [
        {"id": "a_music000", "mediaId": "m_music000", "start": 0.0},  # 30 s of music over an 8 s video
        {"id": "a_quiet000", "mediaId": "m_quiet000", "start": 1.0, "muted": True},
    ]
    p["overlays"] = [{"id": "o_pic00000", "mediaId": "m_pic00000", "layer": "l_x", "start": 0.0, "length": 2.0}]
    p["captions"] = [{"id": "c_1", "text": "結尾", "start": 7.0, "end": 9.0},
                     {"id": "c_2", "text": "", "start": 7.0, "end": 12.0}]
    p["tracks"] = {"videoHidden": True}
    c = export_check(p, tmp_path)
    # The muted clip is not in the mix, so its missing file does not matter.
    assert c["missingAudio"] == ["song.mp3"] and c["missingOverlays"] == ["logo.png"]
    assert (c["audioPastEnd"], c["captionsPastEnd"], c["videoHidden"]) == (1, 1, True)
    p["tracks"] = {"musicMuted": True}
    assert export_check(p, tmp_path)["missingAudio"] == []
