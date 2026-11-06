/*
 * The video queue: the videos a visitor has lined up to play next.
 *
 * The list is kept in this browser (localStorage), so it survives reloads and
 * moving between pages. A video is added either first ("Play next") or last,
 * and the list can be reordered or cleared. The player (player.js) takes the
 * first video off the list each time one finishes.
 *
 * Markup it drives (see videos/_queue_menu.html and videos/_queue.html):
 *   [data-queue-anchor]          one per video; its data-queue-* attributes
 *                                describe that video
 *   [data-queue-toggle]          opens the menu of an anchor
 *   [data-queue-menu]            the menu; its [data-queue-add] buttons pick
 *                                "next" or "last"
 *   [data-queue-list]            a list rendered from the queue
 *   [data-queue-action]          row and header buttons
 *   [data-queue-section]         hidden while the queue is empty
 */
(function () {
    "use strict";

    var KEY = "fazakkir-queue";
    var DRAWER_ID = "fzQueue";
    var TOAST_MS = 2600;

    var items = readStored();
    var labels = {};
    var toastTimer = null;

    function readStored() {
        try {
            var list = JSON.parse(localStorage.getItem(KEY) || "[]");
            return Array.isArray(list) ? list.filter(isValid) : [];
        } catch (e) {
            return [];
        }
    }

    function isValid(entry) {
        return Boolean(entry) && typeof entry.id === "number" && Boolean(entry.playback_url);
    }

    function store() {
        try {
            localStorage.setItem(KEY, JSON.stringify(items));
        } catch (e) {
            /* storage unavailable: the queue then lasts only for this page */
        }
    }

    function indexOf(id) {
        for (var i = 0; i < items.length; i++) {
            if (items[i].id === id) return i;
        }
        return -1;
    }

    function changed() {
        store();
        render();
        document.dispatchEvent(new CustomEvent("fz:queue"));
    }

    // A video already in the queue is moved to the requested place instead of
    // being listed twice.
    function add(entry, position) {
        var at = indexOf(entry.id);
        if (at !== -1) items.splice(at, 1);
        if (position === "next") {
            items.unshift(entry);
        } else {
            items.push(entry);
        }
        changed();
    }

    function move(id, action) {
        var at = indexOf(id);
        if (at === -1) return;
        var entry = items.splice(at, 1)[0];
        var target = {
            first: 0,
            last: items.length,
            up: Math.max(0, at - 1),
            down: Math.min(items.length, at + 1),
        }[action];
        items.splice(target, 0, entry);
        changed();
    }

    function remove(id) {
        var at = indexOf(id);
        if (at === -1) return null;
        var entry = items.splice(at, 1)[0];
        changed();
        return entry;
    }

    // Takes the video at the front (or the one with ``id``) off the queue.
    function take(id) {
        if (!items.length) return null;
        var at = id === undefined ? 0 : indexOf(id);
        if (at === -1) return null;
        var entry = items.splice(at, 1)[0];
        changed();
        return entry;
    }

    function clear() {
        items = [];
        changed();
        toast(labels.queueCleared);
    }

    function toast(message) {
        var el = document.getElementById("fzToast");
        if (!el || !message) return;
        el.textContent = message;
        el.classList.add("is-visible");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () {
            el.classList.remove("is-visible");
        }, TOAST_MS);
    }

    function current() {
        return window.FZ && window.FZ.player ? window.FZ.player.current() : null;
    }

    /* --- Rendering ------------------------------------------------------ */

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function thumbnail(entry) {
        var box = el("span", "queue-row-thumb");
        if (entry.thumbnail_url) {
            var img = el("img");
            img.src = entry.thumbnail_url;
            img.alt = "";
            img.loading = "lazy";
            img.addEventListener("error", function () {
                img.remove();
                box.appendChild(el("span", "queue-row-glyph", "▶"));
            });
            box.appendChild(img);
        } else {
            box.appendChild(el("span", "queue-row-glyph", "▶"));
        }
        return box;
    }

    function rowAction(action, id, label, glyph, disabled) {
        var button = el("button", "queue-row-btn", glyph);
        button.type = "button";
        button.dataset.queueAction = action;
        button.dataset.id = String(id);
        button.title = label;
        button.setAttribute("aria-label", label);
        button.disabled = Boolean(disabled);
        return button;
    }

    function upcomingRow(entry, index) {
        var row = el("li", "queue-row");

        var play = el("button", "queue-row-play");
        play.type = "button";
        play.dataset.queueAction = "play";
        play.dataset.id = String(entry.id);
        play.title = labels.playNow;
        play.appendChild(el("span", "queue-row-index", String(index + 1)));
        play.appendChild(thumbnail(entry));
        var text = el("span", "queue-row-text");
        text.appendChild(el("span", "queue-row-title", entry.title));
        text.appendChild(el("span", "queue-row-channel", entry.channel));
        play.appendChild(text);

        var last = items.length - 1;
        var actions = el("span", "queue-row-actions");
        actions.appendChild(rowAction("first", entry.id, labels.playNext, "⤒", index === 0));
        actions.appendChild(rowAction("up", entry.id, labels.moveUp, "▲", index === 0));
        actions.appendChild(rowAction("down", entry.id, labels.moveDown, "▼", index === last));
        actions.appendChild(rowAction("last", entry.id, labels.moveToEnd, "⤓", index === last));
        actions.appendChild(rowAction("watch-page", entry.id, labels.watchPage, "↗", !entry.url));
        actions.appendChild(rowAction("remove", entry.id, labels.remove, "✕", false));

        row.appendChild(play);
        row.appendChild(actions);
        return row;
    }

    function nowRow(entry) {
        var row = el("div", "queue-row queue-row-now");
        var body = el("div", "queue-row-play is-static");
        body.appendChild(thumbnail(entry));
        var text = el("span", "queue-row-text");
        text.appendChild(el("span", "queue-row-label", labels.nowPlaying));
        text.appendChild(el("span", "queue-row-title", entry.title));
        text.appendChild(el("span", "queue-row-channel", entry.channel));
        body.appendChild(text);
        row.appendChild(body);
        return row;
    }

    function renderNow() {
        var playing = current();
        document.querySelectorAll("[data-queue-now]").forEach(function (box) {
            box.textContent = "";
            if (!playing) {
                box.hidden = true;
                return;
            }
            box.hidden = false;
            box.appendChild(nowRow(playing));
        });
    }

    function renderLists() {
        document.querySelectorAll("[data-queue-list]").forEach(function (list) {
            list.textContent = "";
            items.forEach(function (entry, index) {
                list.appendChild(upcomingRow(entry, index));
            });
        });
        document.querySelectorAll("[data-queue-empty]").forEach(function (note) {
            note.hidden = items.length > 0;
        });
    }

    function updateSections() {
        document.querySelectorAll("[data-queue-section]").forEach(function (section) {
            section.hidden = items.length === 0;
        });
        document.querySelectorAll("[data-queue-count]").forEach(function (count) {
            count.textContent = items.length ? "(" + items.length + ")" : "";
        });
        var badge = document.getElementById("fzQueueCount");
        if (badge) {
            badge.textContent = String(items.length);
            badge.hidden = items.length === 0;
        }
    }

    function updateAnchors() {
        document.querySelectorAll("[data-queue-anchor]").forEach(function (anchor) {
            var queued = indexOf(Number(anchor.dataset.queueId)) !== -1;
            anchor.classList.toggle("is-queued", queued);
            var toggle = anchor.querySelector("[data-queue-toggle]");
            if (toggle) toggle.title = queued ? labels.inQueue : labels.queueOptions;
        });
    }

    function render() {
        updateSections();
        updateAnchors();
        renderNow();
        renderLists();
    }

    /* --- Menus and the drawer ------------------------------------------- */

    function closeMenus(except) {
        document.querySelectorAll("[data-queue-menu]").forEach(function (menu) {
            if (menu === except) return;
            menu.hidden = true;
            var anchor = menu.closest("[data-queue-anchor]");
            var toggle = anchor && anchor.querySelector("[data-queue-toggle]");
            if (toggle) toggle.setAttribute("aria-expanded", "false");
        });
    }

    function toggleMenu(toggle) {
        var anchor = toggle.closest("[data-queue-anchor]");
        var menu = anchor && anchor.querySelector("[data-queue-menu]");
        if (!menu) return;
        var opening = menu.hidden;
        closeMenus(menu);
        menu.hidden = !opening;
        toggle.setAttribute("aria-expanded", String(opening));
        if (opening) {
            var first = menu.querySelector("button");
            if (first) first.focus();
        }
    }

    function entryFrom(anchor) {
        var data = anchor.dataset;
        return {
            id: Number(data.queueId),
            title: data.queueTitle || "",
            channel: data.queueChannel || "",
            thumbnail_url: data.queueThumbnail || "",
            url: data.queueUrl || "",
            playback_url: data.queuePlayback || "",
        };
    }

    function drawer() {
        return document.getElementById(DRAWER_ID);
    }

    function setDrawer(open) {
        var panel = drawer();
        if (!panel) return;
        panel.hidden = !open;
        ["fzQueueToggle", "miniQueue"].forEach(function (id) {
            var button = document.getElementById(id);
            if (button) button.setAttribute("aria-expanded", String(open));
        });
        if (document.body) document.body.classList.toggle("fz-queue-open", open);
        if (open) render();
        // The mobile navbar closes its dropdown when this opens; the drawer
        // would otherwise appear underneath it with both panels on screen.
        document.dispatchEvent(new CustomEvent("fz:queue", { detail: { open: open } }));
    }

    function runAction(button) {
        var action = button.dataset.queueAction;
        var id = Number(button.dataset.id);
        var player = window.FZ && window.FZ.player;

        if (action === "play") {
            if (player) player.playFromQueue(id);
        } else if (action === "play-all") {
            if (player) player.playNextFromQueue();
        } else if (action === "watch-page") {
            var entry = take(id);
            if (entry && player && player.watchOnPage) player.watchOnPage(entry);
        } else if (action === "first" || action === "up" || action === "down" || action === "last") {
            move(id, action);
        } else if (action === "remove") {
            remove(id);
        } else if (action === "clear") {
            window.fazakkirConfirm({
                title: labels.clearQueueTitle,
                message: labels.clearQueueMessage,
                confirmLabel: labels.clearQueueConfirm,
            }).then(function (ok) {
                if (ok) clear();
            });
        } else if (action === "close-drawer") {
            setDrawer(false);
        }
    }

    document.addEventListener("click", function (event) {
        var target = event.target instanceof Element ? event.target : null;
        if (!target) return;

        var toggle = target.closest("[data-queue-toggle]");
        if (toggle) {
            event.preventDefault();
            toggleMenu(toggle);
            return;
        }

        var addButton = target.closest("[data-queue-add]");
        if (addButton) {
            event.preventDefault();
            var anchor = addButton.closest("[data-queue-anchor]");
            closeMenus();
            if (anchor) {
                var position = addButton.dataset.queueAdd;
                add(entryFrom(anchor), position);
                toast(position === "next" ? labels.willPlayNext : labels.addedToQueue);
            }
            return;
        }

        var action = target.closest("[data-queue-action]");
        if (action) {
            event.preventDefault();
            runAction(action);
            return;
        }

        if (target.closest("#fzQueueToggle")) {
            event.preventDefault();
            var panel = drawer();
            setDrawer(panel ? panel.hidden : false);
            return;
        }

        closeMenus();
    });

    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        closeMenus();
        var panel = drawer();
        if (panel && !panel.hidden) setDrawer(false);
    });

    window.addEventListener("storage", function (event) {
        if (event.key !== KEY) return;
        items = readStored();
        render();
    });

    // A new page replaces what the panel was showing, so it closes, as the
    // navbar menu does.
    document.addEventListener("fz:page", function () {
        closeMenus();
        setDrawer(false);
        render();
    });
    document.addEventListener("fz:now", render);

    function readLabels() {
        var source = document.getElementById("fzLabels");
        if (source) labels = Object.assign({}, source.dataset);
    }

    readLabels();
    render();

    window.FZ = window.FZ || {};
    window.FZ.queue = {
        items: function () {
            return items.slice();
        },
        count: function () {
            return items.length;
        },
        has: function (id) {
            return indexOf(id) !== -1;
        },
        add: add,
        move: move,
        remove: remove,
        take: take,
        clear: clear,
        toast: toast,
        setDrawer: setDrawer,
        render: render,
    };
})();
