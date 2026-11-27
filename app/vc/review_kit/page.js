// Client review page (審稿.html, see review_pack.py). Plays the exported video beside its shot list; a note sits
// on one frame, or on a stretch of frames up to `end`, with boxes, arrows and labels drawn on the picture
// (coordinates 0-1 of the frame), an insert puts the client's own picture or clip at a frame, and 更換配樂 sends a
// soundtrack to replace the background music. Playing previews both: an insert holds the video for its seconds,
// the soundtrack plays at the client's volume. The timeline is drawn here rather than an <input type=range>, so a
// drag seeks as it goes without the player writing the old position back under the pointer.
// 打包回傳 downloads one ZIP: 修改意見.json + 附件/. Drafts and the
// attached files are kept in this browser's IndexedDB; a browser that refuses it keeps them for this visit only.
(() => {
  "use strict";
  const meta = JSON.parse(document.getElementById("vc-meta").textContent);
  const $ = (id) => document.getElementById(id);
  const video = $("video");
  const peek = $("peek"); // a second decoder for the timeline's hover picture, so the main one stays put
  const ink = $("ink");
  const tl = $("timeline");
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
  const JUMP = 5; // seconds for -5 / +5 and J / L

  // music: { name, size, text, volume } (volume 0-1, null until the client moves the slider)
  const state = { by: "", note: "", items: [], seq: 0, music: null };
  const blobs = new Map(); // item id -> the picture or clip it inserts
  let musicBlob = null;
  let musicUrl = "";
  const unsaved = new Set(); // item ids (or MUSIC) whose file did not fit in the browser's storage
  let db = null;
  let editing = null; // { item, isNew, tool, blob, url, picking, picks, base }
  let draft = null; // the box or arrow being dragged out
  let origin = null;
  let textBox = null; // { finish } while a label is being typed on the picture
  const tune = new Audio(); // the client's soundtrack, played beside the video
  // The cut's sound without its music (審稿匯出 writes it when the cut has music): with a new soundtrack the video
  // is muted and this plays in its place, so the old music drops out.
  let stem = meta.stem ? new Audio(encodeURIComponent(meta.stem)) : null;
  let cue = null; // { queue, i, timer, url, left, since }: the inserts of one frame, shown in place of the video
  let from = 0; // the preview has passed every insert at or before this time
  let resuming = false; // the video starts again after a cue, so `from` stays
  let looping = false;
  let scrub = null; // { t, x, moved, wasPlaying, marker } while the timeline is held down
  let seekWant = null; // the latest seek asked for while one is still running (a drag asks for many)
  let peekWant = null;
  let peekOk = true;
  let spanEnd = null; // the frame where 播放這一段 stops
  let rate = 1;
  let volume = 1;
  let muted = false;
  let hereList = []; // the notes on the paused frame
  let hereKey = "";
  let version = 0; // bumps whenever the items change, so the paused-frame overlay is drawn again
  let lastShot = null;

  // ---------------------------------------------------------------- helpers
  const pad = (n) => String(n).padStart(2, "0");
  const clock = (t) => {
    const s = Math.max(0, t);
    const m = Math.floor(s / 60);
    return `${pad(m)}:${(s - m * 60).toFixed(2).padStart(5, "0")}`;
  };
  // "1:23", "83", "1:23.5" or "0:01:23", typed with half- or full-width digits; NaN for anything else.
  function parseClock(text) {
    const parts = String(text).normalize("NFKC").trim().split(":").map((p) => p.trim());
    if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return NaN;
    return parts.reduce((s, p) => s * 60 + Number(p), 0);
  }
  const total = () => video.duration || meta.duration || 1;
  const frameOf = (t) => Math.max(0, Math.floor(t * FPS + 1e-3));
  const frameTime = (f) => Math.round((f / FPS) * 1000) / 1000;
  const lastFrame = () => Math.max(0, Math.ceil(total() * FPS - 1e-3) - 1); // duration is rounded (231.066667 * 30 = 6932.00001)
  const at = () => Math.min(lastFrame(), frameOf(video.currentTime));
  const r4 = (v) => Math.round(v * 10000) / 10000;
  const clamp01 = (v) => Math.min(1, Math.max(0, v));
  const fmtSize = (n) => (n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const safeName = (name) => String(name).replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").replace(/^[\s.]+|[\s.]+$/g, "").slice(-80) || "file";
  const shotAt = (t) => meta.shots.reduce((hit, s) => (s.start <= t + 1e-6 ? s : hit), meta.shots[0] || null);
  const sorted = () => [...state.items].sort((a, b) => a.frame - b.frame || a.seq - b.seq);
  const byId = (id) => state.items.find((i) => i.id === id);
  const findItem = (id) => (editing && editing.item.id === id ? editing.item : byId(id));
  const spanSeconds = (item) => ((item.end.frame - item.frame + 1) / FPS).toFixed(1);
  // Where a frame sits: its time, and the shot it is in with the time inside that shot.
  function place(frame) {
    const t = frameTime(frame);
    const s = shotAt(t);
    return { t, shot: s ? s.id : "", no: s ? s.no : 0, shotName: s ? s.name : "",
      offset: s ? Math.round((t - s.start) * 1000) / 1000 : t };
  }
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
  let flashTimer = 0;
  function flash(text) {
    $("flash").textContent = text;
    $("flash").hidden = false;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { $("flash").hidden = true; }, 1300);
  }
  // Scroll a list (not the page) so the row shows.
  function reveal(list, row) {
    if (list.hidden || !row) return;
    if (row.offsetTop < list.scrollTop || row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = Math.max(0, row.offsetTop - list.clientHeight / 3);
    }
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
  // One mark as a group; 擦掉 finds it through a wide invisible copy of its outline.
  function markGroup(m, i) {
    const g = document.createElementNS(SVG, "g");
    g.setAttribute("class", "mk");
    g.setAttribute("data-i", i);
    const parts = shapes(m);
    if (m.type !== "text") {
      const hit = parts[0].cloneNode();
      hit.setAttribute("class", "hit");
      hit.setAttribute("stroke-width", Math.max(2, H * 0.006) * 7);
      g.append(hit);
    }
    g.append(...parts);
    return g;
  }
  // Editing: the note's own marks. Otherwise: faintly, the marks of the notes on the paused frame.
  function renderInk() {
    const f = at();
    const marks = editing ? editing.item.marks
      : hereList.filter((i) => i.kind === "note" && i.frame === f).flatMap((i) => i.marks);
    ink.replaceChildren(...marks.map(markGroup), ...(draft ? shapes(draft) : []));
    ink.classList.toggle("ghost", !editing);
    ink.classList.toggle("drawing", !!editing && editing.item.kind === "note");
    ink.classList.toggle("erasing", !!editing && editing.tool === "erase");
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
  // A label typed straight onto the picture where it was clicked: Enter keeps it, Esc drops it.
  function askText(p) {
    if (textBox) textBox.finish(true);
    const box = el("input", "ink-input");
    box.maxLength = 80;
    box.placeholder = "輸入標註文字，按 Enter";
    box.style.left = `min(${p.x * 100}%, calc(100% - min(60%, 320px)))`;
    box.style.top = `${p.y * 100}%`;
    const self = {
      finish(keep) {
        if (textBox !== self) return;
        textBox = null;
        const text = box.value.trim().slice(0, 80);
        box.remove();
        if (keep && text && editing && editing.item.marks.length < MAX_MARKS) {
          editing.item.marks.push({ type: "text", x: r4(p.x), y: r4(p.y), text });
          renderInk();
        }
      },
    };
    textBox = self;
    box.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.isComposing) return;
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        self.finish(true);
        saveEditor();
      } else if (e.key === "Enter") {
        e.preventDefault();
        self.finish(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        self.finish(false);
      }
    });
    box.addEventListener("blur", () => self.finish(true));
    $("stage").append(box);
    box.focus({ preventScroll: true });
  }
  ink.addEventListener("pointerdown", (e) => {
    if (!editing || editing.item.kind !== "note" || e.button !== 0 || editing.picking || !video.paused) return;
    e.preventDefault();
    // Drawing leaves the text box, so ← → move the note again.
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
    if (editing.tool === "erase") {
      const g = e.target.closest && e.target.closest("g.mk");
      if (g) {
        editing.item.marks.splice(Number(g.getAttribute("data-i")), 1);
        renderInk();
      }
      return;
    }
    if (editing.item.marks.length >= MAX_MARKS) {
      alert(`一則意見最多 ${MAX_MARKS} 個標註，請分成兩則`);
      return;
    }
    const p = point(e);
    if (editing.tool === "text") {
      askText(p);
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
  // While a seek is running, keep only the latest one asked for and start it when this one lands.
  function seekTo(t) {
    if (video.seeking) {
      seekWant = t;
      return;
    }
    seekWant = null;
    video.currentTime = t;
  }
  function seekFrame(f) {
    const frame = Math.min(lastFrame(), Math.max(0, f));
    seekWant = null;
    video.currentTime = (frame + 0.5) / FPS; // mid-frame, so the picture shown is this frame and not its neighbour
  }
  const pos = (t) => `${Math.min(100, Math.max(0, (t / total()) * 100))}%`;
  function tick() {
    const t = scrub ? scrub.t : video.currentTime;
    const f = frameOf(t);
    $("clock").textContent = `${clock(frameTime(f))} / ${clock(total())}`;
    $("tl-head").style.left = pos(t);
    $("tl-played").style.width = pos(t);
    tl.setAttribute("aria-valuenow", t.toFixed(2));
    tl.setAttribute("aria-valuetext", clock(frameTime(f)));
    const s = shotAt(t);
    $("shot-now").textContent = cue ? `插入素材：${cue.queue[cue.i].insert.name}` : s ? `#${pad(s.no)} ${s.name}` : "";
    if (s !== lastShot) {
      lastShot = s;
      let on = null;
      document.querySelectorAll("#shots .row").forEach((r) => {
        const hit = !!s && r.dataset.id === s.id;
        r.classList.toggle("is-on", hit);
        if (hit) on = r;
      });
      reveal($("shots"), on);
    }
    $("play").textContent = video.paused && !cue ? "播放" : "暫停";
    if (editing) {
      ink.classList.toggle("away", editing.picking || at() !== editing.item.frame);
      $("ed-span-play").textContent = video.paused ? "播放這一段" : "停止";
      const pick = $("tl-pick");
      pick.hidden = !editing.picking || f <= editing.item.frame;
      if (editing.picking) {
        pick.style.left = pos(frameTime(editing.item.frame));
        pick.style.width = `calc(${pos(frameTime(f))} - ${pos(frameTime(editing.item.frame))})`;
        $("ed-span-text").textContent = `移到要改的最後一格（可以拖時間軸、播放、按 ← → 逐格），再按「設為結束點」。`
          + `目前：${clock(frameTime(f))}`;
      }
    } else {
      ink.classList.remove("away");
    }
    updateHere();
  }
  // One frame's work. timeupdate runs it too: it keeps firing in a background tab, where requestAnimationFrame stops.
  function step() {
    if (!video.paused && !cue) follow(video.currentTime);
    if (spanEnd != null && !video.paused && !cue && at() >= spanEnd) stopSpan();
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
  // The notes on the paused frame (or whose stretch covers it): a chip on the picture opens the first, their rows
  // light up, and the marks of the ones that start here show faintly.
  function updateHere(force) {
    const f = at();
    const still = !editing && !cue && !scrub && video.paused && !video.seeking;
    const list = still ? sorted().filter((i) => i.frame === f || (i.end && i.frame <= f && f <= i.end.frame)) : [];
    const key = still ? `${f}|${version}|${list.map((i) => i.id).join()}` : "-";
    if (!force && key === hereKey) return;
    hereKey = key;
    hereList = list;
    $("here").hidden = !list.length;
    $("here").textContent = `這一格有 ${list.length} 則意見，點一下打開`;
    let first = null;
    document.querySelectorAll("#items .row").forEach((r) => {
      const hit = list.some((i) => i.id === r.dataset.id);
      r.classList.toggle("is-here", hit);
      if (hit && !first) first = r;
    });
    reveal($("items"), first);
    if (!editing) renderInk();
  }

  // ---------------------------------------------------------------- preview (inserts and the new soundtrack)
  const cues = () => sorted().filter((i) => i.kind === "insert" && blobs.has(i.id));
  // Seconds of inserts that play before time t: where the soundtrack is when the video is at t.
  const before = (t) => cues().reduce((n, i) => n + (frameTime(i.frame) <= t ? i.insert.duration : 0), 0);
  const musicVolume = () => (state.music && state.music.volume != null ? state.music.volume : meta.musicVolume ?? 1);
  const cueMedia = () => $("cue").querySelector("video");
  function follow(t) {
    const due = cues().filter((i) => frameTime(i.frame) > from && frameTime(i.frame) <= t);
    if (!due.length) {
      from = t;
      return false;
    }
    const f = due[0].frame;
    cue = { queue: due.filter((i) => i.frame === f), i: 0, timer: 0, url: "", left: 0, since: 0 };
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
      : Object.assign(el("video"), { src: cue.url, playsInline: true, autoplay: true, defaultPlaybackRate: rate,
        playbackRate: rate, volume: muted ? 0 : volume });
    $("cue").replaceChildren(media, el("span", "cue-tag", `${ins.type === "image" ? "插入圖片" : "插入影片"}：${ins.name}`));
    $("cue").hidden = false;
    cue.left = ins.duration; // seconds of the insert still to show, at normal speed
    cue.since = performance.now();
    cue.timer = setTimeout(nextCue, (cue.left * 1000) / rate);
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
    spanEnd = null;
    if (cue) endCue();
    video.pause();
    tune.pause();
    tick();
  }
  const playing = () => (!video.paused || !!cue) && spanEnd == null;
  const togglePlay = () => (video.paused && !cue ? video.play().catch(() => {}) : halt());
  // Go to a frame and keep playing if it was (the play event picks the soundtrack and the inserts up from there).
  function go(frame) {
    const was = playing();
    halt();
    seekFrame(frame);
    if (was) video.play().catch(() => {});
  }
  // Moving around is open while nothing is being edited, and while the end of a stretch is being picked.
  const canNav = () => !editing || !!editing.picking;
  function stepFrames(n) {
    if (!canNav()) return;
    halt();
    seekFrame(at() + n);
  }
  const skip = (sec) => canNav() && go(frameOf(video.currentTime + sec));
  function shotStep(d) {
    if (!canNav() || !meta.shots.length) return;
    const t = video.currentTime;
    const s = shotAt(t);
    const i = meta.shots.indexOf(s);
    const target = d > 0 ? meta.shots[i + 1] : t - s.start > 0.5 ? s : meta.shots[Math.max(0, i - 1)];
    if (target) go(frameOf(target.start));
  }
  function noteStep(d) {
    if (editing) return;
    const f = at();
    const list = sorted();
    const target = d > 0 ? list.find((i) => i.frame > f) : list.reverse().find((i) => i.frame < f);
    if (!target) {
      flash(d > 0 ? "後面沒有意見了" : "前面沒有意見了");
      return;
    }
    halt();
    seekFrame(target.frame);
  }
  function playSpan(a, b) {
    halt();
    seekFrame(a);
    spanEnd = b;
    video.play().catch(() => { spanEnd = null; });
  }
  function stopSpan() {
    const back = editing ? editing.item.frame : spanEnd;
    halt();
    seekFrame(back);
  }
  function syncSound() {
    const t = video.currentTime;
    if (video.muted && stem && Math.abs(stem.currentTime - t) > 0.15) stem.currentTime = t;
    if (!musicUrl) return;
    const pos = t + before(from);
    if (pos >= tune.duration) tune.pause();
    else if (Math.abs(tune.currentTime - pos) > 0.25) tune.currentTime = pos;
  }
  const out = () => (muted ? 0 : volume);
  function tuneVolume() {
    // Cut at the end of the video with a fade, as 套用 does (revisions.music_plan).
    const length = total() + before(Infinity);
    const fade = tune.duration > length + 0.05 ? Math.min(2, length / 2) : 0;
    tune.volume = clamp01(musicVolume() * out() * (fade ? clamp01((length - tune.currentTime) / fade) : 1));
  }
  function renderSound() {
    video.muted = !!(musicUrl && stem);
    if (stem && !video.muted) stem.pause();
    $("music-hint").hidden = !musicUrl;
    $("music-hint").textContent = stem ? "按「播放」試聽，影片會改配這首音樂，旁白和現場聲音照常。"
      : meta.musicVolume != null ? "這份審稿影片沒有附去掉配樂的音軌，試聽時會同時聽到原本的配樂。"
        : "按「播放」試聽，這首音樂會加在影片原本的聲音下面。";
  }
  // The listening volume and mute (only on this page; the soundtrack's own volume is the 更換配樂 slider).
  function applyVolume() {
    video.volume = out(); // video.muted stays for swapping in the sound without music
    if (stem) stem.volume = out();
    if (cueMedia()) cueMedia().volume = out();
    tuneVolume();
    $("vol").value = Math.round(volume * 100);
    $("mute").textContent = muted ? "取消靜音" : "靜音";
    $("mute").classList.toggle("is-on", muted);
  }
  function toggleMute() {
    muted = !muted;
    if (!muted && volume === 0) volume = 1;
    applyVolume();
    flash(muted ? "已靜音" : "取消靜音");
  }
  // Speed: the video, the soundtrack, the sound without music and the inserts all go at it.
  function setRate(r) {
    if (cue) {
      clearTimeout(cue.timer);
      cue.left = Math.max(0, cue.left - ((performance.now() - cue.since) / 1000) * rate);
      cue.since = performance.now();
      cue.timer = setTimeout(nextCue, (cue.left * 1000) / r);
    }
    rate = r;
    [video, tune, stem, cueMedia()].forEach((m) => {
      if (!m) return;
      m.defaultPlaybackRate = r;
      m.playbackRate = r;
    });
    $("rate").value = String(r);
  }
  const player = $("player");
  function toggleFull() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (!editing && player.requestFullscreen) player.requestFullscreen().catch(() => flash("這個瀏覽器不能全螢幕"));
  }
  document.addEventListener("fullscreenchange", () => {
    $("full").textContent = document.fullscreenElement ? "結束全螢幕" : "全螢幕";
  });
  if (stem) stem.addEventListener("error", () => { stem = null; renderSound(); });
  video.addEventListener("loadedmetadata", () => {
    tl.setAttribute("aria-valuemax", total().toFixed(2));
    renderShotBar();
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
  video.addEventListener("seeked", () => {
    if (seekWant == null) return;
    const t = seekWant;
    seekWant = null;
    video.currentTime = t;
  });
  ["pause", "seeked"].forEach((ev) => video.addEventListener(ev, tick));
  video.addEventListener("timeupdate", step);
  video.addEventListener("click", () => canNav() && togglePlay());
  video.addEventListener("dblclick", toggleFull);
  $("cue").addEventListener("click", togglePlay);
  $("cue").addEventListener("dblclick", toggleFull);
  $("here").addEventListener("click", () => hereList[0] && openEditor(hereList[0], false));
  $("play").addEventListener("click", () => canNav() && togglePlay());
  $("prev").addEventListener("click", () => stepFrames(-1));
  $("next").addEventListener("click", () => stepFrames(1));
  $("back5").addEventListener("click", () => skip(-JUMP));
  $("fwd5").addEventListener("click", () => skip(JUMP));
  $("prev-shot").addEventListener("click", () => shotStep(-1));
  $("next-shot").addEventListener("click", () => shotStep(1));
  $("prev-note").addEventListener("click", () => noteStep(-1));
  $("next-note").addEventListener("click", () => noteStep(1));
  $("full").addEventListener("click", toggleFull);
  $("mute").addEventListener("click", toggleMute);
  $("vol").addEventListener("input", (e) => {
    volume = Number(e.target.value) / 100;
    muted = false;
    applyVolume();
  });
  $("vol").addEventListener("change", (e) => e.target.blur());
  $("rate").addEventListener("change", (e) => {
    setRate(Number(e.target.value));
    e.target.blur();
    flash(`${rate} 倍速`);
  });

  // ---------------------------------------------------------------- time typed in
  function closeClock() {
    $("clock-input").hidden = true;
    $("clock").hidden = false;
  }
  $("clock").addEventListener("click", () => {
    if (!canNav()) return;
    const box = $("clock-input");
    box.value = clock(frameTime(at()));
    box.classList.remove("bad");
    $("clock").hidden = true;
    box.hidden = false;
    box.focus();
    box.select();
  });
  $("clock-input").addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.isComposing) return;
    if (e.key === "Enter") {
      e.preventDefault();
      const t = parseClock(e.target.value);
      if (Number.isNaN(t)) {
        e.target.classList.add("bad");
        return;
      }
      closeClock();
      go(frameOf(Math.min(t, total())));
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeClock();
    }
  });
  $("clock-input").addEventListener("input", (e) => e.target.classList.remove("bad"));
  $("clock-input").addEventListener("blur", closeClock);

  // ---------------------------------------------------------------- timeline (drag, click, hover picture)
  const timeAt = (x) => {
    const r = tl.getBoundingClientRect();
    return clamp01((x - r.left) / r.width) * total();
  };
  function showTip(x, t, item) {
    const tip = $("tl-tip");
    const s = shotAt(t);
    $("tip-time").textContent = clock(frameTime(frameOf(t)));
    $("tip-shot").textContent = s ? `#${pad(s.no)} ${s.name}` : "";
    $("tip-note").textContent = item ? describe(item).text : "";
    $("tip-note").hidden = !item;
    tip.classList.toggle("no-peek", !!scrub || !peekOk);
    tip.hidden = false;
    const r = tl.getBoundingClientRect();
    const half = tip.offsetWidth / 2;
    tip.style.left = `${Math.min(r.width - half, Math.max(half, x - r.left))}px`;
  }
  const hideTip = () => { $("tl-tip").hidden = true; };
  let peekStarted = false;
  function peekAt(t) {
    if (!peekOk) return;
    if (!peekStarted) {
      peekStarted = true;
      peek.preload = "auto";
    }
    if (peek.readyState < 1 || peek.seeking) {
      peekWant = t;
      return;
    }
    peekWant = null;
    peek.currentTime = (frameOf(t) + 0.5) / FPS;
  }
  const peekNext = () => {
    if (peekWant == null) return;
    const t = peekWant;
    peekWant = null;
    peek.currentTime = (frameOf(t) + 0.5) / FPS;
  };
  peek.addEventListener("seeked", peekNext);
  peek.addEventListener("loadedmetadata", peekNext);
  peek.addEventListener("error", () => {
    peekOk = false;
    $("tl-tip").classList.add("no-peek");
  });
  tl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !canNav()) return;
    e.preventDefault();
    tl.setPointerCapture(e.pointerId);
    const mk = e.target.closest(".marker");
    const was = playing();
    halt();
    scrub = { t: timeAt(e.clientX), x: e.clientX, moved: false, wasPlaying: was, marker: mk ? findItem(mk.dataset.id) : null };
    seekTo(scrub.t);
    showTip(e.clientX, scrub.t, scrub.marker);
    tick();
  });
  tl.addEventListener("pointermove", (e) => {
    if (scrub) {
      if (Math.abs(e.clientX - scrub.x) > 3) scrub.moved = true;
      if (!scrub.moved) return;
      scrub.t = timeAt(e.clientX);
      seekTo(scrub.t);
      showTip(e.clientX, scrub.t, null);
      tick();
    } else if (e.pointerType === "mouse" && canNav()) {
      const mk = e.target.closest(".marker");
      const item = mk ? findItem(mk.dataset.id) : null;
      const t = item ? frameTime(item.frame) : timeAt(e.clientX);
      showTip(e.clientX, t, item);
      peekAt(t);
    }
  });
  // Let go: land on a whole frame (the marker's own frame for a click on it), and play on if it was playing.
  function endScrub(e, cancel) {
    if (!scrub) return;
    const s = scrub;
    scrub = null;
    seekFrame(s.marker && !s.moved ? s.marker.frame : frameOf(s.t));
    if (s.wasPlaying && !cancel) video.play().catch(() => {});
    if (e.pointerType !== "mouse") hideTip();
    else tick();
  }
  tl.addEventListener("pointerup", (e) => endScrub(e, false));
  tl.addEventListener("pointercancel", (e) => endScrub(e, true));
  tl.addEventListener("pointerleave", () => { if (!scrub) hideTip(); });
  function renderShotBar() {
    $("tl-shots").replaceChildren(...meta.shots.map((s, i) => {
      const d = el("span", `tl-shot${i % 2 ? " alt" : ""}`);
      d.style.left = pos(s.start);
      d.style.width = `${(s.length / total()) * 100}%`;
      return d;
    }));
  }

  // ---------------------------------------------------------------- keys
  // Keys that move around the video; true when the key was one of them.
  function navKey(e) {
    const k = e.code;
    if (e.key === " ") {
      if (e.target.closest && e.target.closest("button, summary, a")) return false; // keyboard users press it
      e.preventDefault();
      if (canNav()) togglePlay();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      stepFrames(Math.round(e.shiftKey ? FPS : 1) * (e.key === "ArrowLeft" ? -1 : 1));
    } else if (k === "KeyJ" || k === "KeyL") {
      e.preventDefault();
      skip(k === "KeyJ" ? -JUMP : JUMP);
    } else if (e.key === "PageUp" || e.key === "PageDown") {
      e.preventDefault();
      shotStep(e.key === "PageUp" ? -1 : 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      if (canNav()) go(0);
    } else if (e.key === "End") {
      e.preventDefault();
      if (canNav()) { halt(); seekFrame(lastFrame()); }
    } else if (k === "KeyM") {
      toggleMute();
    } else if (k === "KeyF") {
      toggleFull();
    } else {
      return false;
    }
    return true;
  }
  document.addEventListener("keydown", (e) => {
    if ($("pack-dialog").open || e.isComposing) return;
    const typing = !!(e.target.closest && e.target.closest("input, textarea, select"));
    if (editing) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        saveEditor();
      } else if (e.key === "Escape") {
        e.preventDefault();
        if (editing.picking) endPick(false);
        else cancelEditor();
      } else if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
        if (editing.picking) navKey(e);
        else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          e.preventDefault();
          nudge(e.key === "ArrowLeft" ? -1 : 1);
        }
      }
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey || navKey(e)) return;
    if (e.code === "KeyN") {
      e.preventDefault();
      if (canAdd()) openEditor(newItem("note"), true);
    } else if (e.code === "BracketLeft" || e.code === "BracketRight") {
      e.preventDefault();
      noteStep(e.code === "BracketLeft" ? -1 : 1);
    }
  });
  // A clicked button or summary does not keep the focus, so the space bar plays and pauses instead of pressing
  // it again (Tab still reaches them).
  document.addEventListener("mousedown", (e) => {
    if (e.target.closest && e.target.closest("button, summary")) e.preventDefault();
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
    version += 1;
    $("items-count").textContent = items.length;
    list.replaceChildren(...items.map((item) => {
      const d = describe(item);
      const li = el("li");
      const row = el("button", `row${editing && editing.item.id === item.id ? " is-on" : ""}`);
      row.type = "button";
      row.dataset.id = item.id;
      const body = el("span", "row-body");
      const head = el("span");
      head.append(el("span", `tag ${d.cls}`, d.tag));
      if (item.end) head.append(el("span", "tag span", `一段 ${spanSeconds(item)} 秒`));
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
    updateHere(true);
  }
  // Markers and stretches on the timeline; the note being edited shows as it is now.
  function renderMarkers() {
    const list = state.items.filter((i) => !editing || i.id !== editing.item.id);
    if (editing) list.push(editing.item);
    const isOn = (i) => (editing && i.id === editing.item.id ? " is-on" : "");
    $("tl-spans").replaceChildren(...list.filter((i) => i.end).map((i) => {
      const d = el("span", `tl-span${isOn(i)}`);
      d.style.left = pos(frameTime(i.frame));
      d.style.width = `calc(${pos(frameTime(i.end.frame + 1))} - ${pos(frameTime(i.frame))})`;
      return d;
    }));
    $("markers").replaceChildren(...list.map((item) => {
      const m = el("span", `marker${item.kind === "insert" ? " insert" : ""}${isOn(item)}`);
      m.dataset.id = item.id;
      m.style.left = pos(frameTime(item.frame));
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
      row.addEventListener("click", () => { if (canNav()) { halt(); seekFrame(frameOf(s.start)); } });
      li.append(row);
      return li;
    }));
    lastShot = null;
  }
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-on", t === tab));
    $("items").hidden = tab.dataset.tab !== "items";
    $("shots").hidden = tab.dataset.tab !== "shots";
    if (tab.dataset.tab === "shots") renderShots();
    hereKey = "";
    tick();
  }));

  // ---------------------------------------------------------------- editor
  function setTool(tool) {
    if (textBox) textBox.finish(true);
    editing.tool = tool;
    document.querySelectorAll(".tool").forEach((b) => b.classList.toggle("is-on", b.dataset.tool === tool));
    renderInk();
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
  function renderWhere() {
    const item = editing.item;
    const s = shotAt(frameTime(item.frame));
    $("ed-where").textContent = `${clock(frameTime(item.frame))}，${s ? `#${pad(s.no)} ${s.name}` : ""}`
      + (item.kind === "insert" ? "，素材從這一格開始播，後面的畫面往後移" : "");
    renderSpan();
  }
  // The stretch a note covers: only its frame, or up to `end` (the last frame of the stretch).
  function renderSpan() {
    const item = editing.item;
    const picking = !!editing.picking;
    $("ed-span").hidden = item.kind !== "note";
    $("ed-span-set").hidden = picking;
    $("ed-span-set").textContent = item.end ? "改結束點" : "設定結束點";
    $("ed-span-play").hidden = picking || !item.end;
    $("ed-span-clear").hidden = picking || !item.end;
    $("ed-span-ok").hidden = !picking;
    $("ed-span-cancel").hidden = !picking;
    if (!picking) {
      $("ed-span-text").textContent = item.end
        ? `範圍：${clock(frameTime(item.frame))} 到 ${clock(frameTime(item.end.frame))}（${spanSeconds(item)} 秒）`
        : "範圍：只有這一格。意見是針對一整段（例如剪掉、換畫面）時，按「設定結束點」。";
    }
    document.body.classList.toggle("picking", picking);
  }
  function startPick() {
    if (textBox) textBox.finish(true);
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
    editing.picking = true;
    renderSpan();
    halt();
    seekFrame(editing.item.end ? editing.item.end.frame : editing.item.frame + Math.round(FPS));
  }
  function endPick(ok) {
    if (ok) {
      const f = at();
      if (f <= editing.item.frame) {
        alert("結束點要在意見那一格之後，請往後移一點");
        return;
      }
      editing.item.end = { frame: f };
    }
    editing.picking = false;
    halt();
    seekFrame(editing.item.frame);
    renderSpan();
    renderMarkers();
    tick();
  }
  // Move the note being edited a frame earlier or later; its marks go with it.
  function nudge(d) {
    if (!editing || editing.picking) return;
    const f = Math.min(lastFrame(), Math.max(0, editing.item.frame + d));
    if (f === editing.item.frame) return;
    editing.item.frame = f;
    if (editing.item.end && editing.item.end.frame <= f) editing.item.end = null;
    halt();
    seekFrame(f);
    renderWhere();
    renderMarkers();
  }
  const snapshot = () => JSON.stringify([editing.item, $("ed-text").value.trim(), editing.picks, $("ed-dur").value]);
  function openEditor(item, isNew, blob) {
    if (editing) return;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    closeClock();
    hideTip();
    halt();
    editing = { item: JSON.parse(JSON.stringify(item)), isNew, tool: "rect", blob: blob || blobs.get(item.id), url: "",
      picking: false, picks: 0, base: "" };
    seekFrame(item.frame);
    document.body.classList.add("editing");
    const n = sorted().indexOf(byId(item.id)) + 1;
    const insert = item.kind === "insert";
    $("ed-title").textContent = isNew ? (insert ? "新增插入素材" : "新增意見") : `第 ${n} 則${insert ? "插入素材" : "意見"}`;
    $("ed-tools").hidden = insert;
    $("ed-quick").hidden = insert;
    $("ed-insert").hidden = !insert;
    $("ed-text-label").textContent = insert ? "說明（選填）" : "要怎麼改";
    $("ed-text").placeholder = insert ? "例如：這張產品照放 3 秒，配上旁白第二句" : "例如：框起來的字改成「全新上市」";
    $("ed-text").value = item.text || "";
    $("ed-delete").hidden = isNew;
    $("editor").hidden = false;
    renderWhere();
    if (insert) showInsert();
    else setTool("rect");
    editing.base = snapshot();
    renderInk();
    renderItems();
    // A note is drawn on the picture, so the whole frame stays in view; an insert only needs its form.
    if (insert) $("editor").scrollIntoView({ block: "nearest", behavior: "smooth" });
    else $("stage").scrollIntoView({ block: "start", behavior: "smooth" });
    $("ed-text").focus({ preventScroll: true });
  }
  function closeEditor() {
    if (textBox) textBox.finish(false);
    if (editing && editing.url) URL.revokeObjectURL(editing.url);
    if (!video.paused || spanEnd != null) halt();
    editing = null;
    origin = draft = null;
    $("editor").hidden = true;
    $("tl-pick").hidden = true;
    document.body.classList.remove("editing", "picking");
    renderItems();
    tick();
  }
  // 取消 or Esc: changes made since it opened are dropped only after asking.
  function cancelEditor() {
    if (!editing) return;
    if (editing.picking) endPick(false);
    if (textBox) textBox.finish(true);
    if (snapshot() !== editing.base && !confirm("放棄這次的修改？")) return;
    closeEditor();
  }
  async function saveEditor() {
    if (!editing) return;
    if (editing.picking) endPick(false);
    if (textBox) textBox.finish(true);
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
    Object.assign(item, place(item.frame));
    if (item.kind === "note" && item.end && item.end.frame > item.frame) {
      item.end = { frame: item.end.frame, ...place(item.end.frame) };
    } else {
      delete item.end;
    }
    const idx = state.items.findIndex((i) => i.id === item.id);
    if (idx >= 0) state.items[idx] = item;
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
  $("ed-earlier").addEventListener("click", () => nudge(-1));
  $("ed-later").addEventListener("click", () => nudge(1));
  $("ed-span-set").addEventListener("click", startPick);
  $("ed-span-ok").addEventListener("click", () => endPick(true));
  $("ed-span-cancel").addEventListener("click", () => endPick(false));
  $("ed-span-clear").addEventListener("click", () => {
    editing.item.end = null;
    renderSpan();
    renderMarkers();
  });
  $("ed-span-play").addEventListener("click", () => {
    if (!video.paused) stopSpan();
    else if (editing.item.end) playSpan(editing.item.frame, editing.item.end.frame);
  });
  document.querySelectorAll("#ed-quick .chip").forEach((b) => b.addEventListener("click", () => {
    const box = $("ed-text");
    const v = box.value.trimEnd();
    const add = b.textContent;
    box.value = (!v ? add : /[，。、；！？,.;!?]$/.test(v) ? v + add : `${v}，${add}`).slice(0, 2000);
    box.focus({ preventScroll: true });
    box.setSelectionRange(box.value.length, box.value.length);
  }));
  $("ed-save").addEventListener("click", saveEditor);
  $("ed-cancel").addEventListener("click", cancelEditor);
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
  const newItem = (kind) => ({ id: `c${++state.seq}`, seq: state.seq, kind, frame: at(), text: "", marks: [] });
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
      editing.picks += 1;
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
  // What is about to be sent, shown before the download; resolves true on 打包下載.
  function confirmPack(lines) {
    const dlg = $("pack-dialog");
    $("pack-summary").replaceChildren(...lines.map((t) => el("li", "", t)));
    if (typeof dlg.showModal !== "function") return Promise.resolve(confirm(lines.join("\n")));
    return new Promise((resolve) => {
      const onClose = () => {
        dlg.removeEventListener("close", onClose);
        resolve(dlg.returnValue === "ok");
      };
      dlg.returnValue = "";
      dlg.addEventListener("close", onClose);
      dlg.showModal();
    });
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
    const notes = items.filter((i) => i.kind === "note");
    const spans = notes.filter((i) => i.end).length;
    const inserts = items.filter((i) => i.kind === "insert");
    const pics = inserts.filter((i) => i.insert.type === "image").length;
    const bytes = files.reduce((n, f) => n + f.blob.size, 0);
    const ok = await confirmPack([
      `您的稱呼：${by}`,
      `修改意見：${notes.length} 則${spans ? `（其中 ${spans} 則標了一段範圍）` : ""}`,
      `插入素材：${inserts.length ? `${inserts.length} 個（圖片 ${pics}、影片 ${inserts.length - pics}）` : "沒有"}`,
      `更換配樂：${state.music ? state.music.name : "沒有"}`,
      `整體意見：${note ? "有" : "沒有"}`,
      `附件：${files.length ? `${files.length} 個，共 ${fmtSize(bytes)}` : "沒有"}`,
    ]);
    if (!ok) return;
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
  $("stage").style.aspectRatio = `${W} / ${H}`;
  document.documentElement.style.setProperty("--ar", (W / H).toFixed(4));
  tl.setAttribute("aria-valuemax", String(meta.duration));
  renderShotBar();
  applyVolume();
  restore().then(() => {
    $("by").value = state.by;
    $("note").value = state.note;
    renderMusic();
    renderItems();
    renderShots();
    tick();
  });
})();
