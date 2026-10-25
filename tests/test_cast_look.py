"""Cast looks (SB-11): a member's look text joins the prompt of every shot that names them, and a deleted
look picture leaves the cast member the way a deleted reference leaves a shot."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_klein import add_pictures, send
from test_server import App, Client, app, client, project  # noqa: F401  (server fixtures)
from vc.runners import Runners
from vc.sequence import cast_looks

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
SEQUENCE_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "js" / "lib" / "sequence.js"

CAST = [
    {"id": "p_ming00001", "name": "小明", "look": "短黑髮，藍色制服"},
    {"id": "p_hua000001", "name": "小華", "look": ""},  # no look: nothing to add
    {"id": "p_auntie001", "name": "阿姨", "look": "紅外套"},
    {"id": "p_blank0001", "name": " ", "look": "不該出現"},  # no name: never matches
]
SHOT = {"prompt": "小明和小華在海邊奔跑", "imagePrompt": "", "motion": "向前跑", "camera": "推近", "negative": ""}
PROJECT = {"style": {"prompt": "電影感", "negative": ""}, "cast": CAST}


def test_named_cast_looks_go_between_the_shot_text_and_the_style() -> None:
    assert cast_looks(PROJECT, SHOT["prompt"]) == "小明：短黑髮，藍色制服"
    image, _ = Runners._prompts("image", PROJECT, SHOT, {}, {})
    assert image == "小明和小華在海邊奔跑\n小明：短黑髮，藍色制服\n電影感"
    video, _ = Runners._prompts("video", PROJECT, SHOT, {}, {})
    assert video == "小明和小華在海邊奔跑\n向前跑\n推近\n小明：短黑髮，藍色制服\n電影感"
    # the shot's own picture prompt is sent as written
    own = {**SHOT, "imagePrompt": "照這段畫"}
    assert Runners._prompts("image", PROJECT, own, {}, {})[0] == "照這段畫"


@needs_node
def test_the_web_mirror_composes_the_same_looks() -> None:
    script = ("const m = await import(process.argv[1]);"
              "console.log(JSON.stringify(m.castLooks(JSON.parse(process.argv[2]), process.argv[3])));")
    texts = [SHOT["prompt"], "阿姨與小明", "沒有人物"]
    for text in texts:
        proc = subprocess.run(["node", "--input-type=module", "-e", script, SEQUENCE_JS.as_uri(),
                               json.dumps(PROJECT, ensure_ascii=False), text],
                              capture_output=True, check=True, text=True, encoding="utf-8")
        assert json.loads(proc.stdout) == cast_looks(PROJECT, text)


def test_look_fields_save_and_a_deleted_look_picture_is_cleared_until_undo(app: App, client: Client,
                                                                           project: dict) -> None:
    pid = project["id"]
    (picture,) = add_pictures(app, client, pid, 1)
    member = {"id": "p_ming00001", "name": "小明", "look": "短黑髮", "lookRef": picture}
    send(client, pid, {"op": "cast.add", "item": member})
    send(client, pid, {"op": "cast.update", "id": member["id"], "patch": {"lookRef": "not an id"}}, status=400)

    def saved() -> dict:
        return client.json("GET", f"/api/projects/{pid}")["project"]["cast"][0]

    assert saved()["look"] == "短黑髮" and saved()["lookRef"] == picture
    send(client, pid, {"op": "media.delete", "id": picture})
    assert saved()["lookRef"] is None
    client.json("POST", f"/api/projects/{pid}/undo")
    assert saved()["lookRef"] == picture
