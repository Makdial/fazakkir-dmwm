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

  // Playback. Sound is on unless the visitor mutes it, and that choice carries
  // from one Short to the next. The sound sign switches every Short to its MP3
  // alone (no picture) until it is tapped again.
  var soundOn = true;
  var listenOnly = false;
  var active = null;  // the media element playing right now
  var inView = null;  // the Short's wrapper that is on screen
  var currentSlide = null;  // the slide currently on screen

  function wrapOf(media) { return media.closest(".short-video-wrap"); }
  function videoIn(wrap) { return wrap.querySelector(".short-video"); }
  function audioIn(wrap) { return wrap.querySelector(".short-audio"); }
  function hasSoundSource(wrap) {
    var audio = audioIn(wrap);
    return !!(audio && audio.getAttribute("src"));
  }

  // What a Short plays: its MP3 in sound-only mode, otherwise its picture.
  function mediaFor(wrap) {
    var audio = audioIn(wrap);
    return listenOnly && audio ? audio : videoIn(wrap);
  }

  function setListenOnly(on) {
    listenOnly = on;
    document.querySelectorAll(".sound-toggle").forEach(function (b) {
      b.setAttribute("aria-pressed", String(on));
    });
  }

  function showMuteState(media) {
    var btn = wrapOf(media).querySelector(".short-mute-btn");
    if (btn) btn.textContent = media.muted ? "🔇" : "🔊";
  }

  function setBusy(el, busy) {
    if (el) el.classList.toggle("busy", busy);
  }

  // Ask the server to make a Short's MP3, or say where it is. A dropped
  // connection, or a gateway page instead of an answer, is tried once more.
  function requestSound(url, retry) {
    return fetch(url, { method: "POST", headers: { "X-CSRFToken": CSRF } })
      .then(function (r) { return r.json(); })
      .catch(function () {
        if (!retry) return { success: false };
        return new Promise(function (resolve) { setTimeout(resolve, 1500); })
          .then(function () { return requestSound(url, false); });
      });
  }

  // Make a Short's MP3 if it has none yet. The server answers with the address
  // to play from and the address to download from.
  function prepareSound(wrap) {
    return requestSound(wrap.getAttribute("data-audio-prepare-url"), true)
      .then(function (d) {
        if (!d.success || !d.url) {
          throw new Error(d.error || "The sound could not be prepared. Please try again.");
        }
        var audio = audioIn(wrap);
        if (audio) audio.setAttribute("src", d.url);
        wrap.setAttribute("data-audio-download", d.download_url);
        return d;
      });
  }

  // Browsers only let a page start a video with sound after the visitor has
  // tapped the page. When that is refused, the media starts muted instead and
  // the speaker button turns the sound on.
  function playMedia(media) {
    active = media;
    media.muted = !soundOn;
    showMuteState(media);
    var playPromise = media.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(function (err) {
        if (err && err.name === "NotAllowedError" && active === media) {
          media.muted = true;
          showMuteState(media);
          media.play().catch(function () {});
        }
      });
    }
  }

  function startMedia(wrap, media) {
    var other = media === audioIn(wrap) ? videoIn(wrap) : audioIn(wrap);
    if (other) other.pause();
    wrap.classList.toggle("listen-only", media !== videoIn(wrap));
    playMedia(media);
  }

  // Start a Short in view. An older Short makes its MP3 first, and only plays
  // it if it is still the one on screen when that is done.
  function playWrap(wrap) {
    inView = wrap;
    var media = mediaFor(wrap);
    if (media !== videoIn(wrap) && !hasSoundSource(wrap)) {
      var toggle = wrap.querySelector(".sound-toggle");
      setBusy(toggle, true);
      prepareSound(wrap).then(function () {
        setBusy(toggle, false);
        if (inView === wrap) playWrap(wrap);
      }, function (err) {
        setBusy(toggle, false);
        alert(err.message);
        setListenOnly(false);
        if (inView === wrap) startMedia(wrap, videoIn(wrap));
      });
      return;
    }
    startMedia(wrap, media);
  }

  function stopWrap(wrap) {
    var video = videoIn(wrap);
    var audio = audioIn(wrap);
    if (video) video.pause();
    if (audio) audio.pause();
    wrap.classList.remove("listen-only");
    if (active && wrapOf(active) === wrap) active = null;
  }

  // When a Short finishes playing, move on to the next one, the way a feed
  // should: snap the next slide into view and let the observer start it.
  function advanceToNext() {
    if (!slides || !slides.length) return;
    var index = currentSlide ? slides.indexOf(currentSlide) : -1;
    if (index < 0) {
      slides.forEach(function (s, i) {
        var r = s.getBoundingClientRect();
        if (r.top >= -10 && r.top < window.innerHeight / 2) index = i;
      });
    }
    if (index < 0) index = 0;
    var next = index + 1;
    if (next >= slides.length) return;
    var nextSlide = slides[next];
    var nextWrap = nextSlide.querySelector(".short-video-wrap");
    if (inView) stopWrap(inView);
    if (feed && typeof feed.scrollTo === "function") {
      feed.scrollTo({ top: nextSlide.offsetTop || (next * (feed.clientHeight || window.innerHeight)), behavior: "smooth" });
    } else {
      nextSlide.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    // Fallback: if the observer does not fire on this device, start it ourselves.
    setTimeout(function () {
      if (currentSlide !== nextSlide && nextWrap) {
        playWrap(nextWrap);
      }
    }, 400);
  }

  // IntersectionObserver for autoplay + view tracking
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var slide = entry.target;
      var wrap = slide.querySelector(".short-video-wrap");
      if (!wrap) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
        currentSlide = slide;
        // pause others
        slides.forEach(function (s) {
          if (s !== slide) stopWrap(s.querySelector(".short-video-wrap"));
        });
        playWrap(wrap);
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
        if (inView === wrap) inView = null;
        stopWrap(wrap);
      }
    });
  }, { threshold: [0.6] });

  slides.forEach(function (s) { io.observe(s); });

  // progress bar update: follows whichever element the Short is playing
  document.querySelectorAll(".short-video-wrap").forEach(function (wrap) {
    var bar = wrap.querySelector(".short-progress-bar");
    if (!bar) return;
    [videoIn(wrap), audioIn(wrap)].forEach(function (media) {
      if (!media) return;
      media.addEventListener("timeupdate", function () {
        if (media === mediaFor(wrap) && media.duration) {
          bar.style.width = (media.currentTime / media.duration * 100) + "%";
        }
      });
    });
  });

  // advance to the next Short when the current one finishes
  document.querySelectorAll(".short-video-wrap").forEach(function (wrap) {
    [videoIn(wrap), audioIn(wrap)].forEach(function (media) {
      if (!media) return;
      media.addEventListener("ended", function () {
        if (media !== active) return;
        advanceToNext();
      });
    });
  });

  // tap the picture (or the sound card) to play or pause
  document.querySelectorAll(".short-video-wrap").forEach(function (wrap) {
    [videoIn(wrap), wrap.querySelector(".short-audio-card")].forEach(function (target) {
      if (!target) return;
      target.addEventListener("click", function () {
        var media = mediaFor(wrap);
        if (media.paused) playWrap(wrap); else media.pause();
      });
    });
  });

  // mute toggles: the detail page has no .short-slide, so find the media through its wrapper
  document.querySelectorAll(".short-mute-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var media = mediaFor(btn.closest(".short-video-wrap"));
      media.muted = !media.muted;
      soundOn = !media.muted;
      showMuteState(media);
    });
  });

  // sound sign: play only the MP3, or the picture again when tapped once more
  document.querySelectorAll(".sound-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setListenOnly(!listenOnly);
      if (inView) playWrap(inView);
    });
  });

  // download the MP3, making it first when this Short has none yet
  document.querySelectorAll(".download-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var wrap = btn.closest(".short-video-wrap");
      var direct = wrap.getAttribute("data-audio-download");
      if (direct) {
        window.location.href = direct;
        return;
      }
      setBusy(btn, true);
      prepareSound(wrap).then(function (d) {
        setBusy(btn, false);
        window.location.href = d.download_url;
      }, function (err) {
        setBusy(btn, false);
        alert(err.message);
      });
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

  // start the first Short: the top of the feed, or the one on a detail page
  var firstWrap = slides.length
    ? slides[0].querySelector(".short-video-wrap")
    : document.querySelector(".short-detail-page .short-video-wrap");
  if (firstWrap) playWrap(firstWrap);
})();
