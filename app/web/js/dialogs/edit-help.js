// Edit help (palette "剪輯功能說明", and a button in the shortcut list): what each timeline feature does,
// with the keys it has right now (lib/keymap.js, so a rebind shows here too).
import { html } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { keysOf, readKeymap, selectKeymap } from "../lib/keymap.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Keys, Kbd } from "../components/ui.js";

// Rows are [command ids, title, text]; mouse rows give their gesture as text instead of command ids.
const SECTIONS = [
  { title: "分割與裁剪", items: [
    [["edit.split", "edit.splitAll"], "分割",
      "在播放頭切開片段。選取哪一軌就切那一軌，沒有選取時切畫面軌；分割所有軌道會把播放頭經過的每一軌一起切開。"],
    [["edit.trimStart", "edit.trimEnd"], "向左、向右裁剪",
      "刪掉片段在播放頭前面或後面的部分。畫面軌上後面的鏡頭會往前補上空隙。"],
    [["edit.toolSelect", "edit.toolBlade"], "選擇模式、分割模式",
      "分割模式下點片段，就在點的位置切開；切回選擇模式才能拖曳與選取。"],
    [["edit.toolTrim"], "修剪模式（滾動、滑動、滑移）",
      "只作用在畫面軌，總長度不變。拖曳兩個鏡頭之間的邊緣是滾動：剪接點移動，前一個鏡頭變長、後一個變短；拖曳鏡頭中間是滑動內容：位置與長度不變，改用素材前後的另一段畫面；按住 Shift 拖曳是滑移鏡頭：內容不變，在前後鏡頭之間移動，前後鏡頭一個變長一個變短。素材用完或鏡頭到最短就停住，按 Esc 取消。"],
  ] },
  { title: "片段", items: [
    [["右鍵"], "片段右鍵選單",
      "在時間軸的鏡頭、上方畫面、音訊、字幕或調整圖層上按右鍵，列出這種片段能用的操作與快捷鍵（鏡頭另有添加靜幀、原聲、倒放、變速、隱藏分鏡畫面與標籤顏色）。隱藏分鏡畫面：畫面變黑、片段變灰，原聲照常播放與輸出，給整段被滿版上層片段蓋住的鏡頭用。做不到的項目變灰，滑鼠停在上面說明原因。"],
    [["edit.delete"], "刪除片段",
      "刪除選取的片段。刪除鏡頭前會先確認（可在設定關掉）；連動上下軌開著時，整句落在鏡頭上的字幕會一起刪除。"],
    [["edit.rippleDelete"], "漣漪刪除",
      "刪除後，同一軌後面的片段往前補上空隙（一般刪除則留空）。字幕與它的配音視為一組，一起刪除、一起移動。刪除鏡頭時，連動上下軌關著也會讓其他軌一起補位。"],
    [["edit.toggleEnabled"], "停用片段",
      "停用的鏡頭保留位置，預覽與輸出都是黑畫面加靜音；上方畫面、字幕與調整圖層會被略過，音訊會靜音。再按一次恢復。"],
    [["edit.detachAudio"], "分離、還原原聲",
      "把鏡頭的原聲拆成音訊軌上的片段，鏡頭本身改成無聲，之後就能單獨修剪、淡入淡出、加音量關鍵幀。對已分離的鏡頭或那段音訊再按一次就還原。變速中的鏡頭不能分離。"],
    [["edit.volumeUp", "edit.volumeDown"], "音量",
      "選取的音訊、上方影片或鏡頭每按一次加減 1dB。設了區域時，只調整區域內的音量（自動在兩端加關鍵幀，前後各 0.1 秒漸變）。鏡頭原聲只能整段調，只調一段請用原聲關鍵幀（選取鏡頭按 K）或先分離；Ctrl 點選逐一加選、Shift 點選連續選取多個鏡頭，可在右側一起調原聲音量。"],
  ] },
  { title: "速度與方向", items: [
    [["edit.reverse", "edit.forward"], "倒著播放、往前播放",
      "播放中往同方向再按一次，預覽加速到 2x、4x、8x，控制列顯示目前速度；暫停後回到 1x。倒著播放只有畫面，沒有聲音。只影響預覽，不改片段。"],
    [["edit.speed"], "變速",
      "開啟右側「速度與方向」，可選 0.25x、0.5x、1x、1.5x、2x、4x。變速後時間軸長度跟著改，聲音也一起變速。"],
    [["edit.reverseClip"], "倒放",
      "第一次會在背景產生倒放的影片（佇列可看進度），完成後自動換上；之後再按就在正放、倒放之間切換。"],
    [["edit.freeze"], "定格",
      "在播放頭切開影片，插入 5 秒的靜止畫面（靜幀，那一格的畫面由背景工作擷取），後段畫面接在靜幀之後。播放頭在片段頭尾時，定格加在前面或後面。鏡頭右鍵選單的「添加靜幀（5 秒）」效果相同。"],
  ] },
  { title: "區域與標記", items: [
    [["edit.markIn", "edit.markOut"], "入點、出點",
      "在播放頭設定區域的開始與結束，尺規上會出現區域條。"],
    [["edit.rangeFromClip"], "以片段設定區域", "把區域設成選取片段（或播放頭下方鏡頭）的頭尾。"],
    [["edit.playRange", "edit.clearRange"], "播放、清除區域", "從入點播到出點後自動停下；清除後回到整條時間軸。"],
    [["edit.markerAdd", "edit.markerColor"], "標記",
      "在播放頭新增標記，再按切換顏色（藍、紅、橘、黃、綠、紫）。點尺規上的標記跳過去，右鍵可換顏色或刪除。"],
    [["edit.markerPrev", "edit.markerNext"], "跳到標記", "跳到前一個或下一個標記，時間軸捲到標記在畫面正中央。"],
  ] },
  { title: "關鍵幀", items: [
    [["edit.keyframeAdd"], "添加關鍵幀",
      "在播放頭記下選取片段目前的值。上方畫面記位置、大小、不透明度與音量；音訊記音量；調整圖層記調色；鏡頭記原聲音量。兩個關鍵幀之間的值會平順變化。選取的音訊或原聲片段上會畫音量線，上下拖曳兩個關鍵幀之間的線段就一起調整，拉到最下面是靜音。"],
    [["edit.keyframePanel"], "關鍵幀面板",
      "在右側列出片段的所有關鍵幀，可跳過去或刪除。有關鍵幀的欄位，拖滑桿就是改播放頭那一格的值（沒有就新增一個）。鏡頭畫面與字幕不能加關鍵幀：要動的畫面放上方軌，要漸變的調色用調整圖層。"],
  ] },
  { title: "時間軸檢視", items: [
    [["edit.zoomIn", "edit.zoomOut", "edit.zoomFit"], "縮放", "放大、縮小時間軸，或讓整條時間軸剛好填滿寬度。"],
    [["edit.trackTaller", "edit.trackShorter"], "軌道高度", "增高或降低每一條軌道。"],
    [["edit.meters"], "音量表",
      "時間軸右側顯示左右聲道的峰值，刻度與 Final Cut Pro 相同：綠色低於 -6dB，黃色到 0dB，超過 0dB 變紅（輸出可能破音）。上方數字是最近的峰值，點一下重設。量的是輸出的混音，不受播放器音量影響。播放器時間碼旁的小音量表也可以開關。"],
    [["edit.scopes"], "示波器",
      "預覽畫面右側顯示亮度波形與向量儀。波形由左到右對應畫面左到右，高度是亮度，0 是黑、100 是白，整片貼在 0 或 100 代表暗部或亮部已經沒有細節。向量儀離中心越遠越飽和，方向是色相，方塊是 75% 的標準色，斜線是膚色線，人臉大致落在線上。量的是預覽畫面（含調色、轉場、子母畫面），不含字幕與遮罩。"],
    [["edit.chat"], "AI 助理",
      "預覽畫面右側開對話框，用一般的話交代剪輯或字幕的修改，由文字模型讀取專案後操作；可附加中英對照的 Word 檔匯入雙語字幕。每次修改是一個「AI 助理：…」復原步驟。要先在「引擎 › 文字模型」開啟。"],
    [["Alt＋滾輪", "Ctrl＋滾輪"], "滾輪縮放", "以滑鼠位置為中心縮放時間軸。"],
    [["滾輪", "Shift＋滾輪"], "滾輪捲動",
      "滾輪上下捲動軌道，Shift＋滾輪左右捲動；軌道沒有超出高度時，滾輪直接左右捲動。"],
  ] },
  { title: "還沒有支援", items: [
    [["edit.speedCurve", "edit.group", "edit.compound", "edit.uncompound", "edit.precompose"], "曲線變速、組合、複合片段、預合成",
      "已經保留按鍵，按下只會提示還沒有支援。"],
  ] },
];

