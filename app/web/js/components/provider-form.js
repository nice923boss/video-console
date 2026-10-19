// Settings form for one cloud provider, built from the field list the server describes. Edits stay in
// a local draft until saved, so a half-typed URL never reaches a queued job; the test button checks
// the draft exactly as typed. Keys are saved on their own (SecretField), never as part of the config.
// Model fields offer the models detected from the vendor (newest first) and flag a retired one; detection
// runs when a key is saved, after a passing test and when the list is missing or old. A saved model is
// only ever changed by the user.
import { html, useState, useEffect, useMemo, useRef } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveProvider } from "../lib/actions.js";
import { fmtDateTime } from "../lib/format.js";
import { UNUSABLE, modelStatus, modelWarning } from "../lib/model-status.js";
import { Icon } from "./icons.js";
import { Field, TextInput, TextArea, NumberInput, Select, Toggle, Button, IconButton, Badge } from "./ui.js";
import { openMenu } from "./overlay.js";
import { toast } from "./toast.js";
import { SecretField } from "./secret-field.js";

const NONE = [];
const NO_SECRETS = {};
const MODE_LABEL = { t2i: "文字生圖", i2i: "參考圖生圖", t2v: "文字生影片", i2v: "圖生影片" };
const STALE_SECONDS = 3 * 86400;
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function jsonError(text) {
  if (!String(text || "").trim()) return "";
  try {
    JSON.parse(text);
    return "";
  } catch (err) {
    return `不是有效的 JSON：${err.message}`;
  }
}

function urlError(text) {
  const v = String(text || "").trim();
  return v && !/^https?:\/\/[^\s/]+/.test(v) ? "必須是 http:// 或 https:// 開頭的網址" : "";
}

function defaultsOf(fields) {
  return Object.fromEntries(fields.map((f) => [f.key, f.default]));
}

// A model field lists what detection found (newest first, the suggested one marked), else the built-in options.
function ComboInput({ field, value, onChange, detected }) {
  const found = (detected && detected.options) || NONE;
  const options = found.length ? found : field.options || NONE;
  const pick = (e) => openMenu(e, [
    { header: found.length ? "偵測到的可用模型（新到舊）" : "常用選項" },
    ...options.map((o) => ({
      label: o.value === detected?.suggested ? `${o.label}（建議）` : o.label,
      hint: o.date || "",
      title: o.label === o.value ? undefined : o.value,
      checked: o.value === value,
      onClick: () => onChange(o.value),
    })),
  ]);
  return html`<div class="combo">
    <${TextInput} mono value=${value} onChange=${onChange} spellcheck=${false} autocomplete="off" />
    ${options.length > 0 && html`<${IconButton} icon="chevron-down" size=${15}
      title=${found.length ? "從偵測到的模型挑選" : "從常用選項挑選"} onClick=${pick} />`}
  </div>`;
}

// Whether the typed model is still callable, judged against the last detection.
function ModelState({ detected, models, field, value, vendor, onChange }) {
  const model = String(value || "").trim();
  if (!detected || !model) return null;
  const status = modelStatus(models, field.key, model);
  const suggested = detected.suggested && detected.suggested !== model ? detected.suggested : "";
  const use = suggested && html`<${Button} size="sm" kind="ghost" title=${`改填 ${suggested}，按「儲存設定」後才生效`}
    onClick=${() => onChange(suggested)}>改用建議模型<//>`;
  if (UNUSABLE.has(status)) {
    return html`<div class="note note-warn" role="status">
      <${Icon} name="alert" size=${14} />
      <span class="note-text">${modelWarning(status, model, vendor)}${suggested ? `。建議改用 ${suggested}` : "。"}</span>
      ${use}
    </div>`;
  }
  const option = (detected.options || NONE).find((o) => o.value === model) || {};
  const about = [option.date, option.hint].filter(Boolean).join("，");
  const newer = status === "ok" && suggested && detected.for === model;
  return html`<div class="model-state">
    ${status === "ok"
      ? html`<span class="model-ok"><${Icon} name="check-circle" size=${13} />可以使用${about ? `（${about}）` : ""}</span>`
      : html`<span>偵測到的清單裡沒有這個模型，儲存後重新偵測可確認能否使用</span>`}
    ${newer && html`<span>有較新的同系列模型 <span class="mono">${suggested}</span></span>`}
    ${newer && use}
    ${detected.message && html`<span>${detected.message}</span>`}
  </div>`;
}

