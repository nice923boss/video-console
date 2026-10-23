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
  ] },
  { title: "片段", items: [
    [["edit.delete"], "刪除片段",
      "刪除選取的片段。刪除鏡頭前會先確認（可在設定關掉）；連動上下軌開著時，整句落在鏡頭上的字幕會一起刪除。"],
    [["edit.toggleEnabled"], "停用片段",
      "停用的鏡頭保留位置，預覽與輸出都是黑畫面加靜音；上方畫面、字幕與調整圖層會被略過，音訊會靜音。再按一次恢復。"],
    [["edit.detachAudio"], "分離、還原原聲",
      "把鏡頭的原聲拆成音訊軌上的片段，鏡頭本身改成無聲，之後就能單獨剪、調音量。對已分離的鏡頭或那段音訊再按一次就還原。變速中的鏡頭不能分離。"],
    [["edit.volumeUp", "edit.volumeDown"], "音量",
      "選取的音訊或上方影片每按一次加減 1dB。設了區域時，只調整區域內的音量（自動在兩端加關鍵幀，前後各 0.1 秒漸變）。鏡頭原聲要先分離才能調。"],
  ] },
  { title: "速度與方向", items: [
    [["edit.reverse", "edit.forward"], "倒著播放、往前播放",
      "播放中往同方向再按一次，預覽加速到 2x、4x、8x，控制列顯示目前速度；暫停後回到 1x。倒著播放只有畫面，沒有聲音。只影響預覽，不改片段。"],
    [["edit.speed"], "變速",
      "開啟右側「速度與方向」，可選 0.25x、0.5x、1x、1.5x、2x、4x。變速後時間軸長度跟著改，聲音也一起變速。"],
    [["edit.reverseClip"], "倒放",
      "第一次會在背景產生倒放的影片（佇列可看進度），完成後自動換上；之後再按就在正放、倒放之間切換。"],
    [["edit.freeze"], "定格",
      "在播放頭切開影片，插入 2 秒的靜止畫面（那一格的畫面由背景工作擷取）。播放頭在片段頭尾時，定格加在前面或後面。"],
  ] },
  { title: "區域與標記", items: [
    [["edit.markIn", "edit.markOut"], "入點、出點",
      "在播放頭設定區域的開始與結束，尺規上會出現區域條。"],
    [["edit.rangeFromClip"], "以片段設定區域", "把區域設成選取片段（或播放頭下方鏡頭）的頭尾。"],
    [["edit.playRange", "edit.clearRange"], "播放、清除區域", "從入點播到出點後自動停下；清除後回到整條時間軸。"],
    [["edit.markerAdd", "edit.markerColor"], "標記",
      "在播放頭新增標記，再按切換顏色（藍、紅、橘、黃、綠、紫）。點尺規上的標記跳過去，右鍵可換顏色或刪除。"],
    [["edit.markerPrev", "edit.markerNext"], "跳到標記", "跳到前一個或下一個標記。"],
  ] },
  { title: "關鍵幀", items: [
    [["edit.keyframeAdd"], "添加關鍵幀",
      "在播放頭記下選取片段目前的值。上方畫面記位置、大小、不透明度與音量；音訊記音量；調整圖層記調色。兩個關鍵幀之間的值會平順變化。"],
    [["edit.keyframePanel"], "關鍵幀面板",
      "在右側列出片段的所有關鍵幀，可跳過去或刪除。有關鍵幀的欄位，拖滑桿就是改播放頭那一格的值（沒有就新增一個）。鏡頭與字幕不能加關鍵幀：要動的畫面放上方軌，要漸變的調色用調整圖層。"],
  ] },
  { title: "時間軸檢視", items: [
    [["edit.zoomIn", "edit.zoomOut", "edit.zoomFit"], "縮放", "放大、縮小時間軸，或讓整條時間軸剛好填滿寬度。"],
    [["edit.trackTaller", "edit.trackShorter"], "軌道高度", "增高或降低每一條軌道。"],
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
