// Status of a configured model against the vendor's detected list. Mirrors model_status and model_warning
// in app/vc/providers/catalog.py; the same wording reaches the job notes.
const NONE = [];

const WARNINGS = {
  deprecated: (model, vendor) => `模型 ${model} 已被${vendor}標記為停用`,
  missing: (model, vendor) => `${vendor}的模型清單裡已找不到 ${model}（可能已下架）`,
  unlisted: (model, vendor) => `${vendor}的公開模型清單查不到 ${model}（可能已下架、打錯字，或是私人部署）`,
  incompatible: (model) => `模型 ${model} 需要本程式不會送出的參數，送出後可能失敗`,
};

export const UNUSABLE = new Set(Object.keys(WARNINGS));

export function modelStatus(models, key, value) {
  const field = models && models.fields && models.fields[key];
  const v = String(value || "").trim().replace(/^models\//, "");
  if (!field || !v) return "unknown";
  if ((field.options || NONE).some((o) => o.value === v)) return "ok";
  return (field.known || {})[v] || (field.complete ? "missing" : "unknown");
}

export function modelWarning(status, model, vendor) {
  return WARNINGS[status] ? WARNINGS[status](model, vendor || "供應商") : "";
}

// Model fields of a provider whose saved model is retired or unusable: [{key, label, text}].
export function savedModelProblems(provider) {
  const keys = [...new Set(Object.values(provider.modelKeys || {}))];
  const config = provider.config || {};
  return keys.map((key) => {
    const status = modelStatus(provider.models, key, config[key]);
    if (!UNUSABLE.has(status)) return null;
    const field = (provider.fields || NONE).find((f) => f.key === key);
    return { key, label: field ? field.label : key, text: modelWarning(status, config[key], provider.vendor) };
  }).filter(Boolean);
}
