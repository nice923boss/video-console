// Caption spelling check against the user's word list (settings glossary, CP-12). Each line is a correct term,
// optionally followed by "=" and wrong forms already seen, separated by commas: "黃政文=黃正文,黃正雯".

const WORD = /[A-Za-z0-9]/;
const MIN_NEAR = 3; // shorter terms are checked only through their listed wrong forms

export function parseGlossary(text) {
  const entries = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const [head, ...rest] = line.split("=");
    const term = head.trim();
    if (!term) continue;
    const wrong = rest.join("=").split(/[,，、]/).map((w) => w.trim()).filter((w) => w && w !== term);
    entries.push({ term, wrong });
  }
  return entries;
}

// A run of text that stands as a whole word where the term starts or ends with a Latin letter or digit, so
// "Sora" is not checked inside "Soraya".
function bounded(text, i, n, term) {
  return !(WORD.test(term[0]) && i > 0 && WORD.test(text[i - 1]))
    && !(WORD.test(term[n - 1]) && i + n < text.length && WORD.test(text[i + n]));
}

// The spots in a caption that look like a misspelt term: a listed wrong form, or a run as long as a term (three
// characters or more) that differs from it in one character or only in letter case. A run that is itself one
// of the terms is left alone. Returns [{found, term}], each pair once.
export function glossaryHits(text, entries) {
  const src = String(text || "");
  const terms = new Set(entries.map((e) => e.term));
  const hits = new Map();
  const add = (found, term) => {
    if (!terms.has(found)) hits.set(`${found}\u0000${term}`, { found, term });
  };
  for (const { term, wrong } of entries) {
    for (const w of wrong) if (src.includes(w)) add(w, term);
    const n = term.length;
    if (n < MIN_NEAR) continue;
    const low = term.toLowerCase();
    for (let i = 0; i + n <= src.length; i++) {
      const run = src.slice(i, i + n);
      if (run === term || !bounded(src, i, n, term)) continue;
      const lowRun = run.toLowerCase();
      let diff = 0;
      for (let j = 0; j < n && diff < 2; j++) if (lowRun[j] !== low[j]) diff++;
      if (diff <= 1) add(run, term);
    }
  }
  return [...hits.values()];
}

// The caption text with every hit replaced by its term.
export function fixHits(text, hits) {
  return hits.reduce((out, h) => out.split(h.found).join(h.term), String(text || ""));
}

export const hitText = (hits) => hits.map((h) => `「${h.found}」→「${h.term}」`).join("、");
