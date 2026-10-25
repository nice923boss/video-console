"""AU-14: a replaced voice-over goes to the project trash, where it stays restorable (and undoable) until
清除 (purge) deletes its record and file."""
from __future__ import annotations

import os
import time

from test_server import Client, app, client, project  # noqa: F401  (shared fixtures)
from vc.app import App


def media_of(client: Client, pid: str) -> dict:
    return client.json("GET", f"/api/projects/{pid}")["project"]["media"]


def test_purge_empties_the_project_trash(app: App, client: Client, project: dict) -> None:
    pid = project["id"]
    folder = app.store.project_dir(pid) / "media" / "import"
    folder.mkdir(parents=True, exist_ok=True)
    old_voice, kept = folder / "old.wav", folder / "kept.wav"
    for f in (old_voice, kept):
        f.write_bytes(b"x" * 10)
        os.utime(f, (time.time() - 600, time.time() - 600))
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "media.add", "media": {"id": "m_old0000001", "kind": "audio", "file": "media/import/old.wav"}},
        {"op": "media.add", "media": {"id": "m_kept000001", "kind": "audio", "file": "media/import/kept.wav"}}]})
    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "media.update", "id": "m_old0000001", "patch": {"trashed": True}}]})
    usage = client.json("GET", f"/api/projects/{pid}/usage")
    assert (usage["trashed"], usage["unused"], usage["orphans"]) == (1, 10, 1)

    client.json("POST", f"/api/projects/{pid}/undo")  # the trash flag undoes like any edit
    assert not media_of(client, pid)["m_old0000001"].get("trashed")
    client.json("POST", f"/api/projects/{pid}/redo")
    assert media_of(client, pid)["m_old0000001"]["trashed"] is True

    assert client.json("POST", f"/api/projects/{pid}/purge")["freed"] == 10
    media = media_of(client, pid)
    assert "m_old0000001" not in media and "m_kept000001" in media
    assert not old_voice.exists() and kept.exists()
