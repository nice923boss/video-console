// Client review (SB-13): download the single-file review page of the sequence, and read back the comments
// file the client downloads from it (vc/review.py). The comments land on the shots in one undo step.
import { getState } from "./store.js";
import { upload } from "./api.js";
import { toast, toastError } from "../components/toast.js";

const enc = encodeURIComponent;

export function downloadReviewPage() {
  const { pid } = getState();
  if (!pid) return;
  const a = document.createElement("a");
  a.href = `/api/projects/${enc(pid)}/review.html`;
  a.download = "";
  a.click();
  toast("已下載客戶審稿網頁；把這個 .html 檔傳給客戶，客戶留言後會下載一個 .json 檔傳回來", { level: "success" });
}

export async function importReview(file) {
  const { pid } = getState();
  if (!pid) return;
  try {
    const res = await upload(`/api/projects/${enc(pid)}/review`, file);
    const parts = [
      res.shots && `${res.shots} 則鏡頭意見`,
      res.general && `${res.general} 則整體意見`,
      res.missing && `${res.missing} 則屬於已刪除的鏡頭（列在全片的客戶意見）`,
    ].filter(Boolean);
    const dup = res.duplicate ? `；${res.duplicate} 則先前已匯入，略過` : "";
    toast(`已匯入 ${res.by} 的審稿意見：${parts.join("、")}${dup}`, { level: "success" });
  } catch (err) {
    toastError(err, "無法匯入審稿意見");
  }
}

export function pickReviewFile() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = () => input.files[0] && importReview(input.files[0]);
  input.click();
}
