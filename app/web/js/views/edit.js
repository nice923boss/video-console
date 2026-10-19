// Edit view: the preview player on top, the timeline below (drag the divider to resize it), and the
// property panel for whatever is selected on the timeline.
import { html, useMemo, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { navigate } from "../lib/actions.js";
import { buildSequence } from "../lib/sequence.js";
import { clamp } from "../lib/format.js";
import { usePref } from "../lib/prefs.js";
import { Button, Empty } from "../components/ui.js";
import { Player } from "../components/player.js";
import { Timeline } from "../components/timeline.js";
import { EditProps } from "../components/edit-props.js";

const TL_DEFAULT = 280;
const TL_MIN = 170;
const TL_MAX_SHARE = 0.72;
const TL_KEY_STEP = 24;

const validHeight = (v) => Number.isFinite(v) && v >= TL_MIN && v <= 4000;

export function EditView() {
  const { project, editSel } = useStore((s) => ({ project: s.project, editSel: s.editSel }));
  const [height, setHeight] = usePref("vc.edit.tlh", TL_DEFAULT, validHeight);
  const mainRef = useRef(null);
  const clips = useMemo(() => (project ? buildSequence(project) : []), [project]);

  const limit = (h) => {
    const main = mainRef.current;
    const max = main ? Math.max(TL_MIN, main.clientHeight * TL_MAX_SHARE) : 4000;
    return Math.round(clamp(h, TL_MIN, max));
  };

  // While dragging, write the CSS variable directly so the player does not re-render on every move;
  // the final height is saved once on release.
  const startDrag = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const handle = e.currentTarget;
    const main = mainRef.current;
    const startY = e.clientY;
    let h = height;
    handle.setPointerCapture(e.pointerId);
    main.classList.add("is-resizing");
    const move = (ev) => {
      h = limit(height - (ev.clientY - startY));
      main.style.setProperty("--tl-h", `${h}px`);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      main.classList.remove("is-resizing");
      setHeight(h);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };
  const onKey = (e) => {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      e.stopPropagation();
      setHeight(limit(height + (e.key === "ArrowUp" ? TL_KEY_STEP : -TL_KEY_STEP)));
    }
  };

  if (!project) {
    return html`<div class="view view-edit view-empty">
      <${Empty} icon="scissors" title="還沒有開啟專案"
        actions=${html`<${Button} kind="primary" icon="home" onClick=${() => navigate("home")}>回到專案列表<//>`}>
        開啟專案後，這裡會把所有鏡頭排成一條時間軸，可以預覽、修剪、加音樂與字幕。
      <//>
    </div>`;
  }

  return html`<div class="view view-edit">
    <section class="edit-main" ref=${mainRef} style=${`--tl-h:${height}px`}>
      <div class="edit-stage">
        <${Player} project=${project} clips=${clips} />
      </div>
      <div class="edit-split" role="separator" aria-orientation="horizontal" aria-label="調整時間軸高度"
          aria-valuenow=${height} tabindex="0" title="拖曳調整時間軸高度，雙擊還原"
          onPointerDown=${startDrag} onDblClick=${() => setHeight(limit(TL_DEFAULT))} onKeyDown=${onKey}>
        <i></i>
      </div>
      <div class="edit-timeline">
        <${Timeline} project=${project} clips=${clips} />
      </div>
    </section>
    <${EditProps} project=${project} sel=${editSel} />
  </div>`;
}
