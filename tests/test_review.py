"""SB-13 client review (review.py): the single-file review page of the sequence and the comments file it
downloads, read back as comments on the shots in one undo step."""
from __future__ import annotations

import json
import re

import pytest

from test_captions import _project
from test_server import Client, app, client, project  # noqa: F401  (server fixtures)
from vc import ops
from vc.review import render_review, review_ops
from vc.util import AppError

META_RE = re.compile(r'<script type="application/json" id="vc-review-meta">(.*?)</script>', re.S)


def _with_shots() -> dict:
    p = _project()
    p["name"] = "港口</script>短片"
    ops.apply_ops(p, [
        {"op": "shot.add", "shot": {"id": "s_rev000001", "name": "開場", "duration": 1, "prompt": "清晨的港口"}},
        {"op": "shot.add", "shot": {"id": "s_rev000002", "duration": 1}},
        {"op": "shot.add", "shot": {"id": "s_rev000003", "name": "備用", "duration": 2, "inSequence": False}},
    ], {})
    return p


def _comments(p: dict, **extra: object) -> dict:
    return {"vc": "review-comments", "version": 1, "project": p["id"], "name": p["name"], "by": "陳經理",
            "at": "2026-10-25T10:00:00.000Z", "note": "", "shots": [], **extra}


def test_page_lists_the_sequence_and_carries_its_meta() -> None:
    p = _with_shots()
    page = render_review(p, lambda shot: "data:image/png;base64,AA" if shot["id"] == "s_rev000001" else None, "港口")
    assert page.count("</script>") == 2  # the name cannot close the meta block early
    meta = json.loads(META_RE.search(page).group(1))
    assert meta["vc"] == "review" and meta["project"] == p["id"] and meta["name"] == p["name"]
    assert meta["shots"] == {"s_rev000001": {"no": 1, "name": "開場"}, "s_rev000002": {"no": 2, "name": "鏡頭 2"}}
    assert meta["file"] == "港口_審稿意見_{by}.json"
    assert re.findall(r'data-shot="([^"]+)"', page) == ["s_rev000001", "s_rev000002"]  # out of the sequence: left out
    assert 'src="data:image/png;base64,AA"' in page and "尚無畫面" in page
    first, second = page.split('<article class="card">')[1:]
    assert "清晨的港口" in first
    assert "第一句" in first and "第二句" not in first and "第二句" in second  # each clip's subtitles by time


def test_comments_land_on_their_shots_and_a_second_read_adds_nothing() -> None:
    p = _with_shots()
    data = _comments(p, note="節奏再快一點", shots=[
        {"id": "s_rev000001", "no": 1, "name": "開場", "text": " 天空再亮一點 "},
        {"id": "s_rev000002", "no": 2, "name": "鏡頭 2", "text": "  "},
        {"id": "s_gone00001", "no": 3, "name": "結尾", "text": "字太小"},
    ])
    batch, counts = review_ops(p, data)
    assert counts == {"shots": 1, "general": 1, "missing": 1, "duplicate": 0}
    inverse = ops.apply_ops(p, batch, {})
    assert p["shots"][0]["review"] == [{"by": "陳經理", "at": "2026-10-25T10:00:00.000Z", "text": "天空再亮一點"}]
    assert "review" not in p["shots"][1]
    assert [c["text"] for c in p["review"]] == ["（已刪除的鏡頭 #03 結尾）字太小", "節奏再快一點"]
    again, counts = review_ops(p, data)
    assert again == [] and counts["duplicate"] == 3
    ops.apply_ops(p, inverse, {})
    assert "review" not in p["shots"][0] and p["review"] == []


@pytest.mark.parametrize("data, words", [
    ({"vc": "review", "project": "p_test000001"}, "不是審稿網頁"),
    ([1, 2], "不是審稿網頁"),
    ({"vc": "review-comments", "project": "p_other00001", "name": "別的片"}, "「別的片」"),
    ({"vc": "review-comments", "project": "p_test000001", "shots": {"a": 1}}, "格式不正確"),
])
def test_other_files_are_refused(data: object, words: str) -> None:
    with pytest.raises(AppError, match=words):
        review_ops(_with_shots(), data)


def test_stored_comments_are_checked() -> None:
    p = _with_shots()
    ops.apply_ops(p, [{"op": "shot.update", "id": "s_rev000001", "patch": {"review": [
        {"by": "x" * 99, "at": 5, "text": "  好  "}, {"by": "y", "text": ""}]}}], {})
    assert p["shots"][0]["review"] == [{"by": "x" * 60, "at": "5", "text": "好"}]
    with pytest.raises(AppError, match="最多 200 則"):
        ops.apply_ops(p, [{"op": "project.update", "patch": {"review": [{"text": "a"}] * 201}}], {})


def test_download_and_import_over_http(client: Client, project: dict) -> None:
    pid = project["id"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.add", "shot": {"id": "s_rev000001", "name": "開場", "duration": 2}}]})
    code, headers, body = client.request("GET", f"/api/projects/{pid}/review.html")
    assert code == 200 and headers["content-type"].startswith("text/html")
    assert "attachment" in headers["content-disposition"] and "%E5%AF%A9%E7%A8%BF.html" in headers["content-disposition"]
    assert 'data-shot="s_rev000001"' in body.decode("utf-8")

    fresh = client.json("GET", f"/api/projects/{pid}")["project"]
    data = _comments(fresh, shots=[{"id": "s_rev000001", "no": 1, "name": "開場", "text": "改成黃昏"}])
    raw = ("﻿" + json.dumps(data, ensure_ascii=False)).encode("utf-8")  # a BOM from a Windows editor is fine
    got = client.json("POST", f"/api/projects/{pid}/review", raw)
    assert got["shots"] == 1 and got["duplicate"] == 0 and got["by"] == "陳經理"
    shot = client.json("GET", f"/api/projects/{pid}")["project"]["shots"][-1]
    assert [c["text"] for c in shot["review"]] == ["改成黃昏"]

    code, _, body = client.request("POST", f"/api/projects/{pid}/review", raw)
    assert code == 400 and "已經匯入過了" in json.loads(body)["error"]
    code, _, body = client.request("POST", f"/api/projects/{pid}/review", b"not json")
    assert code == 400 and "不是有效的 JSON" in json.loads(body)["error"]

    client.json("POST", f"/api/projects/{pid}/undo")
    shot = client.json("GET", f"/api/projects/{pid}")["project"]["shots"][-1]
    assert "review" not in shot
