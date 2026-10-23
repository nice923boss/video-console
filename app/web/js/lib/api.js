// Thin fetch wrapper for the local server. Every state-changing request carries the X-VC header the
// server requires, so a page from another origin cannot drive the API.
import { getState } from "./store.js";

export class ApiError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const OFFLINE = "無法連線到 Video Console 伺服器，請確認程式仍在執行";
const WATCHED = "觀看模式中，不會修改專案";

// Watch mode backstop (lib/watch.js): no write to the open project leaves this window, whichever
// control sent it. Marking it opened, showing its folder and copying it (open / reveal / duplicate) stay allowed.
function watchBlocks(method, path, body) {
  const { watch, pid } = getState();
  if (!watch || !pid || !UNSAFE.has(method)) return false;
  if (path === "/api/jobs" || path === "/api/jobs/batch") {
    return !!body && (body.pid === pid || (body.jobs || []).some((j) => j.pid === pid));
  }
  const base = `/api/projects/${encodeURIComponent(pid)}`;
  return (path === base || path.startsWith(`${base}/`)) && !/^\/(open|reveal|duplicate)$/.test(path.slice(base.length));
}

export async function request(method, path, body, { signal } = {}) {
  if (watchBlocks(method, path, body)) throw new ApiError(WATCHED, 0);
  const headers = { Accept: "application/json" };
  const init = { method, headers, cache: "no-store", credentials: "same-origin", signal };
  if (UNSAFE.has(method)) headers["X-VC"] = "1";
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, init);
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(OFFLINE, 0);
  }
  if (resp.status === 204) return null;
  const text = await resp.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!resp.ok) {
    const message = (data && data.error) || `伺服器錯誤（${resp.status}）`;
    throw new ApiError(message, resp.status, data ? data.detail : null);
  }
  return data;
}

export const api = {
  get: (path, opts) => request("GET", path, undefined, opts),
  post: (path, body = {}, opts) => request("POST", path, body, opts),
  put: (path, body = {}, opts) => request("PUT", path, body, opts),
  del: (path, opts) => request("DELETE", path, undefined, opts),
};

// Raw-body upload with progress (fetch has no upload progress events).
export function upload(path, file, onProgress) {
  if (watchBlocks("POST", path.split("?")[0])) return Promise.reject(new ApiError(WATCHED, 0));
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("X-VC", "1");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText || "null");
      } catch {
        data = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError((data && data.error) || `上傳失敗（${xhr.status}）`, xhr.status));
    };
    xhr.onerror = () => reject(new ApiError("上傳中斷：" + OFFLINE, 0));
    xhr.send(file);
  });
}
