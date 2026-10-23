"""Where finished videos and ZIP packages go: one folder shared by every project (settings export.folder,
default <data>/exports), so they are easy to find. Each project lists the files it wrote there in
exports/.index.json, so renaming the project or changing the folder later keeps them listed. Files from
before the shared folder stay in the project's own exports/ and are still listed and served."""
from __future__ import annotations

import threading
from pathlib import Path

from .util import AppError, read_json, write_json_atomic

INDEX = ".index.json"
_lock = threading.Lock()


def export_root(folder: str, data_dir: Path) -> Path:
    """The configured export folder, or <data>/exports when none is set."""
    return Path(folder) if folder else data_dir / "exports"


def ensure_root(root: Path) -> Path:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppError(f"無法使用匯出資料夾 {root}（{exc.strerror or exc}），請到「設定 › 影片輸出」換一個資料夾") from exc
    return root


def unique_path(folder: Path, name: str) -> Path:
    """folder/name, or name-2, name-3, ... when that name is taken (two projects can share a name)."""
    stem, ext = Path(name).stem, Path(name).suffix
    path, n = folder / name, 1
    while path.exists() or (folder / f".{path.name}.part").exists():
        n += 1
        path = folder / f"{stem}-{n}{ext}"
    return path


def file_ref(path: Path, project_dir: Path) -> str:
    """How API results name a file: exports/<name> inside the project folder, the full path elsewhere."""
    try:
        return path.relative_to(project_dir).as_posix()
    except ValueError:
        return str(path)


def _index(project_dir: Path) -> Path:
    return project_dir / "exports" / INDEX


def _recorded(project_dir: Path) -> list[str]:
    data = read_json(_index(project_dir), [])
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def record_export(project_dir: Path, path: Path) -> None:
    with _lock:
        paths = [p for p in _recorded(project_dir) if p != str(path)]
        write_json_atomic(_index(project_dir), [*paths, str(path)])


def forget_export(project_dir: Path, path: Path) -> None:
    with _lock:
        paths = _recorded(project_dir)
        if str(path) in paths:
            write_json_atomic(_index(project_dir), [p for p in paths if p != str(path)])


def project_exports(project_dir: Path) -> list[Path]:
    """This project's finished files: older ones in its own exports/, then those it wrote to the shared folder.
    Missing files are skipped but stay recorded, since the folder may be on a drive that is unplugged."""
    own = project_dir / "exports"
    found = {str(f): f for f in (own.iterdir() if own.is_dir() else ())
             if not f.name.startswith(".") and not f.name.endswith(".part")}
    found.update((p, Path(p)) for p in _recorded(project_dir))
    return [f for f in found.values() if f.is_file()]


def find_export(project_dir: Path, ref: str) -> Path | None:
    """The project's file that a file name or an API path (exports/<name> or a full path) points at."""
    for f in project_exports(project_dir):
        if ref in (f.name, str(f), file_ref(f, project_dir)):
            return f
    return None
