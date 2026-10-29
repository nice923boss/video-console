// Storyboard inspector: the project's look when nothing is selected, batch tools for a
// multi-selection, and the tabbed editor for a single shot.
import { html, useMemo, useState } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import {
  updateProject, updateShots, ops, deleteShots, duplicateShots, moveShot, focusShot, navigate,
} from "../lib/actions.js";
import { generate, submitBatch, activeJobsFor } from "../lib/jobs-actions.js";
import { shotLabel, fmtTime, fmtSeconds, round } from "../lib/format.js";
import { buildSequence, sequenceDuration, shotCover, videoTake } from "../lib/sequence.js";
import { Icon } from "./icons.js";
import { Button, IconButton, Field, DraftText, NumberInput, Select, Slider, Tabs, Section, Spinner, Segmented } from "./ui.js";
import { openMenu } from "./overlay.js";
import { toast } from "./toast.js";
import { keyLabel } from "../lib/keymap.js";
import { Thumb } from "./media.js";
import { ContentTab, ColorSwatches } from "./insp-content.js";
import { GenerateTab, RouteChips, kenburnsFor } from "./insp-generate.js";
import { TakesTab } from "./insp-takes.js";
import { EditTab } from "./insp-edit.js";
import { isStabilized, stabilizeShots } from "./tl-commands.js";
import { StyleLibrary } from "./style-library.js";
import { ReviewSection } from "./review-list.js";

const TABS = [
  { id: "content", label: "描述", icon: "pencil" },
  { id: "generate", label: "生成", icon: "sparkles" },
  { id: "takes", label: "版本", icon: "layers" },
  { id: "edit", label: "剪輯", icon: "scissors" },
];
const FPS_CHOICES = [12, 16, 24, 25, 29.97, 30, 60];
const BATCH_THUMBS = 8;
const NO_ROUTES = [];
const COUNT_OPTIONS = [1, 2, 3, 4].map((n) => ({ value: n, label: String(n), title: `每個鏡頭生成 ${n} 份` }));

export function takesOf(project, shotId) {
  return Object.values(project.takes || {})
    .filter((t) => t.shot === shotId)
    .sort((a, b) => (b.created || 0) - (a.created || 0));
}

// ---------------------------------------------------------------- nothing selected
function AspectPicker({ value, aspects, onPick }) {
  return html`<div class="aspect-pick" role="radiogroup" aria-label="畫面比例">
    ${aspects.map((a) => {
      const [w, h] = a.sizes[0];
      const k = 24 / Math.max(w, h);
      return html`<button type="button" key=${a.id} role="radio" aria-checked=${a.id === value ? "true" : "false"}
          class=${`aspect-opt ${a.id === value ? "active" : ""}`} title=${a.label}
          onClick=${() => a.id !== value && onPick(a)}>
        <span class="aspect-box"><span class="aspect-shape" style=${`width:${Math.round(w * k)}px;height:${Math.round(h * k)}px`}></span></span>
        <span>${a.id}</span>
      </button>`;
    })}
  </div>`;
}

function sizeOptions(aspect, st) {
  const list = (aspect ? aspect.sizes : []).map(([w, h], i) => ({
    value: `${w}x${h}`, label: `${w} × ${h}${i === 0 ? "（預設）" : ""}`,
  }));
  const cur = `${st.width}x${st.height}`;
  if (!list.some((o) => o.value === cur)) list.push({ value: cur, label: `${st.width} × ${st.height}（自訂）` });
  return list;
}

