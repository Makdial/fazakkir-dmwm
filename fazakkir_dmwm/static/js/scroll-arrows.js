/*
 * Floating page up/down arrows, revealed while the visitor scrolls.
 *
 * The arrows stay out of the way at rest and at the top of the page, so they
 * never cover a card while reading. Scrolling brings them in for a moment; they
 * disappear again a couple of seconds after the page stops moving. One click
 * advances six video cards' worth of content, and clicking the same arrow twice
 * within 350 ms jumps all the way to the top or bottom.
 */
(function () {
    "use strict";

    var container = document.getElementById("scrollArrows");
    var upBtn = document.getElementById("scrollTopBtn");
    var downBtn = document.getElementById("scrollBottomBtn");
    if (!container || !upBtn || !downBtn) return;

    var DOUBLE_CLICK_MS = 350;
    var HIDE_DELAY_MS = 2500;
    var TOP_THRESHOLD = 8;
    var lastUpClick = 0;
    var lastDownClick = 0;
    var hideTimer = null;
    var reduceMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function currentY() {
        return window.scrollY || window.pageYOffset || 0;
    }

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

    function hide() {
        if (hideTimer) {
            window.clearTimeout(hideTimer);
            hideTimer = null;
        }
        container.hidden = true;
    }

    function scheduleHide() {
        if (hideTimer) window.clearTimeout(hideTimer);
        hideTimer = window.setTimeout(function () {
            hideTimer = null;
            container.hidden = true;
        }, HIDE_DELAY_MS);
    }

    function show() {
        container.hidden = false;
        scheduleHide();
    }

    function refreshButtons() {
        var max = maxScroll();
        var y = currentY();
        upBtn.disabled = y <= 1;
        downBtn.disabled = y >= max - 1;
    }

    /* Layout changes can make a page scrollable or push the visitor back to the
       top. They never reveal the controls on their own; only scrolling does. */
    function refresh() {
        refreshButtons();
        if (maxScroll() <= TOP_THRESHOLD || currentY() <= TOP_THRESHOLD) {
            hide();
        }
    }

    function handleScroll() {
        refreshButtons();
        if (maxScroll() <= TOP_THRESHOLD || currentY() <= TOP_THRESHOLD) {
            hide();
            return;
        }
        show();
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
            scrollToY(currentY() + direction * stepSize());
        }
        refreshButtons();
    }

    upBtn.addEventListener("click", function () { handle(-1); });
    downBtn.addEventListener("click", function () { handle(1); });

    window.addEventListener("scroll", handleScroll, { passive: true });
    window.addEventListener("resize", refresh);
    window.addEventListener("load", refresh);
    window.addEventListener("orientationchange", function () {
        window.setTimeout(refresh, 150);
    });

    // The content swaps in place during client-side navigation. Hide the old
    // page's controls immediately, then re-check the new page's height.
    document.addEventListener("fz:leave", hide);
    document.addEventListener("fz:page", function () {
        refresh();
        window.setTimeout(refresh, 250);
    });

    if (window.ResizeObserver) {
        new ResizeObserver(refresh).observe(document.body);
    }

    refresh();
})();
