"""The cross-project style library (IM-07): save the project's style with a sample picture, list, delete."""
from __future__ import annotations

from test_klein import add_pictures
from test_server import App, Client, app, client, project  # noqa: F401  (server fixtures)


def test_a_saved_style_keeps_its_text_and_a_copy_of_the_sample(app: App, client: Client, project: dict) -> None:
    pid = project["id"]
    [mid] = add_pictures(app, client, pid, 1)
    body = {"name": "  港口晨光  ", "prompt": "35mm 底片，暖色", "negative": "文字", "pid": pid, "item": mid}
    saved = client.json("POST", "/api/styles", body)["style"]
    assert (saved["name"], saved["prompt"], saved["negative"]) == ("港口晨光", "35mm 底片，暖色", "文字")
    original = app.store.project_dir(pid) / f"media/import/{mid}.png"
    code, _, data = client.request("GET", f"/api/styles/{saved['id']}/picture")
    assert code == 200 and data == original.read_bytes()

    # the copy outlives the project's own file
    original.unlink()
    assert client.request("GET", f"/api/styles/{saved['id']}/picture")[0] == 200

    plain = client.json("POST", "/api/styles", {"name": "黑白", "prompt": "黑白高反差"})["style"]
    assert plain["picture"] is None
    assert client.request("GET", f"/api/styles/{plain['id']}/picture")[0] == 404
    assert [s["id"] for s in client.json("GET", "/api/styles")["styles"]] == [plain["id"], saved["id"]]

    left = client.json("DELETE", f"/api/styles/{saved['id']}")["styles"]
    assert [s["id"] for s in left] == [plain["id"]]
    assert not list((app.paths.data / "style-library").iterdir())
    client.json("DELETE", f"/api/styles/{saved['id']}", status=404)


def test_a_style_needs_a_name_and_some_text(client: Client, project: dict) -> None:
    client.json("POST", "/api/styles", {"name": "", "prompt": "暖色"}, status=400)
    client.json("POST", "/api/styles", {"name": "空的", "prompt": " ", "negative": ""}, status=400)
    client.json("POST", "/api/styles", {"name": "x", "prompt": "暖色", "pid": project["id"], "item": "m_nope"},
                status=404)
    assert client.json("GET", "/api/styles")["styles"] == []
