/*
 * Executes the shared player (static/js/player.js) against a minimal DOM.
 *
 * Django's tests only ever asserted that certain strings appeared in the HTML,
 * which happily passed a script that threw on the first line. This catches the
 * class of bug where a variable is referenced but never declared.
 *
 * Usage: node static/js/tests/player_smoke.js <path-to-player.js> [payload.json]
 *
 * With a payload (the playback data a watch page embeds) the harness docks a
 * page slot and loads that video, so the playback paths run as well.
 */
"use strict";

const fs = require("fs");
const path = process.argv[2] || "C:/Users/makhf/AppData/Local/Temp/deployed.js";
const payloadPath = process.argv[3] || "";
const payloadText = payloadPath ? fs.readFileSync(payloadPath, "utf8") : "";
const payload = payloadText ? JSON.parse(payloadText) : null;

const REQUIRED = [
    "playerStage", "playerBar", "seekBar", "playPauseBtn", "backBtn", "fwdBtn",
    "currentTime", "duration", "muteBtn", "volumeBar", "rateSelect",
    "fullscreenBtn", "tapZone", "bigPlay", "video-player", "audio-player",
    "playerContainer",
];

const listeners = [];
function makeEl(id) {
    const el = {
        id,
        style: {},
        dataset: {},
        parentNode: null,
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); },
            remove(c) { this._s.delete(c); },
            toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
            contains(c) { return this._s.has(c); },
        },
        attributes: {},
        children: [],
        textContent: "",
        value: "",
        volume: 1,
        muted: false,
        paused: true,
        ended: false,
        duration: NaN,
        currentTime: 0,
        playbackRate: 1,
        hidden: false,
        playCalled: false,
        addEventListener(n, f) { listeners.push([el, n, f]); },
        removeEventListener() {},
        setAttribute(k, v) { el.attributes[k] = String(v); },
        getAttribute(k) { return el.attributes[k] ?? null; },
        hasAttribute(k) { return Object.prototype.hasOwnProperty.call(el.attributes, k); },
        removeAttribute(k) { delete el.attributes[k]; },
        appendChild(c) { el.children.push(c); c.parentNode = el; return c; },
        querySelector: () => null,
        querySelectorAll: () => [],
        closest: () => null,
        remove() {},
        focus() {},
        click() {},
        load() {},
        play() { el.playCalled = true; el.paused = false; return Promise.resolve(); },
        pause() { el.paused = true; },
        requestFullscreen() { el.fullscreenRequested = true; return Promise.resolve(); },
        requestPictureInPicture: () => Promise.resolve(),
        textTracks: [],
    };
    // Browsers reflect `src` into the attribute; the script relies on that.
    Object.defineProperty(el, "src", {
        get() { return el.attributes.src ?? ""; },
        set(value) { el.attributes.src = String(value); },
        configurable: true,
    });
    return el;
}

const elements = {};
[...REQUIRED, "video-error", "audioStatus", "sharePanel", "shareToggle",
 "commentsCount", "commentList", "commentEmpty", "mode-video", "mode-audio",
 "autoSubPanel", "customThumbnail", "subtitleFile", "commentBody"].forEach((id) => {
    elements[id] = makeEl(id);
});
elements["fz-video-data"] = makeEl("fz-video-data");
elements["fz-video-data"].textContent = payloadText;
elements.slot = makeEl("slot");

// Node 21+ exposes some of these as read-only getters, so plain assignment
// throws. defineProperty works for all of them.
function define(name, value) {
    Object.defineProperty(globalThis, name, {
        value, writable: true, configurable: true, enumerable: true,
    });
}

define("window", {
    isSecureContext: true,
    matchMedia: (q) => ({ matches: String(q).includes("hover: hover"), media: q }),
    requestAnimationFrame: (fn) => fn(),
    addEventListener() {},
    getComputedStyle: () => ({}),
});
const requested = new Set();

// Always hand back a stub so a lookup for an id the harness does not know
// about cannot mask a genuine error; ids that the page does not actually
// contain are reported at the end and checked by the Django test.
define("document", {
    getElementById: (id) => {
        requested.add(id);
        if (!elements[id]) elements[id] = makeEl(id);
        return elements[id];
    },
    querySelector: (selector) => (payload && selector === "[data-player-slot]" ? elements.slot : null),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl("created-" + tag),
    addEventListener() {},
    dispatchEvent() { return true; },
    body: makeEl("body"),
    documentElement: makeEl("html"),
});
define("CustomEvent", class {
    constructor(type, init) {
        this.type = type;
        this.detail = init ? init.detail : undefined;
    }
});
define("navigator", { share: null, clipboard: null, mediaSession: null });
define("location", { href: "", reload() {} });
define("localStorage", { getItem: () => null, setItem() {}, removeItem() {} });
define("MediaMetadata", function (m) { return m; });
define("setTimeout", () => 0);
define("clearTimeout", () => {});
define("fetch", () => Promise.resolve({ json: () => Promise.resolve({}) }));
define("alert", () => {});
define("confirm", () => false);

