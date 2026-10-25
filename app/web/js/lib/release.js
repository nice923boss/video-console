// IN-04: at startup, compare this build with the latest release on GitHub and point to the download page
// when there is a newer one. Each release is announced once; the settings page shows the state any time.
import { api } from "./api.js";
import { readPref, writePref } from "./prefs.js";
import { toast } from "../components/toast.js";

const SEEN_PREF = "vc.release.seen"; // the newest version already announced

export const openRelease = (info) => window.open(info.url, "_blank", "noopener");

export async function announceRelease() {
  // Offline or rate-limited at startup is not worth a notice: the server logs it and the settings page shows it.
  const info = await api.get("/api/system/release").catch(() => null);
  if (!info || !info.newer || readPref(SEEN_PREF, "", (v) => typeof v === "string") === info.latest) return;
  writePref(SEEN_PREF, info.latest);
  toast(`目前是 ${info.current}，GitHub 上已有 ${info.latest}${info.published ? `（${info.published} 發布）` : ""}。`, {
    id: "new-release", title: "Video Console 有新版本", duration: 0,
    action: { label: "前往下載頁", fn: () => openRelease(info) },
  });
}