function ProjectPanel({ project }) {
  const presets = useStore((s) => s.presets);
  const styles = presets.styles || [];
  const aspects = presets.aspects || [];
  const st = project.settings;
  const style = project.style || {};
  const preset = styles.find((x) => x.id === style.preset);
  const aspect = aspects.find((a) => a.id === st.aspect);
  const stats = useMemo(() => ({
    shots: project.shots.length,
    takes: Object.keys(project.takes || {}).length,
    duration: sequenceDuration(buildSequence(project)),
  }), [project]);
  const fpsList = FPS_CHOICES.includes(st.fps) ? FPS_CHOICES : [...FPS_CHOICES, st.fps].sort((a, b) => a - b);

  const setSettings = (patch, label) => updateProject({ settings: patch }, { label });
  const pickStyle = (id) => {
    const p = styles.find((x) => x.id === id);
    if (!p) return updateProject({ style: { preset: "" } }, { label: "清除風格預設" });
    return updateProject({ style: { preset: p.id, prompt: p.prompt, negative: p.negative } }, { label: `套用風格：${p.label}` });
  };
  const pickAspect = (a) => {
    const [width, height] = a.sizes[0];
    setSettings({ aspect: a.id, width, height }, "變更畫面比例");
  };
  const pickSize = (v) => {
    const [width, height] = v.split("x").map(Number);
    setSettings({ width, height }, "變更畫面尺寸");
  };

  return html`<div class="insp-scroll">
    <div class="insp-hero">
      <span class="insp-eyebrow">專案設定</span>
      <h2>${project.name}</h2>
      <div class="insp-stats">
        <div class="stat"><b>${stats.shots}</b><span>鏡頭</span></div>
        <div class="stat"><b>${stats.takes}</b><span>版本</span></div>
        <div class="stat"><b class="mono">${fmtTime(stats.duration, { tenths: false })}</b><span>序列長度</span></div>
      </div>
    </div>
    <${ReviewSection} id="insp-project-review" items=${project.review}
      onChange=${(review) => updateProject({ review }, { label: "刪除客戶意見" })} />
    <${Section} title="風格" icon="palette">
      <${Field} label="風格預設" hint=${preset ? preset.desc : "選一個預設，會帶入對應的風格描述與負向提示詞"}>
        <${Select} value=${preset ? preset.id : ""} onChange=${pickStyle}
          options=${[{ value: "", label: "（無）" }, ...styles.map((x) => ({ value: x.id, label: x.label }))]} />
      <//>
      <${Field} label="風格描述" hint="全片共用，會接在每個鏡頭的提示詞後面">
        <${DraftText} multiline rows=${3} value=${style.prompt || ""} placeholder="例如：電影感打光、底片顆粒、淺景深"
          onCommit=${(v) => updateProject({ style: { prompt: v } }, { label: "修改風格描述", coalesce: "style.prompt" })} />
      <//>
      <${Field} label="全片負向提示詞" hint="不希望出現在任何鏡頭裡的內容">
        <${DraftText} multiline rows=${2} value=${style.negative || ""} placeholder="例如：模糊、浮水印、文字"
          onCommit=${(v) => updateProject({ style: { negative: v } }, { label: "修改負向提示詞", coalesce: "style.negative" })} />
      <//>
      <${StyleLibrary} project=${project} />
    <//>
    <${Section} title="畫面" icon="frame">
      <${Field} label="比例"><${AspectPicker} value=${st.aspect} aspects=${aspects} onPick=${pickAspect} /><//>
      <div class="row-2">
        <${Field} label="尺寸">
          <${Select} value=${`${st.width}x${st.height}`} options=${sizeOptions(aspect, st)} onChange=${pickSize} />
        <//>
        <${Field} label="影格率">
          <${Select} value=${st.fps} options=${fpsList.map((f) => ({ value: f, label: `${f} fps` }))}
            onChange=${(fps) => setSettings({ fps }, "變更影格率")} />
        <//>
      </div>
      <div class="row-2">
        <${Field} label="片頭淡入">
          <${NumberInput} value=${st.fadeIn || 0} min=${0} max=${10} step=${0.5} digits=${2} suffix="秒"
            onCommit=${(fadeIn) => setSettings({ fadeIn }, "修改片頭淡入")} />
        <//>
        <${Field} label="片尾淡出">
          <${NumberInput} value=${st.fadeOut || 0} min=${0} max=${10} step=${0.5} digits=${2} suffix="秒"
            onCommit=${(fadeOut) => setSettings({ fadeOut }, "修改片尾淡出")} />
        <//>
      </div>
    <//>
    <${Section} title="專案備註" icon="book" collapsible id="insp-notes" defaultOpen=${true}>
      <${DraftText} multiline rows=${4} value=${project.notes || ""} placeholder="劇情大綱、拍攝重點、待辦事項…"
        onCommit=${(notes) => updateProject({ notes }, { label: "修改專案備註", coalesce: "project.notes" })} />
    <//>
    <p class="insp-hint"><${Icon} name="info" size=${14} /><span>點一下鏡頭卡片開始編輯，按住 Ctrl 或 Shift 可以多選。</span></p>
  </div>`;
}

