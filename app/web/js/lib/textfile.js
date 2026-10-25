// Text files (scripts, subtitles) from Windows tools are often Big5, GBK, Shift-JIS or UTF-16 instead of UTF-8.
// decodeText tries a byte-order mark, then strict UTF-8, then each legacy encoding that decodes without errors,
// and picks the one whose result reads like real text; when none decodes cleanly, the lines that lost characters
// are reported so the caller can ask the user to pick another encoding instead of silently showing garbage.

export const TEXT_ENCODINGS = [
  { value: "auto", label: "自動偵測" },
  { value: "utf-8", label: "UTF-8" },
  { value: "big5", label: "Big5（繁體中文）" },
  { value: "gbk", label: "GBK（簡體中文）" },
  { value: "shift_jis", label: "Shift-JIS（日文）" },
  { value: "utf-16le", label: "UTF-16 LE" },
  { value: "utf-16be", label: "UTF-16 BE" },
];

const LEGACY = ["big5", "gbk", "shift_jis"];
const BAD = String.fromCharCode(0xfffd);
// Very common characters in Traditional and Simplified Chinese: a wrong legacy decode turns into rare ones.
const COMMON = new Set([..."的一是不了在人有我他這个這個們们來来為为說说時时會会對对過过還还後后麼么樣样與与從从學学國国見见現现"
  + "點点開开問问經经關关裡里間间發发動动話话長长當当給给讓让應应實实種种體体電电中上大和地到以要就出可也你能而子那得著着下自之年用道行所然家事"
  + "成方多去法如都同天分好小其些主心本前但因只想日者意無无力把十第公此已工使情明性知全三又正外兩两高由很最重物手向頭头文字幕畫画面鏡镜旁白"]);
const isKana = (code) => code >= 0x3040 && code <= 0x30ff;

function score(text) {
  let n = 0;
  for (const ch of text) if (COMMON.has(ch) || isKana(ch.charCodeAt(0))) n += 1;
  return n;
}

function decode(buf, encoding, fatal) {
  try {
    return new TextDecoder(encoding, { fatal }).decode(buf);
  } catch {
    return null;
  }
}

// 1-based numbers of the lines that hold a replacement character (bytes the encoding could not read).
function badLines(text) {
  const out = [];
  text.split(/\r\n|\r|\n/).forEach((line, i) => {
    if (line.includes(BAD)) out.push(i + 1);
  });
  return out;
}

function bomEncoding(bytes) {
  if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) return "utf-8";
  if (bytes[0] === 0xff && bytes[1] === 0xfe) return "utf-16le";
  if (bytes[0] === 0xfe && bytes[1] === 0xff) return "utf-16be";
  return null;
}

function result(text, encoding) {
  return { text, encoding, badLines: badLines(text) };
}

// { text, encoding, badLines } for an ArrayBuffer; encoding "auto" detects it, any other value forces it.
export function decodeText(buf, encoding = "auto") {
  const bytes = new Uint8Array(buf);
  if (encoding !== "auto") return result(decode(bytes, encoding, false) || "", encoding);
  const bom = bomEncoding(bytes);
  if (bom) return result(decode(bytes, bom, false) || "", bom);
  const utf8 = decode(bytes, "utf-8", true);
  if (utf8 !== null) return result(utf8, "utf-8");
  const clean = LEGACY.map((enc) => ({ enc, text: decode(bytes, enc, true) })).filter((c) => c.text !== null);
  if (clean.length) {
    const best = clean.reduce((a, b) => (score(b.text) > score(a.text) ? b : a));
    return result(best.text, best.enc);
  }
  // Nothing decodes cleanly: keep the reading that loses the fewest characters, and say which lines lost them.
  const loose = ["utf-8", ...LEGACY].map((enc) => {
    const text = decode(bytes, enc, false) || "";
    return { enc, text, bad: text.split(BAD).length - 1 };
  });
  const best = loose.reduce((a, b) => (b.bad < a.bad || (b.bad === a.bad && score(b.text) > score(a.text)) ? b : a));
  return result(best.text, best.enc);
}

export const encodingLabel = (encoding) => (TEXT_ENCODINGS.find((e) => e.value === encoding) || { label: encoding }).label;

// "第 3、7、12 行" (at most five numbers, then "等 N 行").
export function linesLabel(lines) {
  const head = lines.slice(0, 5).join("、");
  return lines.length > 5 ? `第 ${head} 等 ${lines.length} 行` : `第 ${head} 行`;
}
