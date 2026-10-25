// Edit view: the preview player on top (with the media bin beside it when shown), the timeline below (drag
// the divider to resize it), and the property panel for whatever is selected on the timeline (not shown in
// watch mode, which changes nothing).
import { html, useEffect, useMemo, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { navigate } from "../lib/actions.js";
import { buildSequence } from "../lib/sequence.js";
import { clamp } from "../lib/format.js";
import { usePref } from "../lib/prefs.js";
import { releaseFocus } from "../lib/keys.js";
import { useKeymap } from "../lib/keymap.js";
import { Button, Empty } from "../components/ui.js";
import { Player } from "../components/player.js";
import { Timeline } from "../components/timeline.js";
import { EditProps } from "../components/edit-props.js";
import { AudioMeters } from "../components/audio-meter.js";
import { MediaBin } from "../components/media-bin.js";
import { layoutMenu } from "../components/layouts.js";

const TL_DEFAULT = 280;
const TL_MIN = 170;
const TL_MAX_SHARE = 0.72;
const TL_KEY_STEP = 24;

const validHeight = (v) => Number.isFinite(v) && v >= TL_MIN && v <= 4000;
const isBool = (v) => typeof v === "boolean";

export function EditView() {
  const { project, editSel, watch } = useStore((s) => ({ project: s.project, editSel: s.editSel, watch: s.watch }));
  const [height, setHeight] = usePref("vc.edit.tlh", TL_DEFAULT, validHeight);
  const [meters, setMeters] = usePref("vc.edit.meters", true, isBool);
  const toggleMeters = () => setMeters(!meters);
  const [bin, setBin] = usePref("vc.edit.bin", false, isBool);
  const toggleBin = () => setBin(!bin);
  useKeymap({ "edit.meters": toggleMeters, "edit.bin": toggleBin });
  const mainRef = useRef(null);
  const clips = useMemo(() => (project ? buildSequence(project) : []), [project]);

  const limit = (h) => {
    const main = mainRef.current;
    const max = main ? Math.max(TL_MIN, main.clientHeight * TL_MAX_SHARE) : 4000;
    return Math.round(clamp(h, TL_MIN, max));
  };
  // A layout (UI-08) saved on a taller window may not fit this one.
  useEffect(() => {
    const fit = limit(height);
    if (fit !== height) setHeight(fit);
  }, [height]);

  // While dragging, write the CSS variable directly so the player does not re-render on every move;
  // the final height is saved once on release.
  const startDrag = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    releaseFocus();
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
        ${bin && !watch && html`<${MediaBin} project=${project} />`}
        <${Player} project=${project} clips=${clips} meters=${meters} onToggleMeters=${toggleMeters}
          bin=${bin} onToggleBin=${watch ? null : toggleBin} onLayout=${layoutMenu} />
      </div>
      <div class="edit-split" role="separator" aria-orientation="horizontal" aria-label="調整時間軸高度"
          aria-valuenow=${height} tabindex="0" title="拖曳調整時間軸高度，雙擊還原"
          onPointerDown=${startDrag} onDblClick=${() => setHeight(limit(TL_DEFAULT))} onKeyDown=${onKey}>
        <i></i>
      </div>
      <div class="edit-timeline">
        <${Timeline} project=${project} clips=${clips} />
        ${meters && html`<${AudioMeters} />`}
      </div>
    </section>
    ${!watch && html`<${EditProps} project=${project} sel=${editSel} />`}
  </div>`;
}
