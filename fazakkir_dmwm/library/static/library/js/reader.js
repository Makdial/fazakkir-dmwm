/* Library PDF reader — continuous, scrollable pages.
 * - Crisp rendering (devicePixelRatio-aware, capped for iOS limits)
 * - Lazy render around the viewport, pages pruned when far away
 * - Placeholder skeletons so nothing looks "broken" while loading
 * - Falls back to opening the PDF in a new tab if a page cannot render
 * - Text selection triggers context-aware translation
 */
(function(){
  "use strict";

  var pdfUrl = window.LIBRARY_PDF_URL || "";
  var startPage = Math.max(1, window.LIBRARY_START_PAGE || 1);
  var slug = window.LIBRARY_SLUG || "";
  var translateUrl = window.LIBRARY_TRANSLATE_URL;
  var progressUrl = window.LIBRARY_PROGRESS_URL;
  var bookmarkUrl = window.LIBRARY_BOOKMARK_URL;
  var csrf = window.LIBRARY_CSRF;
  var sourceLang = window.LIBRARY_SOURCE_LANG || "auto";

  var readerMain = document.getElementById("readerMain");
  var pagesContainer = document.getElementById("pdfPages");
  var nativeFrame = document.getElementById("nativeFrame");
  var statusEl = document.getElementById("readerStatus");
  var pageInfo = document.getElementById("pageInfo");
  var pageInfoMobile = document.getElementById("pageInfoMobile");
  var zoomLevel = document.getElementById("zoomLevel");

  var pdfDoc = null;
  var numPages = 0;
  var scale = 1.2;
  var pageNum = startPage;
  var pageDims = {};
  var baseDims = null;
  var pagePromises = {};
  var renderedAt = {};
  var inflight = {};
  var visibleRatios = {};
  var useNative = false;
  var zoomTimer = null;
  var saveTimer = null;
  var dimsCursor = 2;

  var RENDER_RADIUS = 3;
  var PRUNE_RADIUS = 8;
  var DPR_CAP = 2;
  var MAX_CANVAS_PX = 4096;

  function setStatus(msg){ if (statusEl) statusEl.textContent = msg; }
  function fail(msg){ console.error("[reader]", msg); setStatus(msg); }

  /* ---------------- Translation ---------------- */
  var translationBox = document.getElementById("translationBox");
  var trOriginal = document.getElementById("translationOriginal");
  var trResult = document.getElementById("translationResult");
  var translateToggle = document.getElementById("translateToggle");
  var targetLang = document.getElementById("targetLang");
  var viewMode = document.getElementById("viewMode");

  function applyViewMode(){
    if (!translationBox) return;
    var mode = viewMode ? viewMode.value : "sidebyside";
    translationBox.classList.remove("mode-original","mode-translated","mode-sidebyside");
    translationBox.classList.add("mode-" + mode);
    trOriginal.hidden = (mode === "translated");
    trResult.hidden = (mode === "original");
  }
  if (viewMode) { viewMode.addEventListener("change", applyViewMode); applyViewMode(); }

  var translateEnabled = translateToggle ? translateToggle.checked : false;
  if (translateToggle) {
    translateToggle.addEventListener("change", function(){ translateEnabled = translateToggle.checked; });
  }

  function doTranslate(text, pageForContext){
    if (!text || text.length < 2 || !translationBox) return;
    trOriginal.textContent = text;
    trResult.textContent = "...";
    translationBox.hidden = false;
    fetch(translateUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({
        text: text,
        source: sourceLang,
        target: targetLang.value,
        context: pageText(pageForContext),
        book: slug,
        page: pageForContext
      })
    }).then(function(r){return r.json();}).then(function(data){
      trResult.textContent = data.translated || data.error || "Translation unavailable";
    }).catch(function(){ trResult.textContent = "Translation unavailable"; });
  }

  function maybeTranslate(win){
    if (!translateEnabled) return;
    var sel = win.getSelection ? win.getSelection() : null;
    if (!sel || sel.isCollapsed) return;
    var text = sel.toString().trim();
    if (!text) return;
    var page = pageNum;
    try {
      var el = sel.anchorNode;
      el = el && (el.nodeType === 1 ? el : el.parentElement);
      while (el && el !== pagesContainer) {
        if (el.dataset && el.dataset.page) { page = parseInt(el.dataset.page, 10) || page; break; }
        el = el.parentElement;
      }
    } catch (e) { /* ignore */ }
    doTranslate(text, page);
  }

  pagesContainer.addEventListener("mouseup", function(){ maybeTranslate(window); });
  if (nativeFrame) {
    nativeFrame.addEventListener("load", function(){
      try {
        var win = nativeFrame.contentWindow;
        if (win && win.document) win.document.addEventListener("mouseup", function(){ maybeTranslate(win); });
      } catch (e) { /* not same-origin */ }
    });
  }

  /* ---------------- Shells ---------------- */
  function shell(num){
    return pagesContainer.querySelector('.pdf-page[data-page="' + num + '"]');
  }
  function pageText(num){
    var el = shell(num);
    var tl = el && el.querySelector(".text-layer");
    return tl ? (tl.textContent || "") : "";
  }
  function getPage(num){
    if (!pagePromises[num]) pagePromises[num] = pdfDoc.getPage(num);
    return pagePromises[num];
  }
  function pixelFactor(vp){
    var f = Math.min(window.devicePixelRatio || 1, DPR_CAP);
    var longest = Math.max(vp.width, vp.height);
    if (longest * f > MAX_CANVAS_PX) f = MAX_CANVAS_PX / longest;
    return Math.max(f, 0.5);
  }
  function sizeShell(num, d){
    var el = shell(num);
    if (!el || !d) return;
    el.style.width = Math.round(d.width) + "px";
    el.style.height = Math.round(d.height) + "px";
  }
  function showSkeleton(el){
    if (el.querySelector(".pdf-skeleton")) return;
    var s = document.createElement("div");
    s.className = "pdf-skeleton";
    el.appendChild(s);
  }
  function hideSkeleton(el){
    if (!el) return;
    var s = el.querySelector(".pdf-skeleton");
    if (s) s.remove();
  }

  var openedFallback = false;
  function failPage(num, err){
    var el = shell(num);
    if (!el) return;
    hideSkeleton(el);
    if (el.querySelector(".page-failed")) return;
    var box = document.createElement("div");
    box.className = "page-failed";
    var p = document.createElement("p");
    p.textContent = "Page " + num + " could not be rendered.";
    if (err && err.message) {
      var small = document.createElement("small");
      small.textContent = err.message;
      small.style.display = "block";
      small.style.opacity = "0.7";
      small.style.fontSize = ".75rem";
      small.style.marginTop = ".25rem";
      box.appendChild(small);
    }
    var a = document.createElement("a");
    a.href = pdfUrl || "#";
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Open the PDF directly";
    box.appendChild(p);
    box.appendChild(a);
    el.appendChild(box);
    console.error("[reader] page " + num + " failed:", err);
    if (pdfUrl && !openedFallback) {
      openedFallback = true;
      try { window.open(pdfUrl, "_blank", "noopener"); } catch (e) { /* ignore */ }
      setStatus("Some pages could not be rendered. The PDF was opened in a new tab.");
    }
  }

  function buildShell(num){
    var el = shell(num);
    if (el) return el;
    el = document.createElement("div");
    el.className = "pdf-page";
    el.dataset.page = num;
    el.appendChild(document.createElement("canvas"));
    var tl = document.createElement("div");
    tl.className = "text-layer";
    el.appendChild(tl);
    if (baseDims) { sizeShell(num, {width: baseDims.width * scale, height: baseDims.height * scale}); }
    pagesContainer.appendChild(el);
    showSkeleton(el);
    if (observer) observer.observe(el);
    return el;
  }

  /* ---------------- Rendering ---------------- */
  function renderPage(num){
    if (!pdfDoc || useNative) return;
    var el = buildShell(num);
    var canvas = el.querySelector("canvas");
    var want = Math.round(scale * 100);
    if (renderedAt[num] === want && !inflight[num]) return;

    if (inflight[num]) {
      try { inflight[num].cancel(); } catch (e) { /* ignore */ }
      delete inflight[num];
    }

    var localWant = want;
    showSkeleton(el);
    getPage(num).then(function(page){
      if (localWant !== Math.round(scale * 100)) return;
      var vpCss = page.getViewport({scale: scale});
      var f = pixelFactor(vpCss);
      pageDims[num] = {width: vpCss.width, height: vpCss.height};
      sizeShell(num, pageDims[num]);
      var vpPix = page.getViewport({scale: scale * f});
      canvas.width = Math.round(vpPix.width);
      canvas.height = Math.round(vpPix.height);
      canvas.style.width = Math.round(vpCss.width) + "px";
      canvas.style.height = Math.round(vpCss.height) + "px";
      var task = page.render({canvasContext: canvas.getContext("2d"), viewport: vpPix});
      inflight[num] = task;
      task.promise.then(function(){
        if (localWant !== Math.round(scale * 100)) return;
        renderedAt[num] = want;
        hideSkeleton(el);
        loadedOk = true;
        setStatus("");
      }).catch(function(err){
        if (err && err.name === "RenderingCancelledException") return;
        console.error("[reader] render p" + num + " failed:", err);
        // Retry once with annotations disabled (some PDFs have malformed
        // annotations that make the normal render throw).
        if (inflight[num]) {
          try { inflight[num].cancel(); } catch (e) { /* ignore */ }
          delete inflight[num];
        }
        getPage(num).then(function(page2){
          if (localWant !== Math.round(scale * 100)) return;
          var vp2 = page2.getViewport({scale: scale});
          var c2 = el.querySelector("canvas");
          c2.width = 0; c2.height = 0;
          c2.width = Math.round(vp2.width);
          c2.height = Math.round(vp2.height);
          c2.style.width = Math.round(vp2.width) + "px";
          c2.style.height = Math.round(vp2.height) + "px";
          var t2 = page2.render({canvasContext: c2.getContext("2d"), viewport: vp2, annotationMode: 0});
          inflight[num] = t2;
          t2.promise.then(function(){
            if (localWant !== Math.round(scale * 100)) return;
            renderedAt[num] = want;
            hideSkeleton(el);
            loadedOk = true;
            setStatus("");
          }).catch(function(err2){
            if (err2 && err2.name === "RenderingCancelledException") return;
            console.error("[reader] render p" + num + " retry failed:", err2);
            failPage(num, err2);
          }).finally(function(){
            if (inflight[num] === t2) delete inflight[num];
          });
          var tl = el.querySelector(".text-layer");
          if (!tl.hasChildNodes()) {
            page2.getTextContent().then(function(tc){
              if (localWant !== Math.round(scale * 100)) return;
              try {
                if (typeof pdfjsLib.renderTextLayer === "function") {
                  pdfjsLib.renderTextLayer({textContent: tc, container: tl, viewport: vp2, textDivs: []});
                }
              } catch (e) { console.warn("[reader] text layer", e); }
            }).catch(function(){});
          }
        }).catch(function(err2){
          console.error("[reader] getPage retry " + num, err2);
          failPage(num, err2);
        });
      }).finally(function(){
        if (inflight[num] === task) delete inflight[num];
      });
      var tl = el.querySelector(".text-layer");
      tl.innerHTML = "";
      page.getTextContent().then(function(textContent){
        if (localWant !== Math.round(scale * 100)) return;
        try {
          if (typeof pdfjsLib.renderTextLayer === "function") {
            pdfjsLib.renderTextLayer({textContent: textContent, container: tl, viewport: vpCss, textDivs: []});
          } else {
            textContent.items.forEach(function(item){
              var tx = pdfjsLib.Util.transform(vpCss.transform, item.transform);
              var span = document.createElement("span");
              span.textContent = item.str;
              span.style.left = tx[4] + "px";
              span.style.top = (tx[5] - item.height) + "px";
              span.style.fontSize = item.height + "px";
              tl.appendChild(span);
            });
          }
        } catch (e) { console.warn("[reader] text layer", e); }
      }).catch(function(err){ console.warn("[reader] text content", err); });
    }).catch(function(err){
      console.error("[reader] getPage " + num, err);
      failPage(num, err);
    });
  }

  function clearPage(num){
    var el = shell(num);
    if (!el) return;
    if (inflight[num]) {
      try { inflight[num].cancel(); } catch (e) { /* ignore */ }
      delete inflight[num];
    }
    var canvas = el.querySelector("canvas");
    canvas.width = 0; canvas.height = 0;
    delete renderedAt[num];
    showSkeleton(el);
  }

  function renderVisible(){
    var lo = Math.max(1, pageNum - RENDER_RADIUS);
    var hi = Math.min(numPages, pageNum + RENDER_RADIUS);
    for (var n = lo; n <= hi; n++) renderPage(n);
    var plo = Math.max(1, pageNum - PRUNE_RADIUS);
    var phi = Math.min(numPages, pageNum + PRUNE_RADIUS);
    Object.keys(renderedAt).forEach(function(k){
      var n = parseInt(k, 10);
      if (n < plo || n > phi) clearPage(n);
    });
  }

  var observer = null;
  function setupObserver(){
    if (observer) observer.disconnect();
    observer = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        var num = parseInt(en.target.dataset.page, 10);
        visibleRatios[num] = en.isIntersecting ? en.intersectionRatio : 0;
        if (en.isIntersecting) renderPage(num);
      });
      var best = 0, bestNum = pageNum;
      Object.keys(visibleRatios).forEach(function(k){
        var r = visibleRatios[k] || 0;
        if (r > best) { best = r; bestNum = parseInt(k, 10); }
      });
      if (best > 0 && bestNum !== pageNum) {
        pageNum = bestNum;
        updatePageInfo();
        scheduleSave();
      }
      renderVisible();
    }, {root: readerMain, rootMargin: "900px 0px", threshold: [0, 0.1, 0.3, 0.6, 0.9, 1]});
    pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){ observer.observe(el); });
  }

  function preloadDims(){
    if (!pdfDoc || dimsCursor > numPages) return;
    var batch = 0;
    function next(){
      if (dimsCursor > numPages || batch >= 8) { requestIdle(preloadDims); return; }
      var n = dimsCursor++;
      batch++;
      getPage(n).then(function(page){
        if (baseDims) {
          var vp = page.getViewport({scale: 1});
          var d = {width: vp.width, height: vp.height};
          if (Math.abs(d.width - baseDims.width) > 2 || Math.abs(d.height - baseDims.height) > 2) {
            pageDims[n] = {width: d.width * scale, height: d.height * scale};
            if (renderedAt[n] === undefined) sizeShell(n, pageDims[n]);
          }
        }
        next();
      }).catch(function(){ next(); });
    }
    next();
  }
  function requestIdle(fn){
    if (window.requestIdleCallback) window.requestIdleCallback(fn, {timeout: 800});
    else setTimeout(fn, 200);
  }

  /* ---------------- Navigation / progress ---------------- */
  function goToPage(num){
    num = Math.min(Math.max(1, num), numPages || 1);
    var el = shell(num) || buildShell(num);
    el.scrollIntoView({behavior: "smooth", block: "start"});
    pageNum = num;
    updatePageInfo();
    scheduleSave();
    renderVisible();
  }
  function updatePageInfo(){
    var txt = (pdfDoc ? pageNum : 1) + " / " + (numPages || 1);
    if (pageInfo) pageInfo.textContent = txt;
    if (pageInfoMobile) pageInfoMobile.textContent = txt;
  }
  function scheduleSave(){
    if (!progressUrl) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function(){
      var percent = numPages ? (pageNum / numPages * 100) : 0;
      fetch(progressUrl, {
        method: "POST",
        headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
        body: JSON.stringify({page: pageNum, percent: percent})
      }).catch(function(){});
    }, 800);
  }

  /* ---------------- Zoom / fit ---------------- */
  function updateScaleVar(){
    if (pagesContainer) pagesContainer.style.setProperty("--scale-factor", String(scale));
  }
  function scheduleZoomRender(delay){
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(renderVisible, delay);
  }
  function applyScale(next, opts){
    if (useNative || !pdfDoc) return;
    scale = Math.max(0.4, Math.min(3, next));
    updateScaleVar();
    if (zoomLevel) zoomLevel.textContent = Math.round(scale * 100) + "%";
    pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){
      var n = parseInt(el.dataset.page, 10);
      var d = pageDims[n];
      if (!d && baseDims) d = {width: baseDims.width * scale, height: baseDims.height * scale};
      sizeShell(n, d);
      if (renderedAt[n] !== undefined) { delete renderedAt[n]; }
    });
    if (opts && opts.keepPage) {
      requestAnimationFrame(function(){ goToPage(pageNum); });
    }
    scheduleZoomRender(160);
  }
  function fitToWidth(){
    if (useNative || !pdfDoc) return;
    getPage(1).then(function(page){
      var vp = page.getViewport({scale: 1});
      var target = Math.min((readerMain ? readerMain.clientWidth : window.innerWidth) - 28, 1400);
      applyScale(target / vp.width, {keepPage: true});
    });
  }
  function autoFitIfSmall(){
    var w = readerMain ? readerMain.clientWidth : window.innerWidth;
    if (!useNative && pdfDoc && w < 1024) fitToWidth();
  }

  /* ---------------- Controls ---------------- */
  function bind(id, fn){
    var el = document.getElementById(id);
    if (el) el.addEventListener("click", fn);
  }
  bind("prevPage", function(){ goToPage(pageNum - 1); });
  bind("nextPage", function(){ goToPage(pageNum + 1); });
  bind("zoomIn", function(){ applyScale(scale + 0.2, {keepPage: true}); });
  bind("zoomOut", function(){ applyScale(scale - 0.2, {keepPage: true}); });
  bind("fitWidth", fitToWidth);

  var darkToggle = document.getElementById("darkMode");
  var readerEl = document.getElementById("reader");
  if (darkToggle) darkToggle.addEventListener("change", function(){
    readerEl.classList.toggle("dark", darkToggle.checked);
  });

  var sidebar = document.getElementById("readerSidebar");
  var toggleSidebar = document.getElementById("toggleSidebar");
  var backdrop = null;
  function openDrawer(){
    if (!sidebar) return;
    sidebar.classList.add("open");
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.className = "reader-backdrop";
      backdrop.addEventListener("click", closeDrawer);
      document.body.appendChild(backdrop);
    }
  }
  function closeDrawer(){
    if (sidebar) sidebar.classList.remove("open");
    if (backdrop) { backdrop.remove(); backdrop = null; }
  }
  if (toggleSidebar) toggleSidebar.addEventListener("click", function(){
    if (sidebar && sidebar.classList.contains("open")) closeDrawer(); else openDrawer();
  });

  bind("addBookmark", function(){
    fetch(bookmarkUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({page: pageNum, note: ""})
    }).then(function(r){return r.json();}).then(function(d){
      if (d.ok) setStatus("Bookmark added on page " + pageNum);
    }).catch(function(){});
  });

  bind("fullscreenBtn", function(){
    var el = document.getElementById("readerMain");
    if (el && el.requestFullscreen) el.requestFullscreen();
  });

  bind("nativeToggle", function(){
    if (pdfUrl) window.open(pdfUrl, "_blank", "noopener");
  });

  document.addEventListener("keydown", function(e){
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT")) return;
    if (useNative) return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); goToPage(pageNum - 1); }
    else if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); goToPage(pageNum + 1); }
    else if (e.key === "+" || e.key === "=") applyScale(scale + 0.2, {keepPage: true});
    else if (e.key === "-") applyScale(scale - 0.2, {keepPage: true});
  });

  window.addEventListener("resize", function(){ autoFitIfSmall(); });

  /* ---------------- Load ---------------- */
  if (typeof pdfjsLib === "undefined") {
    fail("PDF.js failed to load.");
    if (pdfUrl) window.open(pdfUrl, "_blank", "noopener");
    return;
  }

  setStatus("Loading document...");
  pdfjsLib.getDocument({url: pdfUrl, disableRange: true, disableStream: true}).promise.then(function(doc){
    pdfDoc = doc;
    numPages = doc.numPages;
    for (var n = 1; n <= numPages; n++) buildShell(n);
    setupObserver();
    updatePageInfo();
    getPage(1).then(function(page){
      var vp = page.getViewport({scale: 1});
      baseDims = {width: vp.width, height: vp.height};
      pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){
        var n = parseInt(el.dataset.page, 10);
        sizeShell(n, {width: baseDims.width * scale, height: baseDims.height * scale});
      });
      updateScaleVar();
      if (window.innerWidth < 1024) fitToWidth();
      requestAnimationFrame(function(){
        goToPage(startPage);
        renderVisible();
        preloadDims();
      });
    });
  }).catch(function(err){
    console.error("[reader] PDF load error:", err);
    if (pdfUrl) window.open(pdfUrl, "_blank", "noopener");
    fail("Failed to load PDF: " + (err && err.message ? err.message : err) + ". Opened the PDF in a new tab.");
  });
})();
