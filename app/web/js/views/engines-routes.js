// Engines page, routes tab. A route pairs an image engine with a video engine (ComfyUI template on a
// server, or a cloud provider). Shots pick a route; the first route is the fallback when a shot's route
// was removed. The whole list is edited as a draft and saved in one go.
import { html, useState, useEffect, useMemo } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { saveSettings } from "../lib/actions.js";
import { Button, IconButton, Field, TextInput, Select, Segmented, Badge } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { confirmDialog } from "../components/overlay.js";
import { toast } from "../components/toast.js";

const NONE = [];
const PALETTE = ["#c6f24e", "#6ea8ff", "#ff9f5a", "#f06bd0", "#4fd1c5", "#ffd25a", "#a78bfa", "#ff6b6b"];
const BACKENDS = [
  { value: "comfy", label: "ComfyUI", icon: "server" },
  { value: "provider", label: "雲端 API", icon: "cloud" },
  { value: "none", label: "不使用", icon: "x" },
];
const KIND_TEXT = { image: "圖片", video: "影片" };
const MODE_TEXT = { t2i: "文字生圖", i2i: "參考圖生圖", i2v: "圖轉影片", t2v: "文字生影片", upscale: "放大" };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function nextId(routes) {
  const used = new Set(routes.map((r) => r.id));
  for (let c = 65; c <= 90; c += 1) {
    const id = String.fromCharCode(c);
    if (!used.has(id)) return id;
  }
  let n = 1;
  while (used.has(`R${n}`)) n += 1;
  return `R${n}`;
}

// Defaults that make a freshly switched backend usable right away.
function sectionFor(kind, backend, workflows, providers) {
  if (backend === "comfy") {
    const tpl = workflows.find((w) => w.kind === kind && (kind === "video" || w.mode === "t2i"));
    const out = { backend, server: "auto", workflow: tpl ? tpl.id : "" };
    if (kind === "image") out.workflowI2I = "";
    return out;
  }
  if (backend === "provider") {
    const prov = providers.find((p) => p.kind === kind);
    return { backend, provider: prov ? prov.id : "" };
  }
  return { backend: "none" };
}

function workflowOptions(workflows, kind, modes, current, emptyLabel) {
  const list = workflows.filter((w) => w.kind === kind && (!modes || modes.includes(w.mode)));
  const opts = list.map((w) => ({ value: w.id, label: `${w.name}（${MODE_TEXT[w.mode] || w.mode}）` }));
  if (current && !list.some((w) => w.id === current)) opts.unshift({ value: current, label: `找不到範本：${current}` });
  return emptyLabel != null ? [{ value: "", label: emptyLabel }, ...opts] : opts;
}

