"""One-click installs of the external tools the app runs: ffmpeg (required) and Video2X (optional).

Every download is pinned to a fixed URL, size and SHA-256, so a changed or damaged file is rejected before
anything is unpacked. Files land in data/tools, a folder the user owns, so no administrator rights are needed.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .net import USER_AGENT
from .util import AppError

log = logging.getLogger("vc.deps")

CHUNK = 256 * 1024
PROGRESS_SEC = 0.4  # at most this often a download reports progress


def _unpack_ffmpeg(zf: zipfile.ZipFile, dest: Path) -> None:
    """The imageio-ffmpeg wheel carries one static ffmpeg build; only that file is needed."""
    with zf.open("imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe") as src, open(dest / "ffmpeg.exe", "wb") as out:
        shutil.copyfileobj(src, out, CHUNK)


def _unpack_all(zf: zipfile.ZipFile, dest: Path) -> None:
    zf.extractall(dest)  # extractall drops absolute paths and ".." parts


@dataclass(frozen=True)
class Package:
    id: str
    name: str
    url: str
    size: int
    sha256: str
    exe: str  # path of the program inside data/tools
    unpack: Callable[[zipfile.ZipFile, Path], None]


PACKAGES = {
    "ffmpeg": Package(
        id="ffmpeg", name="ffmpeg 7.1（imageio-ffmpeg 0.6.0）",
        url="https://files.pythonhosted.org/packages/2c/c6/fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/"
            "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl",
        size=31_246_824, sha256="02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a",
        exe="ffmpeg/ffmpeg.exe", unpack=_unpack_ffmpeg),
    "video2x": Package(
        id="video2x", name="Video2X 6.4.0 可攜版",
        url="https://github.com/k4yt3x/video2x/releases/download/6.4.0/video2x-windows-amd64.zip",
        size=196_989_277, sha256="0337b6dcae2bad2fd13e43fbdb73ef728d2f6289f37cc3617e87ac3a1a5cf793",
        exe="video2x/video2x.exe", unpack=_unpack_all),
}


class DepInstaller:
    """Runs one background install per package and keeps its progress for the settings page."""

    def __init__(self, tools_dir: Path, on_change: Callable[[str, bool], None]):
        self._tools = tools_dir
        self._on_change = on_change  # (package id, finished successfully)
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}

    def tasks(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._tasks.items()}

    def start(self, pkg_id: str) -> None:
        pkg = PACKAGES.get(pkg_id)
        if pkg is None:
            raise AppError("不支援安裝這個元件")
        with self._lock:
            if self._tasks.get(pkg_id, {}).get("state") in ("downloading", "unpacking"):
                raise AppError(f"{pkg.name} 正在安裝中", 409)
            self._tasks[pkg_id] = {"state": "downloading", "received": 0, "total": pkg.size, "error": ""}
        threading.Thread(target=self._run, args=(pkg,), name=f"install-{pkg_id}", daemon=True).start()
        self._on_change(pkg_id, False)

    def _update(self, pkg_id: str, **fields: object) -> None:
        with self._lock:
            self._tasks[pkg_id] = {**self._tasks[pkg_id], **fields}

    def _run(self, pkg: Package) -> None:
        part = self._tools / ".download" / f"{pkg.id}.part"
        staging = self._tools / f".{pkg.id}.new"
        target = self._tools / pkg.exe.split("/")[0]
        ok = False
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            self._download(pkg, part)
            self._update(pkg.id, state="unpacking")
            self._on_change(pkg.id, False)
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            with zipfile.ZipFile(part) as zf:
                pkg.unpack(zf, staging)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            if not (self._tools / pkg.exe).is_file():
                raise AppError("安裝檔裡找不到程式檔", 502)
            self._update(pkg.id, state="done")
            ok = True
            log.info("Installed %s into %s", pkg.name, target)
        except AppError as exc:
            self._fail(pkg, exc.message)
        except (OSError, zipfile.BadZipFile) as exc:
            self._fail(pkg, _describe(exc))
        finally:
            part.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            self._on_change(pkg.id, ok)

    def _fail(self, pkg: Package, message: str) -> None:
        log.warning("Installing %s failed: %s", pkg.name, message)
        self._update(pkg.id, state="error", error=message)

    def _download(self, pkg: Package, part: Path) -> None:
        req = urllib.request.Request(pkg.url, headers={"User-Agent": USER_AGENT})
        digest = hashlib.sha256()
        received = 0
        reported = time.monotonic()
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:  # noqa: S310 - fixed https URL
            while chunk := resp.read(CHUNK):
                received += len(chunk)
                if received > pkg.size:
                    raise AppError("下載的檔案比預期大，已停止安裝", 502)
                out.write(chunk)
                digest.update(chunk)
                if time.monotonic() - reported >= PROGRESS_SEC:
                    reported = time.monotonic()
                    self._update(pkg.id, received=received)
                    self._on_change(pkg.id, False)
        self._update(pkg.id, received=received)
        if received != pkg.size or digest.hexdigest() != pkg.sha256:
            raise AppError("下載的檔案驗證失敗（大小或 SHA-256 不符），請再試一次", 502)


def _describe(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"下載失敗（伺服器回應 {exc.code}），請稍後再試"
    if isinstance(exc, urllib.error.URLError):
        return f"無法下載，請確認網路連線：{exc.reason}"
    if isinstance(exc, TimeoutError):
        return "下載逾時，請確認網路連線後再試"
    if isinstance(exc, zipfile.BadZipFile):
        return "下載的安裝檔無法解壓縮，請再試一次"
    return f"安裝失敗：{exc}"
