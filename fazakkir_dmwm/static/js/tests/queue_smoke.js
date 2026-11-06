/*
 * Runs the queue (static/js/queue.js) against a minimal DOM and checks its rules:
 * where a video lands, moving, taking, removing and clearing, and what is kept
 * in localStorage.
 *
 * Usage: node static/js/tests/queue_smoke.js <path-to-queue.js>
 */
"use strict";

const fs = require("fs");
const path = process.argv[2] || "static/js/queue.js";

const KEY = "fazakkir-queue";
const storage = {};
function seed(value) {
    storage[KEY] = value;
}

function define(name, value) {
    Object.defineProperty(globalThis, name, {
        value, writable: true, configurable: true, enumerable: true,
    });
}

define("localStorage", {
    getItem: (key) => (key in storage ? storage[key] : null),
    setItem: (key, value) => { storage[key] = String(value); },
    removeItem: (key) => { delete storage[key]; },
});
define("CustomEvent", class {
    constructor(type, init) { this.type = type; this.detail = init ? init.detail : undefined; }
});
define("window", { addEventListener() {} });
define("document", {
    getElementById: () => null,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: () => ({ dataset: {}, classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, appendChild() {} }),
    addEventListener() {},
    dispatchEvent() { return true; },
});

const video = (id) => ({
    id,
    title: "Video " + id,
    channel: "owner",
    thumbnail_url: "",
    url: "/" + id,
    playback_url: "/" + id + "/playback/",
});

let failures = 0;
function expect(name, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    if (!ok) failures += 1;
    console.log((ok ? "ok    " : "FAIL  ") + name + (ok ? "" : `: got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`));
}

// Stored entries that cannot be played are dropped when the queue loads.
seed(JSON.stringify([video(9), { id: 8, title: "no link" }, "junk"]));
new Function(fs.readFileSync(path, "utf8"))();
const Q = window.FZ.queue;
expect("entries without a playback link are dropped on load", Q.items().map((v) => v.id), [9]);

Q.clear();
expect("clear empties the queue", Q.items(), []);

Q.add(video(1), "last");
Q.add(video(2), "next");
Q.add(video(3), "last");
expect("last appends and next goes first", Q.items().map((v) => v.id), [2, 1, 3]);

Q.add(video(1), "next");
expect("adding a queued video moves it instead of listing it twice", Q.items().map((v) => v.id), [1, 2, 3]);

Q.move(3, "first");
expect("first moves a video to the front", Q.items().map((v) => v.id), [3, 1, 2]);

Q.move(1, "down");
expect("down swaps with the next video", Q.items().map((v) => v.id), [3, 2, 1]);

Q.move(1, "up");
expect("up swaps with the previous video", Q.items().map((v) => v.id), [3, 1, 2]);

Q.move(3, "last");
expect("last moves a video to the end", Q.items().map((v) => v.id), [1, 2, 3]);

Q.move(1, "up");
expect("up on the first video leaves it first", Q.items().map((v) => v.id), [1, 2, 3]);

Q.move(3, "down");
expect("down on the last video leaves it last", Q.items().map((v) => v.id), [1, 2, 3]);

const first = Q.take();
expect("take returns the front video", first.id, 1);
expect("take removes it", Q.items().map((v) => v.id), [2, 3]);

const picked = Q.take(3);
expect("take by id returns that video", picked.id, 3);
expect("take by id leaves the rest in order", Q.items().map((v) => v.id), [2]);

expect("take of an unknown id returns nothing", Q.take(42), null);

Q.add(video(4), "last");
expect("remove drops one video", Q.remove(2).id, 2);
expect("the rest keep their order", Q.items().map((v) => v.id), [4]);

const saved = JSON.parse(storage[KEY]).map((v) => v.id);
expect("every change is saved to localStorage", saved, [4]);

Q.clear();
expect("clear is saved too", JSON.parse(storage[KEY]), []);

console.log(failures ? `\nFAILURES: ${failures}` : "\nOK");
process.exitCode = failures ? 1 : 0;
