"""IN-04: the latest GitHub release against this build, kept for a while so startup does not spend the API quota."""
from __future__ import annotations

import pytest

from vc import VERSION, release


@pytest.fixture()
def github(monkeypatch, mock_http):
    monkeypatch.setattr(release, "RELEASE_URL", mock_http.url + "/latest")
    monkeypatch.setattr(release, "_last", None)
    return mock_http


def answer(tag: str, url: str = release.RELEASES_PAGE + "/tag/v9.0.0") -> tuple[int, dict]:
    return 200, {"tag_name": tag, "html_url": url, "published_at": "2026-11-02T08:00:00Z"}


def test_a_newer_release_is_offered(github):
    github.route("GET", "/latest", answer("v9.0.0"))
    info = release.latest()
    assert info == {"current": VERSION, "latest": "9.0.0", "newer": True, "url": release.RELEASES_PAGE + "/tag/v9.0.0",
                    "published": "2026-11-02", "remark": ""}
    assert github.calls[0].headers["accept"] == "application/vnd.github+json"


@pytest.mark.parametrize("tag", [f"v{VERSION}", VERSION, "v0.9.0"])
def test_the_same_or_an_older_release_is_not(github, tag):
    github.route("GET", "/latest", answer(tag))
    info = release.latest()
    assert not info["newer"] and info["latest"] == tag.removeprefix("v")


def test_the_answer_is_kept_until_forced(github):
    github.route("GET", "/latest", answer("v9.0.0"))
    release.latest()
    github.route("GET", "/latest", answer("v9.1.0"))
    assert release.latest()["latest"] == "9.0.0"
    assert len(github.calls) == 1
    assert release.latest(force=True)["latest"] == "9.1.0"
    assert len(github.calls) == 2


def test_failures_give_a_remark(github):
    github.route("GET", "/latest", (404, {"message": "Not Found"}))
    info = release.latest()
    assert info["latest"] == "" and not info["newer"] and "取不到" in info["remark"]
    github.route("GET", "/latest", (200, {"name": "no tag"}))
    assert "格式不對" in release.latest(force=True)["remark"]


def test_a_link_outside_the_releases_page_is_replaced(github):
    github.route("GET", "/latest", answer("v9.0.0", "https://evil.example/download"))
    assert release.latest()["url"] == release.RELEASES_PAGE
