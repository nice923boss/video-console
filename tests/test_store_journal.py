"""Incremental saving (PF-04): edits go to project.journal at once, project.json follows when they pause."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vc import store as storemod
from vc.events import EventBus
from vc.paths import Paths
from vc.settings import SettingsStore
from vc.store import JOURNAL, ProjectStore


def _store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(Paths(tmp_path / "data"), EventBus(), SettingsStore(tmp_path / "settings.json"))


def _on_disk(store: ProjectStore, pid: str) -> dict:
    return json.loads((store.project_dir(pid) / "project.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_background_save(monkeypatch: pytest.MonkeyPatch) -> None:
    # only an explicit flush/close saves, so each test decides when project.json is written
    monkeypatch.setattr(storemod, "FLUSH_IDLE", 3600.0)
    monkeypatch.setattr(storemod, "FLUSH_MAX", 3600.0)


def test_an_edit_lands_in_the_journal_and_a_restart_replays_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pid = store.create("港口")["id"]
    store.apply(pid, [{"op": "project.update", "patch": {"name": "港口 v2", "notes": "第一行 第二行"}}])
    store.apply(pid, [{"op": "shot.add", "shot": {"id": "s_journal01", "name": "新鏡頭"}}])
    store.undo(pid)
    store.redo(pid)
    assert _on_disk(store, pid)["rev"] == 0  # project.json not rewritten per edit
    assert len((store.project_dir(pid) / JOURNAL).read_text(encoding="utf-8").splitlines()) >= 4

    reloaded = _store(tmp_path)  # as after a crash: nothing was flushed
    project = reloaded.get(pid)
    assert project == store.get(pid)
    assert project["name"] == "港口 v2" and project["notes"] == "第一行 第二行"
    assert [s["id"] for s in project["shots"]][-1] == "s_journal01"
    assert _on_disk(reloaded, pid)["rev"] == 4 and not (reloaded.project_dir(pid) / JOURNAL).exists()


def test_a_flush_writes_the_project_and_empties_the_journal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pid = store.create("港口")["id"]
    store.apply(pid, [{"op": "project.update", "patch": {"name": "一"}}])
    store.flush(pid)
    assert _on_disk(store, pid)["name"] == "一" and not (store.project_dir(pid) / JOURNAL).exists()
    store.apply(pid, [{"op": "project.update", "patch": {"name": "二"}}])
    store.close()
    assert _on_disk(store, pid)["name"] == "二" and not (store.project_dir(pid) / JOURNAL).exists()


def test_a_torn_last_line_keeps_every_edit_before_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pid = store.create("港口")["id"]
    store.apply(pid, [{"op": "project.update", "patch": {"name": "一"}}])
    store.apply(pid, [{"op": "project.update", "patch": {"name": "二"}}])
    journal = store.project_dir(pid) / JOURNAL
    journal.write_bytes(journal.read_bytes()[:-25])  # the crash hit mid-write
    project = _store(tmp_path).get(pid)
    assert (project["name"], project["rev"]) == ("一", 1)


def test_lines_already_saved_are_skipped_and_a_gap_stops_the_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pid = store.create("港口")["id"]
    store.apply(pid, [{"op": "project.update", "patch": {"name": "一"}}])
    old = (store.project_dir(pid) / JOURNAL).read_text(encoding="utf-8")
    store.flush(pid)
    store.apply(pid, [{"op": "project.update", "patch": {"name": "二"}}])
    journal = store.project_dir(pid) / JOURNAL
    journal.write_text(old + journal.read_text(encoding="utf-8"), encoding="utf-8")  # trim never happened
    assert _store(tmp_path).get(pid)["name"] == "二"

    store.apply(pid, [{"op": "project.update", "patch": {"name": "三"}}])  # r3; the reload saved r2
    gap = journal.read_text(encoding="utf-8").replace('{"rev":3,', '{"rev":4,')
    journal.write_text(gap, encoding="utf-8")  # r4 cannot follow r2
    project = _store(tmp_path).get(pid)
    assert (project["name"], project["rev"]) == ("二", 2)


def test_a_due_history_snapshot_is_written_with_the_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pid = store.create("港口")["id"]
    store.apply(pid, [{"op": "project.update", "patch": {"name": "一"}}])
    assert not list((store.project_dir(pid) / "history").glob("*.json"))
    assert [h["rev"] for h in store.history(pid)] == [0]  # listing saves first
    store.restore_history(pid, 0)
    assert store.get(pid)["name"] == "港口"
    assert _store(tmp_path).get(pid)["name"] == "港口"


def test_trash_saves_first_and_duplicate_leaves_the_journal_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pid = store.create("港口")["id"]
    store.apply(pid, [{"op": "project.update", "patch": {"name": "一"}}])
    copy = store.duplicate(pid)
    assert copy["name"] == "一（副本）" and not (store.project_dir(copy["id"]) / JOURNAL).exists()
    store.trash(pid)
    [folder] = store.list_trash()
    assert folder["name"] == "一"
    store.restore_trash(folder["folder"])
    assert store.get(pid)["name"] == "一"
