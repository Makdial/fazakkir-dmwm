// Shared player for link-only YouTube recordings.
//
// Unlike the native player, the YouTube IFrame API creates its own iframe, so
// we keep one persistent player (in #fzYtHost) and move that container between
// the watch page's slot and a floating mini window. Playback survives page
// navigation the same way the native player does.
(function () {
    "use strict";

    var host = document.getElementById("fzYtHost");
    if (!host) return;

    var closeBtn = document.getElementById("fzYtClose");
    var player = null;
    var currentId = null;
    var currentUrl = null;
    var docked = false;

    function loadApi(cb) {
        if (window.YT && window.YT.Player) { cb(); return; }
        var prev = window.onYouTubeIframeAPIReady;
        window.onYouTubeIframeAPIReady = function () {
            if (typeof prev === "function") prev();
            cb();
        };
        if (!document.querySelector('script[src*="youtube.com/iframe_api"]')) {
            var tag = document.createElement("script");
            tag.src = "https://www.youtube.com/iframe_api";
            document.head.appendChild(tag);
        }
    }

    function bindMediaSession(p, slot) {
        if (!navigator.mediaSession) return;
        var titleEl = document.querySelector(".video-details h1");
        var img = document.querySelector('meta[property="og:image"]');
        var artwork = img ? [{ src: img.getAttribute("content"), sizes: "512x512", type: "image/jpeg" }] : [];
        try {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: titleEl ? titleEl.textContent.trim() : document.title,
                artist: "YouTube",
                album: "Fazakkir",
                artwork: artwork,
            });
        } catch (e) {}

        var seekBy = function (delta) {
            try {
                var t = p.getCurrentTime();
                var d = p.getDuration();
                p.seekTo(Math.max(0, Math.min(d, t + delta)), true);
            } catch (e) {}
        };
        var handlers = {
            play: function () { try { p.playVideo(); } catch (e) {} },
            pause: function () { try { p.pauseVideo(); } catch (e) {} },
            seekbackward: function () { seekBy(-10); },
            seekforward: function () { seekBy(10); },
        };
        Object.keys(handlers).forEach(function (action) {
            try { navigator.mediaSession.setActionHandler(action, handlers[action]); } catch (e) {}
        });
    }

    function syncState() {
        if (!navigator.mediaSession || !player || typeof player.getPlayerState !== "function") return;
        var state = player.getPlayerState();
        try {
            navigator.mediaSession.playbackState =
                state === 1 ? "playing" : (state === 2 ? "paused" : "none");
        } catch (e) {}
    }

    function dock(slot) {
        var container = slot.querySelector(".youtube-player") || slot.querySelector(".video-player") || slot;
        if (host.parentNode !== container) container.appendChild(host);
        host.hidden = false;
        host.classList.remove("is-mini");
        host.classList.add("is-docked");

        currentUrl = window.location.pathname + window.location.search;

        var id = slot.getAttribute("data-youtube-id");
        var url = slot.getAttribute("data-youtube-url");
        if (!id) return;

        var autoplay = (location.search.indexOf("autoplay=1") !== -1);

        loadApi(function () {
            if (player && typeof player.loadVideoById === "function") {
                if (currentId !== id) {
                    currentId = id;
                    try { player.loadVideoById(id); } catch (e) { player.cueVideoById(id); }
                }
            } else {
                currentId = id;
                player = new YT.Player("fzYtPlayer", {
                    videoId: id,
                    playerVars: { rel: 0, modestbranding: 1, origin: location.origin },
                    events: {
                        onReady: function (e) {
                            bindMediaSession(e.target, slot);
                            if (autoplay) { try { e.target.playVideo(); } catch (e2) {} }
                        },
                        onStateChange: function (e) {
                            syncState();
                            if (e && e.data === 0) {
                                // Prefer the queue when something is queued, the
                                // way the native player does.
                                if (window.FZ && window.FZ.queue && typeof window.FZ.queue.count === "function" && window.FZ.queue.count() > 0) {
                                    var nextEntry = window.FZ.queue.take ? window.FZ.queue.take() : null;
                                    // Fall back to playNextFromQueue if take is unavailable.
                                    if (!nextEntry && window.FZ.player && typeof window.FZ.player.playNextFromQueue === "function") {
                                        window.FZ.player.playNextFromQueue();
                                        return;
                                    }
                                    if (nextEntry && nextEntry.url) {
                                        if (window.FZ && window.FZ.nav && typeof window.FZ.nav.navigate === "function") {
                                            window.FZ.nav.navigate(nextEntry.url);
                                        } else {
                                            window.location.href = nextEntry.url;
                                        }
                                        return;
                                    }
                                }
                                var next = document.querySelector(".related-card");
                                if (next && next.href) {
                                    var u = next.href;
                                    if (u.indexOf("autoplay=") === -1) {
                                        u += (u.indexOf("?") === -1 ? "?" : "&") + "autoplay=1";
                                    }
                                    if (window.FZ && window.FZ.nav && typeof window.FZ.nav.navigate === "function") {
                                        window.FZ.nav.navigate(u);
                                    } else {
                                        window.location.href = u;
                                    }
                                }
                            }
                        },
                    },
                });
            }
        });
    }

    function undock() {
        if (host.parentNode !== document.body) document.body.appendChild(host);
        if (currentId) {
            host.hidden = false;
            host.classList.remove("is-docked");
            host.classList.add("is-mini");
        } else {
            host.hidden = true;
        }
    }

    function onPage() {
        var slot = document.querySelector(".youtube-embed-slot");
        if (slot) dock(slot);
        else undock();
    }

    var expandBtn = document.getElementById("fzYtExpand");
    if (expandBtn) {
        expandBtn.addEventListener("click", function () {
            if (currentUrl) {
                if (window.FZ && window.FZ.nav) window.FZ.nav.navigate(currentUrl);
                else window.location.href = currentUrl;
            }
        });
    }

    window.FZ = window.FZ || {};
    window.FZ.ytCurrentTime = function () {
        if (player && typeof player.getCurrentTime === "function") {
            try { return player.getCurrentTime() || 0; } catch (e) {}
        }
        return 0;
    };
    window.FZ.ytIsPlaying = function () {
        if (player && typeof player.getPlayerState === "function") {
            try {
                var state = player.getPlayerState();
                // BUFFERING (3) is part of playback, not a pause, so switching
                // to the MP3 at that moment should start it.
                return state === 1 || state === 3;
            } catch (e) {}
        }
        return false;
    };
    window.FZ.ytPause = function () {
        if (player && typeof player.pauseVideo === "function") {
            try { player.pauseVideo(); } catch (e) {}
        }
    };
    window.FZ.ytSeekTo = function (seconds, shouldPlay) {
        if (player && typeof player.seekTo === "function") {
            try {
                player.seekTo(seconds, true);
                if (shouldPlay !== false) player.playVideo();
            } catch (e) {}
        }
    };

    if (closeBtn) {
        closeBtn.addEventListener("click", function () {
            if (player && typeof player.stopVideo === "function") {
                try { player.stopVideo(); } catch (e) {}
            }
            currentId = null;
            currentUrl = null;
            host.hidden = true;
        });
    }

    host.addEventListener("dblclick", function (event) {
        if (docked || !currentUrl) return;
        event.preventDefault();
        if (window.FZ && window.FZ.nav) window.FZ.nav.navigate(currentUrl);
        else window.location.href = currentUrl;
    });

    document.addEventListener("fz:page", onPage);
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", onPage);
    } else {
        onPage();
    }
})();