function FieldInput({ field, value, onChange, detected }) {
  switch (field.type) {
    case "bool":
      return html`<${Toggle} checked=${!!value} label=${field.label} onChange=${onChange} />`;
    case "select":
      return html`<${Select} value=${value} options=${field.options || NONE} onChange=${onChange} />`;
    case "number": {
      const int = Number.isInteger(field.default);
      return html`<${NumberInput} value=${value} min=${field.min} max=${field.max} step=${field.step ?? 1}
        digits=${int ? 0 : 2} onCommit=${(v) => onChange(v == null ? field.default : int ? Math.round(v) : v)} />`;
    }
    case "combo":
      return html`<${ComboInput} field=${field} value=${value ?? ""} onChange=${onChange} detected=${detected} />`;
    case "json":
      return html`<${TextArea} mono autosize rows=${2} value=${value ?? ""} onChange=${onChange} spellcheck=${false} />`;
    case "url":
      return html`<${TextInput} mono value=${value ?? ""} onChange=${onChange} placeholder="https://" spellcheck=${false} />`;
    default:
      return html`<${TextInput} value=${value ?? ""} onChange=${onChange} spellcheck=${false} />`;
  }
}

function TestResult({ result }) {
  const tone = result.error ? "danger" : result.online ? "ok" : "info";
  const icon = { danger: "x-circle", ok: "check-circle", info: "info" }[tone];
  const head = result.error ? "測試失敗" : result.online ? "連線成功" : "已檢查設定";
  return html`<div class=${`note note-${tone}`} role="status">
    <${Icon} name=${icon} size=${14} />
    <span class="note-text"><b>${head}</b>${result.message ? `：${result.message}` : ""}</span>
  </div>`;
}

