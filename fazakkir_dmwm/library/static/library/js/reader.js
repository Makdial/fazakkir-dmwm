(function(){
  "use strict";
  if (typeof pdfjsLib === "undefined") {
    var st = document.getElementById("readerStatus");
    if (st) st.textContent = "PDF.js failed to load.";
    enableNativeMode();
    return;
  }

  var pdfUrl = window.LIBRARY_PDF_URL;
  var startPage = window.LIBRARY_START_PAGE || 1;
  var slug = window.LIBRARY_SLUG;
  var translateUrl = window.LIBRARY_TRANSLATE_URL;
  var progressUrl = window.LIBRARY_PROGRESS_URL;
  var bookmarkUrl = window.LIBRARY_BOOKMARK_URL;
  var csrf = window.LIBRARY_CSRF;
  var sourceLang = window.LIBRARY_SOURCE_LANG || "auto";

  var pdfDoc = null;
  var pageNum = Math.max(1, startPage);
  var pageRendering = false;
  var pageNumPending = null;
  var scale = 1.2;
  var canvas = document.getElementById("pdfCanvas");
  var ctx = canvas.getContext("2d");
  var textLayerDiv = document.getElementById("textLayer");
  var statusEl = document.getElementById("readerStatus");
  var pageInfo = document.getElementById("pageInfo");
  var pageInfoMobile = document.getElementById("pageInfoMobile");
  var zoomLevel = document.getElementById("zoomLevel");
  var nativeFrame = document.getElementById("nativeFrame");
  var canvasWrap = document.getElementById("canvasWrap");
  var currentPageText = "";
  var useNative = false;
  var loadedOk = false;

  function setStatus(msg){ if (statusEl) statusEl.textContent = msg; }
  function fail(msg){ console.error("[reader]", msg); setStatus(msg); }

  // ---------- Native (iframe) mode ----------
  function enableNativeMode(){
    useNative = true;
    canvasWrap.classList.add("native");
    canvas.hidden = true;
    textLayerDiv.hidden = true;
    nativeFrame.hidden = false;
    nativeFrame.src = pdfUrl;
    setStatus("");
  }
  function disableNativeMode(){
    useNative = false;
    canvasWrap.classList.remove("native");
    canvas.hidden = false;
    textLayerDiv.hidden = false;
    nativeFrame.hidden = true;
    nativeFrame.src = "about:blank";
  }
  var nativeToggle = document.getElementById("nativeToggle");
  if (nativeToggle) {
    nativeToggle.addEventListener("click", function(){
      if (useNative) { disableNativeMode(); if (pdfDoc) renderPage(pageNum); else loadPdf(); }
      else enableNativeMode();
    });
  }

  // Translation works in both modes: listen for selection in the iframe too.
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
    if (mode === "original") { trResult.hidden = true; trOriginal.hidden = false; }
    else if (mode === "translated") { trOriginal.hidden = true; trResult.hidden = false; }
    else { trOriginal.hidden = false; trResult.hidden = false; }
  }
  if (viewMode) { viewMode.addEventListener("change", applyViewMode); applyViewMode(); }

  var translateEnabled = translateToggle ? translateToggle.checked : false;
  if (translateToggle) {
    translateToggle.addEventListener("change", function(){ translateEnabled = translateToggle.checked; });
  }

  function getSelectedText(win){
    var sel = win.getSelection ? win.getSelection() : null;
    if (!sel || sel.isCollapsed) return "";
    return sel.toString().trim();
  }

  function doTranslate(text){
    if (!text || text.length < 2) return;
    trOriginal.textContent = text;
    trResult.textContent = "...";
    translationBox.hidden = false;
    fetch(translateUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({text: text, source: sourceLang, target: targetLang.value, context: currentPageText, book: slug, page: pageNum})
    }).then(function(r){return r.json();}).then(function(data){
      if (data.translated) trResult.textContent = data.translated;
      else trResult.textContent = data.error || "Translation unavailable";
    }).catch(function(){ trResult.textContent = "Translation unavailable"; });
  }

  function maybeTranslate(win){
    if (!translateEnabled) return;
    var text = getSelectedText(win);
    if (text) doTranslate(text);
  }

  textLayerDiv.addEventListener("mouseup", function(){ maybeTranslate(window); });
  if (nativeFrame) {
    nativeFrame.addEventListener("load", function(){
      try {
        var win = nativeFrame.contentWindow;
        if (win && win.document) {
          win.document.addEventListener("mouseup", function(){ maybeTranslate(win); });
        }
      } catch (e) { /* cross-origin (not expected) */ }
    });
  }

  // ---------- PDF.js rendering ----------
  function renderPage(num){
    if (!pdfDoc) return;
    pageRendering = true;
    setStatus("Loading page " + num + "...");
    pdfDoc.getPage(num).then(function(page){
      var viewport = page.getViewport({scale: scale});
      canvas.height = viewport.height;
      canvas.width = viewport.width;
      textLayerDiv.style.width = viewport.width + "px";
      textLayerDiv.style.height = viewport.height + "px";
      var renderContext = {canvasContext: ctx, viewport: viewport};
      var renderTask = page.render(renderContext);
      renderTask.promise.then(function(){
        loadedOk = true;
        pageRendering = false;
        setStatus("");
        if (pageNumPending !== null){ renderPage(pageNumPending); pageNumPending = null; }
      }).catch(function(err){
        pageRendering = false;
        fail("Render failed: " + (err && err.message ? err.message : err));
        enableNativeMode();
      });
      // text layer + page text cache
      textLayerDiv.innerHTML = "";
      page.getTextContent().then(function(textContent){
        try {
          currentPageText = textContent.items.map(function(i){return i.str;}).join(" ");
        } catch (e) { currentPageText = ""; }
        try {
          if (typeof pdfjsLib.renderTextLayer === "function") {
            pdfjsLib.renderTextLayer({
              textContent: textContent,
              container: textLayerDiv,
              viewport: viewport,
              textDivs: []
            });
          } else {
            textContent.items.forEach(function(item){
              var tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
              var span = document.createElement("span");
              span.textContent = item.str;
              span.style.left = tx[4] + "px";
              span.style.top = (tx[5] - item.height) + "px";
              span.style.fontSize = item.height + "px";
              textLayerDiv.appendChild(span);
            });
          }
        } catch (e) { console.warn("Text layer error", e); }
        pageNum = num;
        updatePageInfo();
        saveProgress();
      }).catch(function(err){ console.warn("Text content error", err); });
    }).catch(function(err){
      pageRendering = false;
      fail("Could not get page: " + (err && err.message ? err.message : err));
    });
  }

  function queueRenderPage(num){
    if (pageRendering) pageNumPending = num;
    else renderPage(num);
  }

  function onPrevPage(){ if (!pdfDoc || pageNum <= 1) return; queueRenderPage(pageNum - 1); }
  function onNextPage(){ if (!pdfDoc || pageNum >= pdfDoc.numPages) return; queueRenderPage(pageNum + 1); }

  function updatePageInfo(){
    var total = pdfDoc ? pdfDoc.numPages : 1;
    var txt = pageNum + " / " + total;
    if (pageInfo) pageInfo.textContent = txt;
    if (pageInfoMobile) pageInfoMobile.textContent = txt;
  }

  function saveProgress(){
    if (!progressUrl) return;
    var percent = pdfDoc ? (pageNum / pdfDoc.numPages * 100) : 0;
    fetch(progressUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({page: pageNum, percent: percent})
    }).catch(function(){});
  }

  function zoom(delta){
    scale = Math.max(0.5, Math.min(3, scale + delta));
    if (zoomLevel) zoomLevel.textContent = Math.round(scale*100) + "%";
    if (!useNative) queueRenderPage(pageNum);
  }

  // ---------- Controls ----------
  var prevBtn = document.getElementById("prevPage");
  var nextBtn = document.getElementById("nextPage");
  if (prevBtn) prevBtn.addEventListener("click", onPrevPage);
  if (nextBtn) nextBtn.addEventListener("click", onNextPage);
  var zoomInBtn = document.getElementById("zoomIn");
  var zoomOutBtn = document.getElementById("zoomOut");
  var fitBtn = document.getElementById("fitWidth");
  if (zoomInBtn) zoomInBtn.addEventListener("click", function(){ zoom(0.2); });
  if (zoomOutBtn) zoomOutBtn.addEventListener("click", function(){ zoom(-0.2); });
  if (fitBtn) fitBtn.addEventListener("click", function(){
    var container = document.getElementById("readerMain");
    if (useNative) return;
    if (pdfDoc) {
      pdfDoc.getPage(pageNum).then(function(page){
        var vp = page.getViewport({scale:1});
        scale = (container.clientWidth - 40) / vp.width;
        if (zoomLevel) zoomLevel.textContent = Math.round(scale*100) + "%";
        queueRenderPage(pageNum);
      });
    }
  });

  var darkToggle = document.getElementById("darkMode");
  var readerEl = document.getElementById("reader");
  if (darkToggle) {
    darkToggle.addEventListener("change", function(){
      if (darkToggle.checked) readerEl.classList.add("dark");
      else readerEl.classList.remove("dark");
    });
  }

  var sidebar = document.getElementById("readerSidebar");
  var toggleSidebar = document.getElementById("toggleSidebar");
  if (toggleSidebar) toggleSidebar.addEventListener("click", function(){ sidebar.classList.toggle("open"); });

  var addBookmarkBtn = document.getElementById("addBookmark");
  if (addBookmarkBtn) addBookmarkBtn.addEventListener("click", function(){
    fetch(bookmarkUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({page: pageNum, note: ""})
    }).then(function(r){return r.json();}).then(function(d){
      if (d.ok) setStatus("Bookmark added on page " + pageNum);
    }).catch(function(){});
  });

  var fsBtn = document.getElementById("fullscreenBtn");
  if (fsBtn) fsBtn.addEventListener("click", function(){
    var el = document.getElementById("readerMain");
    if (el.requestFullscreen) el.requestFullscreen();
  });

  document.addEventListener("keydown", function(e){
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT")) return;
    if (useNative) return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") onPrevPage();
    else if (e.key === "ArrowRight" || e.key === "PageDown") onNextPage();
    else if (e.key === "+" || e.key === "=") zoom(0.2);
    else if (e.key === "-") zoom(-0.2);
  });

  // ---------- Load ----------
  function loadPdf(){
    setStatus("Loading document...");
    var task = pdfjsLib.getDocument({url: pdfUrl, disableRange: true, disableStream: true});
    task.promise.then(function(doc){
      pdfDoc = doc;
      if (startPage < 1 || startPage > doc.numPages) startPage = 1;
      pageNum = startPage;
      updatePageInfo();
      renderPage(pageNum);
    }).catch(function(err){
      console.error("PDF load error:", err);
      fail("Failed to load PDF: " + (err && err.message ? err.message : err) + ". Switching to native viewer.");
      enableNativeMode();
    });
    // Safety net: if nothing rendered after 12s, switch to native viewer.
    setTimeout(function(){
      if (!loadedOk && !useNative) {
        console.warn("[reader] PDF.js did not finish in 12s; switching to native viewer");
        enableNativeMode();
      }
    }, 12000);
  }

  loadPdf();
})();