// ---------------------------------------------------------------- several shots
function BatchPanel({ project, ids }) {
  const { routes, presets, pid, jobs } = useStore((s) => ({ routes: (s.settings && s.settings.routes) || NO_ROUTES, presets: s.presets, pid: s.pid, jobs: s.jobs }));
  const [count, setCount] = useState(1);
  const shots = project.shots.filter((x) => ids.includes(x.id));
  const same = (key) => {
    const v = shots.length ? shots[0][key] : undefined;
    return shots.every((x) => x[key] === v) ? v : undefined;
  };
  const sameType = shots.length && shots.every((x) => (x.transition || {}).type === (shots[0].transition || {}).type)
    ? (shots[0].transition || {}).type : undefined;
  const withImage = shots.filter((x) => x.activeImage);
  const withVideo = shots.filter((x) => x.activeVideo);
  const ratio = `${project.settings.width}/${project.settings.height}`;

  const imageTool = (kind, payloadOf) => {
    if (!withImage.length) {
      toast("選取的鏡頭都還沒有圖片，請先生圖或匯入圖片", { level: "warn" });
      return;
    }
    const skipped = shots.length - withImage.length;
    if (skipped) toast(`${skipped} 個鏡頭還沒有圖片，已略過`, { level: "warn" });
    submitBatch(withImage.map((x) => ({ kind, pid, shotId: x.id, payload: payloadOf(x) })));
  };
  const upscaleVideos = () => {
    if (!withVideo.length) {
      toast("選取的鏡頭都還沒有影片", { level: "warn" });
      return;
    }
    const skipped = shots.length - withVideo.length;
    if (skipped) toast(`${skipped} 個鏡頭還沒有影片，已略過`, { level: "warn" });
    submitBatch(withVideo.map((x) => ({ kind: "upscalevideo", pid, shotId: x.id, payload: {} })));
  };
  const setTransition = (type) => ops(shots.map((x) => ({
    op: "shot.update", id: x.id, patch: { transition: { duration: 0.5, ...(x.transition || {}), type } },
  })), { label: "批次修改轉場" });
  const clear = () => setState((s) => ({ selected: s.focus && ids.includes(s.focus) ? [s.focus] : [] }));
  // The shots' own sound, one volume for all of them; shots without sound or with it detached are left alone.
  const sounding = shots.filter((x) => !x.audioDetached && (videoTake(project, x) || {}).hasAudio);
  const volumes = sounding.map((x) => Number(x.volume ?? 1));
  const sameVolume = volumes.every((v) => v === volumes[0]) ? volumes[0] : null;
  const setVolume = (volume) => ops(sounding.map((x) => ({ op: "shot.update", id: x.id, patch: { volume } })),
    { label: `${sounding.length} 個鏡頭原聲音量 ${Math.round(volume * 100)}%` });
  // 防抖 on the video shots; one whose copy is still rendering is left out, as its toggle is locked in the edit tab.
  const videoShots = shots.filter((x) => videoTake(project, x));
  const rendering = new Set(jobs.filter((j) => j.pid === pid && j.kind === "stabilize"
    && (j.status === "queued" || j.status === "running")).map((j) => j.shotId));
  const stabFree = videoShots.filter((x) => !rendering.has(x.id));
  const stabbed = stabFree.filter((x) => isStabilized(videoTake(project, x)));
  const busyStab = videoShots.length - stabFree.length;

  return html`<div class="insp-scroll">
    <div class="insp-hero">
      <div class="row">
        <span class="insp-eyebrow grow">多選</span>
        <${Button} size="sm" kind="ghost" icon="x" onClick=${clear}>取消多選<//>
      </div>
      <h2>已選取 ${shots.length} 個鏡頭</h2>
      <div class="batch-strip">
        ${shots.slice(0, BATCH_THUMBS).map((x) => {
          const cover = shotCover(project, x);
          const i = project.shots.indexOf(x);
          return html`<${Thumb} key=${x.id} pid=${pid} path=${cover ? cover.path : ""} ratio=${ratio} icon="clapper"
            title=${shotLabel(x, i)} onClick=${() => focusShot(x.id)} />`;
        })}
        ${shots.length > BATCH_THUMBS && html`<span class="batch-more">+${shots.length - BATCH_THUMBS}</span>`}
      </div>
    </div>
    ${sounding.length > 0 && html`<${Section} title="原聲音量" icon="volume">
      <${Field} label=${sameVolume == null ? "音量（目前各不相同）" : "音量"}
        hint=${`套用到 ${sounding.length} 個有原聲的鏡頭${shots.length > sounding.length ? `，略過 ${shots.length - sounding.length} 個沒有原聲或原聲已分離的鏡頭` : ""}。100% 是原始音量，最高 400%；預覽播放最高只到 100%`}>
        <${Slider} value=${sameVolume ?? 1} min=${0} max=${4} step=${0.05} format=${(v) => `${Math.round(v * 100)}%`}
          onCommit=${(v) => setVolume(round(v, 2))} />
      <//>
      <div class="row">
        <${Button} size="sm" icon="volume-x" onClick=${() => setVolume(0)}>全部靜音<//>
        <${Button} size="sm" icon="volume" onClick=${() => setVolume(1)}>恢復原音量<//>
      </div>
    <//>`}
    ${videoShots.length > 0 && html`<${Section} title="防抖" icon="camera">
      <${Field} label=${`${stabbed.length} / ${videoShots.length} 個影片鏡頭已防抖${busyStab ? `，${busyStab} 個處理中` : ""}`}
        hint=${`還沒做過的在背景處理，只處理各自的剪輯範圍，前後各多留 1 秒；畫面會稍微放大來裁掉晃動的黑邊${shots.length > videoShots.length ? `。略過 ${shots.length - videoShots.length} 個沒有影片的鏡頭` : ""}`}>
        <div class="row">
          <${Button} size="sm" icon="camera" disabled=${stabbed.length === stabFree.length}
            onClick=${() => stabilizeShots(stabFree.map((x) => x.id), true)}>全部防抖<//>
          <${Button} size="sm" icon="x" disabled=${!stabbed.length}
            onClick=${() => stabilizeShots(stabbed.map((x) => x.id), false)}>全部取消防抖<//>
        </div>
      <//>
    <//>`}
    <${Section} title="批次生成" icon="sparkles">
      <${Field} label="每個鏡頭的份數" inline>
        <${Segmented} size="sm" value=${count} onChange=${setCount} options=${COUNT_OPTIONS} />
      <//>
      <div class="btn-grid">
        <${Button} kind="primary" icon="image" onClick=${() => generate("image", ids, { count })}>生圖<//>
        <${Button} icon="film" onClick=${() => generate("video", ids, { count })}>生影片<//>
        <${Button} icon="camera" title="把鏡頭圖片做成推拉搖移的動畫，不需要 GPU"
          onClick=${() => imageTool("kenburns", (x) => ({ preset: kenburnsFor(x, presets) }))}>運鏡動畫<//>
        <${Button} icon="zoom-in" onClick=${() => imageTool("upscale", () => ({}))}>放大圖片<//>
        <${Button} icon="film" title="用 Video2X 把鏡頭影片放大到 1080P，方式照單鏡頭工具的設定"
          onClick=${upscaleVideos}>放大影片<//>
      </div>
    <//>
    <${Section} title="批次修改" icon="sliders">
      <${Field} label="運算路由">
        <${RouteChips} routes=${routes} value=${same("route")}
          onChange=${(route) => updateShots(ids, { route }, "批次變更路由")} />
      <//>
      <div class="row-2">
        <${Field} label="片長">
          <${NumberInput} value=${same("duration") ?? null} nullable placeholder="各不相同" min=${0.2} max=${600}
            step=${0.5} digits=${2} suffix="秒"
            onCommit=${(duration) => duration != null && updateShots(ids, { duration }, "批次修改片長")} />
        <//>
        <${Field} label="轉場">
          <${Select} value=${sameType} placeholder="各不相同" onChange=${setTransition}
            options=${(presets.transitions || []).map((t) => ({ value: t.id, label: t.label }))} />
        <//>
      </div>
      <${Field} label="輸出序列">
        <div class="row">
          <${Button} size="sm" icon="eye" onClick=${() => updateShots(ids, { inSequence: true }, "全部放入序列")}>全部放入<//>
          <${Button} size="sm" icon="eye-off" onClick=${() => updateShots(ids, { inSequence: false }, "全部移出序列")}>全部移出<//>
        </div>
      <//>
      <${Field} label="影片起始畫面" hint="沒有圖片的鏡頭一律用文字生成">
        <div class="row">
          <${Button} size="sm" icon="image" onClick=${() => updateShots(ids, { videoUseImage: true }, "全部用鏡頭圖片當起始畫面")}>全部用鏡頭圖片<//>
          <${Button} size="sm" icon="type" onClick=${() => updateShots(ids, { videoUseImage: false }, "全部改用文字生影片")}>全部用文字<//>
        </div>
      <//>
      <${Field} label="標籤顏色">
        <${ColorSwatches} value=${same("color")} onChange=${(color) => updateShots(ids, { color }, "批次修改顏色")} />
      <//>
    <//>
    <div class="insp-foot">
      <${Button} icon="copy" onClick=${() => duplicateShots(ids)}>複製<//>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${() => deleteShots(ids)}>刪除 ${shots.length} 個鏡頭<//>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- one shot
function shotMenu(e, project, shot, index) {
  const last = project.shots.length - 1;
  openMenu(e, [
    { label: "複製鏡頭", icon: "copy", hint: keyLabel("board.duplicate"), onClick: () => duplicateShots([shot.id]) },
    { label: "移到最前面", icon: "to-top", disabled: index === 0, onClick: () => moveShot(shot.id, 0) },
    { label: "移到最後面", icon: "arrow-right", disabled: index === last, onClick: () => moveShot(shot.id, last) },
    { label: shot.inSequence === false ? "放入輸出序列" : "移出輸出序列", icon: shot.inSequence === false ? "eye" : "eye-off",
      onClick: () => updateShots([shot.id], { inSequence: shot.inSequence === false }, shot.inSequence === false ? "放入序列" : "移出序列") },
    { divider: true },
    { label: "刪除鏡頭", icon: "trash", danger: true, hint: keyLabel("board.delete"), onClick: () => deleteShots([shot.id]) },
  ]);
}

function ShotPanel({ project, shot, index, tab }) {
  const { pid, jobs } = useStore((s) => ({ pid: s.pid, jobs: s.jobs }));
  const takes = useMemo(() => takesOf(project, shot.id), [project.takes, shot.id]);
  const busy = useMemo(() => activeJobsFor(jobs, pid, shot.id), [jobs, pid, shot.id]);
  const cover = shotCover(project, shot);
  const shots = project.shots;
  const go = (d) => {
    const next = shots[index + d];
    if (next) focusShot(next.id);
  };
  const Body = { content: ContentTab, generate: GenerateTab, takes: TakesTab, edit: EditTab }[tab] || ContentTab;
  const tabs = TABS.map((t) => (t.id === "takes" ? { ...t, badge: takes.length || null } : t));

  return html`<div class="insp-shot">
    <header class="insp-head">
      <${Thumb} pid=${pid} path=${cover ? cover.path : ""} class="insp-thumb" ratio=${`${project.settings.width}/${project.settings.height}`} icon="clapper"
        style=${shot.color ? `--shot-color:${shot.color}` : ""} />
      <div class="insp-title">
        <h2 title=${shotLabel(shot, index)}>${shotLabel(shot, index)}</h2>
        <span class="insp-sub">
          ${fmtSeconds(shot.duration)}<i></i>路由 ${shot.route}<i></i>${takes.length} 個版本
          ${shot.inSequence === false && html`<i></i><span class="warn-text">不在序列</span>`}
        </span>
      </div>
      <div class="insp-nav">
        <${IconButton} icon="chevron-left" size=${16} title="上一個鏡頭" disabled=${index === 0} onClick=${() => go(-1)} />
        <${IconButton} icon="chevron-right" size=${16} title="下一個鏡頭" disabled=${index >= shots.length - 1} onClick=${() => go(1)} />
        <${IconButton} icon="more" size=${16} title="更多動作" onClick=${(e) => shotMenu(e, project, shot, index)} />
      </div>
    </header>
    ${busy.length > 0 && html`<button type="button" class="insp-busy" onClick=${() => navigate("queue")}
        title="開啟工作佇列">
      <${Spinner} size=${14} />
      <span>${busy.some((j) => j.status === "running") ? "生成中" : "排隊中"}：${busy.length} 個工作</span>
      <span class="grow"></span>
      <${Icon} name="arrow-right" size=${14} />
    </button>`}
    <${Tabs} class="insp-tabs" value=${tab} tabs=${tabs} onChange=${(inspectorTab) => setState({ inspectorTab })} />
    <div class="insp-scroll" key=${`${shot.id}:${tab}`}>
      <${Body} project=${project} shot=${shot} index=${index} takes=${takes} busy=${busy} />
    </div>
  </div>`;
}

export function Inspector() {
  const { project, selected, focus, tab } = useStore((s) => ({
    project: s.project, selected: s.selected, focus: s.focus, tab: s.inspectorTab,
  }));
  if (!project) return null;
  let body;
  if (selected.length > 1) {
    body = html`<${BatchPanel} project=${project} ids=${selected} />`;
  } else {
    const id = selected[0] || focus;
    const index = id ? project.shots.findIndex((x) => x.id === id) : -1;
    body = index >= 0
      ? html`<${ShotPanel} project=${project} shot=${project.shots[index]} index=${index} tab=${tab} />`
      : html`<${ProjectPanel} project=${project} />`;
  }
  return html`<aside class="inspector" aria-label="屬性面板">${body}</aside>`;
}
