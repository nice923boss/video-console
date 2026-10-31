// Client review page (審稿.html, see review_pack.py). Plays the exported video beside its shot list; a note sits
// on one frame with boxes, arrows and labels drawn on the picture (coordinates 0-1 of the frame), an insert puts
// the client's own picture or clip at a frame, and 更換配樂 sends a soundtrack to replace the background music.
// Playing previews both: an insert holds the video for its seconds, the soundtrack plays at the client's volume.
// 打包回傳 downloads one ZIP: 修改意見.json + 附件/. Drafts and the
// attached files are kept in this browser's IndexedDB; a browser that refuses it keeps them for this visit only.
(() => {
  "use strict";
  const meta = JSON.parse(document.getElementById("vc-meta").textContent);
  const $ = (id) => document.getElementById(id);
  const video = $("video");
  const ink = $("ink");
  const W = meta.width;
  const H = meta.height;
  const FPS = meta.fps || 30;
  const SVG = "http://www.w3.org/2000/svg";
  const MAX_ITEMS = 300;
  const MAX_MARKS = 40;
  const MAX_FILE = 1024 ** 3;
  const MAX_ZIP = 3.9 * 1024 ** 3; // no ZIP64: every size and offset must fit in 32 bits
  const IMAGE = /\.(png|jpe?g|webp|bmp|gif)$/i;
  const CLIP = /\.(mp4|webm|mov|mkv|m4v|avi)$/i;
  const SOUND = /\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i;
  const MUSIC = "music"; // the soundtrack's key in the files store, beside the item ids

  // music: { name, size, text, volume } (volume 0-1, null until the client moves the slider)
  const state = { by: "", note: "", items: [], seq: 0, music: null };
  const blobs = new Map(); // item id -> the picture or clip it inserts
  let musicBlob = null;
  let musicUrl = "";
  const unsaved = new Set(); // item ids (or MUSIC) whose file did not fit in the browser's storage
  let db = null;
  let editing = null; // { item, isNew, tool, blob, url }
  let draft = null; // the box or arrow being dragged out
  let origin = null;
  const tune = new Audio(); // the client's soundtrack, played beside the video
  // The cut's sound without its music (審稿匯出 writes it when the cut has music): with a new soundtrack the video
  // is muted and this plays in its place, so the old music drops out.
  let stem = meta.stem ? new Audio(encodeURIComponent(meta.stem)) : null;
  let cue = null; // { queue, i, timer, url }: the inserts of one frame, shown in place of the video
  let from = 0; // the preview has passed every insert at or before this time
  let resuming = false; // the video starts again after a cue, so `from` stays
  let looping = false;

  // ---------------------------------------------------------------- helpers
  const pad = (n) => String(n).padStart(2, "0");
  const clock = (t) => {
    const s = Math.max(0, t);
    const m = Math.floor(s / 60);
    return `${pad(m)}:${(s - m * 60).toFixed(2).padStart(5, "0")}`;
  };
  const frameOf = (t) => Math.max(0, Math.floor(t * FPS + 1e-3));
  const frameTime = (f) => Math.round((f / FPS) * 1000) / 1000;
  const lastFrame = () => Math.max(0, Math.ceil((video.duration || meta.duration) * FPS) - 1);
  const r4 = (v) => Math.round(v * 10000) / 10000;
  const clamp01 = (v) => Math.min(1, Math.max(0, v));
  const fmtSize = (n) => (n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const safeName = (name) => String(name).replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").replace(/^[\s.]+|[\s.]+$/g, "").slice(-80) || "file";
  const shotAt = (t) => meta.shots.reduce((hit, s) => (s.start <= t + 1e-6 ? s : hit), meta.shots[0] || null);
  const sorted = () => [...state.items].sort((a, b) => a.frame - b.frame || a.seq - b.seq);
  const byId = (id) => state.items.find((i) => i.id === id);
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function banner(text) {
    $("banner").textContent = text;
    $("banner").hidden = !text;
  }

  // ---------------------------------------------------------------- storage
  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open("vc-review", 1);
      req.onupgradeneeded = () => {
        req.result.createObjectStore("drafts");
        req.result.createObjectStore("files");
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(new Error("blocked"));
    });
  }
  function run(storeName, mode, fn) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, mode);
      const req = fn(tx.objectStore(storeName));
      tx.oncomplete = () => resolve(req && req.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }
  const fileKey = (id) => `${meta.review}/${id}`;
  let timer = 0;
  function save(now) {
    clearTimeout(timer);
    timer = setTimeout(persist, now ? 0 : 400);
  }
  async function persist() {
    showDraftState();
    if (!db) return;
    try {
      await run("drafts", "readwrite", (s) => s.put(JSON.parse(JSON.stringify(state)), meta.review));
    } catch (err) {
      db = null;
      showDraftState();
    }
  }
  async function keepFile(id, blob) {
    if (!db) return unsaved.add(id);
    try {
      await run("files", "readwrite", (s) => s.put(blob, fileKey(id)));
      unsaved.delete(id);
    } catch (err) {
      unsaved.add(id); // usually over the browser's storage quota: kept for this visit only
    }
  }
  async function dropFile(id) {
    unsaved.delete(id);
    if (db) await run("files", "readwrite", (s) => s.delete(fileKey(id))).catch(() => {});
  }
  async function restore() {
    try {
      db = await openDb();
    } catch (err) {
      db = null;
      return;
    }
    try {
      const saved = await run("drafts", "readonly", (s) => s.get(meta.review));
      if (!saved || !Array.isArray(saved.items)) return;
      Object.assign(state, { by: saved.by || "", note: saved.note || "", seq: saved.seq || 0, items: saved.items,
        music: saved.music || null });
      for (const item of state.items) {
        if (item.kind !== "insert") continue;
        const blob = await run("files", "readonly", (s) => s.get(fileKey(item.id)));
        if (blob) blobs.set(item.id, blob);
      }
      if (state.music) musicBlob = (await run("files", "readonly", (s) => s.get(fileKey(MUSIC)))) || null;
    } catch (err) {
      banner("讀不到上次的草稿，請重新填寫。");
    }
  }
  function showDraftState() {
    const lost = state.items.filter((i) => i.kind === "insert" && !blobs.has(i.id)).length;
    let text = "草稿自動存在這台電腦的瀏覽器，關掉再開會接著寫。";
    if (!db) text = "這個瀏覽器不能暫存草稿，關閉網頁前請先按「打包回傳」。";
    else if (unsaved.size) text = `有 ${unsaved.size} 個附件太大，沒能暫存，關閉網頁前請先按「打包回傳」。`;
    if (lost) text += `有 ${lost} 則插入的檔案遺失，請點開重新選擇檔案。`;
    if (state.music && !musicBlob) text += "配樂檔案遺失，請重新上傳。";
    $("draft-state").textContent = text;
  }

  // ---------------------------------------------------------------- picture marks
  function shapes(m) {
    const sw = Math.max(2, H * 0.006);
    const make = (tag, attrs) => {
      const n = document.createElementNS(SVG, tag);
      Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
      return n;
    };
    if (m.type === "rect") {
      return [make("rect", { x: m.x * W, y: m.y * H, width: m.w * W, height: m.h * H, rx: sw, fill: "none",
        stroke: "#ff3b30", "stroke-width": sw })];
    }
    if (m.type === "arrow") {
      const x1 = m.x1 * W, y1 = m.y1 * H, x2 = m.x2 * W, y2 = m.y2 * H;
      const a = Math.atan2(y2 - y1, x2 - x1);
      const len = Math.max(sw * 4, H * 0.03);
      const head = [[x2, y2], [x2 - len * Math.cos(a - 0.45), y2 - len * Math.sin(a - 0.45)],
        [x2 - len * Math.cos(a + 0.45), y2 - len * Math.sin(a + 0.45)]].map((p) => p.join(",")).join(" ");
      return [make("line", { x1, y1, x2: x2 - (len * 0.6) * Math.cos(a), y2: y2 - (len * 0.6) * Math.sin(a),
        stroke: "#ff3b30", "stroke-width": sw, "stroke-linecap": "round" }),
      make("polygon", { points: head, fill: "#ff3b30" })];
    }
    const size = H * 0.045;
    const t = make("text", { x: m.x * W, y: m.y * H, "font-size": size, fill: "#ff3b30", stroke: "#fff",
      "stroke-width": size * 0.18, "stroke-linejoin": "round" });
    t.textContent = m.text;
    return [t];
  }
  function renderInk() {
    const marks = editing ? editing.item.marks : [];
    ink.replaceChildren(...[...marks, ...(draft ? [draft] : [])].flatMap(shapes));
    ink.classList.toggle("drawing", !!editing && editing.item.kind === "note");
  }
  function point(e) {
    const r = ink.getBoundingClientRect();
    return { x: clamp01((e.clientX - r.left) / r.width), y: clamp01((e.clientY - r.top) / r.height) };
  }
  function dragShape(a, b) {
    if (editing.tool === "rect") {
      return { type: "rect", x: r4(Math.min(a.x, b.x)), y: r4(Math.min(a.y, b.y)), w: r4(Math.abs(a.x - b.x)),
        h: r4(Math.abs(a.y - b.y)) };
    }
    return { type: "arrow", x1: r4(a.x), y1: r4(a.y), x2: r4(b.x), y2: r4(b.y) };
  }
  ink.addEventListener("pointerdown", (e) => {
    if (!editing || editing.item.kind !== "note" || e.button !== 0) return;
    e.preventDefault();
    if (editing.item.marks.length >= MAX_MARKS) {
      alert(`一則意見最多 ${MAX_MARKS} 個標註，請分成兩則`);
      return;
    }
    const p = point(e);
    if (editing.tool === "text") {
      const text = (prompt("標註文字（例如：這裡換成新 logo）") || "").trim().slice(0, 80);
      if (text) editing.item.marks.push({ type: "text", x: r4(p.x), y: r4(p.y), text });
      renderInk();
      return;
    }
    ink.setPointerCapture(e.pointerId);
    origin = p;
    draft = dragShape(origin, p);
    renderInk();
  });
  ink.addEventListener("pointermove", (e) => {
    if (!origin) return;
    draft = dragShape(origin, point(e));
    renderInk();
  });
  const endDrag = () => {
    if (!origin) return;
    const big = draft.type === "rect" ? draft.w > 0.01 && draft.h > 0.01
      : Math.hypot(draft.x2 - draft.x1, draft.y2 - draft.y1) > 0.02;
    if (big) editing.item.marks.push(draft);
    origin = draft = null;
    renderInk();
  };
  ink.addEventListener("pointerup", endDrag);
  ink.addEventListener("pointercancel", endDrag);

  // ---------------------------------------------------------------- player
  const stageMsg = (text) => {
    $("stage-msg").textContent = text;
    $("stage-msg").hidden = !text;
  };
  function seekFrame(f) {
    const frame = Math.min(lastFrame(), Math.max(0, f));
    video.currentTime = (frame + 0.5) / FPS; // mid-frame, so the picture shown is this frame and not its neighbour
  }
  function tick() {
    const t = video.currentTime;
    $("clock").textContent = `${clock(frameTime(frameOf(t)))} / ${clock(video.duration || meta.duration)}`;
    $("scrub").value = t;
    const s = shotAt(t);
    $("shot-now").textContent = cue ? `插入素材：${cue.queue[cue.i].insert.name}` : s ? `#${pad(s.no)} ${s.name}` : "";
    document.querySelectorAll("#shots .row").forEach((r) => r.classList.toggle("is-on", s && r.dataset.id === s.id));
    $("play").textContent = video.paused && !cue ? "播放" : "暫停";
  }
  // One frame's work. timeupdate runs it too: it keeps firing in a background tab, where requestAnimationFrame stops.
  function step() {
    if (!video.paused && !cue) follow(video.currentTime);
    if (!video.paused) syncSound();
    tuneVolume();
    tick();
  }
  function loop() {
    step();
    looping = !video.paused || !!cue;
    if (looping) requestAnimationFrame(loop);
  }
  function startLoop() {
    if (!looping) requestAnimationFrame(loop);
    looping = true;
  }

  // ---------------------------------------------------------------- preview (inserts and the new soundtrack)
  const cues = () => sorted().filter((i) => i.kind === "insert" && blobs.has(i.id));
  // Seconds of inserts that play before time t: where the soundtrack is when the video is at t.
  const before = (t) => cues().reduce((n, i) => n + (frameTime(i.frame) <= t ? i.insert.duration : 0), 0);
  const musicVolume = () => (state.music && state.music.volume != null ? state.music.volume : meta.musicVolume ?? 1);
  function follow(t) {
    const due = cues().filter((i) => frameTime(i.frame) > from && frameTime(i.frame) <= t);
    if (!due.length) {
      from = t;
      return false;
    }
    const f = due[0].frame;
    cue = { queue: due.filter((i) => i.frame === f), i: 0, timer: 0, url: "" };
    video.pause();
    seekFrame(f);
    showCue();
    if (musicUrl && tune.paused) tune.play().catch(() => {});
    startLoop();
    return true;
  }
  function showCue() {
    const ins = cue.queue[cue.i].insert;
    if (cue.url) URL.revokeObjectURL(cue.url);
    cue.url = URL.createObjectURL(blobs.get(cue.queue[cue.i].id));
    const media = ins.type === "image" ? Object.assign(el("img"), { src: cue.url, alt: "" })
      : Object.assign(el("video"), { src: cue.url, playsInline: true, autoplay: true });
    $("cue").replaceChildren(media, el("span", "cue-tag", `${ins.type === "image" ? "插入圖片" : "插入影片"}：${ins.name}`));
    $("cue").hidden = false;
    cue.timer = setTimeout(nextCue, ins.duration * 1000);
  }
  function endCue() {
    clearTimeout(cue.timer);
    URL.revokeObjectURL(cue.url);
    $("cue").replaceChildren();
    $("cue").hidden = true;
    cue = null;
  }
  function nextCue() {
    cue.i += 1;
    if (cue.i < cue.queue.length) return showCue();
    const f = cue.queue[0].frame;
    endCue();
    from = frameTime(f) + 1e-6;
    resuming = true;
    seekFrame(f);
    video.play().catch(() => { resuming = false; halt(); });
  }
  function halt() {
    if (cue) endCue();
    video.pause();
    tune.pause();
    tick();
  }
  const togglePlay = () => (video.paused && !cue ? video.play().catch(() => {}) : halt());
  function syncSound() {
    const t = video.currentTime;
    if (video.muted && stem && Math.abs(stem.currentTime - t) > 0.15) stem.currentTime = t;
    if (!musicUrl) return;
    const at = t + before(from);
    if (at >= tune.duration) tune.pause();
    else if (Math.abs(tune.currentTime - at) > 0.25) tune.currentTime = at;
  }
  function tuneVolume() {
    // Cut at the end of the video with a fade, as 套用 does (revisions.music_plan).
    const length = (video.duration || meta.duration) + before(Infinity);
    const fade = tune.duration > length + 0.05 ? Math.min(2, length / 2) : 0;
    tune.volume = musicVolume() * (fade ? clamp01((length - tune.currentTime) / fade) : 1);
  }
  function renderSound() {
    video.muted = !!(musicUrl && stem);
    if (stem && !video.muted) stem.pause();
    $("music-hint").hidden = !musicUrl;
    $("music-hint").textContent = stem ? "按「播放」試聽，影片會改配這首音樂，旁白和現場聲音照常。"
      : meta.musicVolume != null ? "這份審稿影片沒有附去掉配樂的音軌，試聽時會同時聽到原本的配樂。"
        : "按「播放」試聽，這首音樂會加在影片原本的聲音下面。";
  }
  if (stem) stem.addEventListener("error", () => { stem = null; renderSound(); });
  video.addEventListener("loadedmetadata", () => {
    $("scrub").max = video.duration || meta.duration;
    renderMarkers();
    tick();
  });
  video.addEventListener("error", () => stageMsg(`找不到或無法播放影片「${meta.video}」。請把收到的資料夾整個解壓縮，`
    + "讓「審稿.html」和影片在同一個資料夾，再重新開啟這個網頁。"));
  video.addEventListener("play", () => {
    if (!resuming) from = frameTime(frameOf(video.currentTime)) - 1e-6;
    resuming = false;
    syncSound();
    if (video.muted && stem) stem.play().catch(() => {});
    tuneVolume();
    if (musicUrl && !(video.currentTime + before(from) >= tune.duration)) tune.play().catch(() => {});
    startLoop();
  });
  video.addEventListener("pause", () => {
    if (stem) stem.pause();
    if (!cue) tune.pause();
  });
  video.addEventListener("ended", () => follow(Infinity)); // an insert on the last frame
  ["pause", "seeked"].forEach((ev) => video.addEventListener(ev, tick));
  video.addEventListener("timeupdate", step);
  video.addEventListener("click", () => !editing && togglePlay());
  $("cue").addEventListener("click", togglePlay);
  $("play").addEventListener("click", togglePlay);
  $("prev").addEventListener("click", () => { halt(); seekFrame(frameOf(video.currentTime) - 1); });
  $("next").addEventListener("click", () => { halt(); seekFrame(frameOf(video.currentTime) + 1); });
  $("scrub").addEventListener("input", (e) => { halt(); video.currentTime = Number(e.target.value); });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (editing) {
      if (e.key === "Escape") closeEditor();
      return;
    }
    if (e.key === " ") {
      e.preventDefault();
      togglePlay();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      halt();
      const step = (e.shiftKey ? FPS : 1) * (e.key === "ArrowLeft" ? -1 : 1);
      seekFrame(frameOf(video.currentTime) + Math.round(step));
    }
  });

  // ---------------------------------------------------------------- lists
  function describe(item) {
    if (item.kind === "insert") {
      const what = item.insert.type === "image" ? "插入圖片" : "插入影片";
      return { tag: what, cls: "insert", text: item.text || `${item.insert.name}（${item.insert.duration} 秒）` };
    }
    const n = item.marks.length;
    return { tag: n ? `意見＋${n} 個標註` : "意見", cls: "", text: item.text || "（只有畫面標註）" };
  }
  function renderItems() {
    const list = $("items");
    const items = sorted();
    $("items-count").textContent = items.length;
    list.replaceChildren(...items.map((item) => {
      const d = describe(item);
      const li = el("li");
      const row = el("button", `row${editing && editing.item.id === item.id ? " is-on" : ""}`);
      row.type = "button";
      const body = el("span", "row-body");
      const head = el("span");
      head.append(el("span", `tag ${d.cls}`, d.tag));
      if (item.kind === "insert" && !blobs.has(item.id)) head.append(el("span", "tag warn", "檔案遺失"));
      const s = shotAt(frameTime(item.frame));
      head.append(el("span", "dim small", s ? `#${pad(s.no)} ${s.name}` : ""));
      body.append(head, el("span", "row-text", d.text));
      row.append(el("span", "row-time", clock(frameTime(item.frame))), body);
      row.addEventListener("click", () => openEditor(item, false));
      li.append(row);
      return li;
    }));
    if (!items.length) list.append(el("li", "empty", "還沒有意見。暫停在要改的畫面，按「在這一格加意見」。"));
    renderMarkers();
    showDraftState();
  }
  function renderMarkers() {
    const total = video.duration || meta.duration || 1;
    $("markers").replaceChildren(...state.items.map((item) => {
      const m = el("button", `marker${item.kind === "insert" ? " insert" : ""}`);
      m.type = "button";
      m.style.left = `${Math.min(100, (frameTime(item.frame) / total) * 100)}%`;
      m.title = `${clock(frameTime(item.frame))} ${describe(item).text}`;
      m.addEventListener("click", () => !editing && openEditor(item, false));
      return m;
    }));
  }
  function renderShots() {
    $("shots").replaceChildren(...meta.shots.map((s) => {
      const li = el("li");
      const row = el("button", "row");
      row.type = "button";
      row.dataset.id = s.id;
      const n = state.items.filter((i) => (shotAt(frameTime(i.frame)) || {}).id === s.id).length;
      const body = el("span", "row-body");
      body.append(el("b", "", s.name), el("span", "dim small", `${clock(s.start)} 起，${s.length.toFixed(1)} 秒`
        + (n ? `，${n} 則意見` : "")));
      row.append(el("span", "row-time", `#${pad(s.no)}`), body);
      row.addEventListener("click", () => { if (!editing) { halt(); seekFrame(frameOf(s.start)); } });
      li.append(row);
      return li;
    }));
  }
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-on", t === tab));
    $("items").hidden = tab.dataset.tab !== "items";
    $("shots").hidden = tab.dataset.tab !== "shots";
    if (tab.dataset.tab === "shots") { renderShots(); tick(); }
  }));

  // ---------------------------------------------------------------- editor
  function setTool(tool) {
    editing.tool = tool;
    document.querySelectorAll(".tool").forEach((b) => b.classList.toggle("is-on", b.dataset.tool === tool));
  }
  function showInsert() {
    const ins = editing.item.insert;
    const blob = editing.blob;
    if (editing.url) URL.revokeObjectURL(editing.url);
    editing.url = blob ? URL.createObjectURL(blob) : "";
    const thumb = $("ed-thumb");
    if (!blob) thumb.replaceChildren(el("span", "dim small", "檔案遺失"));
    else if (ins.type === "image") thumb.replaceChildren(Object.assign(el("img"), { src: editing.url, alt: "" }));
    else thumb.replaceChildren(Object.assign(el("video"), { src: editing.url, muted: true, controls: true }));
    $("ed-file-name").textContent = ins.name;
    $("ed-file-meta").textContent = `${ins.type === "image" ? "圖片" : "影片"}，${fmtSize(ins.size)}`
      + (ins.type === "video" && ins.measured ? `，長 ${ins.duration} 秒` : "");
    $("ed-dur").value = ins.duration;
    $("ed-dur").disabled = ins.type === "video" && !!ins.measured;
  }
  function openEditor(item, isNew, blob) {
    if (editing) return;
    halt();
    editing = { item: JSON.parse(JSON.stringify(item)), isNew, tool: "rect", blob: blob || blobs.get(item.id), url: "" };
    seekFrame(item.frame);
    document.body.classList.add("editing");
    const s = shotAt(frameTime(item.frame));
    const n = sorted().indexOf(byId(item.id)) + 1;
    const insert = item.kind === "insert";
    $("ed-title").textContent = isNew ? (insert ? "新增插入素材" : "新增意見") : `第 ${n} 則${insert ? "插入素材" : "意見"}`;
    $("ed-where").textContent = `${clock(frameTime(item.frame))}，${s ? `#${pad(s.no)} ${s.name}` : ""}`
      + (insert ? "，素材從這一格開始播，後面的畫面往後移" : "");
    $("ed-tools").hidden = insert;
    $("ed-insert").hidden = !insert;
    $("ed-text-label").textContent = insert ? "說明（選填）" : "要怎麼改";
    $("ed-text").placeholder = insert ? "例如：這張產品照放 3 秒，配上旁白第二句" : "例如：框起來的字改成「全新上市」";
    $("ed-text").value = item.text || "";
    $("ed-delete").hidden = isNew;
    $("editor").hidden = false;
    if (insert) showInsert();
    else setTool("rect");
    renderInk();
    renderItems();
    // A note is drawn on the picture, so the whole frame stays in view; an insert only needs its form.
    if (insert) $("editor").scrollIntoView({ block: "nearest", behavior: "smooth" });
    else $("stage").scrollIntoView({ block: "start", behavior: "smooth" });
    $("ed-text").focus({ preventScroll: true });
  }
  function closeEditor() {
    if (editing && editing.url) URL.revokeObjectURL(editing.url);
    editing = null;
    origin = draft = null;
    $("editor").hidden = true;
    document.body.classList.remove("editing");
    renderInk();
    renderItems();
  }
  async function saveEditor() {
    const item = editing.item;
    item.text = $("ed-text").value.trim().slice(0, 2000);
    if (item.kind === "note" && !item.text && !item.marks.length) {
      alert("請寫下要怎麼改，或在畫面上標出要改的地方");
      return;
    }
    if (item.kind === "insert") {
      if (!editing.blob) {
        alert("請按「換一個檔案」重新選擇要插入的檔案");
        return;
      }
      const dur = Number($("ed-dur").value);
      if (!(dur >= 0.5 && dur <= 600)) {
        alert("播放秒數請填 0.5 到 600 之間的數字");
        return;
      }
      item.insert.duration = Math.round(dur * 10) / 10;
    }
    const t = frameTime(item.frame);
    const s = shotAt(t);
    Object.assign(item, { t, shot: s ? s.id : "", no: s ? s.no : 0, shotName: s ? s.name : "",
      offset: s ? Math.round((t - s.start) * 1000) / 1000 : t });
    const at = state.items.findIndex((i) => i.id === item.id);
    if (at >= 0) state.items[at] = item;
    else state.items.push(item);
    const blob = editing.blob;
    const fileChanged = item.kind === "insert" && blobs.get(item.id) !== blob;
    if (item.kind === "insert") blobs.set(item.id, blob);
    closeEditor();
    if (fileChanged) await keepFile(item.id, blob);
    save(true);
    renderItems();
  }
  async function deleteEditor() {
    if (!confirm("刪除這則？")) return;
    const id = editing.item.id;
    state.items = state.items.filter((i) => i.id !== id);
    blobs.delete(id);
    closeEditor();
    await dropFile(id);
    save(true);
    renderItems();
  }
  document.querySelectorAll(".tool").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  $("ed-undo").addEventListener("click", () => { editing.item.marks.pop(); renderInk(); });
  $("ed-clear").addEventListener("click", () => { editing.item.marks = []; renderInk(); });
  $("ed-save").addEventListener("click", saveEditor);
  $("ed-cancel").addEventListener("click", closeEditor);
  $("ed-delete").addEventListener("click", deleteEditor);

  // ---------------------------------------------------------------- new notes and inserts
  function canAdd() {
    if (editing) return false;
    if (state.items.length >= MAX_ITEMS) {
      alert(`最多 ${MAX_ITEMS} 則意見`);
      return false;
    }
    return true;
  }
  const newItem = (kind) => ({ id: `c${++state.seq}`, seq: state.seq, kind, frame: frameOf(video.currentTime),
    text: "", marks: [] });
  $("add-note").addEventListener("click", () => canAdd() && openEditor(newItem("note"), true));
  $("add-image").addEventListener("click", () => canAdd() && $("pick-image").click());
  $("add-video").addEventListener("click", () => canAdd() && $("pick-video").click());
  $("ed-repick").addEventListener("click", () => $(editing.item.insert.type === "image" ? "pick-image" : "pick-video").click());

  function clipLength(file) {
    return new Promise((resolve) => {
      const v = el("video");
      const url = URL.createObjectURL(file);
      const done = (d) => { URL.revokeObjectURL(url); resolve(d); };
      v.preload = "metadata";
      v.onloadedmetadata = () => done(Number.isFinite(v.duration) && v.duration > 0 ? v.duration : 0);
      v.onerror = () => done(0);
      v.src = url;
    });
  }
  async function picked(input, type) {
    const file = input.files && input.files[0];
    input.value = "";
    if (!file) return;
    if (!(type === "image" ? IMAGE : CLIP).test(file.name)) {
      alert(type === "image" ? "請選 PNG、JPG、WebP、BMP 或 GIF 圖片" : "請選 MP4、MOV、WebM、MKV、M4V 或 AVI 影片");
      return;
    }
    if (file.size > MAX_FILE) {
      alert(`「${file.name}」超過 1 GB，請壓小一點再插入，或另外傳給製作方`);
      return;
    }
    const measured = type === "video" ? Math.round((await clipLength(file)) * 10) / 10 : 0;
    const insert = { type, name: file.name.slice(-120), size: file.size, duration: measured || (type === "image" ? 3 : 5),
      measured: measured > 0 };
    if (editing) { // 換一個檔案
      editing.item.insert = { ...insert, duration: type === "image" ? editing.item.insert.duration : insert.duration };
      editing.blob = file;
      showInsert();
      return;
    }
    openEditor({ ...newItem("insert"), insert }, true, file);
  }
  $("pick-image").addEventListener("change", (e) => picked(e.target, "image"));
  $("pick-video").addEventListener("change", (e) => picked(e.target, "video"));

  // ---------------------------------------------------------------- soundtrack (更換配樂)
  function renderMusic() {
    const m = state.music;
    halt();
    if (musicUrl) URL.revokeObjectURL(musicUrl);
    musicUrl = m && musicBlob ? URL.createObjectURL(musicBlob) : "";
    if (musicUrl) tune.src = musicUrl;
    else tune.removeAttribute("src");
    tune.load();
    renderSound();
    $("music-empty").hidden = !!m;
    $("music-file").hidden = !m;
    $("music-remove").hidden = !m;
    $("music-pick").textContent = m ? "換一首" : "上傳配樂";
    if (!m) return;
    $("music-name").textContent = m.name;
    $("music-meta").textContent = musicBlob ? fmtSize(m.size) : "檔案遺失，請按「換一首」重新上傳";
    $("music-text").value = m.text || "";
    showVolume();
  }
  function showVolume() {
    const pct = Math.round(musicVolume() * 100);
    $("music-vol").value = pct;
    $("music-vol-text").textContent = `${pct}%`;
  }
  $("music-pick").addEventListener("click", () => $("pick-music").click());
  $("pick-music").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (!SOUND.test(file.name)) {
      alert("請選 MP3、WAV、M4A、AAC、FLAC、OGG 或 OPUS 音樂檔");
      return;
    }
    if (file.size > MAX_FILE) {
      alert(`「${file.name}」超過 1 GB，請壓小一點再上傳，或另外傳給製作方`);
      return;
    }
    state.music = { name: file.name.slice(-120), size: file.size, text: state.music ? state.music.text : "",
      volume: state.music ? state.music.volume : null };
    musicBlob = file;
    renderMusic();
    await keepFile(MUSIC, file);
    save(true);
  });
  $("music-remove").addEventListener("click", async () => {
    if (!confirm("移除上傳的配樂？影片會沿用原本的配樂。")) return;
    state.music = null;
    musicBlob = null;
    renderMusic();
    await dropFile(MUSIC);
    save(true);
  });
  $("music-vol").addEventListener("input", (e) => {
    if (!state.music) return;
    state.music.volume = Number(e.target.value) / 100;
    showVolume();
    tuneVolume();
    save();
  });
  $("music-text").addEventListener("input", (e) => {
    if (!state.music) return;
    state.music.text = e.target.value;
    save();
  });

  // ---------------------------------------------------------------- pack (makeZip: zip.js)
  function download(blob, name) {
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  }
  function packState(text, cls) {
    $("pack-state").textContent = text;
    $("pack-state").className = `small ${cls || ""}`;
    $("pack-state").hidden = !text;
  }
  $("pack").addEventListener("click", async () => {
    if (editing) {
      alert("請先儲存或取消正在編輯的意見");
      return;
    }
    const by = $("by").value.trim();
    if (!by) {
      alert("請先填寫您的稱呼");
      $("by").focus();
      return;
    }
    const items = sorted();
    const note = $("note").value.trim();
    if (!items.length && !note && !state.music) {
      alert("還沒有任何意見");
      return;
    }
    const lost = items.filter((i) => i.kind === "insert" && !blobs.has(i.id));
    if (lost.length) {
      alert(`有 ${lost.length} 則插入的檔案遺失，請點開那幾則，按「換一個檔案」重新選擇`);
      return;
    }
    if (state.music && !musicBlob) {
      alert("配樂檔案遺失，請按「換一首」重新上傳，或按「移除」");
      return;
    }
    const files = [];
    const out = items.map((item, i) => {
      const { seq, ...rest } = item;
      if (item.kind !== "insert") return rest;
      const { measured, ...insert } = item.insert;
      const file = `附件/${pad(i + 1)}_${safeName(insert.name)}`;
      files.push({ name: file, blob: blobs.get(item.id) });
      return { ...rest, marks: [], insert: { ...insert, file } };
    });
    let music = null;
    if (state.music) {
      music = { name: state.music.name, size: state.music.size, file: `附件/配樂_${safeName(state.music.name)}`,
        text: (state.music.text || "").trim().slice(0, 2000), volume: state.music.volume ?? null };
      files.push({ name: music.file, blob: musicBlob });
    }
    const data = { vc: "review-comments", version: 2, project: meta.project, name: meta.name, review: meta.review,
      exported: meta.exported, video: meta.video, by, at: new Date().toISOString(), note, music, items: out };
    const entries = [{ name: "修改意見.json", blob: new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }) },
      ...files];
    if (entries.reduce((n, e) => n + e.blob.size, 0) > MAX_ZIP) {
      packState("附件加起來超過 3.9 GB，請刪掉幾個大的影片，改用其他方式傳給製作方。", "err");
      return;
    }
    const button = $("pack");
    button.disabled = true;
    try {
      const zip = await makeZip(entries, (f) => { button.textContent = `打包中 ${Math.round(f * 100)}%`; });
      const name = meta.file.replace("{by}", safeName(by));
      download(zip, name);
      packState(`已下載「${name}」（${fmtSize(zip.size)}），請把這個檔案傳回給製作方。`, "ok");
    } catch (err) {
      packState(`打包失敗：${err && err.message ? err.message : err}`, "err");
    } finally {
      button.disabled = false;
      button.textContent = "打包回傳";
    }
  });

  // ---------------------------------------------------------------- start
  $("by").addEventListener("input", (e) => { state.by = e.target.value; save(); });
  $("note").addEventListener("input", (e) => { state.note = e.target.value; save(); });
  window.addEventListener("beforeunload", (e) => {
    if ((!db || unsaved.size) && (state.items.length || state.note || state.music)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  const stage = $("stage");
  stage.style.aspectRatio = `${W} / ${H}`;
  stage.style.setProperty("--ar", (W / H).toFixed(4));
  $("scrub").max = meta.duration;
  restore().then(() => {
    $("by").value = state.by;
    $("note").value = state.note;
    renderMusic();
    renderItems();
    renderShots();
    tick();
  });
})();
