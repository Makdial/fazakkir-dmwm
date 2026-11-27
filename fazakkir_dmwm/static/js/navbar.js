/**
 * Mobile navigation menu.
 *
 * On small screens the navbar actions are hidden behind a hamburger button.
 * This toggles that menu and keeps it accessible (aria-expanded, Escape,
 * outside click, and reset when the viewport grows back to desktop).
 */
(function () {
    "use strict";

    // Any element with data-fz-back returns to the previous page. The href is
    // a sensible fallback for a direct visit with no browser history. Modified
    // clicks (new tab, new window) keep the normal link behaviour.
    document.addEventListener("click", function (event) {
        if (event.defaultPrevented || event.button !== 0) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        var trigger = event.target instanceof Element
            ? event.target.closest("[data-fz-back]")
            : null;
        if (!trigger) return;
        if (window.history.length > 1) {
            event.preventDefault();
            window.history.back();
        }
    });

    // Home is the start of the site, so the back arrow is hidden there. The
    // navbar survives client-side navigation, which means the control has to
    // follow the address bar instead of being decided once at render time.
    function syncBackControl() {
        var back = document.getElementById("navBack");
        if (!back) return;
        var path = window.location.pathname.replace(/\/+$/, "") || "/";
        back.hidden = path === "/";
    }
    syncBackControl();
    document.addEventListener("fz:page", syncBackControl);
    window.addEventListener("popstate", syncBackControl);

    var toggle = document.getElementById("navToggle");
    var menu = document.getElementById("navMenu");
    if (!toggle || !menu) return;

    var MOBILE_BREAKPOINT = 768;

    function setOpen(open) {
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        menu.classList.toggle("open", open);
    }

    function isOpen() {
        return toggle.getAttribute("aria-expanded") === "true";
    }

    toggle.addEventListener("click", function (event) {
        event.stopPropagation();
        setOpen(!isOpen());
    });

    // Close when tapping outside the menu.
    document.addEventListener("click", function (event) {
        if (!isOpen()) return;
        if (menu.contains(event.target) || toggle.contains(event.target)) return;
        setOpen(false);
    });

    // Close on Escape.
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && isOpen()) {
            setOpen(false);
            toggle.focus();
        }
    });

    // Reset to the desktop layout when the window gets wide enough.
    window.addEventListener("resize", function () {
        if (window.innerWidth > MOBILE_BREAKPOINT && isOpen()) {
            setOpen(false);
        }
    });

    // Following a link closes the menu, as a full page load used to.
    document.addEventListener("fz:page", function () {
        setOpen(false);
    });

    // The queue drawer lives above the page too. On a phone both cannot be
    // open, so opening the queue folds the menu away.
    document.addEventListener("fz:queue", function (event) {
        if (event.detail && event.detail.open) setOpen(false);
    });
})();
