// YouTube-style hover/touch preview for video cards.
// Desktop: hover plays a muted, looping clip inside the thumbnail.
// Mobile: press and hold (long press) plays the preview; releasing stops it
// and a normal tap still opens the video, so navigation is never broken.
(function () {
    "use strict";

    var LONG_PRESS_MS = 400;

    function init() {
        var cards = document.querySelectorAll(".video-card");
        cards.forEach(function (card) {
            var thumb = card.querySelector(".video-thumbnail");
            var preview = thumb ? thumb.querySelector(".card-preview") : null;
            if (!preview) return;
            var src = preview.getAttribute("data-src");
            if (!src) return;

            var loaded = false;
            var hoverTimer = null;
            var pressTimer = null;
            var longPressed = false;
            var previewing = false;

            function load() {
                if (!loaded) {
                    preview.src = src;
                    loaded = true;
                }
            }

            function startPreview() {
                load();
                var p = preview.play();
                if (p && typeof p.catch === "function") p.catch(function () {});
                preview.classList.add("is-playing");
                previewing = true;
            }

            function stopPreview() {
                if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
                if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
                try { preview.pause(); } catch (e) {}
                try { preview.currentTime = 0; } catch (e) {}
                preview.classList.remove("is-playing");
                previewing = false;
            }

            // Desktop hover.
            thumb.addEventListener("mouseenter", function () {
                hoverTimer = setTimeout(function () { load(); startPreview(); }, 250);
            });
            thumb.addEventListener("mouseleave", stopPreview);

            // Mobile long-press preview.
            thumb.addEventListener("touchstart", function (e) {
                longPressed = false;
                pressTimer = setTimeout(function () {
                    longPressed = true;
                    startPreview();
                }, LONG_PRESS_MS);
            }, { passive: true });

            thumb.addEventListener("touchmove", function () {
                if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
            }, { passive: true });

            thumb.addEventListener("touchend", function (e) {
                if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
                if (longPressed) {
                    // Prevent the tap from navigating after a long press.
                    e.preventDefault();
                    stopPreview();
                    longPressed = false;
                }
            });

            thumb.addEventListener("touchcancel", function () {
                if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
                stopPreview();
                longPressed = false;
            });

            // Block navigation when a long press just happened.
            card.addEventListener("click", function (e) {
                if (longPressed) {
                    longPressed = false;
                    e.preventDefault();
                    e.stopPropagation();
                }
            }, true);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();