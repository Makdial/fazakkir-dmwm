(function () {
  "use strict";

  function getCookie(name) {
    var m = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return m ? decodeURIComponent(m[2]) : "";
  }
  var CSRF = getCookie("csrftoken");

  function formatCount(n) {
    n = Number(n) || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
    return String(n);
  }

  var feed = document.getElementById("shortsFeed");
  var slides = feed ? Array.prototype.slice.call(feed.querySelectorAll(".short-slide")) : [];
  var viewed = new Set();

  // IntersectionObserver for autoplay + view tracking
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var slide = entry.target;
      var video = slide.querySelector(".short-video");
      if (!video) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
        // pause others
        slides.forEach(function (s) {
          if (s !== slide) {
            var v = s.querySelector(".short-video");
            if (v) { v.pause(); }
          }
        });
        var playPromise = video.play();
        if (playPromise && typeof playPromise.catch === "function") {
          playPromise.catch(function () { /* autoplay blocked; user can tap */ });
        }
        // view tracking once
        var id = slide.getAttribute("data-short-id");
        if (id && !viewed.has(id)) {
          viewed.add(id);
          var viewUrl = slide.getAttribute("data-view-url");
          if (viewUrl) {
            fetch(viewUrl, { method: "POST", headers: { "X-CSRFToken": CSRF } }).catch(function () {});
          }
        }
      } else {
        video.pause();
      }
    });
  }, { threshold: [0.6] });

  slides.forEach(function (s) { io.observe(s); });

  // progress bar update
  slides.forEach(function (slide) {
    var video = slide.querySelector(".short-video");
    var bar = slide.querySelector(".short-progress-bar");
    if (!video || !bar) return;
    video.addEventListener("timeupdate", function () {
      if (video.duration) {
        bar.style.width = (video.currentTime / video.duration * 100) + "%";
      }
    });
  });

  // tap to play/pause
  slides.forEach(function (slide) {
    var video = slide.querySelector(".short-video");
    if (!video) return;
    video.addEventListener("click", function () {
      if (video.paused) video.play(); else video.pause();
    });
  });

  // mute toggles
  document.querySelectorAll(".short-mute-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var slide = btn.closest(".short-slide");
      var video = slide.querySelector(".short-video");
      video.muted = !video.muted;
      btn.textContent = video.muted ? "🔇" : "🔊";
    });
  });

  // like toggles
  document.querySelectorAll(".like-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var slide = btn.closest(".short-slide") || btn.closest(".short-detail-page");
      var url = btn.getAttribute("data-like-url");
      if (!url) return;
      fetch(url, { method: "POST", headers: { "X-CSRFToken": CSRF } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.success) {
            if (d.error) alert(d.error);
            return;
          }
          btn.classList.toggle("liked", d.liked);
          var count = btn.querySelector(".like-count");
          if (count) count.textContent = formatCount(d.likes);
        })
        .catch(function () {});
    });
  });

  // share
  document.querySelectorAll(".share-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var url = btn.getAttribute("data-share-url");
      var title = btn.getAttribute("data-share-title") || "Short";
      if (navigator.share) {
        navigator.share({ title: title, url: url }).catch(function () {});
      } else if (navigator.clipboard) {
        navigator.clipboard.writeText(url).then(function () { alert("Link copied"); });
      }
    });
  });

  // delete
  document.querySelectorAll(".delete-short-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (!confirm("Delete this short?")) return;
      var url = btn.getAttribute("data-delete-url");
      fetch(url, { method: "POST", headers: { "X-CSRFToken": CSRF } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.success) {
            var slide = btn.closest(".short-slide");
            if (slide) { slide.remove(); }
            else { window.location.href = "/shorts/"; }
          } else if (d.error) { alert(d.error); }
        });
    });
  });

  // comments drawer
  var drawer = document.getElementById("shortCommentsDrawer");
  var backdrop = document.getElementById("shortCommentsBackdrop");
  var commentsList = document.getElementById("shortCommentsList");
  var commentsForm = document.getElementById("shortCommentsForm");
  var currentShortId = null;
  var currentListUrl = null;

  function openComments(shortId, addUrl, listUrl) {
    currentShortId = shortId;
    currentListUrl = listUrl;
    if (drawer) {
      drawer.classList.add("open");
      if (backdrop) backdrop.classList.add("open");
      loadComments(listUrl);
    }
    // store add url
    if (commentsForm) commentsForm.setAttribute("data-add-url", addUrl);
  }
  function closeComments() {
    if (drawer) drawer.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
  }
  if (backdrop) backdrop.addEventListener("click", closeComments);
  var closeBtn = document.getElementById("shortCommentsClose");
  if (closeBtn) closeBtn.addEventListener("click", closeComments);

  document.querySelectorAll(".comment-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var shortId = btn.getAttribute("data-short-id");
      var addUrl = btn.getAttribute("data-comment-add-url");
      var listUrl = btn.getAttribute("data-comments-list-url");
      openComments(shortId, addUrl, listUrl);
    });
  });

  function renderComment(c) {
    var div = document.createElement("div");
    div.className = "short-comment";
    div.setAttribute("data-comment-id", c.id);
    var delHtml = c.can_delete
      ? '<button class="del" data-delete-url="' + c.delete_url + '">Delete</button>'
      : '';
    div.innerHTML =
      '<div class="avatar">' + escapeHtml(c.initial) + '</div>' +
      '<div><div class="body">' + escapeHtml(c.body) + '</div>' +
      '<div class="meta">' + escapeHtml(c.user) + ' · ' + escapeHtml(c.created_at) + ' ' + delHtml + '</div></div>';
    var delBtn = div.querySelector(".del");
    if (delBtn) {
      delBtn.addEventListener("click", function () {
        if (!confirm("Delete comment?")) return;
        fetch(delBtn.getAttribute("data-delete-url"), { method: "POST", headers: { "X-CSRFToken": CSRF } })
          .then(function (r) { return r.json(); })
          .then(function (d) { if (d.success) div.remove(); });
      });
    }
    return div;
  }

  function loadComments(listUrl) {
    if (!commentsList || !listUrl) return;
    commentsList.innerHTML = '<p style="opacity:.6;font-size:13px;">Loading...</p>';
    fetch(listUrl)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        commentsList.innerHTML = "";
        if (!d.success || !d.comments || !d.comments.length) {
          commentsList.innerHTML = '<p style="opacity:.6;font-size:13px;">No comments yet.</p>';
          return;
        }
        d.comments.forEach(function (c) { commentsList.appendChild(renderComment(c)); });
      })
      .catch(function () {
        commentsList.innerHTML = '<p style="opacity:.6;font-size:13px;">No comments yet.</p>';
      });
  }

  if (commentsForm) {
    commentsForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var input = commentsForm.querySelector("input[name=body]");
      var body = (input.value || "").trim();
      if (!body) return;
      var addUrl = commentsForm.getAttribute("data-add-url");
      if (!addUrl) return;
      fetch(addUrl, {
        method: "POST",
        headers: { "X-CSRFToken": CSRF, "Content-Type": "application/x-www-form-urlencoded" },
        body: "body=" + encodeURIComponent(body) + "&csrfmiddlewaretoken=" + encodeURIComponent(CSRF),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.success) {
            input.value = "";
            var c = d.comment;
            c.can_delete = true;
            c.delete_url = "/shorts/comments/" + c.id + "/delete/";
            if (commentsList) {
              // remove placeholder
              var placeholder = commentsList.querySelector("p");
              if (placeholder) placeholder.remove();
              commentsList.prepend(renderComment(c));
            }
          } else if (d.error) {
            alert(d.error);
          }
        });
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // keyboard navigation
  document.addEventListener("keydown", function (e) {
    if (!feed) return;
    var idx = 0;
    // find current
    var current = null;
    slides.forEach(function (s, i) {
      var r = s.getBoundingClientRect();
      if (r.top >= -10 && r.top < window.innerHeight / 2) { current = i; }
    });
    if (current === null) current = 0;
    if (e.key === "ArrowDown" || e.key === "PageDown") {
      e.preventDefault();
      var next = Math.min(slides.length - 1, current + 1);
      slides[next].scrollIntoView({ behavior: "smooth" });
    } else if (e.key === "ArrowUp" || e.key === "PageUp") {
      e.preventDefault();
      var prev = Math.max(0, current - 1);
      slides[prev].scrollIntoView({ behavior: "smooth" });
    }
  });

  // initial play first
  if (slides.length) {
    var v = slides[0].querySelector(".short-video");
    if (v) { v.play().catch(function () {}); }
  }
})();
