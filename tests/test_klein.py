"""FLUX3-Klein, the default local picture route: text to image, several reference pictures in one job, the
layout poster that draws lettering on purpose, and the shot's extra references kept in step with deletes."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from test_comfy import app, client, fake, project, submit_image  # noqa: F401  (shared fixtures)
from test_comfy import FakeComfy
from test_server import Client, png_bytes
from vc.app import App
from vc.notext import GUARD, with_guard
from vc.settings import DEFAULTS

COLORS = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30)]


@pytest.fixture()
def klein(app: App) -> App:
    """The shared app with the shipped routes back (test_comfy pins Z-Image for its older tests)."""
    app.settings.update({"routes": copy.deepcopy(DEFAULTS["routes"])})
    return app


def add_pictures(app: App, client: Client, pid: str, count: int) -> list[str]:
    """Imported pictures m_ref00000a, m_ref00000b... each a different colour, so uploads can be told apart."""
    root = app.store.project_dir(pid)
    ids = []
    for i in range(count):
        mid = f"m_ref00000{'abcd'[i]}"
        (root / f"media/import/{mid}.png").write_bytes(png_bytes(rgb=COLORS[i]))
        media = {"id": mid, "kind": "image", "name": f"{mid}.png", "file": f"media/import/{mid}.png",
                 "width": 320, "height": 180}
        send(client, pid, {"op": "media.add", "media": media})
        ids.append(mid)
    return ids


def send(client: Client, pid: str, *ops: dict, status: int = 200) -> Any:
    return client.json("POST", f"/api/projects/{pid}/ops", {"ops": list(ops)}, status=status)


def shot_of(client: Client, pid: str) -> dict:
    return client.json("GET", f"/api/projects/{pid}")["project"]["shots"][0]


def test_the_local_route_draws_with_flux3_klein_by_default(klein: App, client: Client, project: dict,
                                                           fake: FakeComfy) -> None:
    image = DEFAULTS["routes"][0]["image"]
    assert (image["workflow"], image["workflowI2I"]) == ("flux3-klein-t2i", "flux3-klein-edit")
    done = client.wait_job(submit_image(client, project)["id"], timeout=30)
    assert done["status"] == "done", done["error"]

    [queued] = fake.prompts
    graph = queued["prompt"]
    assert graph["1"]["inputs"]["unet_name"] == "flux3-image-klein-int8-convrot.safetensors"
    assert graph["5"]["inputs"]["text"] == with_guard("清晨漁港，薄霧，漁船")
    assert "title card" in graph["6"]["inputs"]["text"]
    assert graph["10"]["inputs"]["filename_prefix"].startswith(f"vc/{done['id']}-")
    [take] = client.json("GET", f"/api/projects/{project['id']}")["project"]["takes"].values()
    assert take["source"]["workflow"] == "flux3-klein-t2i"


def test_every_reference_picture_is_uploaded_and_wired_in_order(klein: App, client: Client, project: dict,
                                                                fake: FakeComfy) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    first, second, third = add_pictures(klein, client, pid, 3)
    send(client, pid, {"op": "shot.update", "id": sid, "patch": {"ref": first, "extraRefs": [second, third]}})
    done = client.wait_job(submit_image(client, project, "第一張圖的人物站在第二張圖的森林裡")["id"], timeout=30)
    assert done["status"] == "done", done["error"]
    assert done["payload"]["workflow"] == "flux3-klein-edit"
    assert done["payload"]["images"] == [first, second, third]
    assert done["payload"]["denoise"] is None  # Klein redraws the whole frame: no reference strength

    uploads = [body for method, path, body in fake.calls if path == "/upload/image"]
    assert [png_bytes(rgb=c) in body for body, c in zip(uploads, COLORS)] == [True, True, True]
    [queued] = fake.prompts
    graph = queued["prompt"]
    loads = sorted(nid for nid, node in graph.items() if node["class_type"] == "LoadImage")
    assert loads == ["7_images0", "7_images1", "7_images2"]
    assert [graph["7"]["inputs"][f"images.image{i}"] for i in range(3)] == [[n, 0] for n in loads]


def test_a_one_picture_template_reads_the_first_reference_and_says_so(klein: App, client: Client,
                                                                      project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    first, second = add_pictures(klein, client, pid, 2)
    send(client, pid, {"op": "shot.update", "id": sid, "patch": {"ref": first, "extraRefs": [second]}})
    job = klein.runners.prepare("image", pid, sid, {"workflow": "zimage-turbo-i2i", "prompt": "漁港黃昏"})
    assert "images" not in job["payload"] and job["payload"]["image"] == first
    assert "範本「Z-Image Turbo 參考圖生圖」只讀 1 張參考圖，額外參考圖已略過" in job["notes"]
    assert job["payload"]["denoise"] is not None  # Z-Image keeps its reference strength


def test_the_layout_poster_keeps_its_lettering_and_checks_the_element_list(klein: App, client: Client,
                                                                          project: dict, fake: FakeComfy) -> None:
    prompt = "夏日特賣海報，標題字卡寫 SUMMER SALE <obj_1>"
    layout = json.dumps([{"type": "text", "text": "SUMMER SALE", "bbox": [80, 100, 260, 900]}])
    done = client.wait_job(submit_image(client, project, prompt, workflow="flux3-klein-layout",
                                        params={"layout": layout}, negative="blurry")["id"], timeout=30)
    assert done["status"] == "done", done["error"]
    [queued] = fake.prompts
    graph = queued["prompt"]
    assert graph["7"]["inputs"]["prompt"] == prompt  # no title direction dropped, no guard added
    assert GUARD not in json.dumps(graph, ensure_ascii=False)
    assert graph["9"]["inputs"]["text"] == "blurry"
    assert graph["5"]["inputs"]["value"] == layout

    broken = {"kind": "image", "pid": project["id"], "shotId": project["shots"][0]["id"],
              "payload": {"prompt": prompt, "workflow": "flux3-klein-layout", "params": {"layout": "[{\"type\": }"}}}
    reply = client.json("POST", "/api/jobs", broken, status=400)
    assert "元素表不是正確的 JSON" in json.dumps(reply, ensure_ascii=False)
    assert len(fake.prompts) == 1


def test_extra_references_are_checked_and_follow_deletes_and_undo(klein: App, client: Client, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    first, second, third = add_pictures(klein, client, pid, 3)
    too_many = [f"m_more0000{i}" for i in range(10)]
    reply = send(client, pid, {"op": "shot.update", "id": sid, "patch": {"extraRefs": too_many}}, status=400)
    assert "參考圖最多 10 張" in json.dumps(reply, ensure_ascii=False)
    send(client, pid, {"op": "shot.update", "id": sid, "patch": {"extraRefs": ["not an id"]}}, status=400)

    send(client, pid, {"op": "shot.update", "id": sid,
                       "patch": {"ref": first, "extraRefs": [second, third, second]}})
    assert shot_of(client, pid)["extraRefs"] == [second, third]  # duplicates dropped, order kept

    send(client, pid, {"op": "media.delete", "id": second})
    assert shot_of(client, pid)["extraRefs"] == [third]
    client.json("POST", f"/api/projects/{pid}/undo")
    assert shot_of(client, pid)["extraRefs"] == [second, third]
