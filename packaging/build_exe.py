"""Build the Windows release: dist/Video Console/Video Console.exe plus a zip of that folder.

    python -m pip install pyinstaller
    python packaging/build_exe.py

The app is pure standard library, so PyInstaller needs no hidden imports; only the web UI and the built-in
ComfyUI workflow templates are added as data. ffmpeg and Video2X are not bundled: the settings page
installs them into data/tools on first use.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from vc import APP_NAME, VERSION  # noqa: E402

DIST = ROOT / "dist"
WORK = ROOT / "build"
EXTRAS = ("README.md", "LICENSE")  # copied beside the exe


def build() -> Path:
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", APP_NAME, "--icon", str(ROOT / "packaging" / "icon.ico"),
        "--add-data", f"{APP / 'web'};web", "--add-data", f"{APP / 'workflows'};workflows",
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        str(APP / "main.py"),
    ], check=True)
    folder = DIST / APP_NAME
    for name in EXTRAS:
        shutil.copy2(ROOT / name, folder / name)
    return folder


def pack(folder: Path) -> Path:
    archive = DIST / f"VideoConsole-{VERSION}-win64.zip"
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, Path(folder.name) / path.relative_to(folder))
    with zipfile.ZipFile(archive) as zf:
        packed = len(zf.namelist())
    if packed != len(files):
        raise SystemExit(f"zip has {packed} files, folder has {len(files)}")
    print(f"{archive} ({archive.stat().st_size / 1_000_000:.1f} MB, {packed} files)")
    return archive


if __name__ == "__main__":
    pack(build())
