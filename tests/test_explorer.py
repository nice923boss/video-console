"""Finding the File Explorer window that shows a folder, so it can be brought to the front."""
from __future__ import annotations

from pathlib import Path

from vc.explorer import shows

FOLDER = Path(r"C:\Users\someone\VideoConsole\projects")


def test_a_title_naming_the_folder_matches() -> None:
    assert shows("projects - 檔案總管", FOLDER)
    assert shows("projects - File Explorer", FOLDER)
    assert shows(r"C:\Users\someone\VideoConsole\projects - 檔案總管", FOLDER)  # full path in the title bar
    assert shows("projects", FOLDER)


def test_other_folders_do_not_match() -> None:
    assert not shows("projects old - 檔案總管", FOLDER)
    assert not shows("exports - 檔案總管", FOLDER)
    assert not shows("my projects - 檔案總管", FOLDER)
    assert not shows("", FOLDER)
