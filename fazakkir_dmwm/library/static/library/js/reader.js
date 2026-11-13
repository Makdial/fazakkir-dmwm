/* Library PDF reader — continuous, scrollable pages.
 * Renders every page as it approaches the viewport, keeps a few pages
 * around the current one in memory, and swaps to the browser's native
 * PDF viewer if anything fails. Text selection triggers translation.
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
  var pageNum = startPage;          // current (most visible) page
  var pageDims = {};                // num -> {width, height} at scale 1
  var pagePromises = {};            // num -> Promise<pdfPage>
  var rendered = {};                // num -> true when canvas painted
  var visibleRatios = {};           // num -> intersectionRatio
  var useNative = false;
  var loadedOk = false;
  var firstFitDone = false;
  var saveTimer = null;

  var KEEP_RADIUS = 3;              // pages kept rendered around the current one

  function setStatus(msg){ if (statusEl) statusEl.textContent = msg; }
  function fail(msg){ console.error("[reader]", msg); setStatus(msg); }

  /* ---------------- Native viewer fallback ---------------- */
  function enableNativeMode(){
    if (useNative) return;
    useNative = true;
    pagesContainer.hidden = true;
    nativeFrame.hidden = false;
    nativeFrame.src = pdfUrl;
    setStatus("");
  }
  function disableNativeMode(){
    useNative = false;
    pagesContainer.hidden = false;
    nativeFrame.hidden = true;
    nativeFrame.src = "about:blank";
    if (pdfDoc) renderVisible();
  }
  var nativeToggle = document.getElementById("nativeToggle");
  if (nativeToggle) {
    nativeToggle.addEventListener("click", function(){
      if (useNative) disableNativeMode();
      else { enableNativeMode(); }
    });
  }

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

  function doTranslate(text, pageNumForContext){
    if (!text || text.length < 2) return;
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
        context: pageText(pageNumForContext),
        book: slug,
        page: pageNumForContext
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
      var node = sel.anchorNode;
      if (node && node.nodeType === 1) { /* element */ }
      var el = node && (node.nodeType === 1 ? node : node.parentElement);
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

  /* ---------------- Page shells + lazy rendering ---------------- */
  function getPage(num){
    if (!pagePromises[num]) pagePromises[num] = pdfDoc.getPage(num);
    return pagePromises[num];
  }
  function pageText(num){
    var el = document.querySelector('.pdf-page[data-page="' + num + '"]');
    if (!el) return "";
    var tl = el.querySelector(".text-layer");
    return tl ? (tl.textContent || "") : "";
  }

  function buildShell(num){
    var el = document.querySelector('.pdf-page[data-page="' + num + '"]');
    if (el) return el;
    el = document.createElement("div");
    el.className = "pdf-page";
    el.dataset.page = num;

    var canvas = document.createElement("canvas");
    canvas.className = "pdf-page-canvas";
    var tl = document.createElement("div");
    tl.className = "text-layer";
    el.appendChild(canvas);
    el.appendChild(tl);
    pagesContainer.appendChild(el);

    var base = pageDims[1] || pageDims[num];
    if (base) {
      var d = pageDims[num] || base;
      el.style.width = Math.round(d.width * scale) + "px";
      el.style.height = Math.round(d.height * scale) + "px";
    }
    return el;
  }

  function setShellSize(num, el, d){
    el.style.width = Math.round(d.width * scale) + "px";
    el.style.height = Math.round(d.height * scale) + "px";
  }

  function renderPage(num){
    if (!pdfDoc) return;
    var el = buildShell(num);
    var canvas = el.querySelector("canvas");
    if (rendered[num] && parseInt(canvas.dataset.scale || "0", 10) === Math.round(scale * 100)) return;

    getPage(num).then(function(page){
      var vp = page.getViewport({scale: scale});
      pageDims[num] = {width: vp.width / scale, height: vp.height / scale};
      setShellSize(num, el, pageDims[num]);
      canvas.width = Math.round(vp.width);
      canvas.height = Math.round(vp.height);
      canvas.dataset.scale = String(Math.round(scale * 100));
      var ctx = canvas.getContext("2d");
      page.render({canvasContext: ctx, viewport: vp}).promise.then(function(){
        rendered[num] = true;
        loadedOk = true;
        setStatus("");
      }).catch(function(err){
        console.error("[reader] render p" + num, err);
        fail("Render failed on page " + num + ". Switching to native viewer.");
        enableNativeMode();
      });
      var tl = el.querySelector(".text-layer");
      tl.innerHTML = "";
      page.getTextContent().then(function(textContent){
        try {
          if (typeof pdfjsLib.renderTextLayer === "function") {
            pdfjsLib.renderTextLayer({textContent: textContent, container: tl, viewport: vp, textDivs: []});
          } else {
            textContent.items.forEach(function(item){
              var tx = pdfjsLib.Util.transform(vp.transform, item.transform);
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
      fail("Could not load page " + num + ". Switching to native viewer.");
      enableNativeMode();
    });
  }

  function clearPage(num){
    var el = document.querySelector('.pdf-page[data-page="' + num + '"]');
    if (!el) return;
    var canvas = el.querySelector("canvas");
    canvas.width = 0; canvas.height = 0;
    canvas.dataset.scale = "0";
    el.querySelector(".text-layer").innerHTML = "";
    delete rendered[num];
  }

  function pruneAround(){
    var lo = Math.max(1, pageNum - KEEP_RADIUS - 2);
    var hi = Math.min(numPages, pageNum + KEEP_RADIUS + 2);
    Object.keys(rendered).forEach(function(k){
      var n = parseInt(k, 10);
      if (n < lo || n > hi) clearPage(n);
    });
  }

  function renderVisible(){
    var lo = Math.max(1, pageNum - KEEP_RADIUS);
    var hi = Math.min(numPages, pageNum + KEEP_RADIUS);
    for (var n = lo; n <= hi; n++) renderPage(n);
    pruneAround();
  }

  var observer = null;
  function setupObserver(){
    if (observer) observer.disconnect();
    observer = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        var num = parseInt(en.target.dataset.page, 10);
        visibleRatios[num] = en.isIntersecting ? en.intersectionRatio : 0;
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
        renderVisible();
      } else {
        renderVisible();
      }
    }, {root: readerMain, rootMargin: "700px 0px", threshold: [0, 0.05, 0.25, 0.5, 0.75, 1]});
    pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){ observer.observe(el); });
  }

  function goToPage(num){
    num = Math.min(Math.max(1, num), numPages || 1);
    var el = document.querySelector('.pdf-page[data-page="' + num + '"]');
    if (el) el.scrollIntoView({behavior: "smooth", block: "start"});
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
  function applyScale(next, opts){
    if (useNative || !pdfDoc) return;
    scale = Math.max(0.4, Math.min(3, next));
    if (zoomLevel) zoomLevel.textContent = Math.round(scale * 100) + "%";
    // resize shells using cached dims (fall back to first page dims)
    var base = pageDims[1] || {width: 612, height: 792};
    pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){
      var n = parseInt(el.dataset.page, 10);
      var d = pageDims[n] || base;
      setShellSize(n, el, d);
      clearPage(n);
    });
    Object.keys(pagePromises).forEach(function(k){ /* keep cache */ });
    if (opts && opts.keepPage) {
      // restore scroll to the current page top
      requestAnimationFrame(function(){ goToPage(pageNum); });
    } else {
      renderVisible();
    }
  }

  function fitToWidth(){
    if (useNative || !pdfDoc) return;
    getPage(1).then(function(page){
      var vp = page.getViewport({scale: 1});
      var target = Math.min((readerMain ? readerMain.clientWidth : window.innerWidth) - 24, 1400);
      applyScale(target / vp.width, {keepPage: true});
    });
  }
  function autoFitIfSmall(){
    if (useNative || !pdfDoc) return;
    var w = readerMain ? readerMain.clientWidth : window.innerWidth;
    if (w < 820) fitToWidth();
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

  document.addEventListener("keydown", function(e){
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT")) return;
    if (useNative) return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); goToPage(pageNum - 1); }
    else if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); goToPage(pageNum + 1); }
    else if (e.key === "+" || e.key === "=") applyScale(scale + 0.2, {keepPage: true});
    else if (e.key === "-") applyScale(scale - 0.2, {keepPage: true});
  });

  window.addEventListener("resize", function(){ autoFitIfSmall(); });

  /* ---------------- Load document ---------------- */
  if (typeof pdfjsLib === "undefined") {
    fail("PDF.js failed to load.");
    enableNativeMode();
    return;
  }

  setStatus("Loading document...");
  pdfjsLib.getDocument({url: pdfUrl, disableRange: true, disableStream: true}).promise.then(function(doc){
    pdfDoc = doc;
    numPages = doc.numPages;
    // Build a light shell for every page so scrolling feels continuous.
    for (var n = 1; n <= numPages; n++) buildShell(n);
    setupObserver();
    updatePageInfo();
    // Jump to the saved page once the first page has real dimensions.
    getPage(1).then(function(page){
      var vp = page.getViewport({scale: 1});
      pageDims[1] = {width: vp.width, height: vp.height};
      // Give every shell a sensible placeholder size so scroll positions work.
      pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){
        var n = parseInt(el.dataset.page, 10);
        var d = pageDims[n] || pageDims[1];
        el.style.width = Math.round(d.width * scale) + "px";
        el.style.height = Math.round(d.height * scale) + "px";
      });
      if (window.innerWidth < 1024 && !firstFitDone) {
        firstFitDone = true;
        fitToWidth();
      }
      requestAnimationFrame(function(){
        goToPage(startPage);
        renderVisible();
      });
    });
  }).catch(function(err){
    console.error("[reader] PDF load error:", err);
    fail("Failed to load PDF: " + (err && err.message ? err.message : err) + ". Switching to native viewer.");
    enableNativeMode();
  });
})();
