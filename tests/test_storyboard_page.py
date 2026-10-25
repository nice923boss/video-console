"""The storyboard page carries its pictures, so the page saved on its own still shows them."""
from __future__ import annotations

import base64

import pytest

from test_server import App, Client, _seed_media, app, client, png_bytes, project  # noqa: F401  (server fixtures)
from vc import storyboard


def test_storyboard_page_carries_its_pictures(app: App, client: Client, project: dict,  # noqa: F811
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_media(app, client, project)
    url = f"/api/projects/{project['id']}/storyboard"
    page = client.request("GET", url)[2].decode("utf-8")
    assert f'src="data:image/png;base64,{base64.b64encode(png_bytes()).decode()}"' in page
    assert "/api/projects/" not in page  # nothing the page needs the app for once saved on its own

    monkeypatch.setattr(storyboard, "MAX_EMBED", 0)  # too large: the thumbnail goes in instead
    page = client.request("GET", url)[2].decode("utf-8")
    assert f'src="data:image/jpeg;base64,{base64.b64encode(b"thumb").decode()}"' in page


def test_a_missing_picture_leaves_the_frame_empty(app: App, client: Client, project: dict) -> None:  # noqa: F811
    _seed_media(app, client, project)
    (app.store.project_dir(project["id"]) / "media/takes/t_pic00000a.png").unlink()
    page = client.request("GET", f"/api/projects/{project['id']}/storyboard")[2].decode("utf-8")
    assert "<img" not in page and "尚無畫面" in page
