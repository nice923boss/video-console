"""One-click installs of the external tools the app runs: ffmpeg (required) and Video2X (optional).

Versions, download addresses, sizes and SHA-256 live in deps.json beside this file. "檢查新版本" fetches the
copy on the GitHub main branch, so the maintainer can move a tool to a new version without a new release
(IN-05). Each package lists the official source first and mirrors after it; a download that breaks keeps its
partial file and the next attempt, on the same or the next source, asks only for the missing bytes (IN-03).
Every file is checked against its size and SHA-256 before anything is unpacked, whichever source it came from.
Files land in data/tools, a folder the user owns, so no administrator rights are needed.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import net
from .net import USER_AGENT
from .util import AppError

log = logging.getLogger("vc.deps")

CHUNK = 256 * 1024
PROGRESS_SEC = 0.4  # at most this often a download reports progress
MANIFEST_FILE = Path(__file__).with_name("deps.json")
MANIFEST_URL = "https://raw.githubusercontent.com/nice923boss/video-console/main/app/vc/deps.json"
EXES = {"ffmpeg": "ffmpeg/ffmpeg.exe", "video2x": "video2x/video2x.exe"}  # where the app looks for each program
MARKER = ".vc-version"  # written beside the program after an install
PRE_MARKER = {"ffmpeg": "7.1", "video2x": "6.4.0"}  # what installs made before the marker existed got
_SHA_RE = re.compile(r"[0-9a-f]{64}")


class DownloadInterrupted(AppError):
    """Every source failed on the network; the partial file stays for the next attempt."""


@dataclass(frozen=True)
class Package:
    id: str
    name: str
    version: str
    urls: tuple[str, ...]  # official source first, then mirrors; all serve the same bytes
    size: int
    sha256: str
    exe: str  # path of the program inside data/tools
    member: str  # the one archive member that becomes the program, or "" to unpack the whole archive

    @property
    def dir(self) -> str:
        return self.exe.split("/")[0]


def parse_manifest(data: object) -> dict[str, Package]:
    """The usable packages of a deps.json; an entry with a missing or odd field is left out."""
    entries = data.get("packages") if isinstance(data, dict) else None
    out: dict[str, Package] = {}
    for pkg_id, exe in EXES.items():
        e = entries.get(pkg_id) if isinstance(entries, dict) else None
        if not isinstance(e, dict):
            continue
        urls = tuple(u for u in e.get("urls") or [] if isinstance(u, str) and u.startswith("https://"))
        size, sha, member = e.get("size"), str(e.get("sha256") or ""), str(e.get("member") or "")
        if not urls or not isinstance(size, int) or size <= 0 or not _SHA_RE.fullmatch(sha) or ".." in member:
            continue
        version = str(e.get("version") or "").strip()
        out[pkg_id] = Package(pkg_id, str(e.get("name") or pkg_id), version, urls, size, sha, exe, member)
    return out


PACKAGES = parse_manifest(json.loads(MANIFEST_FILE.read_text(encoding="utf-8")))


def version_key(version: str) -> tuple:
    """"7.1" < "7.10" < "8"; parts that are not numbers compare as text after the numbers."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.\-]", version or ""))


