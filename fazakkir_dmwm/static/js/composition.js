/* Composition editor: media library, timeline, preview and export.
   The page is a normal full page load, so this script runs once per visit. */
(function () {
    "use strict";

    const config = JSON.parse(document.getElementById("comp-config").textContent);
    const S = config.strings;
    const LIBRARY = config.library;
    const MIN = 0.2;
    const ZERO_ID = "00000000-0000-0000-0000-000000000000";
    const $ = (id) => document.getElementById(id);

    const el = {
        title: $("compTitle"), undo: $("compUndo"), redo: $("compRedo"), exportBtn: $("compExport"),
        library: $("compLibrary"), stage: $("compStage"), preview: $("compPreview"), empty: $("compEmpty"),
        timecode: $("compTimecode"), result: $("compResult"), resultVideo: $("compResultVideo"),
        resultAudio: $("compResultAudio"), save: $("compSave"), backEdit: $("compBackEdit"),
        discard: $("compDiscard"), start: $("compStart"), play: $("compPlay"), end: $("compEnd"),
        progress: $("compProgress"), progressBar: $("compProgressBar"), progressText: $("compProgressText"),
        inspEmpty: $("compInspEmpty"), inspBody: $("compInspBody"), clipTitle: $("compClipTitle"),
        inIn: $("compIn"), inOut: $("compOut"), offsetField: $("compOffsetField"), offset: $("compOffset"),
        vol: $("compVol"), volLabel: $("compVolLabel"), splitBtn: $("compSplitBtn"), delBtn: $("compDelBtn"),
        splitTool: $("compSplitTool"), delTool: $("compDelTool"), zoomOut: $("compZoomOut"),
        zoom: $("compZoom"), zoomIn: $("compZoomIn"), time: $("compTime"), scroll: $("compScroll"),
        inner: $("compInner"), ruler: $("compRuler"), laneVideo: $("compLaneVideo"),
        laneAudio: $("compLaneAudio"), playhead: $("compPlayhead"), toast: $("compToast"),
    };
    const ratioButtons = document.querySelectorAll(".comp-ratio-btn");
    const tabButtons = document.querySelectorAll(".comp-tab");

    const state = { ratio: "16:9", video: [], audio: [], playhead: 0, pps: 60, sel: null };
    const undoStack = [];
    const redoStack = [];
    let uidCounter = 0;
    let libTab = "media";
    let gesture = null;
    let ghost = null;
    let suppressClick = false;
    let playing = false;
    let raf = 0;
    let lastTs = 0;
    let previewUid = null;
    const audioEls = new Map();
    let syncQueued = false;
    let rulerKey = "";
    let job = null;
    let pollTimer = 0;
    let rendering = false;
    let toastTimer = 0;

    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    const r2 = (v) => Math.round(v * 100) / 100;
    const len = (c) => c.outPt - c.inPt;
    const newUid = () => "c" + (++uidCounter) + Math.random().toString(36).slice(2, 6);
    const pad2 = (n) => String(n).padStart(2, "0");
    const fmtShort = (t) => {
        const s = Math.floor(Math.max(0, t));
        return Math.floor(s / 60) + ":" + pad2(s % 60);
    };
    const fmt = (t) => {
        const tenths = Math.floor(Math.max(0, t) * 10);
        return pad2(Math.floor(tenths / 600)) + ":" + pad2(Math.floor((tenths % 600) / 10)) + "." + (tenths % 10);
    };
    const findClip = (track, uid) =>
        (track === "video" ? state.video : state.audio).find((c) => c.uid === uid) || null;
    const selClip = () => (state.sel ? findClip(state.sel.track, state.sel.uid) : null);
    const isSel = (track, uid) => !!state.sel && state.sel.track === track && state.sel.uid === uid;
    const clipNode = (uid) => document.querySelector('.comp-clip[data-uid="' + uid + '"]');

    /* --- Timeline maths ------------------------------------------------------ */

    function videoStarts() {
        let acc = 0;
        return state.video.map((c) => {
            const start = acc;
            acc += len(c);
            return start;
        });
    }

    function videoTotal() {
        return state.video.reduce((sum, c) => sum + len(c), 0);
    }

    function timelineEnd() {
        return state.audio.reduce((end, c) => Math.max(end, c.offset + len(c)), videoTotal());
    }

    function videoAt(t) {
        const starts = videoStarts();
        for (let i = 0; i < state.video.length; i++) {
            const start = starts[i];
            if (t >= start && t < start + len(state.video[i])) return { clip: state.video[i], start: start };
        }
        return null;
    }

    function spanOf(track, c) {
        if (track === "audio") return [c.offset, c.offset + len(c)];
        const i = state.video.indexOf(c);
        if (i < 0) return null;
        const start = videoStarts()[i];
        return [start, start + len(c)];
    }

    function inClip(track, c, t) {
        const span = spanOf(track, c);
        return !!span && t >= span[0] && t < span[1];
    }

    function insertIndex(list, time, skipUid) {
        let index = 0;
        let acc = 0;
        for (const c of list) {
            if (c.uid === skipUid) continue;
            const l = len(c);
            if (time > acc + l / 2) index++;
            acc += l;
        }
        return index;
    }

    /* --- History (undo / redo) ----------------------------------------------- */

    function serialize() {
        return JSON.stringify({ ratio: state.ratio, video: state.video, audio: state.audio });
    }

    function syncHistory() {
        el.undo.disabled = undoStack.length === 0;
        el.redo.disabled = redoStack.length === 0;
    }

    function snapshot() {
        const now = serialize();
        if (undoStack.length && undoStack[undoStack.length - 1] === now) return;
        undoStack.push(now);
        if (undoStack.length > 80) undoStack.shift();
        redoStack.length = 0;
        syncHistory();
    }

    function ensureSnap(g) {
        if (!g.snapped) {
            snapshot();
            g.snapped = true;
        }
    }

    function restore(json) {
        const data = JSON.parse(json);
        state.ratio = data.ratio;
        state.video = data.video;
        state.audio = data.audio;
        if (state.sel && !selClip()) state.sel = null;
        applyRatio();
        renderStructure();
        layout();
        updateInspector();
        scheduleSync();
        syncHistory();
    }

    function undo() {
        const current = serialize();
        while (undoStack.length) {
            const target = undoStack.pop();
            if (target !== current) {
                redoStack.push(current);
                restore(target);
                return;
            }
        }
        syncHistory();
    }

    function redo() {
        const current = serialize();
        while (redoStack.length) {
            const target = redoStack.pop();
            if (target !== current) {
                undoStack.push(current);
                restore(target);
                return;
            }
        }
        syncHistory();
    }

    /* --- Canvas ratio ---------------------------------------------------------- */

    function applyRatio() {
        const parts = state.ratio.split(":");
        el.stage.style.setProperty("--w", parts[0]);
        el.stage.style.setProperty("--h", parts[1]);
        ratioButtons.forEach((b) => b.classList.toggle("is-active", b.dataset.ratio === state.ratio));
    }

    function setRatio(ratio) {
        if (ratio === state.ratio) return;
        snapshot();
        state.ratio = ratio;
        applyRatio();
    }

    /* --- Media library --------------------------------------------------------- */

    const searchInput = $("compSearch");
    let previewCard = null;
    let previewVideo = null;
    let lastPointerType = "mouse";

    function libraryItems() {
        const isAudio = libTab === "audio";
        const query = searchInput.value.trim().toLowerCase();
        return LIBRARY.filter((v) => (isAudio ? v.hasAudio : v.hasVideo))
            .filter((v) => !query || v.title.toLowerCase().indexOf(query) !== -1);
    }

    function usageOf(item) {
        const list = libTab === "audio" ? state.audio : state.video;
        return list.filter((c) => c.vid === item.id).length;
    }

    function refreshUsage() {
        el.library.querySelectorAll(".comp-card").forEach((card) => {
            const item = LIBRARY.find((v) => String(v.id) === card.dataset.vid);
            const badge = card.querySelector(".comp-card-used");
            if (!item || !badge) return;
            const count = usageOf(item);
            badge.hidden = count === 0;
            badge.textContent = "\u2713 " + count;
            badge.setAttribute("aria-label", S.onTimeline + " \u00D7" + count);
        });
        document.querySelectorAll(".comp-tab-count").forEach((tab) => {
            const isAudio = tab.dataset.count === "audio";
            tab.textContent = String(LIBRARY.filter((v) => (isAudio ? v.hasAudio : v.hasVideo)).length);
        });
    }

    function renderLibrary() {
        hidePreview();
        const isAudio = libTab === "audio";
        el.library.innerHTML = "";
        const total = LIBRARY.filter((v) => (isAudio ? v.hasAudio : v.hasVideo)).length;
        const items = libraryItems();
        if (!items.length) {
            const hint = document.createElement("p");
            hint.className = "comp-hint";
            hint.textContent = total ? S.noMatches : S.noItems;
            el.library.appendChild(hint);
        }
        items.forEach((item) => el.library.appendChild(buildCard(item, isAudio)));
        refreshUsage();
    }

    function buildCard(item, isAudio) {
        const card = document.createElement("div");
        card.className = "comp-card" + (isAudio ? " is-audio" : "");
        card.dataset.vid = String(item.id);

        const thumb = document.createElement("div");
        thumb.className = "comp-card-thumb";
        if (isAudio) {
            thumb.textContent = "\u266A";
            thumb.classList.add("is-sound");
        } else if (item.thumb) {
            const img = document.createElement("img");
            img.src = item.thumb;
            img.alt = "";
            img.loading = "lazy";
            thumb.appendChild(img);
        } else {
            thumb.classList.add("is-placeholder");
            thumb.textContent = (item.title.trim().charAt(0) || "\u25B6").toUpperCase();
        }

        const used = document.createElement("span");
        used.className = "comp-card-used";
        used.hidden = true;

        const add = document.createElement("button");
        add.type = "button";
        add.className = "comp-add";
        add.textContent = "+";
        add.title = S.add;
        add.setAttribute("aria-label", S.add);


        const length = document.createElement("span");
        length.className = "comp-card-dur";
        length.textContent = fmtShort(item.duration);
        const caption = document.createElement("span");
        caption.className = "comp-card-caption";
        const name = document.createElement("span");
        name.className = "comp-card-title";
        name.textContent = item.title;
        caption.appendChild(name);
        thumb.append(length, used, caption, add);
        card.appendChild(thumb);
        return card;
    }

    function showPreview(card) {
        if (card === previewCard || libTab !== "media") return;
        const item = LIBRARY.find((v) => String(v.id) === card.dataset.vid);
        const thumb = card.querySelector(".comp-card-thumb");
        if (!item || !item.videoUrl || !thumb) return;
        hidePreview();
        if (!previewVideo) {
            previewVideo = document.createElement("video");
            previewVideo.className = "comp-card-preview";
            previewVideo.muted = true;
            previewVideo.loop = true;
            previewVideo.playsInline = true;
            previewVideo.preload = "auto";
        }
        previewCard = card;
        card.classList.add("is-previewing");
        previewVideo.src = item.videoUrl;
        thumb.insertBefore(previewVideo, thumb.firstChild);
        previewVideo.play().catch(() => {});
    }

    function hidePreview() {
        if (previewVideo) {
            previewVideo.pause();
            previewVideo.removeAttribute("src");
            previewVideo.load();
            previewVideo.remove();
        }
        if (previewCard) previewCard.classList.remove("is-previewing");
        previewCard = null;
    }

    function probe(url, isVideo) {
        return new Promise((resolve) => {
            if (!url) {
                resolve(0);
                return;
            }
            const media = document.createElement(isVideo ? "video" : "audio");
            const done = (value) => {
                clearTimeout(timer);
                media.removeAttribute("src");
                media.load();
                resolve(value);
            };
            const timer = setTimeout(() => done(0), 10000);
            media.preload = "metadata";
            media.onloadedmetadata = () => done(isFinite(media.duration) ? media.duration : 0);
            media.onerror = () => done(0);
            media.src = url;
        });
    }

    function ensureDuration(item, track) {
        if (item.duration > 0) return Promise.resolve(item.duration);
        const url = track === "video" ? item.videoUrl : item.audioUrl;
        return probe(url, track === "video").then((d) => {
            if (d > 0) item.duration = d;
            return d;
        });
    }

    function addItem(item, track, at) {
        if (track === "video" ? !item.hasVideo : !item.hasAudio) return;
        ensureDuration(item, track).then((dur) => {
            if (!dur) {
                toast(S.loadFailed);
                return;
            }
            snapshot();
            if (track === "video") {
                const clip = {
                    uid: newUid(), vid: item.id, name: item.title, thumb: item.thumb,
                    src: item.videoUrl, dur: dur, inPt: 0, outPt: dur, vol: 100,
                };
                const index = at == null ? state.video.length : insertIndex(state.video, at, null);
                state.video.splice(index, 0, clip);
                state.sel = { track: "video", uid: clip.uid };
            } else {
                const clip = {
                    uid: newUid(), vid: item.id, name: item.title, thumb: "",
                    src: item.audioUrl, dur: dur, inPt: 0, outPt: dur, vol: 100,
                    offset: r2(Math.max(0, at == null ? state.playhead : at)),
                };
                state.audio.push(clip);
                state.sel = { track: "audio", uid: clip.uid };
            }
            renderStructure();
            afterEdit();
            if (phoneQuery.matches) closeDrawers();
        });
    }

    /* --- Clip elements and layout --------------------------------------------- */

    function buildClip(c, track) {
        const node = document.createElement("div");
        node.className = "comp-clip" + (track === "audio" ? " is-audio" : "");
        node.dataset.uid = c.uid;
        node.dataset.track = track;
        if (c.thumb) {
            const img = document.createElement("img");
            img.className = "comp-clip-thumb";
            img.src = c.thumb;
            img.alt = "";
            node.appendChild(img);
        }
        const name = document.createElement("span");
        name.className = "comp-clip-name";
        name.textContent = c.name;
        node.appendChild(name);
        ["l", "r"].forEach((edge) => {
            const handle = document.createElement("span");
            handle.className = "comp-handle";
            handle.dataset.edge = edge;
            node.appendChild(handle);
        });
        return node;
    }

    function renderStructure() {
        el.laneVideo.querySelectorAll(".comp-clip").forEach((n) => n.remove());
        el.laneAudio.querySelectorAll(".comp-clip").forEach((n) => n.remove());
        state.video.forEach((c) => el.laneVideo.appendChild(buildClip(c, "video")));
        state.audio.forEach((c) => el.laneAudio.appendChild(buildClip(c, "audio")));
        refreshUsage();
    }

    function placeClip(node, start, length, selected) {
        node.style.left = start * state.pps + "px";
        node.style.width = Math.max(4, length * state.pps) + "px";
        node.classList.toggle("is-selected", selected);
    }

    function renderRuler(seconds) {
        const pps = state.pps;
        const step = [1, 2, 5, 10, 15, 30, 60, 120].find((s) => s * pps >= 70) || 300;
        const key = pps + ":" + step + ":" + Math.ceil(seconds);
        if (key === rulerKey) return;
        rulerKey = key;
        el.ruler.innerHTML = "";
        for (let t = 0; t <= seconds; t += step) {
            const tick = document.createElement("span");
            tick.className = "comp-tick";
            tick.style.left = t * pps + "px";
            tick.textContent = fmtShort(t);
            el.ruler.appendChild(tick);
        }
    }

    function layout() {
        const pps = state.pps;
        const total = Math.max(timelineEnd(), 30);
        el.inner.style.width = (total + 20) * pps + 240 + "px";
        renderRuler(total + 20);

        const starts = videoStarts();
        const dragging = gesture && gesture.type === "move" && gesture.active ? gesture : null;
        state.video.forEach((c, i) => {
            const node = clipNode(c.uid);
            if (!node) return;
            let start = starts[i];
            if (dragging && dragging.track === "video" && dragging.uid === c.uid && dragging.dragStart != null) {
                start = dragging.dragStart;
            }
            placeClip(node, start, len(c), isSel("video", c.uid));
        });
        state.audio.forEach((c) => {
            const node = clipNode(c.uid);
            if (node) placeClip(node, c.offset, len(c), isSel("audio", c.uid));
        });
        updatePlayhead();
        updateStage();
    }

    function updatePlayhead() {
        const end = Math.max(timelineEnd(), 0);
        if (state.playhead > end) state.playhead = end;
        el.playhead.style.left = state.playhead * state.pps + "px";
        el.time.textContent = fmt(state.playhead);
        el.timecode.textContent = fmt(state.playhead) + " / " + fmt(end);
    }

    function followPlayhead() {
        const x = state.playhead * state.pps;
        const view = el.scroll.clientWidth;
        if (x < el.scroll.scrollLeft + 20 || x > el.scroll.scrollLeft + view - 60) {
            el.scroll.scrollLeft = Math.max(0, x - 80);
        }
    }

    function updateStage() {
        const hasVideo = state.video.length > 0;
        const hasAudio = state.audio.length > 0;
        el.empty.hidden = hasVideo;
        if (!hasVideo && !hasAudio) el.empty.textContent = S.empty;
        else if (!hasVideo) el.empty.textContent = S.audioOnly;
    }

    function afterEdit() {
        layout();
        updateInspector();
        scheduleSync();
        syncHistory();
    }

    function select(track, uid) {
        state.sel = track && uid ? { track: track, uid: uid } : null;
        layout();
        updateInspector();
    }

    /* --- Inspector --------------------------------------------------------------- */

    function updateInspector() {
        const clip = selClip();
        el.inspEmpty.hidden = !!clip;
        el.inspBody.hidden = !clip;
        if (!clip) return;
        const audio = state.sel.track === "audio";
        el.clipTitle.textContent = clip.name;
        if (document.activeElement !== el.inIn) el.inIn.value = r2(clip.inPt);
        if (document.activeElement !== el.inOut) el.inOut.value = r2(clip.outPt);
        el.offsetField.hidden = !audio;
        if (audio && document.activeElement !== el.offset) el.offset.value = r2(clip.offset);
        el.vol.value = String(clip.vol);
        el.volLabel.textContent = clip.vol + "%";
    }

    el.inIn.addEventListener("change", () => {
        const clip = selClip();
        if (!clip) return;
        const v = parseFloat(el.inIn.value);
        const next = isFinite(v) ? clamp(r2(v), 0, clip.outPt - MIN) : clip.inPt;
        if (next !== clip.inPt) {
            snapshot();
            if (state.sel.track === "audio") clip.offset = r2(Math.max(0, clip.offset + (next - clip.inPt)));
            clip.inPt = next;
        }
        afterEdit();
    });

    el.inOut.addEventListener("change", () => {
        const clip = selClip();
        if (!clip) return;
        const v = parseFloat(el.inOut.value);
        const limit = clip.dur > 0 ? clip.dur : Infinity;
        const next = isFinite(v) ? clamp(r2(v), clip.inPt + MIN, limit) : clip.outPt;
        if (next !== clip.outPt) {
            snapshot();
            clip.outPt = next;
        }
        afterEdit();
    });

    el.offset.addEventListener("change", () => {
        const clip = selClip();
        if (!clip || state.sel.track !== "audio") return;
        const v = parseFloat(el.offset.value);
        const next = isFinite(v) ? r2(Math.max(0, v)) : clip.offset;
        if (next !== clip.offset) {
            snapshot();
            clip.offset = next;
        }
        afterEdit();
    });

    el.vol.addEventListener("pointerdown", () => snapshot());
    el.vol.addEventListener("input", () => {
        const clip = selClip();
        if (!clip) return;
        clip.vol = parseInt(el.vol.value, 10);
        el.volLabel.textContent = clip.vol + "%";
        scheduleSync();
    });

    /* --- Split and delete ---------------------------------------------------------- */

    function splitAtPlayhead() {
        const t = state.playhead;
        let track = null;
        let clip = null;
        const current = selClip();
        if (current && inClip(state.sel.track, current, t)) {
            track = state.sel.track;
            clip = current;
        }
        if (!clip) {
            const hit = videoAt(t);
            if (hit) {
                track = "video";
                clip = hit.clip;
            }
        }
        if (!clip) {
            const audioHit = state.audio.find((c) => inClip("audio", c, t));
            if (audioHit) {
                track = "audio";
                clip = audioHit;
            }
        }
        if (!clip) {
            toast(S.splitNothing);
            return;
        }

        const local = track === "video" ? t - spanOf("video", clip)[0] : t - clip.offset;
        if (local < MIN || len(clip) - local < MIN) {
            toast(S.splitTooClose);
            return;
        }
        snapshot();
        const split = r2(clip.inPt + local);
        if (track === "video") {
            const index = state.video.indexOf(clip);
            const first = Object.assign({}, clip, { outPt: split });
            const second = Object.assign({}, clip, { uid: newUid(), inPt: split });
            state.video.splice(index, 1, first, second);
            state.sel = { track: "video", uid: second.uid };
        } else {
            const index = state.audio.indexOf(clip);
            const first = Object.assign({}, clip, { outPt: split });
            const second = Object.assign({}, clip, { uid: newUid(), inPt: split, offset: r2(t) });
            state.audio.splice(index, 1, first, second);
            state.sel = { track: "audio", uid: second.uid };
        }
        renderStructure();
        afterEdit();
    }

    function deleteSelected() {
        if (!state.sel) return;
        const track = state.sel.track;
        const uid = state.sel.uid;
        snapshot();
        if (track === "video") state.video = state.video.filter((c) => c.uid !== uid);
        else state.audio = state.audio.filter((c) => c.uid !== uid);
        state.sel = null;
        renderStructure();
        afterEdit();
    }

    /* --- Pointer gestures: scrub, move, trim, drag from library ---------------- */

    function timeFromX(x) {
        const rect = el.inner.getBoundingClientRect();
        return Math.max(0, (x - rect.left) / state.pps);
    }

    function dropAt(x, y, item) {
        const inside = (r) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
        const time = timeFromX(x);
        if (item.hasVideo && inside(el.laneVideo.getBoundingClientRect())) return { track: "video", time: time };
        if (item.hasAudio && inside(el.laneAudio.getBoundingClientRect())) return { track: "audio", time: time };
        return null;
    }

    function addGestureListeners() {
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
        window.addEventListener("pointercancel", onUp);
    }

    function endGesture() {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        if (ghost) {
            ghost.remove();
            ghost = null;
        }
        el.laneVideo.classList.remove("is-drop");
        el.laneAudio.classList.remove("is-drop");
        gesture = null;
    }

    function moveVideo(g, dt) {
        const clip = findClip("video", g.uid);
        if (!clip) return;
        g.dragStart = g.start0 + dt;
        const index = insertIndex(state.video, g.dragStart + len(clip) / 2, g.uid);
        const order = state.video.filter((c) => c.uid !== g.uid);
        order.splice(index, 0, clip);
        if (order.some((c, i) => c !== state.video[i])) {
            ensureSnap(g);
            state.video = order;
        }
    }

    function moveAudio(g, dt) {
        const clip = findClip("audio", g.uid);
        if (!clip) return;
        const next = r2(Math.max(0, g.start0 + dt));
        if (next !== clip.offset) {
            ensureSnap(g);
            clip.offset = next;
        }
    }

    function trimClip(g, dt) {
        const clip = findClip(g.track, g.uid);
        if (!clip) return;
        if (g.edge === "l") {
            const next = clamp(r2(g.inPt0 + dt), 0, g.outPt0 - MIN);
            if (next === clip.inPt) return;
            ensureSnap(g);
            if (g.track === "audio") clip.offset = r2(Math.max(0, clip.offset + (next - clip.inPt)));
            clip.inPt = next;
        } else {
            const limit = clip.dur > 0 ? clip.dur : Infinity;
            const next = clamp(r2(g.outPt0 + dt), clip.inPt + MIN, limit);
            if (next === clip.outPt) return;
            ensureSnap(g);
            clip.outPt = next;
        }
    }

    function onMove(e) {
        const g = gesture;
        if (!g) return;
        const dx = e.clientX - g.x0;
        const dy = e.clientY - g.y0;

        if (g.type === "lib") {
            if (!g.active) {
                if (Math.hypot(dx, dy) < 6) return;
                g.active = true;
                ghost = document.createElement("div");
                ghost.className = "comp-ghost";
                ghost.textContent = g.item.title;
                document.body.appendChild(ghost);
            }
            ghost.style.left = e.clientX + "px";
            ghost.style.top = e.clientY + "px";
            const drop = dropAt(e.clientX, e.clientY, g.item);
            el.laneVideo.classList.toggle("is-drop", !!drop && drop.track === "video");
            el.laneAudio.classList.toggle("is-drop", !!drop && drop.track === "audio");
            return;
        }

        if (g.type === "scrub") {
            seek(timeFromX(e.clientX));
            return;
        }

        if (!g.active) {
            if (Math.hypot(dx, dy) < 4) return;
            g.active = true;
        }
        const dt = dx / state.pps;
        if (g.type === "move" && g.track === "video") moveVideo(g, dt);
        else if (g.type === "move") moveAudio(g, dt);
        else trimClip(g, dt);
        layout();
        updateInspector();
    }

    function onUp(e) {
        const g = gesture;
        if (!g) return;
        if (g.type === "lib") {
            const drop = g.active ? dropAt(e.clientX, e.clientY, g.item) : null;
            if (g.active) suppressClick = true;
            endGesture();
            if (drop) addItem(g.item, drop.track, drop.time);
            return;
        }
        endGesture();
        afterEdit();
    }

    el.library.addEventListener("pointerdown", (e) => {
        lastPointerType = e.pointerType;
        if (e.pointerType === "mouse" && e.button !== 0) return;
        if (e.target.closest(".comp-add")) return;
        const card = e.target.closest(".comp-card");
        if (!card) return;
        const item = LIBRARY.find((v) => String(v.id) === card.dataset.vid);
        if (!item) return;
        suppressClick = false;
        gesture = {
            type: "lib", item: item, track: libTab === "audio" ? "audio" : "video",
            x0: e.clientX, y0: e.clientY, active: false,
        };
        addGestureListeners();
    });

    el.library.addEventListener("pointerover", (e) => {
        if (e.pointerType !== "mouse") return;
        const card = e.target.closest(".comp-card");
        if (card) showPreview(card);
    });

    el.library.addEventListener("pointerout", (e) => {
        if (e.pointerType !== "mouse") return;
        const card = e.target.closest(".comp-card");
        if (card && card === previewCard && !card.contains(e.relatedTarget)) hidePreview();
    });

    el.library.addEventListener("click", (e) => {
        if (suppressClick) {
            suppressClick = false;
            return;
        }
        const card = e.target.closest(".comp-card");
        if (!card) return;
        const item = LIBRARY.find((v) => String(v.id) === card.dataset.vid);
        if (!item) return;
        if (e.target.closest(".comp-add")) {
            addItem(item, libTab === "audio" ? "audio" : "video", null);
        } else if (lastPointerType === "touch" && e.target.closest(".comp-card-thumb")) {
            if (previewCard === card) hidePreview();
            else showPreview(card);
        }
    });

    searchInput.addEventListener("input", renderLibrary);

    el.scroll.addEventListener("pointerdown", (e) => {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        const clipEl = e.target.closest(".comp-clip");
        if (!clipEl) {
            pause();
            if (state.sel) select(null, null);
            gesture = { type: "scrub", x0: e.clientX, y0: e.clientY, active: true };
            seek(timeFromX(e.clientX));
            addGestureListeners();
            return;
        }
        e.preventDefault();
        const track = clipEl.dataset.track;
        const uid = clipEl.dataset.uid;
        const clip = findClip(track, uid);
        if (!clip) return;
        const handle = e.target.closest(".comp-handle");
        const index = track === "video" ? state.video.indexOf(clip) : -1;
        gesture = {
            type: handle ? "trim" : "move",
            edge: handle ? handle.dataset.edge : null,
            track: track,
            uid: uid,
            x0: e.clientX,
            y0: e.clientY,
            active: false,
            snapped: false,
            start0: track === "video" ? videoStarts()[index] : clip.offset,
            inPt0: clip.inPt,
            outPt0: clip.outPt,
            dragStart: null,
        };
        select(track, uid);
        addGestureListeners();
    });

    /* --- Zoom, tabs, buttons ------------------------------------------------------ */

    function setZoom(value) {
        state.pps = clamp(Number(value), 10, 200);
        el.zoom.value = String(state.pps);
        layout();
    }

    el.zoom.addEventListener("input", () => setZoom(el.zoom.value));
    el.zoomIn.addEventListener("click", () => setZoom(state.pps * 1.25));
    el.zoomOut.addEventListener("click", () => setZoom(state.pps / 1.25));

    tabButtons.forEach((button) => {
        button.addEventListener("click", () => {
            libTab = button.dataset.tab;
            tabButtons.forEach((t) => {
                const on = t === button;
                t.classList.toggle("is-active", on);
                t.setAttribute("aria-selected", String(on));
            });
            renderLibrary();
        });
    });

    ratioButtons.forEach((button) => {
        button.addEventListener("click", () => setRatio(button.dataset.ratio));
    });

    el.splitTool.addEventListener("click", splitAtPlayhead);
    el.splitBtn.addEventListener("click", splitAtPlayhead);
    el.delTool.addEventListener("click", deleteSelected);
    el.delBtn.addEventListener("click", deleteSelected);
    el.undo.addEventListener("click", undo);
    el.redo.addEventListener("click", redo);

    /* --- Playback ------------------------------------------------------------------ */

    function updatePlayButton() {
        el.play.textContent = playing ? "\u275A\u275A" : "\u25B6";
        const label = playing ? S.pause : S.play;
        el.play.title = label;
        el.play.setAttribute("aria-label", label);
    }

    function scheduleSync() {
        if (syncQueued) return;
        syncQueued = true;
        requestAnimationFrame(() => {
            syncQueued = false;
            syncMedia();
        });
    }

    function syncVideo() {
        const t = state.playhead;
        const hit = videoAt(t);
        if (!hit || !hit.clip.src) {
            previewUid = null;
            if (!el.preview.paused) el.preview.pause();
            return;
        }
        const clip = hit.clip;
        if (previewUid !== clip.uid) {
            previewUid = clip.uid;
            el.preview.src = clip.src;
        }
        el.preview.volume = clamp(clip.vol / 100, 0, 1);
        const want = clip.inPt + (t - hit.start);
        if (el.preview.readyState >= 1 && Math.abs(el.preview.currentTime - want) > (playing ? 0.35 : 0.05)) {
            try {
                el.preview.currentTime = want;
            } catch (err) {
                /* not seekable yet */
            }
        }
        if (playing) {
            if (el.preview.paused) el.preview.play().catch(() => {});
        } else if (!el.preview.paused) {
            el.preview.pause();
        }
    }

    function syncAudio() {
        const t = state.playhead;
        const live = new Set(state.audio.map((c) => c.uid));
        audioEls.forEach((node, uid) => {
            if (!live.has(uid)) {
                node.pause();
                node.removeAttribute("src");
                node.load();
                audioEls.delete(uid);
            }
        });
        state.audio.forEach((clip) => {
            const active = t >= clip.offset && t < clip.offset + len(clip) && !!clip.src;
            let node = audioEls.get(clip.uid);
            if (!active) {
                if (node && !node.paused) node.pause();
                return;
            }
            if (!node) {
                node = new Audio();
                node.preload = "auto";
                node.src = clip.src;
                node.addEventListener("loadedmetadata", () => scheduleSync());
                audioEls.set(clip.uid, node);
            }
            node.volume = clamp(clip.vol / 100, 0, 1);
            const want = clip.inPt + (t - clip.offset);
            if (node.readyState >= 1 && Math.abs(node.currentTime - want) > (playing ? 0.35 : 0.05)) {
                try {
                    node.currentTime = want;
                } catch (err) {
                    /* not seekable yet */
                }
            }
            if (playing) {
                if (node.paused) node.play().catch(() => {});
            } else if (!node.paused) {
                node.pause();
            }
        });
    }

    function syncMedia() {
        syncVideo();
        syncAudio();
    }

    el.preview.addEventListener("loadedmetadata", () => {
        const hit = videoAt(state.playhead);
        if (hit && hit.clip.uid === previewUid) {
            el.preview.currentTime = hit.clip.inPt + (state.playhead - hit.start);
        }
    });

    function seek(t) {
        state.playhead = clamp(t, 0, Math.max(timelineEnd(), 0));
        updatePlayhead();
        scheduleSync();
    }

    function tick(ts) {
        if (!playing) return;
        const dt = Math.min(0.25, Math.max(0, (ts - lastTs) / 1000));
        lastTs = ts;
        const end = timelineEnd();
        state.playhead = Math.min(end, state.playhead + dt);
        if (state.playhead >= end) {
            playing = false;
            raf = 0;
            updatePlayButton();
        }
        updatePlayhead();
        followPlayhead();
        syncMedia();
        if (playing) raf = requestAnimationFrame(tick);
    }

    function play() {
        if (!state.video.length && !state.audio.length) {
            toast(S.addFirst);
            return;
        }
        if (state.playhead >= timelineEnd()) state.playhead = 0;
        playing = true;
        lastTs = performance.now();
        updatePlayButton();
        raf = requestAnimationFrame(tick);
    }

    function pause() {
        playing = false;
        cancelAnimationFrame(raf);
        raf = 0;
        updatePlayButton();
        syncMedia();
    }

    function togglePlay() {
        if (playing) pause();
        else play();
    }

    el.play.addEventListener("click", togglePlay);
    el.start.addEventListener("click", () => {
        pause();
        seek(0);
    });
    el.end.addEventListener("click", () => {
        pause();
        seek(timelineEnd());
    });

    /* --- Export ------------------------------------------------------------------------ */

    function toast(message) {
        el.toast.textContent = message;
        el.toast.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => {
            el.toast.hidden = true;
        }, 3200);
    }

    function showProgress(percent, label) {
        el.progress.hidden = false;
        el.progressBar.style.width = clamp(percent, 0, 100) + "%";
        el.progressText.textContent = label;
    }

    function hideProgress() {
        el.progress.hidden = true;
        el.progressBar.style.width = "0%";
    }

    function stageLabel(stage) {
        const labels = {
            download: S.stageDownload,
            encode: S.stageEncode,
            join: S.stageJoin,
            mix: S.stageMix,
            finish: S.stageDone,
        };
        return labels[stage] || S.waiting;
    }

    function failExport(code) {
        rendering = false;
        el.exportBtn.disabled = false;
        hideProgress();
        toast((S.errors && S.errors[code]) || S.errors.failed);
    }

    function showResult(data) {
        rendering = false;
        el.exportBtn.disabled = false;
        hideProgress();
        const audio = data.kind === "audio";
        el.resultVideo.hidden = audio;
        el.resultAudio.hidden = !audio;
        const media = audio ? el.resultAudio : el.resultVideo;
        media.src = data.preview_url;
        el.save.href = data.download_url;
        el.result.hidden = false;
        pause();
    }

    function readJson(response) {
        return response.json().then((data) => data, () => ({ success: false, error: "invalid" }));
    }

    function poll() {
        clearTimeout(pollTimer);
        fetch(config.urls.status.replace(ZERO_ID, job))
            .then((r) => r.json().then((data) => ({ ok: r.ok, data: data }), () => ({ ok: false, data: {} })))
            .then(({ ok, data }) => {
                if (!ok) throw new Error(data.error || "missing");
                if (data.status === "ready") {
                    showResult(data);
                    return;
                }
                if (data.status === "failed") {
                    failExport(data.error || "failed");
                    return;
                }
                showProgress(data.percent || 0, stageLabel(data.stage));
                pollTimer = setTimeout(poll, 1200);
            })
            .catch((err) => failExport(err instanceof TypeError ? "network" : err.message));
    }

    function exportProject() {
        if (rendering) return;
        if (!state.video.length && !state.audio.length) {
            toast(S.addFirst);
            return;
        }
        pause();
        const payload = {
            title: el.title.value.trim(),
            canvas: state.ratio,
            video: state.video.map((c) => ({
                video_id: c.vid, in: r2(c.inPt), out: r2(c.outPt), volume: c.vol / 100,
            })),
            audio: state.audio.map((c) => ({
                video_id: c.vid, in: r2(c.inPt), out: r2(c.outPt), offset: r2(c.offset), volume: c.vol / 100,
            })),
        };
        rendering = true;
        el.exportBtn.disabled = true;
        showProgress(0, S.waiting);
        fetch(config.urls.start, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": config.csrf },
            body: JSON.stringify(payload),
        })
            .then(readJson)
            .then((data) => {
                if (!data.success) throw new Error(data.error || "failed");
                job = data.job_id;
                poll();
            })
            .catch((err) => failExport(err instanceof TypeError ? "network" : err.message));
    }

    el.exportBtn.addEventListener("click", exportProject);

    el.backEdit.addEventListener("click", () => {
        el.resultVideo.pause();
        el.resultAudio.pause();
        el.result.hidden = true;
    });

    el.discard.addEventListener("click", () => {
        if (!job) return;
        const id = job;
        job = null;
        el.resultVideo.pause();
        el.resultAudio.pause();
        el.resultVideo.removeAttribute("src");
        el.resultAudio.removeAttribute("src");
        el.result.hidden = true;
        fetch(config.urls.discard.replace(ZERO_ID, id), {
            method: "POST",
            headers: { "X-CSRFToken": config.csrf },
        }).catch(() => {});
        toast(S.discarded);
    });

    /* --- Keyboard ------------------------------------------------------------------------ */

    document.addEventListener("keydown", (e) => {
        if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
        const mod = e.ctrlKey || e.metaKey;
        const key = e.key.toLowerCase();
        if (mod && key === "z") {
            e.preventDefault();
            if (e.shiftKey) redo();
            else undo();
        } else if (mod && key === "y") {
            e.preventDefault();
            redo();
        } else if (mod) {
            return;
        } else if (e.key === " ") {
            e.preventDefault();
            togglePlay();
        } else if (key === "s") {
            splitAtPlayhead();
        } else if (e.key === "Delete" || e.key === "Backspace") {
            e.preventDefault();
            deleteSelected();
        } else if (e.key === "ArrowLeft") {
            pause();
            seek(state.playhead - (e.shiftKey ? 1 : 0.1));
        } else if (e.key === "ArrowRight") {
            pause();
            seek(state.playhead + (e.shiftKey ? 1 : 0.1));
        } else if (e.key === "Home") {
            pause();
            seek(0);
        } else if (e.key === "Escape") {
            select(null, null);
        }
    });

    /* --- Drawers: the media library and the properties on phones and tablets --- */

    const mediaPanel = document.querySelector(".comp-media");
    const inspPanel = document.querySelector(".comp-inspector");
    const backdrop = $("compBackdrop");
    const mediaToggle = $("compMediaToggle");
    const propsToggle = $("compPropsToggle");
    const phoneQuery = window.matchMedia("(max-width: 767px)");

    function closeDrawers() {
        mediaPanel.classList.remove("is-open");
        inspPanel.classList.remove("is-open");
        backdrop.hidden = true;
        mediaToggle.setAttribute("aria-expanded", "false");
        propsToggle.setAttribute("aria-expanded", "false");
    }

    function toggleDrawer(panel, toggle) {
        const wasOpen = panel.classList.contains("is-open");
        closeDrawers();
        if (wasOpen) return;
        panel.classList.add("is-open");
        backdrop.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
    }

    mediaToggle.addEventListener("click", () => toggleDrawer(mediaPanel, mediaToggle));
    propsToggle.addEventListener("click", () => toggleDrawer(inspPanel, propsToggle));
    backdrop.addEventListener("click", closeDrawers);
    document.querySelectorAll(".comp-drawer-close").forEach((button) => {
        button.addEventListener("click", closeDrawers);
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeDrawers();
    });
    window.matchMedia("(max-width: 959px)").addEventListener("change", closeDrawers);
    phoneQuery.addEventListener("change", closeDrawers);

    /* --- Start ------------------------------------------------------------------------------ */

    applyRatio();
    renderLibrary();
    renderStructure();
    setZoom(60);
    updateInspector();
    updatePlayButton();
    syncHistory();
})();
