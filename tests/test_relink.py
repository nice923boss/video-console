"""Relink (LB-09): a library item points at a new file under the same id, its uses follow, and undo goes back."""
from __future__ import annotations

from test_klein import add_pictures, send
from test_server import App, Client, app, client, needs_ffmpeg, png_bytes, project  # noqa: F401  (server fixtures)


@needs_ffmpeg
def test_relink_swaps_the_file_for_the_item_and_its_takes(app: App, client: Client, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    picture, old = "m_relink000a", "media/import/m_relink000a.png"
    (app.store.project_dir(pid) / old).write_bytes(png_bytes())
    # Imports keep empty facts as None (a picture has no strip); undo has to put the item back regardless.
    send(client, pid, {"op": "media.add", "media": {"id": picture, "kind": "image", "name": "old.png", "file": old,
                                                    "strip": None, "width": 320, "height": 180}})
    take = {"id": "t_relink000a", "shot": sid, "kind": "image", "file": old, "width": 320, "height": 180,
            "created": 1.0, "source": {"backend": "import", "name": "old.png"}}
    send(client, pid, {"op": "take.add", "take": take},
         {"op": "shot.update", "id": sid, "patch": {"activeImage": take["id"]}})
    (app.store.project_dir(pid) / old).unlink()
    assert client.json("GET", f"/api/projects/{pid}/missing") == {"media": [picture]}

    job = client.json("POST", f"/api/projects/{pid}/upload?name=new.png&target=relink&media={picture}",
                      png_bytes(160, 90))["job"]
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    assert done["result"]["mediaIds"] == [picture]
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    item, swapped = saved["media"][picture], saved["takes"][take["id"]]
    assert item["file"] != old and item["file"] == swapped["file"]
    assert (item["width"], item["height"]) == (160, 90) and item["name"] == "old.png"
    assert saved["shots"][0]["activeImage"] == take["id"]
    assert client.json("GET", f"/api/projects/{pid}/missing") == {"media": []}

    client.json("POST", f"/api/projects/{pid}/undo")
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    assert saved["media"][picture]["file"] == old and saved["takes"][take["id"]]["file"] == old
    assert saved["media"][picture]["width"] == 320 and "strip" not in saved["media"][picture]
    assert client.json("GET", f"/api/projects/{pid}")["undo"]["redo"] == 1


def test_relink_needs_the_same_kind_of_file(app: App, client: Client, project: dict) -> None:
    pid = project["id"]
    (picture,) = add_pictures(app, client, pid, 1)
    client.json("POST", f"/api/projects/{pid}/upload?name=voice.wav&target=relink&media={picture}", b"RIFF",
                status=400)
    client.json("POST", f"/api/projects/{pid}/upload?name=new.png&target=relink&media=m_nothere00a", png_bytes(),
                status=404)