const code = fs.readFileSync(path, "utf8");
let failures = 0;

try {
    new Function(code)();
    console.log("script executed without throwing");
} catch (err) {
    console.log("THREW " + err.constructor.name + ": " + err.message);
    console.log(String(err.stack).split("\n").slice(1, 4).join("\n"));
    failures += 1;
}

function names(id) {
    return listeners.filter(([el]) => el.id === id).map(([, n]) => n);
}

const expected = [
    ["tapZone", "click"],
    ["bigPlay", "click"],
    ["playPauseBtn", "click"],
    ["video-player", "play"],
    ["video-player", "pause"],
];

console.log("listeners bound:");
for (const [id, ev] of expected) {
    const bound = names(id).includes(ev);
    if (!bound) failures += 1;
    console.log("  " + id.padEnd(15) + ev.padEnd(9) + (bound ? "ok" : "NOT BOUND"));
}

const video = elements["video-player"];
const audio = elements["audio-player"];
const audioMode = Boolean(payload && payload.is_audio_only === true);

if (payload) {
    // The page docks the player and loads the video it describes, then the
    // viewer taps the picture. The tap must start whichever element is active.
    for (const [el, n, fn] of listeners) {
        if (el.id === "tapZone" && n === "click") fn({ preventDefault() {} });
    }
    const activeStub = audioMode ? audio : video;
    console.log("after tapping the picture:");
    console.log("  active element      : " + (audioMode ? "audio" : "video"));
    console.log("  video.play() called : " + (video.playCalled === true));
    console.log("  audio.play() called : " + (audio.playCalled === true));
    console.log("  bar visible         : " + elements["playerBar"].classList.contains("is-visible"));
    console.log("  big play hidden     : " + elements["bigPlay"].hidden);
    if (!activeStub.playCalled) failures += 1;
}

// --- Fullscreen -----------------------------------------------------------
// iPhone Safari exposes no Element.requestFullscreen, no webkitRequestFullscreen
// prefix and no document.exitFullscreen; the only thing it will expand is the
// <video>, through its own webkitEnterFullscreen(). The button previously called
// requestFullscreen() unguarded, so on iOS it threw synchronously - before
// .catch could be attached - and the button did nothing. All three routes are
// exercised here so that cannot regress unnoticed.
const container = elements["playerContainer"];
const fullscreenClicks = listeners
    .filter(([el, n]) => el.id === "fullscreenBtn" && n === "click")
    .map(([, , fn]) => fn);
console.log("fullscreen handler bound : " + (fullscreenClicks.length > 0));
if (!fullscreenClicks.length) failures += 1;

// 1. Desktop / Android: the player's container expands.
container.fullscreenRequested = false;
fullscreenClicks.forEach((fn) => fn({ preventDefault() {} }));
console.log("  desktop -> container.requestFullscreen called : " + (container.fullscreenRequested === true));
if (!container.fullscreenRequested) failures += 1;

// 2. iPhone: the container has neither method, so the video's own API is used.
delete container.requestFullscreen;
delete container.webkitRequestFullscreen;
video.enteredFullscreen = false;
video.webkitEnterFullscreen = function () { this.enteredFullscreen = true; };
fullscreenClicks.forEach((fn) => fn({ preventDefault() {} }));
console.log("  iPhone  -> video.webkitEnterFullscreen called : " + (video.enteredFullscreen === true));
if (!video.enteredFullscreen) failures += 1;

// 3. Leaving fullscreen still works wherever the API exists.
document.fullscreenElement = container;
document.exitFullscreen = function () { document.exitCalled = true; return Promise.resolve(); };
fullscreenClicks.forEach((fn) => fn({ preventDefault() {} }));
console.log("  exit    -> document.exitFullscreen called     : " + (document.exitCalled === true));
if (!document.exitCalled) failures += 1;
document.fullscreenElement = null;

// Everything the script looks up by id, so the Django test can confirm each one
// really exists in the rendered markup.
console.log("requested ids: " + Array.from(requested).sort().join(" "));

console.log(failures ? "\nFAILURES: " + failures : "\nOK");
process.exitCode = failures ? 1 : 0;