function KeyCell({ keys, km }) {
  const cells = keys.flatMap((k) => (k.includes(".")
    ? keysOf(k, km).map((c) => html`<${Keys} key=${`${k}:${c}`} combo=${c} />`)
    : [html`<${Kbd} key=${k}>${k}<//>`]));
  if (!cells.length) return html`<span class="kb-keys dim">未設定</span>`;
  return html`<span class="kb-keys">${cells.flatMap((c, i) => (i ? [html`<span key=${`sep${i}`} class="dim">、</span>`, c] : [c]))}</span>`;
}

function EditHelp() {
  const km = readKeymap(useStore(selectKeymap));
  return html`<div class="edit-help">
    ${SECTIONS.map((s) => html`<section class="kb-group" key=${s.title}>
      <h4>${s.title}</h4>
      ${s.items.map(([keys, title, text]) => html`<div class="kb-row" key=${title}>
        <${KeyCell} keys=${keys} km=${km} />
        <span><b>${title}</b>${text}</span>
      </div>`)}
    </section>`)}
  </div>`;
}

let open = false;

export function openEditHelp() {
  if (open) return Promise.resolve(null);
  open = true;
  return openModal((close) => html`<${Modal} title="剪輯功能說明" width=${820} onClose=${() => close(null)}
      footer=${html`<span class="modal-foot-note dim">按鍵依目前的快捷鍵設定顯示；到「設定」的「快捷鍵」可以改鍵</span>
        <${Button} kind="primary" onClick=${() => close(null)}>知道了<//>`}>
    <${EditHelp} />
  <//>`).finally(() => { open = false; });
}
