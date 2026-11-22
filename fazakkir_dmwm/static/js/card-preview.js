// YouTube-style hover preview for video cards.
// On hover, load (once) and play a muted, looping clip of the video inside the
// thumbnail; on leave, pause and reset it so the thumbnail shows again.
(function () {
    "use strict";

    function init() {
        var thumbs = document.querySelectorAll(".video-thumbnail");
        thumbs.forEach(function (thumb) {
            var preview = thumb.querySelector(".card-preview");
            if (!preview) return;
            var src = preview.getAttribute("data-src");
            if (!src) return;

            var loaded = false;
            var hoverTimer = null;

            function start() {
                if (!loaded) {
                    preview.src = src;
                    loaded = true;
                }
                // Small delay so a quick pass over a card doesn't start a heavy load.
                hoverTimer = setTimeout(function () {
                    var p = preview.play();
                    if (p && typeof p.catch === "function") p.catch(function () {});
                    preview.classList.add("is-playing");
                }, 250);
            }

            function stop() {
                if (hoverTimer) {
                    clearTimeout(hoverTimer);
                    hoverTimer = null;
                }
                try { preview.pause(); } catch (e) {}
                try { preview.currentTime = 0; } catch (e) {}
                preview.classList.remove("is-playing");
            }

            thumb.addEventListener("mouseenter", start);
            thumb.addEventListener("mouseleave", stop);
            thumb.addEventListener("touchstart", stop, { passive: true });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();