"""Build the Windows release: dist/Video Console/Video Console.exe plus a zip of that folder.

    python -m pip install pyinstaller numpy
    python packaging/build_exe.py

The app is standard library plus numpy (降噪 / 去回音 in vc/voiceclean.py, which PyInstaller finds by itself), so
it needs no hidden imports; only the web UI, the built-in
ComfyUI workflow templates, the model menus (vc/providers/model_lists.json), the tool versions (vc/deps.json) and the client review page
(vc/review_kit) are added as data. ffmpeg and Video2X are not bundled: the settings page
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

from vc import APP_NAME, BUILD, VERSION  # noqa: E402

DIST = ROOT / "dist"
WORK = ROOT / "build"
EXTRAS = ("README.md", "LICENSE", "docs", "tools", "claude")  # copied beside the exe


def build() -> Path:
    try:
        import numpy  # noqa: F401 - an exe built without it would refuse 降噪 / 去回音
    except ImportError:
        raise SystemExit("numpy is missing: python -m pip install numpy") from None
    # The packaged vc/BUILD lets the exe report the source build it was made from, and lets vc_client read it.
    stamp = WORK / "stamp" / "BUILD"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(BUILD, encoding="utf-8")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", APP_NAME, "--icon", str(ROOT / "packaging" / "icon.ico"),
        "--add-data", f"{APP / 'web'};web", "--add-data", f"{APP / 'workflows'};workflows",
        "--add-data", f"{stamp};vc",
        "--add-data", f"{APP / 'vc' / 'providers' / 'model_lists.json'};vc/providers",
        "--add-data", f"{APP / 'vc' / 'deps.json'};vc",
        "--add-data", f"{APP / 'vc' / 'review_kit'};vc/review_kit",
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        str(APP / "main.py"),
    ], check=True)
    folder = DIST / APP_NAME
    for name in EXTRAS:
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, folder / name, dirs_exist_ok=True)
        else:
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
