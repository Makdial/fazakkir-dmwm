(function(){
  "use strict";
  if (typeof pdfjsLib === "undefined") {
    document.getElementById("readerStatus").textContent = "PDF.js failed to load.";
    return;
  }
  if (!pdfjsLib.GlobalWorkerOptions.workerSrc) {
    pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/library/js/pdf.worker.min.js";
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
  var pageNum = startPage;
  var currentPageText = "";
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

  function setStatus(msg){ if(statusEl) statusEl.textContent = msg; }

  function renderPage(num){
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
        pageRendering = false;
        if (pageNumPending !== null){ renderPage(pageNumPending); pageNumPending = null; }
        setStatus("");
      });
      // text layer
      textLayerDiv.innerHTML = "";
      page.getTextContent().then(function(textContent){
        currentPageText = textContent.items.map(function(i){return i.str;}).join(" ");
        if (typeof pdfjsLib.renderTextLayer === "function") {
          pdfjsLib.renderTextLayer({
            textContent: textContent,
            container: textLayerDiv,
            viewport: viewport,
            textDivs: []
          });
        } else {
          // fallback: simple spans
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
      });
      pageNum = num;
      updatePageInfo();
      saveProgress();
    });
  }

  function queueRenderPage(num){
    if (pageRendering) pageNumPending = num;
    else renderPage(num);
  }

  function onPrevPage(){ if (pageNum <= 1) return; queueRenderPage(pageNum - 1); }
  function onNextPage(){ if (pageNum >= pdfDoc.numPages) return; queueRenderPage(pageNum + 1); }

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
    zoomLevel.textContent = Math.round(scale*100) + "%";
    queueRenderPage(pageNum);
  }

  // translation
  var translateToggle = document.getElementById("translateToggle");
  var targetLang = document.getElementById("targetLang");
  var viewMode = document.getElementById("viewMode");
  function applyViewMode(){
    var mode = viewMode ? viewMode.value : "sidebyside";
    translationBox.classList.remove("mode-original","mode-translated","mode-sidebyside");
    translationBox.classList.add("mode-" + mode);
    if (mode === "original") { trResult.hidden = true; trOriginal.hidden = false; }
    else if (mode === "translated") { trOriginal.hidden = true; trResult.hidden = false; }
    else { trOriginal.hidden = false; trResult.hidden = false; }
  }
  if (viewMode) { viewMode.addEventListener("change", applyViewMode); applyViewMode(); }
  var translationBox = document.getElementById("translationBox");
  var trOriginal = document.getElementById("translationOriginal");
  var trResult = document.getElementById("translationResult");
  var translateEnabled = translateToggle ? translateToggle.checked : false;
  if (translateToggle) {
    translateToggle.addEventListener("change", function(){ translateEnabled = translateToggle.checked; });
  }

  function getSelectedText(){
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed) return "";
    return sel.toString().trim();
  }

  function doTranslate(text){
    if (!text) return;
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

  // Listen for selection changes in text layer
  textLayerDiv.addEventListener("mouseup", function(){
    if (!translateEnabled) return;
    var text = getSelectedText();
    if (text && text.length > 1) doTranslate(text);
  });

  // also context menu? no

  // controls
  document.getElementById("prevPage").addEventListener("click", onPrevPage);
  document.getElementById("nextPage").addEventListener("click", onNextPage);
  document.getElementById("zoomIn").addEventListener("click", function(){ zoom(0.2); });
  document.getElementById("zoomOut").addEventListener("click", function(){ zoom(-0.2); });
  document.getElementById("fitWidth").addEventListener("click", function(){
    // fit to container width
    var container = document.getElementById("readerMain");
    if (pdfDoc) {
      pdfDoc.getPage(pageNum).then(function(page){
        var vp = page.getViewport({scale:1});
        scale = (container.clientWidth - 40) / vp.width;
        zoomLevel.textContent = Math.round(scale*100) + "%";
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
  if (toggleSidebar) {
    toggleSidebar.addEventListener("click", function(){ sidebar.classList.toggle("open"); });
  }

  document.getElementById("addBookmark").addEventListener("click", function(){
    fetch(bookmarkUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({page: pageNum, note: ""})
    }).then(function(r){return r.json();}).then(function(d){
      if (d.ok) setStatus("Bookmark added on page " + pageNum);
    }).catch(function(){});
  });

  var fsBtn = document.getElementById("fullscreenBtn");
  if (fsBtn) {
    fsBtn.addEventListener("click", function(){
      var el = document.getElementById("readerMain");
      if (el.requestFullscreen) el.requestFullscreen();
    });
  }

  // keyboard
  document.addEventListener("keydown", function(e){
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT")) return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") onPrevPage();
    else if (e.key === "ArrowRight" || e.key === "PageDown") onNextPage();
    else if (e.key === "+" || e.key === "=") zoom(0.2);
    else if (e.key === "-") zoom(-0.2);
  });

  // load pdf
  setStatus("Loading document...");
  pdfjsLib.getDocument(pdfUrl).promise.then(function(doc){
    pdfDoc = doc;
    if (startPage < 1 || startPage > doc.numPages) startPage = 1;
    pageNum = startPage;
    updatePageInfo();
    renderPage(pageNum);
  }).catch(function(err){
    setStatus("Failed to load PDF: " + (err && err.message ? err.message : err));
  });
})();