class DepInstaller:
    """Runs one background install per package and keeps its progress for the settings page."""

    def __init__(self, tools_dir: Path, on_change: Callable[[str, bool], None]):
        self._tools = tools_dir
        self._on_change = on_change  # (package id, finished successfully)
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._published: dict[str, Package] = {}  # from the last "檢查新版本"

    def tasks(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._tasks.items()}

    def package(self, pkg_id: str) -> Package | None:
        """What an install gets: the newer of the bundled and the last published entry."""
        bundled, published = PACKAGES.get(pkg_id), self._published.get(pkg_id)
        if bundled is None or published is None:
            return bundled or published
        return published if version_key(published.version) > version_key(bundled.version) else bundled

    def installed_version(self, pkg_id: str) -> str | None:
        """Version of the copy in data/tools, or None when there is none."""
        exe = EXES.get(pkg_id)
        if exe is None or not (self._tools / exe).is_file():
            return None
        try:
            return (self._tools / exe.split("/")[0] / MARKER).read_text(encoding="utf-8").strip() or None
        except OSError:
            return PRE_MARKER.get(pkg_id)

    def is_managed(self, pkg_id: str, path: str) -> bool:
        """Whether `path` (the program the app runs) is the copy in data/tools."""
        exe = EXES.get(pkg_id)
        if not path or exe is None:
            return False
        try:
            return Path(path).resolve() == (self._tools / exe).resolve()
        except OSError:
            return False

    def check(self) -> str:
        """Fetch the published deps.json; returns a remark when it cannot be used (the bundled one stays)."""
        try:
            data = net.request("GET", MANIFEST_URL, timeout=15).json()
        except AppError as exc:
            return f"取不到 GitHub 上的版本清單（{exc.message}），先用程式內建的版本"
        found = parse_manifest(data)
        if not found:
            return "GitHub 上的版本清單格式不對，先用程式內建的版本"
        self._published = found
        return ""

    def start(self, pkg_id: str) -> None:
        pkg = self.package(pkg_id)
        if pkg is None:
            raise AppError("不支援安裝這個元件")
        with self._lock:
            if self._tasks.get(pkg_id, {}).get("state") in ("downloading", "unpacking"):
                raise AppError(f"{pkg.name} 正在安裝中", 409)
            self._tasks[pkg_id] = {"state": "downloading", "received": 0, "total": pkg.size, "error": "",
                                   "source": "", "version": pkg.version}
        threading.Thread(target=self._run, args=(pkg,), name=f"install-{pkg_id}", daemon=True).start()
        self._on_change(pkg_id, False)

    def _update(self, pkg_id: str, **fields: object) -> None:
        with self._lock:
            self._tasks[pkg_id] = {**self._tasks[pkg_id], **fields}

    def _run(self, pkg: Package) -> None:
        downloads = self._tools / ".download"
        # Named by hash, so a partial file of another version is never resumed into this one.
        part = downloads / f"{pkg.id}-{pkg.sha256[:16]}.part"
        staging = self._tools / f".{pkg.id}.new"
        target = self._tools / pkg.dir
        ok = False
        keep_part = False
        error = ""
        try:
            downloads.mkdir(parents=True, exist_ok=True)
            for old in downloads.glob(f"{pkg.id}*.part"):
                if old != part:
                    old.unlink(missing_ok=True)
            self._download(pkg, part)
            self._update(pkg.id, state="unpacking")
            self._on_change(pkg.id, False)
            _verify(pkg, part)
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            with zipfile.ZipFile(part) as zf:
                _unpack(pkg, zf, staging)
            if not (staging / pkg.exe.split("/", 1)[1]).is_file():
                raise AppError("安裝檔裡找不到程式檔", 502)
            (staging / MARKER).write_text(pkg.version, encoding="utf-8")
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            ok = True
            log.info("Installed %s into %s", pkg.name, target)
        except AppError as exc:
            keep_part = isinstance(exc, DownloadInterrupted)
            error = exc.message
        except (OSError, zipfile.BadZipFile) as exc:
            error = _describe(exc)
        finally:
            if not keep_part:
                part.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            # The final state goes out after the cleanup, so whoever sees it also sees the files as they stay.
            if ok:
                self._update(pkg.id, state="done")
            elif error:
                self._fail(pkg, error)
            self._on_change(pkg.id, ok)

    def _fail(self, pkg: Package, message: str) -> None:
        log.warning("Installing %s failed: %s", pkg.name, message)
        self._update(pkg.id, state="error", error=message)

    def _download(self, pkg: Package, part: Path) -> None:
        """Try each source in turn, each one resuming where the partial file ends."""
        last: Exception | None = None
        for url in pkg.urls:
            try:
                self._fetch(pkg, url, part)
                return
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as exc:
                last = exc
                log.warning("Downloading %s from %s failed: %s", pkg.name, url, exc)
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 416:
                    part.unlink(missing_ok=True)  # the partial file is longer than the source's file
        have = part.stat().st_size if part.exists() else 0
        kept = "，已下載的部分會保留，再按一次會從中斷處接著下載" if have else ""
        raise DownloadInterrupted(f"{_describe(last)}（{len(pkg.urls)} 個下載來源都失敗{kept}）", 502)

    def _fetch(self, pkg: Package, url: str, part: Path) -> None:
        start = part.stat().st_size if part.exists() else 0
        if start >= pkg.size:
            return  # complete from an earlier attempt; the hash check decides whether it is good
        headers = {"User-Agent": USER_AGENT}
        if start:
            headers["Range"] = f"bytes={start}-"
        self._update(pkg.id, received=start, source=urllib.parse.urlsplit(url).hostname or "")
        self._on_change(pkg.id, False)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - https URL from deps.json
            if start and resp.status == 206:
                if not str(resp.headers.get("Content-Range") or "").startswith(f"bytes {start}-"):
                    raise ConnectionError("續傳位置不符")
            else:
                start = 0  # no range support here: download the whole file again
            received = start
            reported = time.monotonic()
            with open(part, "ab" if start else "wb") as out:
                while chunk := resp.read(CHUNK):
                    received += len(chunk)
                    if received > pkg.size:
                        raise AppError("下載的檔案比預期大，已停止安裝", 502)
                    out.write(chunk)
                    if time.monotonic() - reported >= PROGRESS_SEC:
                        reported = time.monotonic()
                        self._update(pkg.id, received=received)
                        self._on_change(pkg.id, False)
        self._update(pkg.id, received=received)
        if received < pkg.size:
            raise ConnectionError("連線在下載完成前中斷")


def _verify(pkg: Package, part: Path) -> None:
    digest = hashlib.sha256()
    with open(part, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    if part.stat().st_size != pkg.size or digest.hexdigest() != pkg.sha256:
        raise AppError("下載的檔案驗證失敗（大小或 SHA-256 不符），請再試一次", 502)


def _unpack(pkg: Package, zf: zipfile.ZipFile, dest: Path) -> None:
    if not pkg.member:
        zf.extractall(dest)  # extractall drops absolute paths and ".." parts
        return
    # The imageio-ffmpeg wheel carries one static ffmpeg build; only that file is needed.
    try:
        src = zf.open(pkg.member)
    except KeyError:
        raise AppError("安裝檔裡找不到程式檔", 502) from None
    with src, open(dest / pkg.exe.split("/", 1)[1], "wb") as out:
        shutil.copyfileobj(src, out, CHUNK)


def _describe(exc: Exception | None) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"下載失敗（伺服器回應 {exc.code}）"
    if isinstance(exc, urllib.error.URLError):
        return f"無法下載，請確認網路連線：{exc.reason}"
    if isinstance(exc, TimeoutError):
        return "下載逾時，請確認網路連線"
    if isinstance(exc, ConnectionError):
        return f"下載中斷：{exc}"
    if isinstance(exc, zipfile.BadZipFile):
        return "下載的安裝檔無法解壓縮，請再試一次"
    return f"安裝失敗：{exc}"
