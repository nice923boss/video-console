// First-run tour (UI-09): points at where each stage of making a video lives, one step at a time. It opens
// by itself the first time the app starts with no projects; the palette's 新手導覽 replays it.
import { html, useState, useEffect, useLayoutEffect } from "../lib/h.js";
import { readPref, writePref } from "../lib/prefs.js";
import { keyLabel, useDialogKeys } from "../lib/keymap.js";
import { openModal } from "../components/overlay.js";
import { Button } from "../components/ui.js";

const DONE_KEY = "vc.tour.done";
const GAP = 14;
const CARD_W = 340;

const railItem = (label) => () => document.querySelector(`.rail-item[title^="${label}"]`);
const exportButton = () => [...document.querySelectorAll(".topbar .btn")].find((b) => b.textContent.trim() === "輸出影片");

const STEPS = [
  { title: "歡迎使用 Video Console", body: "做一支影片分四步：分鏡、生成、剪輯、輸出。接下來一步一步指給你看在哪裡。" },
  { target: railItem("分鏡"), title: "1. 分鏡",
    body: "把影片拆成一個個鏡頭，寫下每個鏡頭要拍什麼。也可以貼上整份劇本，讓文字模型一次拆好。" },
  { target: railItem("引擎"), title: "2. 生成",
    body: "先在引擎頁接好 ComfyUI 或雲端 API。之後在分鏡頁選一個鏡頭，右側「生成」分頁就能出圖、出影片、配音。" },
  { target: railItem("佇列"), title: "生成進度", body: "送出的生成工作在佇列頁排隊，可以暫停、調整順序或排定開始時間。" },
  { target: railItem("剪輯"), title: "3. 剪輯", body: "鏡頭照分鏡順序排成時間軸，在這裡修剪、加音樂、字幕與轉場。" },
  { target: exportButton, title: "4. 輸出", body: "完成後按右上角的「輸出影片」做成 MP4。",
    missing: "開啟專案後，右上角會出現「輸出影片」按鈕，按下去就能做成 MP4。" },
];

const clampY = (y) => Math.max(8, Math.min(y, window.innerHeight - 240));

function Tour({ close }) {
  const [step, setStep] = useState(0);
  const [rect, setRect] = useState(null);
  const s = STEPS[step];
  const last = step === STEPS.length - 1;
  const finish = () => {
    writePref(DONE_KEY, true);
    close();
  };
  useDialogKeys({ "dialog.close": { fn: finish, allowInInput: true } }, { modal: true });

  useLayoutEffect(() => {
    const measure = () => {
      const el = s.target && s.target();
      setRect(el ? el.getBoundingClientRect() : null);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [step]);
  useEffect(() => document.querySelector(".tour-card .btn-primary")?.focus(), [step]);

  // Beside a target on the left half (the rail), under one on the right (the top bar), centred without one.
  let pos = { left: `calc(50% - ${CARD_W / 2}px)`, top: "30%" };
  if (rect && rect.left < window.innerWidth / 2) pos = { left: `${rect.right + GAP}px`, top: `${clampY(rect.top - 8)}px` };
  else if (rect) pos = { right: `${Math.max(8, window.innerWidth - rect.right)}px`, top: `${clampY(rect.bottom + GAP)}px` };
  const palette = keyLabel("app.palette");
  const body = rect || !s.missing ? s.body : s.missing;

  return html`<div class="tour" role="dialog" aria-modal="true" aria-label="新手導覽">
    ${rect
      ? html`<div class="tour-spot" style=${`left:${rect.left - 4}px;top:${rect.top - 4}px;width:${rect.width + 8}px;height:${rect.height + 8}px`}></div>`
      : html`<div class="tour-shade"></div>`}
    <div class="tour-card" style=${pos}>
      <h2>${s.title}</h2>
      <p>${body}</p>
      ${last && html`<p class="dim">想再看一次，打開指令面板${palette ? `（${palette}）` : ""}搜尋「新手導覽」。</p>`}
      <div class="tour-foot">
        <span class="dim">${step + 1}／${STEPS.length}</span>
        <span class="grow"></span>
        ${!last && html`<${Button} size="sm" kind="ghost" onClick=${finish}>略過<//>`}
        ${step > 0 && html`<${Button} size="sm" onClick=${() => setStep(step - 1)}>上一步<//>`}
        <${Button} size="sm" kind="primary" onClick=${() => (last ? finish() : setStep(step + 1))}>
          ${last ? "開始使用" : "下一步"}
        <//>
      </div>
    </div>
  </div>`;
}

export const openTour = () => openModal((close) => html`<${Tour} close=${close} />`);

// At the first start with nothing made yet; someone who already has projects knows the way around.
export function maybeOpenTour(hasProjects) {
  if (readPref(DONE_KEY, false, (v) => v === true)) return;
  if (hasProjects) writePref(DONE_KEY, true);
  else openTour();
}
