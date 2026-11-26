/*
 * Client-side navigation between the pages that share one live player.
 *
 * A normal link reloads the whole document, which destroys the <video> element
 * and stops playback. For the pages listed in SPA_PATHS, a link or search is
 * instead fetched in the background and only the content of <main> is swapped.
 * The player and the queue live outside <main> (see base.html), so they keep
 * playing while the visitor moves around the site.
 *
 * Rules for a page on this list:
 *   - its inline scripts are wrapped in a function and marked data-fz-page, so
 *     they can run again each time the page is shown;
 *   - anything it attaches to the document is removed through
 *     FZ.nav.onLeave(), because the document outlives the page.
 *
 * Events on document:
 *   fz:leave  just before the content is replaced
 *   fz:page   after the new content is in place, before its scripts run
 */
(function () {
    "use strict";

    var SPA_PATHS = [
        /^\/$/,                    // home
        /^\/search\/?$/,           // search results
        /^\/channel\/[^\/]+\/?$/,  // a channel
        /^\/\d+\/?$/               // a watch page
    ];

    var STYLE_TIMEOUT_MS = 1000;

    var leaveHandlers = [];
    var sequence = 0;

    function isSpaPath(pathname) {
        return SPA_PATHS.some(function (pattern) {
            return pattern.test(pathname);
        });
    }

    function isSpaUrl(href) {
        try {
            var url = new URL(href, window.location.href);
            return url.origin === window.location.origin && isSpaPath(url.pathname);
        } catch (e) {
            return false;
        }
    }

    // Read the CSRF cookie at request time. The token can be rotated without a
    // full page reload (for example when a visitor is given a background guest
    // account), and an embedded token would then be stale.
    function csrfToken() {
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function onLeave(handler) {
        leaveHandlers.push(handler);
    }

    function leave() {
        var handlers = leaveHandlers;
        leaveHandlers = [];
        handlers.forEach(function (handler) {
            try {
                handler();
            } catch (e) {
                console.error(e);
            }
        });
        document.dispatchEvent(new CustomEvent("fz:leave"));
    }

    // Stylesheets a page adds (for example the watch page's) are loaded before
    // the swap, so the new content never appears unstyled.
    function loadStyles(doc) {
        var present = {};
        Array.prototype.forEach.call(
            document.querySelectorAll('link[rel="stylesheet"]'),
            function (link) {
                present[link.getAttribute("href")] = true;
            }
        );

        var pending = [];
        Array.prototype.forEach.call(
            doc.querySelectorAll('link[rel="stylesheet"]'),
            function (link) {
                var href = link.getAttribute("href");
                if (!href || present[href]) return;
                present[href] = true;

                var element = document.createElement("link");
                element.rel = "stylesheet";
                element.href = href;
                pending.push(new Promise(function (resolve) {
                    element.onload = resolve;
                    element.onerror = resolve;
                }));
                document.head.appendChild(element);
            }
        );

        var timeout = new Promise(function (resolve) {
            setTimeout(resolve, STYLE_TIMEOUT_MS);
        });
        return Promise.race([Promise.all(pending), timeout]);
    }

    // Scripts marked data-fz-page are copied into the document so they run.
    // Anything without the marker (the shared scripts in base.html) is left
    // alone, which is why those must never be duplicated.
    function runPageScripts(doc) {
        Array.prototype.forEach.call(
            doc.querySelectorAll("script[data-fz-page]"),
            function (source) {
                var script = document.createElement("script");
                script.textContent = source.textContent;
                document.body.appendChild(script);
                script.remove();
            }
        );
    }

    function swap(doc, url, push, scrollY) {
        var nextMain = doc.querySelector("main.main-content");
        var currentMain = document.querySelector("main.main-content");
        if (!nextMain || !currentMain) {
            window.location.assign(url);
            return;
        }

        document.title = doc.title;
        document.body.className = doc.body.className;
        currentMain.replaceChildren.apply(currentMain, Array.from(nextMain.childNodes));

        if (push) {
            history.pushState({ fz: true, scrollY: 0 }, "", url);
            window.scrollTo(0, 0);
        } else {
            // Restore the position the visitor had on this page, so going back
            // does not drop them at the top of a long list.
            var y = (typeof scrollY === "number") ? scrollY : 0;
            window.scrollTo(0, y);
        }
        document.dispatchEvent(new CustomEvent("fz:page", { detail: { url: url } }));
        runPageScripts(doc);
    }

    /**
     * Show another page without reloading the document.
     *
     * Anything that cannot be handled in place (an error, a redirect to a page
     * that is not on the list, a file download) falls back to a normal visit.
     */
    function navigate(href, options) {
        options = options || {};
        var target = new URL(href, window.location.href);

        // Remember where the visitor is on the current page so a later "back"
        // can bring them to the same spot.
        try {
            var currentScroll = (typeof window.scrollY === "number") ? window.scrollY : 0;
            var baseState = (history.state && typeof history.state === "object") ? history.state : { fz: true };
            history.replaceState(Object.assign({}, baseState, { scrollY: currentScroll }), "", window.location.href);
        } catch (e) {}

        if (!options.force) {
            if (target.href === window.location.href) {
                window.scrollTo(0, 0);
                return Promise.resolve();
            }
            if (target.pathname === window.location.pathname && target.hash) {
                window.location.assign(target.href);
                return Promise.resolve();
            }
        }

        var token = ++sequence;
        var restoreScroll = (typeof options.scrollY === "number") ? options.scrollY : null;
        return fetch(target.href, {
            credentials: "same-origin",
            headers: { Accept: "text/html" },
        })
            .then(function (response) {
                var type = response.headers.get("Content-Type") || "";
                if (!response.ok || type.indexOf("text/html") === -1) {
                    throw new Error("not a page");
                }
                var finalUrl = new URL(response.url || target.href);
                if (finalUrl.origin !== window.location.origin || !isSpaPath(finalUrl.pathname)) {
                    throw new Error("redirected away from the shared pages");
                }
                return response.text().then(function (html) {
                    return { html: html, url: finalUrl.href };
                });
            })
            .then(function (page) {
                if (token !== sequence) return;
                var doc = new DOMParser().parseFromString(page.html, "text/html");
                return loadStyles(doc).then(function () {
                    if (token !== sequence) return;
                    leave();
                    var push = options.push !== false;
                    var scrollY = push ? 0 : (restoreScroll !== null ? restoreScroll : 0);
                    swap(doc, page.url, push, scrollY);
                });
            })
            .catch(function (error) {
                if (token !== sequence) return;
                console.warn("Falling back to a full page load:", error);
                window.location.assign(target.href);
            });
    }

    // Prefetch SPA pages on hover so navigation feels instant.
    function prefetchLink(link) {
        if (!link || link.hasAttribute("download") || link.hasAttribute("data-no-spa")) return;
        if (!isSpaUrl(link.href)) return;
        if (link.dataset.prefetched === "1") return;
        link.dataset.prefetched = "1";
        fetch(link.href, { credentials: "same-origin" }).catch(function () {});
    }

    document.addEventListener("mouseover", function (event) {
        var link = event.target instanceof Element ? event.target.closest("a[href]") : null;
        prefetchLink(link);
    });

    // Prefetch on mousedown too, so even a quick click benefits.
    document.addEventListener("mousedown", function (event) {
        var link = event.target instanceof Element ? event.target.closest("a[href]") : null;
        prefetchLink(link);
    });

    document.addEventListener("click", function (event) {
        if (event.defaultPrevented || event.button !== 0) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

        var link = event.target instanceof Element ? event.target.closest("a[href]") : null;
        if (!link || link.hasAttribute("download") || link.hasAttribute("data-no-spa")) return;
        if (link.target && link.target !== "_self") return;
        // Allow jumping to a shared page (such as Home) from any page, so the
        // app icon / Home link never forces a full reload.
        if (!isSpaUrl(link.href)) return;

        event.preventDefault();
        navigate(link.href, { push: true });
    });

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!(form instanceof HTMLFormElement) || event.defaultPrevented) return;
        if ((form.method || "get").toLowerCase() !== "get") return;
        if (form.hasAttribute("data-no-spa")) return;
        if (!isSpaPath(window.location.pathname)) return;

        var action = new URL(form.getAttribute("action") || window.location.href, window.location.href);
        if (!isSpaUrl(action.href)) return;

        event.preventDefault();
        var query = new URLSearchParams(new FormData(form)).toString();
        navigate(action.pathname + (query ? "?" + query : ""), { push: true });
    });

    window.addEventListener("popstate", function (event) {
        if (isSpaPath(window.location.pathname)) {
            var state = (event && event.state && typeof event.state === "object") ? event.state : (history.state || {});
            var scrollY = (typeof state.scrollY === "number") ? state.scrollY : 0;
            navigate(window.location.href, { push: false, force: true, scrollY: scrollY });
        } else {
            window.location.reload();
        }
    });

    window.FZ = window.FZ || {};
    window.FZ.csrfToken = csrfToken;
    window.FZ.nav = {
        navigate: function (href, options) {
            return navigate(href, options);
        },
        reload: function () {
            return navigate(window.location.href, { push: false, force: true });
        },
        onLeave: onLeave,
        isSpaUrl: isSpaUrl,
    };
})();
