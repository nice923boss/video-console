// Watch mode: this window only shows the open project (for example while Claude edits it through the API),
// so nothing done here changes it. Kept per browser tab (sessionStorage) across reloads.
import { getState, setState } from "./store.js";
import { toast } from "../components/toast.js";

const KEY = "vc.watch";

export function readWatch() {
  try {
    return sessionStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function setWatch(on) {
  setState(on ? { watch: true, editSel: null } : { watch: false });
  try {
    if (on) sessionStorage.setItem(KEY, "1");
    else sessionStorage.removeItem(KEY);
  } catch {
    // convenience only: the mode still applies until this page closes
  }
}

// True (and a short notice) when an edit was attempted in watch mode.
export function watchRefuses() {
  if (!getState().watch) return false;
  toast("觀看模式中，不會修改專案；要編輯請先結束觀看", { id: "watch-refused", duration: 2000 });
  return true;
}
