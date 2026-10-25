"""Debug bundle and log clearing on the settings page (QA-09)."""
from __future__ import annotations

import io
import json
import logging
import logging.handlers
import threading
import zipfile
from pathlib import Path

import pytest

from test_server import Client, app, client, project  # noqa: F401 - pytest fixtures
from vc.diagnostics import clear_logs, debug_bundle, log_files
from vc.logsetup import _log_thread_crash


def write_logs(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    for name in ("app.log", "app.log.1", "stdout.log", "notes.txt"):
        (log_dir / name).write_text(f"{name} line\n", encoding="utf-8")


def test_bundle_has_the_logs_and_no_secrets(tmp_path: Path) -> None:
    write_logs(tmp_path)
    settings = {"ntfy": {"enabled": True, "topic": "my-private-topic"}, "ui": {"theme": "dark"}}
    secrets = {"llm.api_key": {"set": True, "masked": "sk-…abcd"}, "voai.api_key": {"set": False, "masked": ""}}
    data = debug_bundle(tmp_path, {"version": "1.0"}, settings, secrets, [{"id": "j_1"}], {"name": "港口短片"})
    z = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(z.namelist()) == ["jobs.json", "logs/app.log", "logs/app.log.1", "logs/stdout.log", "project.json",
                                    "secrets.json", "settings.json", "system.json"]
    assert json.loads(z.read("secrets.json")) == {"llm.api_key": True, "voai.api_key": False}
    assert "abcd" not in z.read("secrets.json").decode("utf-8")
    saved = json.loads(z.read("settings.json"))
    assert saved["ntfy"] == {"enabled": True, "topic": "***"} and saved["ui"] == {"theme": "dark"}
    assert settings["ntfy"]["topic"] == "my-private-topic"  # the caller's copy is untouched
    assert json.loads(z.read("project.json"))["name"] == "港口短片"
    assert "project.json" not in zipfile.ZipFile(io.BytesIO(debug_bundle(tmp_path, {}, {}, {}, []))).namelist()


def test_clear_empties_the_open_log_and_deletes_the_rest(tmp_path: Path) -> None:
    write_logs(tmp_path)
    handler = logging.handlers.RotatingFileHandler(tmp_path / "app.log", maxBytes=2_000_000, encoding="utf-8")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("vc.test").warning("before")
        freed = clear_logs(tmp_path)
        logging.getLogger("vc.test").warning("after")
    finally:
        root.removeHandler(handler)
        handler.close()
    assert freed > 0
    assert [p.name for p in log_files(tmp_path)] == ["app.log"]
    text = (tmp_path / "app.log").read_text(encoding="utf-8")
    # The file was emptied through the handler's own handle: no NUL padding, no old lines.
    assert "\x00" not in text and "before" not in text and "app.log line" not in text
    assert text == "after\n"
    assert (tmp_path / "notes.txt").exists()


def test_thread_crash_is_logged(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(threading, "excepthook", _log_thread_crash)

    def boom() -> None:
        raise ValueError("thread went down")

    t = threading.Thread(target=boom, name="poller")
    with caplog.at_level(logging.CRITICAL, logger="vc"):
        t.start()
        t.join()
    record = next(r for r in caplog.records if r.getMessage() == "Thread poller crashed")
    assert record.exc_info and record.exc_info[0] is ValueError


def test_bundle_and_clear_routes(client: Client, project: dict) -> None:  # noqa: F811
    client.json("POST", "/api/system/client-error", {"message": "x is undefined", "stack": "at render", "view": "edit"})
    code, headers, data = client.request("GET", f"/api/system/debug-bundle?project={project['id']}")
    assert code == 200 and headers["content-type"] == "application/zip"
    assert "attachment" in headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(data))
    assert json.loads(z.read("project.json"))["id"] == project["id"]
    assert json.loads(z.read("system.json"))["version"]
    code, _, _ = client.request("GET", "/api/system/debug-bundle?project=p_missing000")
    assert code == 404
    res = client.json("POST", "/api/system/clear-logs")
    assert res["freed"] >= 0 and "logBytes" in res["system"]