export function ProviderForm({ provider }) {
  const providers = useStore((s) => s.providers || NONE);
  const fields = provider.fields || NONE;
  const [draft, setDraft] = useState(provider.config || {});
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState(null);
  const [detected, setDetected] = useState(null); // a detection run on the unsaved draft
  const [detecting, setDetecting] = useState(false);
  const [detectError, setDetectError] = useState("");
  const secrets = useStore((s) => s.secrets || NO_SECRETS);
  const configKey = JSON.stringify(provider.config || {});
  const modelKeys = useMemo(() => new Set(Object.values(provider.modelKeys || {})), [provider.modelKeys]);
  const models = detected || provider.models || null;
  const keys = (provider.secrets || NONE).map((s) => secrets[provider.secretNames[s.key]] || NO_SECRETS);
  const hasKey = keys.length > 0 && keys.every((k) => k.set && !k.unreadable);
  const keySig = keys.map((k) => k.masked || "").join("|");
  const lastKeySig = useRef(keySig);

  useEffect(() => {
    setDraft(provider.config || {});
    setResult(null);
    setDetected(null);
  }, [provider.id, configKey]);

  const errors = useMemo(() => Object.fromEntries(fields.map((f) => [f.key,
    f.type === "json" ? jsonError(draft[f.key]) : f.type === "url" ? urlError(draft[f.key]) : ""])), [fields, draft]);
  const invalid = Object.values(errors).some(Boolean);
  const dirty = !same(draft, provider.config || {});
  const atDefaults = same(draft, defaultsOf(fields));
  const owner = provider.keyOwner && providers.find((p) => p.id === provider.keyOwner);
  const shared = owner ? `與「${owner.name}」共用，在這裡更換也會套用到那邊。` : "";

  const set = (key) => (value) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setResult(null);
  };

  const labelOf = (key) => (fields.find((f) => f.key === key) || {}).label || key;

  // Without a draft the server may move model fields the user never saved off a retired default ("pinned").
  const detect = async ({ quiet = false, body = dirty ? { config: draft } : {} } = {}) => {
    setDetecting(true);
    setDetectError("");
    try {
      const res = await api.post(`/api/providers/${encodeURIComponent(provider.id)}/models`, body);
      setState((s) => ({ providers: s.providers.map((p) => (p.id === provider.id ? res.provider : p)) }));
      setDetected(body.config ? res.models : null);
      const pinned = Object.entries(res.pinned || {});
      if (pinned.length) {
        toast(pinned.map(([k, v]) => `${labelOf(k)}改用 ${v}`).join("；"), { level: "success", title: "原本的預設模型已過時" });
      } else if (!quiet) {
        toast(`已更新「${provider.name}」的模型清單`, { level: "success" });
      }
    } catch (err) {
      setDetectError(err.message);
    } finally {
      setDetecting(false);
    }
  };

  useEffect(() => {
    const age = models ? Date.now() / 1000 - models.fetchedAt : Infinity;
    if (modelKeys.size && hasKey && age > STALE_SECONDS) detect({ quiet: true });
  }, [provider.id]);

  useEffect(() => {
    if (keySig === lastKeySig.current) return;
    lastKeySig.current = keySig;
    if (modelKeys.size && hasKey) detect();
  }, [keySig]);

  const save = async () => {
    setSaving(true);
    const info = await saveProvider(provider.id, draft);
    setSaving(false);
    if (info) {
      setDraft(info.config || {});
      toast(`已儲存「${provider.name}」的設定`, { level: "success" });
      const unchecked = [...modelKeys].some((k) => modelStatus(info.models, k, info.config[k]) === "unknown");
      if (unchecked && hasKey) detect({ quiet: true, body: {} });
    }
  };

  const test = async () => {
    setTesting(true);
    setResult(null);
    let ok = false;
    try {
      const res = await api.post(`/api/providers/${encodeURIComponent(provider.id)}/test`, { config: draft });
      ok = !!res.ok;
      setResult(res.ok ? res : { ...res, error: true });
    } catch (err) {
      setResult({ error: true, message: err.message });
    } finally {
      setTesting(false);
    }
    if (ok && modelKeys.size) detect({ quiet: true });
  };

  return html`<div class="prov-form">
    <header class="prov-head">
      <div class="prov-title">
        <h3>${provider.name}</h3>
        ${provider.vendor && html`<span class="dim">${provider.vendor}</span>`}
      </div>
      <div class="prov-modes">
        <${Badge} tone=${provider.kind === "video" ? "blue" : "accent"}>${provider.kind === "video" ? "影片" : "圖片"}<//>
        ${(provider.modes || NONE).map((m) => html`<${Badge} key=${m} tone="neutral">${MODE_LABEL[m] || m}<//>`)}
        ${provider.resumable && html`<${Badge} tone="neutral" title="程式重新啟動後，會繼續等待已送出的雲端任務">可接續<//>`}
      </div>
      ${provider.docs && html`<a class="prov-docs" href=${provider.docs} target="_blank" rel="noopener noreferrer">
        <${Icon} name="key" size=${14} />取得 API 金鑰<${Icon} name="external" size=${12} />
      </a>`}
    </header>
    ${provider.notes && html`<p class="prov-notes">${provider.notes}</p>`}

    ${(provider.secrets || NONE).length > 0 && html`<section class="prov-section">
      ${provider.secrets.map((s) => html`<${SecretField} key=${s.key} name=${provider.secretNames[s.key]}
        label=${s.label} help=${s.help || ""} shared=${shared} />`)}
    </section>`}

    ${fields.length > 0 && html`<section class="prov-section">
      ${modelKeys.size > 0 && html`<div class="prov-models">
        <${Icon} name=${detectError ? "alert" : "sparkles"} size=${14} />
        <span class=${`prov-models-text${detectError ? " warn-text" : ""}`}>${detectError
          || (models ? `模型清單：上次偵測 ${fmtDateTime(models.fetchedAt)}，下拉選單依新到舊列出可呼叫的模型`
            : "尚未偵測模型清單。填好金鑰後會自動偵測，也可以按右側按鈕")}</span>
        <${Button} size="sm" kind="ghost" icon="refresh" busy=${detecting} title="向供應商查詢目前可呼叫的模型"
          onClick=${() => detect()}>${models ? "重新偵測" : "偵測模型"}<//>
      </div>`}
      ${fields.map((f) => {
        const own = modelKeys.has(f.key) && models && models.fields ? models.fields[f.key] || null : null;
        return html`<${Field} key=${f.key} label=${f.type === "bool" ? "" : f.label}
            hint=${f.help || ""} error=${errors[f.key]}>
          <${FieldInput} field=${f} value=${draft[f.key]} onChange=${set(f.key)} detected=${own} />
          <${ModelState} detected=${own} models=${models} field=${f} value=${draft[f.key]} vendor=${provider.vendor}
            onChange=${set(f.key)} />
        <//>`;
      })}
    </section>`}

    ${result && html`<${TestResult} result=${result} />`}

    <footer class="prov-foot">
      ${dirty && html`<span class="prov-dirty warn-text"><${Icon} name="dot" size=${14} />有未儲存的變更</span>`}
      <span class="grow"></span>
      ${!atDefaults && html`<${Button} size="sm" kind="ghost" icon="undo" title="把所有欄位恢復成預設值（不影響金鑰）"
        onClick=${() => { setDraft(defaultsOf(fields)); setResult(null); }}>還原預設值<//>`}
      <${Button} size="sm" icon="plug" busy=${testing} disabled=${invalid}
        title=${provider.onlineTest ? "用目前填寫的內容實際連線到供應商" : "這個供應商沒有免費的檢查端點，只會確認金鑰與設定已填寫"}
        onClick=${test}>${provider.onlineTest ? "測試連線" : "檢查設定"}<//>
      <${Button} size="sm" kind="primary" icon="save" busy=${saving} disabled=${!dirty || invalid} onClick=${save}>儲存設定<//>
    </footer>
  </div>`;
}