function EngineSection({ kind, value, onChange, servers, workflows, providers }) {
  const sec = value || { backend: "none" };
  const set = (patch) => onChange({ ...sec, ...patch });
  const serverOpts = [
    { value: "auto", label: "自動：依伺服器順序，選第一台在線且模型齊全的" },
    ...servers.map((s) => ({ value: s.id, label: s.enabled === false ? `${s.name}（已停用）` : s.name })),
  ];
  if (sec.server && sec.server !== "auto" && !servers.some((s) => s.id === sec.server)) {
    serverOpts.push({ value: sec.server, label: `找不到伺服器：${sec.server}` });
  }
  const provList = providers.filter((p) => p.kind === kind);
  const provOpts = provList.map((p) => ({ value: p.id, label: p.vendor && p.vendor !== p.name ? `${p.name}（${p.vendor}）` : p.name }));
  const prov = provList.find((p) => p.id === sec.provider);
  const keyMissing = prov && !prov.id.startsWith("custom-") && (prov.secrets || NONE).some((s) => !prov.secretSet[s.key]);

  return html`<div class=${`route-engine kind-${kind}`}>
    <div class="route-engine-head">
      <${Icon} name=${kind === "image" ? "image" : "film"} size=${16} />
      <b>${KIND_TEXT[kind]}</b>
      <${Segmented} size="sm" value=${sec.backend} options=${BACKENDS}
        onChange=${(b) => b !== sec.backend && onChange(sectionFor(kind, b, workflows, providers))} />
    </div>
    ${sec.backend === "comfy" && html`<div class="route-engine-body">
      <${Field} label="伺服器" inline>
        <${Select} value=${sec.server || "auto"} options=${serverOpts} onChange=${(v) => set({ server: v })} />
      <//>
      <${Field} label=${kind === "image" ? "文字生圖範本" : "影片範本"} inline>
        <${Select} value=${sec.workflow || ""} placeholder="選擇範本"
          options=${workflowOptions(workflows, kind, kind === "image" ? ["t2i", "i2i"] : null, sec.workflow)}
          onChange=${(v) => set({ workflow: v })} />
      <//>
      ${kind === "image" && html`<${Field} label="有參考圖時" inline hint="鏡頭設定了參考圖就改用這個範本；留空則一律用上面的範本">
        <${Select} value=${sec.workflowI2I || ""}
          options=${workflowOptions(workflows, "image", ["i2i"], sec.workflowI2I, "沿用文字生圖範本")}
          onChange=${(v) => set({ workflowI2I: v })} />
      <//>`}
    </div>`}
    ${sec.backend === "provider" && html`<div class="route-engine-body">
      <${Field} label="供應商" inline>
        <${Select} value=${sec.provider || ""} placeholder="選擇供應商" options=${provOpts}
          onChange=${(v) => set({ provider: v })} />
      <//>
      ${keyMissing && html`<p class="warn-text route-warn"><${Icon} name="key" size=${13} />
        ${prov.name} 還沒有填寫金鑰，請到「雲端 API」分頁設定</p>`}
    </div>`}
    ${sec.backend === "none" && html`<p class="dim route-engine-body">使用這條路由的鏡頭不能生成${KIND_TEXT[kind]}，可以自行匯入。</p>`}
  </div>`;
}

