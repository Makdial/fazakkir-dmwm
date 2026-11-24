// YouTube-style hover/touch preview for video cards.
// Desktop: hover plays a muted, looping clip inside the thumbnail.
// Mobile: press and hold (long press) plays the preview; releasing stops it
// and a normal tap still opens the video, so navigation is never broken.
// YouTube-sourced cards (link-only) preview through a muted, autoplaying
// embed, since there is no locally stored file to play.
(function () {
    "use strict";

    var LONG_PRESS_MS = 400;

    function init() {
        var cards = document.querySelectorAll(".video-card");
        cards.forEach(function (card) {
            if (card.getAttribute("data-preview-bound") === "1") return;
            var thumb = card.querySelector(".video-thumbnail");
            if (!thumb) return;
            var preview = thumb.querySelector(".card-preview");
            var ytId = thumb.getAttribute("data-youtube-id");
            var src = preview ? preview.getAttribute("data-src") : null;

            if (!preview && !ytId) return;
            card.setAttribute("data-preview-bound", "1");

            var loaded = false;
            var hoverTimer = null;
            var pressTimer = null;
            var longPressed = false;
            var previewing = false;
            var ytFrame = null;

            function load() {
                if (preview && !loaded) {
                    preview.src = src;
                    loaded = true;
                }
                if (ytId && !ytFrame) {
                    ytFrame = document.createElement("iframe");
                    ytFrame.className = "card-preview card-preview-yt";
                    ytFrame.setAttribute("src",
                        "https://www.youtube.com/embed/" + ytId +
                        "?autoplay=1&mute=1&controls=0&modestbranding=1&rel=0&playsinline=1&loop=1&playlist=" + ytId);
                    ytFrame.setAttribute("frameborder", "0");
                    ytFrame.setAttribute("allow", "autoplay; encrypted-media; picture-in-picture");
                    ytFrame.setAttribute("aria-hidden", "true");
                    thumb.appendChild(ytFrame);
                }
            }

            function startPreview() {
                load();
                if (preview) {
                    var p = preview.play();
                    if (p && typeof p.catch === "function") p.catch(function () {});
                    preview.classList.add("is-playing");
                }
                if (ytFrame) ytFrame.classList.add("is-playing");
                previewing = true;
            }

            function stopPreview() {
                if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
                if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
                if (preview) {
                    try { preview.pause(); } catch (e) {}
                    try { preview.currentTime = 0; } catch (e) {}
                    preview.classList.remove("is-playing");
                }
                if (ytFrame) {
                    ytFrame.classList.remove("is-playing");
                    // Remove the iframe so the video stops and the thumbnail shows.
                    try { thumb.removeChild(ytFrame); } catch (e) {}
                    ytFrame = null;
                    loaded = false;
                }
                previewing = false;
            }

            thumb.addEventListener("mouseenter", function () {
                hoverTimer = setTimeout(function () { startPreview(); }, 250);
            });
            thumb.addEventListener("mouseleave", stopPreview);

            thumb.addEventListener("touchstart", function () {
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

            card.addEventListener("click", function (e) {
                if (longPressed) {
                    longPressed = false;
                    e.preventDefault();
                    e.stopPropagation();
                }
            }, true);
        });
    }

    document.addEventListener("fz:page", init);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();