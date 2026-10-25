"""The project's framing choice (settings.fit): the preview shows it and the export uses it unless the job overrides it."""
from __future__ import annotations

import pytest

from test_timeline_keys import make_project
from vc.export import export_options
from vc.ops import apply_ops
from vc.util import AppError


def test_export_follows_the_project_framing() -> None:
    p = make_project()
    assert export_options(p, {}, {})["fit"] == "contain"
    p["settings"]["fit"] = "cover"
    assert export_options(p, {}, {})["fit"] == "cover"
    assert export_options(p, {}, {"fit": "contain"})["fit"] == "contain"


def test_framing_accepts_only_contain_or_cover() -> None:
    p = make_project()
    apply_ops(p, [{"op": "project.update", "patch": {"settings": {"fit": "cover"}}}], {})
    assert p["settings"]["fit"] == "cover"
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "project.update", "patch": {"settings": {"fit": "stretch"}}}], {})
