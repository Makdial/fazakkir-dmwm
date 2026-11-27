/*
 * Floating page up/down arrows, available on every scrollable page.
 *
 * One click advances by six video cards (six rows on a phone, or about one
 * screen on a page without cards). Clicking the same arrow twice within 350 ms
 * jumps straight to the top or bottom, which makes long grids quick to
 * traverse. The arrows stay available instead of disappearing on a timer; they
 * only disable themselves at the ends of the page.
 */
(function () {
    "use strict";

    var container = document.getElementById("scrollArrows");
    var upBtn = document.getElementById("scrollTopBtn");
    var downBtn = document.getElementById("scrollBottomBtn");
    if (!container || !upBtn || !downBtn) return;

    var DOUBLE_CLICK_MS = 350;
    var lastUpClick = 0;
    var lastDownClick = 0;
    var reduceMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function maxScroll() {
        return Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    }

    function scrollToY(y) {
        y = Math.max(0, Math.min(y, maxScroll()));
        if (reduceMotion) {
            window.scrollTo(0, y);
            return;
        }
        try {
            window.scrollTo({ top: y, behavior: "smooth" });
        } catch (e) {
            window.scrollTo(0, y);
        }
    }

    /* Six cards plus the grid's own row gap. Pages without cards move by
       roughly one screen instead. */
    function stepSize() {
        var card = document.querySelector(
            ".video-grid .video-card-wrap, .video-grid .card, .related-card, .card"
        );
        if (!card) return Math.max(320, Math.round(window.innerHeight * 0.85));

        var gap = 16;
        var grid = card.closest(".video-grid");
        if (grid) {
            var rowGap = parseFloat(window.getComputedStyle(grid).rowGap);
            if (!isNaN(rowGap)) gap = rowGap;
        }
        var height = card.getBoundingClientRect().height;
        return Math.max((height + gap) * 6, 240);
    }

    function refresh() {
        var max = maxScroll();
        var y = window.scrollY || window.pageYOffset || 0;
        var scrollable = max > 8;

        container.hidden = !scrollable;
        if (!scrollable) return;

        upBtn.disabled = y <= 1;
        downBtn.disabled = y >= max - 1;
    }

    function handle(direction) {
        var now = Date.now();
        var lastClick = direction < 0 ? lastUpClick : lastDownClick;
        var doubleClick = (now - lastClick) < DOUBLE_CLICK_MS;

        if (direction < 0) {
            lastUpClick = now;
        } else {
            lastDownClick = now;
        }

        if (doubleClick) {
            scrollToY(direction < 0 ? 0 : maxScroll());
            if (direction < 0) {
                lastUpClick = 0;
            } else {
                lastDownClick = 0;
            }
        } else {
            scrollToY((window.scrollY || 0) + direction * stepSize());
        }
        refresh();
    }

    upBtn.addEventListener("click", function () { handle(-1); });
    downBtn.addEventListener("click", function () { handle(1); });

    window.addEventListener("scroll", refresh, { passive: true });
    window.addEventListener("resize", refresh);
    window.addEventListener("load", refresh);
    window.addEventListener("orientationchange", function () {
        window.setTimeout(refresh, 150);
    });

    // The content swaps in place during client-side navigation, so wait for the
    // new page (and any late layout) before deciding whether the arrows apply.
    document.addEventListener("fz:page", function () {
        refresh();
        window.setTimeout(refresh, 250);
    });

    if (window.ResizeObserver) {
        new ResizeObserver(refresh).observe(document.body);
    }

    refresh();
})();
