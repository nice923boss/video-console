"""Caption spelling check against the settings word list (CP-12): the setting and web/js/lib/glossary.js."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vc.settings import MAX_GLOSSARY, SettingsStore
from vc.util import AppError

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
GLOSSARY_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "js" / "lib" / "glossary.js"

WORDS = "黃政文=黃正文,黃正雯\nVideo Console\nSora\n台=臺"


def _check(cases: list[str]) -> list[dict]:
    """Each caption's hits and its fixed text, worked out by glossary.js in node."""
    script = ("const m = await import(process.argv[1]); const e = m.parseGlossary(process.argv[2]);"
              "console.log(JSON.stringify(JSON.parse(process.argv[3]).map((t) => {"
              "const hits = m.glossaryHits(t, e); return { hits, fixed: m.fixHits(t, hits), text: m.hitText(hits) }; })));")
    proc = subprocess.run(["node", "--input-type=module", "-e", script, GLOSSARY_JS.as_uri(), WORDS,
                           json.dumps(cases, ensure_ascii=False)],
                          capture_output=True, check=True, text=True, encoding="utf-8")
    return json.loads(proc.stdout)


def test_glossary_setting_is_a_bounded_string(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    assert store.get()["glossary"] == ""
    assert store.update({"glossary": WORDS})["glossary"] == WORDS
    assert SettingsStore(tmp_path / "settings.json").get()["glossary"] == WORDS
    with pytest.raises(AppError):
        store.update({"glossary": "x" * (MAX_GLOSSARY + 1)})
    with pytest.raises(AppError):
        store.update({"glossary": ["黃政文"]})
    assert store.get()["glossary"] == WORDS  # a rejected patch keeps the saved list


@needs_node
def test_hits_listed_wrong_forms_and_near_misses() -> None:
    listed, near, case, fine, inside, short = _check([
        "主講人黃正文老師",        # a listed wrong form
        "歡迎使用 Video Consale",  # one letter off
        "歡迎使用 video console",  # letter case only
        "黃政文用 Video Console 和 Sora 做影片",  # every term spelt right
        "Soraya 不是 Sora 的錯字",  # a Latin term inside a longer word is not checked
        "台北與台南",              # a short term is checked only through its listed wrong forms
    ])
    assert listed["hits"] == [{"found": "黃正文", "term": "黃政文"}]
    assert listed["fixed"] == "主講人黃政文老師"
    assert near["hits"] == [{"found": "Video Consale", "term": "Video Console"}]
    assert near["text"] == "「Video Consale」→「Video Console」"
    assert case["fixed"] == "歡迎使用 Video Console"
    assert fine["hits"] == [] and inside["hits"] == []
    assert short["hits"] == []


@needs_node
def test_near_miss_of_a_chinese_term_is_fixed_everywhere() -> None:
    [one] = _check(["黃政雯說，黃政雯會到"])
    assert one["hits"] == [{"found": "黃政雯", "term": "黃政文"}]
    assert one["fixed"] == "黃政文說，黃政文會到"
