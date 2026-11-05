/*
 * Runs shorts.js against a minimal DOM that enforces the browser autoplay rule:
 * media may only start with sound after the visitor has tapped the page.
 *
 * Usage: node static/js/tests/shorts_smoke.js <path-to-shorts.js> <mode>
 *   feed         two Shorts; the first has its MP3, the second does not yet
 *   detail       one Short on its own page, with its MP3
 *   older        one Short without an MP3, which is made on first use
 *   older-fails  the same, but making the MP3 fails
 */
"use strict";

const fs = require("fs");

const scriptPath = process.argv[2];
const mode = process.argv[3] || "feed";
const code = fs.readFileSync(scriptPath, "utf8");

let tapped = false;
let failures = 0;
const fetches = [];
const alerts = [];
const navigations = [];
let observerCallback = null;

function check(label, ok) {
    console.log((ok ? "PASS " : "FAIL ") + label);
    if (!ok) failures += 1;
}

function classList() {
    const names = new Set();
    return {
        toggle(name, force) {
            const on = force === undefined ? !names.has(name) : Boolean(force);
            if (on) names.add(name); else names.delete(name);
            return on;
        },
        add(name) { names.add(name); },
        remove(name) { names.delete(name); },
        contains(name) { return names.has(name); },
    };
}

function listeners() {
    const map = {};
    return {
        add(type, fn) { (map[type] = map[type] || []).push(fn); },
        fire(type) { (map[type] || []).forEach((fn) => fn({})); },
    };
}

// A <video> or <audio> element. Playing without a source fails, as it does in
// a browser, so a script that forgets to make the MP3 first is caught.
function makeMedia(name, src) {
    const attrs = { src };
    const events = listeners();
    const media = {
        name,
        muted: false,
        paused: true,
        plays: [],
        play() {
            media.plays.push(media.muted ? "muted" : "sound");
            if (!attrs.src) {
                return Promise.reject(Object.assign(new Error("no source"), { name: "NotSupportedError" }));
            }
            if (!media.muted && !tapped) {
                return Promise.reject(Object.assign(new Error("autoplay blocked"), { name: "NotAllowedError" }));
            }
            media.paused = false;
            return Promise.resolve();
        },
        pause() { media.paused = true; },
        click() { tapped = true; events.fire("click"); },
        addEventListener: events.add,
        getAttribute: (k) => (k in attrs ? attrs[k] : null),
        setAttribute: (k, v) => { attrs[k] = v; },
    };
    return media;
}

function makeButton(wrap) {
    const attrs = {};
    const events = listeners();
    return {
        textContent: "",
        classList: classList(),
        click() { tapped = true; events.fire("click"); },
        addEventListener: events.add,
        getAttribute: (k) => (k in attrs ? attrs[k] : null),
        setAttribute: (k, v) => { attrs[k] = v; },
        closest: (sel) => (sel === ".short-video-wrap" ? wrap : null),
    };
}

// One Short as the page markup builds it: a slide holding a video wrapper, which
// holds the picture, the MP3, the sound card, the speaker and the sound sign.
function makeShort(id, { hasAudio, hasMp3 }) {
    const attrs = {
        "data-audio-prepare-url": `/shorts/${id}/audio/`,
        "data-audio-download": hasMp3 ? `https://r2.example/audio/${id}.mp3?download=1` : "",
    };
    const wrap = {
        classList: classList(),
        getAttribute: (k) => (k in attrs ? attrs[k] : null),
        setAttribute: (k, v) => { attrs[k] = v; },
        querySelector: (sel) => ({
            ".short-video": video,
            ".short-audio": audio,
            ".short-audio-card": card,
            ".short-mute-btn": mute,
            ".sound-toggle": toggle,
            ".short-progress-bar": bar,
        })[sel] || null,
    };
    const video = makeMedia("video", "/media/video.mp4");
    const audio = hasAudio ? makeMedia("audio", hasMp3 ? `https://r2.example/audio/${id}.mp3?sig=1` : null) : null;
    const card = { ...makeButton(wrap), classList: classList() };
    const mute = makeButton(wrap);
    const toggle = hasAudio ? makeButton(wrap) : null;
    const download = hasAudio ? makeButton(wrap) : null;
    const bar = { style: {} };
    [video, audio].forEach((media) => { if (media) media.closest = (sel) => (sel === ".short-video-wrap" ? wrap : null); });

    const slide = {
        getAttribute: (name) => (name === "data-short-id" ? id : name === "data-view-url" ? `/shorts/${id}/view/` : null),
        querySelector: (sel) => (sel === ".short-video-wrap" ? wrap : null),
    };
    return { id, video, audio, card, mute, toggle, download, wrap, slide };
}

