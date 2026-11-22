// Background playback + lock-screen controls for YouTube embeds.
//
// Imported (native) videos use the site's own player and already get this for
// free. Link-only YouTube recordings are played through YouTube's embedded
// player, so we drive it through the IFrame API and register a Media Session
// so the title, thumbnail and play/pause/seek controls show up on the lock
// screen and keep working while the tab is in the background.
(function () {
    "use strict";

    var slots = document.querySelectorAll(".youtube-embed-slot iframe");
    if (!slots.length) return;

    var pending = [];
    slots.forEach(function (iframe) {
        pending.push(iframe);
    });

    function setupPlayer(iframe) {
        if (!window.YT || !window.YT.Player) return;
        var player = new window.YT.Player(iframe, {
            events: {
                onReady: function (e) {
                    bindMediaSession(e.target, iframe);
                },
                onStateChange: function () {
                    syncState();
                },
            },
        });
        iframe._ytPlayer = player;
    }

    function syncState() {
        if (!navigator.mediaSession) return;
        // Find the first player and report its state.
        var iframe = pending[0];
        var player = iframe && iframe._ytPlayer;
        if (!player || typeof player.getPlayerState !== "function") return;
        var state = player.getPlayerState();
        try {
            navigator.mediaSession.playbackState =
                state === 1 ? "playing" : (state === 2 ? "paused" : "none");
        } catch (e) {}
    }

    function bindMediaSession(player, iframe) {
        if (!navigator.mediaSession) return;
        var title = document.querySelector(".video-details h1");
        var thumb = document.querySelector(".video-details") ? null : null;
        var img = document.querySelector('meta[property="og:image"]');
        var artwork = img ? [{ src: img.getAttribute("content"), sizes: "512x512", type: "image/jpeg" }] : [];
        try {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: title ? title.textContent.trim() : document.title,
                artist: "YouTube",
                album: "Fazakkir",
                artwork: artwork,
            });
        } catch (e) {}

        var seekBy = function (delta) {
            try {
                var t = player.getCurrentTime();
                var d = player.getDuration();
                player.seekTo(Math.max(0, Math.min(d, t + delta)), true);
            } catch (e) {}
        };
        var handlers = {
            play: function () { try { player.playVideo(); } catch (e) {} },
            pause: function () { try { player.pauseVideo(); } catch (e) {} },
            seekbackward: function () { seekBy(-10); },
            seekforward: function () { seekBy(10); },
            stop: function () { try { player.stopVideo(); } catch (e) {} },
        };
        Object.keys(handlers).forEach(function (action) {
            try { navigator.mediaSession.setActionHandler(action, handlers[action]); } catch (e) {}
        });
    }

    function loadApiAndInit() {
        if (window.YT && window.YT.Player) {
            pending.forEach(setupPlayer);
            return;
        }
        var tag = document.createElement("script");
        tag.src = "https://www.youtube.com/iframe_api";
        document.head.appendChild(tag);
        window.onYouTubeIframeAPIReady = function () {
            pending.forEach(setupPlayer);
        };
    }

    loadApiAndInit();
})();