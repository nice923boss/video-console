"""Finished-job notifications: a toast event for the open window, and one ntfy push when the queue empties
(only when something failed, if the user chose so)."""
from __future__ import annotations

import logging
import threading
from typing import Any

from .events import EventBus
from .net import request
from .secrets import SecretStore
from .settings import SettingsStore
from .util import AppError

log = logging.getLogger("vc.notify")

NOTIFY_KINDS = {"image", "video", "upscale", "upscalevideo", "kenburns", "export", "package", "transcribe", "align"}
NTFY_TOKEN = "ntfy.token"
PUSH_TITLE = "Video Console"
MAX_LISTED = 5


class Notifier:
    def __init__(self, settings: SettingsStore, secrets: SecretStore, bus: EventBus) -> None:
        self._settings = settings
        self._secrets = secrets
        self._bus = bus
        self._lock = threading.Lock()
        self._batch: list[dict[str, str]] = []

    def on_job(self, job: dict, active: int) -> None:
        """JobManager listener: called once per job that reaches a final state."""
        status = job.get("status")
        if job.get("kind") in NOTIFY_KINDS and status in ("done", "failed"):
            title = str(job.get("title") or "")
            detail = job.get("message") if status == "done" else (job.get("error") or job.get("message"))
            self._bus.publish("notify", {
                "level": "success" if status == "done" else "error",
                "title": title,
                "message": str(detail or ""),
                "jobId": job.get("id"),
                "pid": job.get("pid"),
                "kind": job.get("kind"),
                "status": status,
            })
            with self._lock:
                self._batch.append({"status": status, "title": title, "error": str(job.get("error") or "")})
        if active:
            return
        with self._lock:
            batch, self._batch = self._batch, []
        cfg = self._settings.section("notify")["ntfy"]
        if cfg.get("when") == "failed":
            batch = [b for b in batch if b["status"] == "failed"]
        if batch and cfg.get("enabled") and cfg.get("topic"):
            message, tags = summarize(batch)
            threading.Thread(target=self._push_quietly, args=(cfg, message, tags), name="ntfy-push",
                             daemon=True).start()

    def test(self) -> dict[str, Any]:
        """Send one push with the saved ntfy settings; raises AppError with the reason when it fails."""
        cfg = self._settings.section("notify")["ntfy"]
        if not cfg.get("topic"):
            raise AppError("請先填寫 ntfy 主題並儲存設定")
        try:
            self._push(cfg, "測試通知：手機推播設定成功", ["bell"])
        except AppError as exc:
            raise AppError(f"ntfy 推播失敗：{exc.message}", 502) from None
        return {"ok": True, "message": f"已送出測試通知到主題「{cfg['topic']}」，請查看手機"}

    def _push_quietly(self, cfg: dict, message: str, tags: list[str]) -> None:
        try:
            self._push(cfg, message, tags)
        except AppError as exc:
            log.warning("ntfy push failed: %s", exc.message)

    def _push(self, cfg: dict, message: str, tags: list[str]) -> None:
        # JSON publishing to the server root keeps titles and messages UTF-8 safe (headers are not).
        headers = {}
        token = self._secrets.get(NTFY_TOKEN)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"topic": cfg["topic"], "title": PUSH_TITLE, "message": message, "tags": tags}
        request("POST", cfg["server"] + "/", headers=headers, json_body=body, timeout=15, max_bytes=1024 * 1024)


def summarize(batch: list[dict[str, str]]) -> tuple[str, list[str]]:
    """One push message (and ntfy tags) for every job finished since the queue was last empty."""
    failed = [b for b in batch if b["status"] == "failed"]
    tags = ["warning"] if failed else ["white_check_mark"]
    if len(batch) == 1:
        item = batch[0]
        if failed:
            return f"失敗：{item['title']}\n{item['error'][:200]}".rstrip(), tags
        return f"完成：{item['title']}", tags
    done = len(batch) - len(failed)
    head = (f"{done} 個工作完成" + (f"，{len(failed)} 個失敗" if failed else "")) if done else f"{len(failed)} 個工作失敗"
    listed = (failed + [b for b in batch if b["status"] != "failed"])[:MAX_LISTED]
    lines = [("✗ " if b["status"] == "failed" else "✓ ") + b["title"] for b in listed]
    if len(batch) > MAX_LISTED:
        lines.append(f"…還有 {len(batch) - MAX_LISTED} 個")
    return "\n".join([head, *lines]), tags
