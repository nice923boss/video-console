"""GR-10 title cards (lib/title-templates.js): each template part becomes a caption with its own pinned style,
burnt in like any subtitle but left out of the .srt and kept when an .srt replaces the subtitles."""
from __future__ import annotations

import re
import shutil

import pytest

from test_captions import _project
from test_server import Client, app, client, project  # noqa: F401  (server fixtures)
from test_timeline_keys import JS, run_js
from vc import ops
from vc.captions import caption_ass
from vc.export import captions_srt
from vc.stt import srt_ops

TITLES = JS / "lib" / "title-templates.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def every_template(project: dict) -> dict:
    """titleOps for each template with its sample words, one after another, against `project`."""
    return run_js(TITLES, """console.log(JSON.stringify(Object.fromEntries(m.TEMPLATES.map((t, i) =>
      [t.id, m.titleOps(data, t, m.startValues(t), i * 10)]))));""", project)


@needs_node
def test_every_template_is_valid_and_its_parts_stay_where_they_are_put() -> None:
    p = _project()
    p["captions"] = []
    made = every_template(p)
    for res in made.values():
        ops.apply_ops(p, res["list"], {})  # the server's validation takes every style and caption
    assert "captionStyle" not in p["settings"]  # never the project default
    names = [s["name"] for s in p["captionStyles"]]
    assert len(names) == len(set(names))
    assert all(c["title"] is True and c["position"] == "middle" for c in p["captions"])
    _, events = caption_ass(p, p["captions"], 1920, 52)
    for cap in p["captions"]:
        style = next(s for s in p["captionStyles"] if s["id"] == cap["style"])
        want = f"\\pos({round(style['x'] * 1920)},{round(style['y'] * 1080)})"
        lines = [e for e in events if e.startswith("Dialogue:") and cap["text"] in e
                 and e.split(",")[1] == f"0:00:{cap['start']:05.2f}"]
        assert lines and all(want in e for e in lines), (cap["text"], want, lines[:1])  # not moved by stacking


@needs_node
def test_same_look_reuses_its_style_and_new_colours_make_another() -> None:
    got = run_js(TITLES, """
      const t = m.TEMPLATES.find((x) => x.id === "lower-box");
      const first = m.titleOps({ captionStyles: [] }, t, m.startValues(t), 12.3456);
      const styles = first.list.filter((o) => o.op === "captionStyle.add").map((o) => o.item);
      const again = m.titleOps({ captionStyles: styles }, t, m.startValues(t), 20, 2);
      const v = m.startValues(t);
      v.name = { ...v.name, color: "#ff0000" };
      v.role = { ...v.role, text: "  " };
      const red = m.titleOps({ captionStyles: styles }, t, v, 20);
      const empty = m.titleOps({ captionStyles: [] }, t, { name: { text: " " }, role: { text: "" } }, 0);
      const full = m.titleOps({ captionStyles: Array.from({ length: 50 }, (_, i) => ({ name: `s${i}` })) }, t, m.startValues(t), 0);
      console.log(JSON.stringify({ first, again, red, empty, full }));""", None)
    first = got["first"]
    assert [o["op"] for o in first["list"]] == ["captionStyle.add"] * 2 + ["caption.add"] * 2
    caps = [o["item"] for o in first["list"] if o["op"] == "caption.add"]
    assert [(c["start"], c["end"], c["text"]) for c in caps] == [(12.346, 17.346, "王小明"), (12.346, 17.346, "產品經理")]
    assert first["first"] == caps[0]["id"]
    assert [o["op"] for o in got["again"]["list"]] == ["caption.add"] * 2  # both looks already there
    assert [o["item"]["end"] for o in got["again"]["list"]] == [22, 22]
    red = got["red"]["list"]
    assert [o["op"] for o in red] == ["captionStyle.add", "caption.add"]  # empty role is skipped
    assert red[0]["item"]["color"] == "#ff0000" and red[0]["item"]["name"] == "下標字卡（色塊）：姓名 2"
    assert got["empty"] == {"error": "請輸入標題文字"}
    assert "最多 50 個" in got["full"]["error"]


def test_titles_stay_out_of_the_srt_and_an_srt_replace_keeps_them() -> None:
    p = _project()
    ops.apply_ops(p, [{"op": "caption.add", "item": {"id": "c_title0001", "start": 0, "end": 3, "text": "影片標題",
                                                     "position": "middle", "title": True}}], {})
    assert next(c for c in p["captions"] if c["id"] == "c_title0001")["title"] is True
    srt = captions_srt(p)
    assert "第一句" in srt and "影片標題" not in srt
    _, events = caption_ass(p, p["captions"], 1920, 52)
    assert any("影片標題" in e for e in events)  # burnt in like the rest
    deleted = {o["id"] for o in srt_ops(p, "1\n00:00:05,000 --> 00:00:06,000\n新的\n", replace=True)
               if o["op"] == "caption.delete"}
    assert deleted == {"c_aaaaaa01", "c_aaaaaa02"}


def test_srt_download_leaves_titles_out(client: Client, project: dict) -> None:
    pid = project["id"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "caption.add", "item": {"id": "c_sub000001", "start": 1, "end": 2, "text": "一般字幕", "position": "bottom"}},
        {"op": "caption.add", "item": {"id": "c_title0001", "start": 0, "end": 3, "text": "片頭標題", "position": "middle",
                                       "title": True}}]})
    code, _, body = client.request("GET", f"/api/projects/{pid}/captions.srt")
    assert code == 200
    text = body.decode("utf-8-sig")
    assert "一般字幕" in text and "片頭標題" not in text
    assert re.match(r"1\n00:00:01,000 --> 00:00:02,000\n", text)