function makeShorts() {
    if (mode === "feed") {
        return [makeShort("1", { hasAudio: true, hasMp3: true }), makeShort("2", { hasAudio: true, hasMp3: false })];
    }
    if (mode === "detail") return [makeShort("1", { hasAudio: true, hasMp3: true })];
    return [makeShort("1", { hasAudio: true, hasMp3: false })];
}

const shorts = makeShorts();
const onFeed = mode !== "detail";
const slides = shorts.map((s) => s.slide);
const feed = { querySelectorAll: (sel) => (sel === ".short-slide" ? slides : []) };
const wraps = shorts.map((s) => s.wrap);
const mutes = shorts.map((s) => s.mute);
const toggles = shorts.filter((s) => s.toggle).map((s) => s.toggle);
const downloads = shorts.filter((s) => s.download).map((s) => s.download);
const prepareFails = mode === "older-fails";

globalThis.document = {
    cookie: "",
    getElementById: (id) => (onFeed && id === "shortsFeed" ? feed : null),
    querySelectorAll: (sel) => ({
        ".short-video-wrap": wraps,
        ".short-mute-btn": mutes,
        ".sound-toggle": toggles,
        ".download-btn": downloads,
    })[sel] || [],
    querySelector: (sel) => (!onFeed && sel === ".short-detail-page .short-video-wrap" ? wraps[0] : null),
    addEventListener() {},
};
globalThis.window = {
    location: {
        set href(value) { navigations.push(value); },
        get href() { return ""; },
    },
};
globalThis.alert = (message) => alerts.push(message);
globalThis.confirm = () => false;
globalThis.fetch = (url, options) => {
    fetches.push(url);
    let body = { success: true };
    if (options && options.method === "POST" && /\/audio\/$/.test(url)) {
        const id = url.match(/\/shorts\/(\d+)\/audio\//)[1];
        body = prepareFails
            ? { success: false, error: "The sound could not be prepared. Please try again." }
            : { success: true, url: `https://r2.example/audio/${id}.mp3?sig=1`, download_url: `https://r2.example/audio/${id}.mp3?download=1` };
    }
    return Promise.resolve({ json: () => Promise.resolve(body) });
};
globalThis.IntersectionObserver = class {
    constructor(callback) { observerCallback = callback; }
    observe() {}
};

const flush = () => new Promise((resolve) => setImmediate(resolve));
const entry = (slide, visible) => ({
    target: slide, isIntersecting: visible, intersectionRatio: visible ? 1 : 0,
});

async function feedScenario() {
    const [first, second] = shorts;

    check("first short tries to start with sound", first.video.plays[0] === "sound");
    check("refused sound falls back to muted playback", first.video.plays[1] === "muted" && first.video.muted && !first.video.paused);
    check("speaker icon shows muted", first.mute.textContent === "🔇");

    first.mute.click();
    await flush();
    check("tapping the speaker turns sound on", !first.video.muted && first.mute.textContent === "🔊");

    observerCallback([entry(second.slide, true), entry(first.slide, false)]);
    await flush();
    check("next short starts with sound", !second.video.muted && !second.video.paused && second.mute.textContent === "🔊");
    check("previous short is paused", first.video.paused);

    second.mute.click();
    await flush();
    check("muting the second short shows the muted icon", second.video.muted && second.mute.textContent === "🔇");

    observerCallback([entry(first.slide, true), entry(second.slide, false)]);
    await flush();
    check("muted choice carries back to the first short", first.video.muted && !first.video.paused && first.mute.textContent === "🔇");

    // Sound on again, then a short whose sound is refused, scrolled past before the refusal arrives.
    first.mute.click();
    await flush();
    tapped = false;
    observerCallback([entry(second.slide, true)]);
    observerCallback([entry(second.slide, false)]);
    await flush();
    check("a short scrolled past is not restarted", !second.video.muted && second.video.paused && second.video.plays[second.video.plays.length - 1] === "sound");

    // From here the visitor has tapped the page, which stays true for the rest of the visit.
    tapped = true;
    observerCallback([entry(first.slide, true), entry(second.slide, false)]);
    await flush();
    check("back on the first short with sound", !first.video.muted && !first.video.paused);

    first.toggle.click();
    await flush();
    check("the sound sign plays only the MP3", !first.audio.paused && !first.audio.muted && first.video.paused);
    check("the picture is hidden in sound-only mode", first.wrap.classList.contains("listen-only"));
    check("the sound sign shows as pressed", first.toggle.getAttribute("aria-pressed") === "true");

    observerCallback([entry(second.slide, true), entry(first.slide, false)]);
    await flush();
    check("the next short's MP3 is made and plays", fetches.includes("/shorts/2/audio/") && second.audio.getAttribute("src") !== null && !second.audio.paused);
    check("the next short keeps its picture hidden", second.wrap.classList.contains("listen-only") && second.video.paused);
    check("the previous short's MP3 is paused", first.audio.paused);
    check("a short scrolled away from shows its picture again", !first.wrap.classList.contains("listen-only"));

    second.card.click();
    await flush();
    check("tapping the sound card pauses the MP3", second.audio.paused);
    second.card.click();
    await flush();
    check("tapping the sound card again plays it", !second.audio.paused);

    second.toggle.click();
    await flush();
    check("tapping the sound sign again brings the picture back", !second.wrap.classList.contains("listen-only") && !second.video.paused && second.audio.paused);
    check("the sound sign shows as not pressed", second.toggle.getAttribute("aria-pressed") === "false" && first.toggle.getAttribute("aria-pressed") === "false");

    first.download.click();
    check("download goes straight to a short's MP3", navigations[navigations.length - 1] === "https://r2.example/audio/1.mp3?download=1");
}

async function detailScenario() {
    const [only] = shorts;

    check("detail video tries to start with sound", only.video.plays[0] === "sound");
    check("detail video falls back to muted playback", only.video.plays[1] === "muted" && only.video.muted && !only.video.paused);
    check("detail speaker icon shows muted", only.mute.textContent === "🔇");

    try {
        only.mute.click();
        check("detail speaker button runs without error", true);
    } catch (err) {
        check("detail speaker button runs without error (" + err.message + ")", false);
    }
    await flush();
    check("tapping the detail speaker turns sound on", !only.video.muted && only.mute.textContent === "🔊");

    only.toggle.click();
    await flush();
    check("the detail sound sign plays only the MP3", !only.audio.paused && only.video.paused && only.wrap.classList.contains("listen-only"));

    only.download.click();
    check("the detail download goes straight to the MP3", navigations[navigations.length - 1] === "https://r2.example/audio/1.mp3?download=1");
}

async function olderScenario() {
    const [only] = shorts;

    only.download.click();
    await flush();
    check("downloading makes the MP3 first", fetches.includes("/shorts/1/audio/"));
    check("the download then starts", navigations[navigations.length - 1] === "https://r2.example/audio/1.mp3?download=1");

    only.toggle.click();
    await flush();
    check("the sound sign plays the MP3 once it exists", !only.audio.paused && only.wrap.classList.contains("listen-only"));
}

async function olderFailsScenario() {
    const [only] = shorts;

    only.toggle.click();
    await flush();
    check("a failed MP3 shows the server's message", alerts[alerts.length - 1] === "The sound could not be prepared. Please try again.");
    check("a failed MP3 turns sound-only mode off", !only.wrap.classList.contains("listen-only") && only.toggle.getAttribute("aria-pressed") === "false");
    check("the picture plays instead", !only.video.paused && only.audio.paused);

    only.download.click();
    await flush();
    check("a failed download is reported and goes nowhere", alerts.length === 2 && navigations.length === 0);
}

async function main() {
    try {
        new Function(code)();
        check("script runs without throwing", true);
    } catch (err) {
        check("script runs without throwing (" + err.message + ")", false);
        return;
    }
    await flush();
    try {
        if (mode === "feed") await feedScenario();
        else if (mode === "detail") await detailScenario();
        else if (mode === "older") await olderScenario();
        else await olderFailsScenario();
    } catch (err) {
        check("the visitor's taps run without error (" + err.message + ")", false);
    }
}

main().then(() => {
    console.log(failures ? "RESULT: FAILED " + failures : "RESULT: OK");
    process.exitCode = failures ? 1 : 0;
});
