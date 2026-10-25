"""Library tags (LB-04): media.update keeps a trimmed, de-duplicated tag list and undo puts the old one back."""
from __future__ import annotations

from test_klein import add_pictures, send
from test_server import App, Client, app, client, project  # noqa: F401  (server fixtures)


def test_tags_are_cleaned_saved_and_undone(app: App, client: Client, project: dict) -> None:
    pid = project["id"]
    (picture,) = add_pictures(app, client, pid, 1)

    def tags() -> list | None:
        return client.json("GET", f"/api/projects/{pid}")["project"]["media"][picture].get("tags")

    assert tags() is None
    send(client, pid, {"op": "media.update", "id": picture, "patch": {"tags": [" 海邊 ", "人物", "海邊", ""]}})
    assert tags() == ["海邊", "人物"]
    send(client, pid, {"op": "media.update", "id": picture, "patch": {"tags": "海邊"}}, status=400)
    send(client, pid, {"op": "media.update", "id": picture, "patch": {"tags": [f"t{i}" for i in range(21)]}},
         status=400)
    send(client, pid, {"op": "media.update", "id": picture, "patch": {"tags": ["夜景"]}})
    client.json("POST", f"/api/projects/{pid}/undo")
    assert tags() == ["海邊", "人物"]
    client.json("POST", f"/api/projects/{pid}/undo")
    assert tags() is None
