// Engines page, cloud API tab: every supported provider grouped by kind with its key status, and the
// settings form of the selected one. The selection lives in the store so other pages can link here.
import { html } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { Dot, Empty } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { ProviderForm } from "../components/provider-form.js";
import { savedModelProblems } from "../lib/model-status.js";

const NONE = [];
const GROUPS = [
  { kind: "image", label: "生圖", icon: "image" },
  { kind: "video", label: "圖轉影片與文字生影片", icon: "film" },
];

function keyStatus(p, secrets) {
  const names = p.secretNames || {};
  const entries = (p.secrets || NONE).map((s) => secrets[names[s.key]] || {});
  if (entries.some((e) => e.unreadable)) return { tone: "danger", text: "金鑰無法解密，請重新填寫" };
  if (entries.length && entries.every((e) => e.set)) return { tone: "ok", text: "金鑰已填寫" };
  if (p.id.startsWith("custom-")) return { tone: "neutral", text: "自訂端點，金鑰選填" };
  return { tone: "warn", text: "尚未填寫金鑰" };
}

function routeUsers(routes, pid) {
  return (routes || NONE).filter((r) => ["image", "video"].some((k) => {
    const sec = r[k] || {};
    return sec.backend === "provider" && sec.provider === pid;
  })).map((r) => r.name);
}

export function ProvidersTab() {
  const providers = useStore((s) => s.providers || NONE);
  const secrets = useStore((s) => s.secrets || {});
  const routes = useStore((s) => s.settings.routes);
  const sel = useStore((s) => s.providerSel);
  if (!providers.length) {
    return html`<${Empty} icon="cloud" title="讀不到雲端供應商清單">重新整理頁面再試一次。<//>`;
  }
  const current = providers.find((p) => p.id === sel) || providers[0];
  return html`<div class="prov-layout">
    <nav class="prov-list" aria-label="雲端供應商">
      ${GROUPS.map((g) => html`<section class="prov-group" key=${g.kind}>
        <h4><${Icon} name=${g.icon} size=${14} />${g.label}</h4>
        ${providers.filter((p) => p.kind === g.kind).map((p) => {
          const st = keyStatus(p, secrets);
          const users = routeUsers(routes, p.id);
          const problems = savedModelProblems(p);
          return html`<button type="button" key=${p.id} class=${`prov-item${p.id === current.id ? " active" : ""}`}
              aria-current=${p.id === current.id ? "true" : undefined} onClick=${() => setState({ providerSel: p.id })}>
            <${Dot} tone=${st.tone} title=${st.text} />
            <span class="prov-item-text">
              <span class="prov-item-name">${p.name}</span>
              <span class="prov-item-sub">${users.length ? `路由「${users.join("」「")}」使用中` : p.vendor || st.text}</span>
            </span>
            ${problems.length > 0 && html`<span class="prov-item-warn warn-text"
                title=${problems.map((x) => `${x.label}：${x.text}`).join("\n")}>
              <${Icon} name="alert" size=${14} />
            </span>`}
          </button>`;
        })}
      </section>`)}
    </nav>
    <div class="prov-detail">
      <${ProviderForm} key=${current.id} provider=${current} />
    </div>
  </div>`;
}