function RouteCard({ route, index, count, isDefault, onChange, onMove, onRemove, onDefault, lists }) {
  return html`<article class="route-card" style=${`--route:${route.color}`}>
    <header class="route-card-head">
      <label class="route-color" title="路由顏色，分鏡卡片上的色標">
        <input type="color" value=${route.color} onInput=${(e) => onChange({ ...route, color: e.currentTarget.value })} />
      </label>
      <${Badge} tone="neutral" title="路由代號，鏡頭以此記錄使用的路由">${route.id}<//>
      <${TextInput} class="route-name" value=${route.name} maxLength=${40} placeholder="路由名稱"
        onChange=${(v) => onChange({ ...route, name: v })} />
      ${isDefault
        ? html`<${Badge} tone="accent" title="新建立的專案與鏡頭預設使用這條路由">新專案預設<//>`
        : html`<${Button} size="sm" kind="ghost" icon="star" onClick=${onDefault}>設為預設<//>`}
      ${index === 0 && html`<${Badge} tone="neutral" title="鏡頭指定的路由被刪除時，改用第一條路由">後備<//>`}
      <span class="grow"></span>
      <${IconButton} icon="chevron-up" title="往前移" disabled=${index === 0} onClick=${() => onMove(-1)} />
      <${IconButton} icon="chevron-down" title="往後移" disabled=${index === count - 1} onClick=${() => onMove(1)} />
      <${IconButton} icon="trash" title=${count === 1 ? "至少要保留一條路由" : "刪除路由"} disabled=${count === 1}
        onClick=${onRemove} />
    </header>
    <div class="route-card-body">
      ${["image", "video"].map((kind) => html`<${EngineSection} key=${kind} kind=${kind} value=${route[kind]}
        onChange=${(sec) => onChange({ ...route, [kind]: sec })} ...${lists} />`)}
    </div>
  </article>`;
}

export function RoutesTab() {
  const { settings, workflows, providers, secrets } = useStore((s) => ({
    settings: s.settings, workflows: s.workflows, providers: s.providers, secrets: s.secrets,
  }));
  const saved = settings.routes || NONE;
  const savedDefault = (settings.defaults && settings.defaults.route) || "";
  const [draft, setDraft] = useState(saved);
  const [defRoute, setDefRoute] = useState(savedDefault);
  const [busy, setBusy] = useState(false);
  const savedKey = JSON.stringify(saved);
  useEffect(() => {
    setDraft(saved);
    setDefRoute(savedDefault);
  }, [savedKey, savedDefault]);

  // Which keys each provider already has, so a route card can warn before anything is generated.
  const provs = useMemo(() => (providers || NONE).map((p) => ({
    ...p,
    secretSet: Object.fromEntries((p.secrets || NONE).map((s) => [s.key,
      !!(secrets[p.secretNames[s.key]] && secrets[p.secretNames[s.key]].set)])),
  })), [providers, secrets]);
  const lists = { servers: (settings.comfy && settings.comfy.servers) || NONE, workflows: workflows || NONE, providers: provs };
  const dirty = !same(draft, saved) || defRoute !== savedDefault;
  const nameless = draft.some((r) => !r.name.trim());

  const update = (i) => (route) => setDraft((d) => d.map((r, j) => (j === i ? route : r)));
  const move = (i) => (dir) => setDraft((d) => {
    const next = d.slice();
    const [r] = next.splice(i, 1);
    next.splice(i + dir, 0, r);
    return next;
  });
  const remove = (i) => async () => {
    const route = draft[i];
    const ok = await confirmDialog({ title: `刪除路由「${route.name}」`, danger: true, confirmLabel: "刪除",
      message: "使用這條路由的鏡頭會改用第一條路由生成。按「儲存」後才會生效。" });
    if (!ok) return;
    const next = draft.filter((_, j) => j !== i);
    setDraft(next);
    if (defRoute === route.id) setDefRoute(next[0].id);
  };
  const add = () => setDraft((d) => {
    const id = nextId(d);
    const color = PALETTE.find((c) => !d.some((r) => r.color === c)) || PALETTE[d.length % PALETTE.length];
    return [...d, {
      id, name: `路由 ${id}`, color,
      image: sectionFor("image", "comfy", lists.workflows, provs),
      video: sectionFor("video", "comfy", lists.workflows, provs),
    }];
  });
  const save = async () => {
    setBusy(true);
    const patch = { routes: draft.map((r) => ({ ...r, name: r.name.trim() })) };
    if (defRoute !== savedDefault) patch.defaults = { route: defRoute };
    const ok = await saveSettings(patch);
    setBusy(false);
    if (ok) toast("已儲存路由設定", { level: "success" });
  };
  const revert = () => {
    setDraft(saved);
    setDefRoute(savedDefault);
  };

  return html`<div class="eng-pane">
    <div class="eng-intro">
      <p>路由決定鏡頭用哪個引擎生成：每條路由各指定一個圖片引擎與影片引擎。分鏡板與鏡頭檢視器可以替每個鏡頭切換路由，
        例如同一支片的草稿用本機 ComfyUI、定稿改走雲端 API。</p>
    </div>
    <div class="route-list">
      ${draft.map((r, i) => html`<${RouteCard} key=${r.id} route=${r} index=${i} count=${draft.length}
        isDefault=${defRoute === r.id} onDefault=${() => setDefRoute(r.id)} lists=${lists}
        onChange=${update(i)} onMove=${move(i)} onRemove=${remove(i)} />`)}
    </div>
    <div class="eng-foot">
      <${Button} icon="plus" disabled=${draft.length >= 12} onClick=${add}>新增路由<//>
      <span class="grow"></span>
      ${dirty && html`<span class="warn-text eng-dirty"><${Icon} name="dot" size=${14} />有未儲存的變更</span>`}
      <${Button} kind="ghost" icon="undo" disabled=${!dirty || busy} onClick=${revert}>還原<//>
      <${Button} kind="primary" icon="save" busy=${busy} disabled=${!dirty || nameless}
        title=${nameless ? "每條路由都要有名稱" : undefined} onClick=${save}>儲存<//>
    </div>
  </div>`;
}
