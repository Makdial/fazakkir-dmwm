/*
 * The video and MP3 player, shared by every page.
 *
 * The <video> and <audio> elements sit in videos/_player.html, outside the page
 * content, so they keep playing while nav.js moves the visitor between pages.
 * On a watch page the player is docked into that page's [data-player-slot]; on
 * any other page it floats in the corner as a mini player. Every video from the
 * queue (queue.js) is loaded into these same elements, so the picture never has
 * to be rebuilt between two videos.
 *
 * Data for a video is the payload described in videos/views.py
 * (_playback_payload): id, title, channel, url, playback_url, thumbnail_url,
 * is_audio_only, video_url, audio_url and subtitles.
 */
(function () {
    "use strict"

    const videoPlayer = document.getElementById("video-player")
    const audioPlayer = document.getElementById("audio-player")
    const audioCover = document.getElementById("audioCover")
    const videoError = document.getElementById("video-error")
    const host = document.getElementById("fzPlayerHost")
    const playerContainer = document.getElementById("playerContainer")
    const playerStage = document.getElementById("playerStage")
    const playerBar = document.getElementById("playerBar")
    const captionLayer = document.getElementById("captionLayer")
    const captionText = document.getElementById("captionText")
    const tapZone = document.getElementById("tapZone")
    const bigPlay = document.getElementById("bigPlay")
    const seekBar = document.getElementById("seekBar")
    const playPauseBtn = document.getElementById("playPauseBtn")
    const backBtn = document.getElementById("backBtn")
    const fwdBtn = document.getElementById("fwdBtn")
    const nextBtn = document.getElementById("nextBtn")
    const currentTimeEl = document.getElementById("currentTime")
    const durationEl = document.getElementById("duration")
    const muteBtn = document.getElementById("muteBtn")
    const volumeBar = document.getElementById("volumeBar")
    const rateSelect = document.getElementById("rateSelect")
    const pipBtn = document.getElementById("pipBtn")
    const fullscreenBtn = document.getElementById("fullscreenBtn")
    const ccList = document.getElementById("ccList")
    const miniTitle = document.getElementById("miniTitle")
    const miniChannel = document.getElementById("miniChannel")
    const miniPlay = document.getElementById("miniPlay")
    const miniNext = document.getElementById("miniNext")
    const miniQueue = document.getElementById("miniQueue")
    const miniExpand = document.getElementById("miniExpand")
    const miniClose = document.getElementById("miniClose")
    const labels = (document.getElementById("fzLabels") || { dataset: {} }).dataset
    const mediaSession = navigator.mediaSession || null

    const NOW_KEY = "fazakkir-now"
    const SUBTITLE_KEY = "fazakkir-subtitle"
    const CONTROLS_IDLE_MS = 3200
    const SAVE_EVERY_MS = 3000

    // The video in charge of the player, or null before anything has loaded.
    let item = null
    // The id whose media is attached to the elements. Differs from item.id only
    // while a restored or queued video is still being fetched.
    let loadedId = null
    let docked = false
    let mode = "video"
    let activePlayer = videoPlayer
    let subtitlePref = ""
    let currentSubtitle = ""
    let pendingAutoplay = false
    let resumeAt = 0
    let pendingSeek = 0
    let scrubbing = false
    let hideTimer = null
    let lastSave = 0
    const cueCache = new Map()

    try {
        subtitlePref = localStorage.getItem(SUBTITLE_KEY) || ""
    } catch (e) { /* storage unavailable */ }

    /* --- Small helpers --------------------------------------------------- */

    function isFull(data) {
        return Boolean(data) && "video_url" in data
    }

    function isPlaying() {
        return Boolean(activePlayer) && !activePlayer.paused && !activePlayer.ended
    }

    function summaryOf(data) {
        return {
            id: data.id,
            title: data.title,
            channel: data.channel,
            url: data.url,
            playback_url: data.playback_url,
            thumbnail_url: data.thumbnail_url,
            is_audio_only: data.is_audio_only,
        }
    }

    function formatTime(seconds) {
        if (!isFinite(seconds) || seconds < 0) return "0:00"
        const whole = Math.floor(seconds)
        const hours = Math.floor(whole / 3600)
        const minutes = Math.floor((whole % 3600) / 60)
        const secs = String(whole % 60).padStart(2, "0")
        return hours
            ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}`
            : `${minutes}:${secs}`
    }

    function setSource(element, url) {
        if (url) {
            if (element.getAttribute("src") !== url) element.src = url
        } else if (element.hasAttribute("src")) {
            element.removeAttribute("src")
            element.load()
        }
    }

    function clearTracks(element) {
        element.querySelectorAll("track").forEach(track => track.remove())
    }

    // Text tracks are only handed to the browser so iOS can show them in its
    // own fullscreen player. The id prefix tells the video and audio copies
    // apart (see applySubtitle).
    function buildTracks(element, subtitles, prefix) {
        clearTracks(element)
        subtitles.forEach(sub => {
            const track = document.createElement("track")
            track.id = prefix + sub.lang
            track.kind = "subtitles"
            track.srclang = sub.is_lyrics ? "und" : sub.lang
            track.label = sub.label
            track.src = sub.url
            element.appendChild(track)
        })
    }

    function notifyNow() {
        document.dispatchEvent(new CustomEvent("fz:now"))
    }

    function parseJson(text) {
        try {
            return JSON.parse(text)
        } catch (e) {
            return null
        }
    }

    /* --- Remembering what was playing ------------------------------------- */

    function currentTime() {
        if (loadedId !== null && activePlayer) return activePlayer.currentTime || 0
        return resumeAt
    }

    function saveNow() {
        if (!item) return
        lastSave = Date.now()
        try {
            localStorage.setItem(NOW_KEY, JSON.stringify({
                item: summaryOf(item),
                time: currentTime(),
            }))
        } catch (e) { /* storage unavailable */ }
    }

    function clearNow() {
        try {
            localStorage.removeItem(NOW_KEY)
        } catch (e) { /* storage unavailable */ }
    }

    // After a reload the last video is shown paused in the mini player, so the
    // visitor can carry on where they stopped.
    function restoreNow() {
        if (item) return
        const saved = parseJson(localStorage.getItem(NOW_KEY) || "null")
        if (saved && saved.item && typeof saved.item.id === "number") {
            item = saved.item
            resumeAt = Number(saved.time) || 0
        }
    }

    /* --- Loading a video -------------------------------------------------- */

    function fetchPayload(target) {
        return fetch(target.playback_url, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        }).then(response => {
            if (!response.ok) throw new Error("HTTP " + response.status)
            return response.json()
        })
    }

    // Plays a queue entry or a restored summary: fetch its media links first.
    function playEntry(entry, options) {
        options = options || {}
        return fetchPayload(entry)
            .then(data => {
                setCurrent(data, { autoplay: options.autoplay, resumeAt: options.resumeAt })
            })
            .catch(error => {
                console.warn("Could not load the video", error)
                if (item && item.id === entry.id && !isFull(item)) {
                    item = null
                    loadedId = null
                    clearNow()
                    refreshAll()
                    updateHost()
                }
                toast(labels.loadFailed)
                if (options.autoplay) playNextFromQueue()
            })
    }

    function toast(message) {
        if (window.FZ && window.FZ.queue && message) window.FZ.queue.toast(message)
    }

    function setCurrent(data, options) {
        options = options || {}
        if (item && item.id === data.id && loadedId === data.id) {
            item = data
            saveNow()
            refreshAll()
        } else {
            attach(data, options.resumeAt || 0)
        }
        if (options.autoplay) playNow()
    }

    function attach(data, resume) {
        item = data
        loadedId = data.id
        resumeAt = 0
        pendingSeek = resume || 0

        videoPlayer.pause()
        audioPlayer.pause()
        if (data.is_audio_only) mode = "audio"
        else if (mode === "audio" && !data.audio_url) mode = "video"

        setSource(videoPlayer, data.is_audio_only ? "" : data.video_url)
        setSource(audioPlayer, mode === "audio" ? data.audio_url : "")
        buildTracks(videoPlayer, data.subtitles, "track-")
        buildTracks(audioPlayer, data.subtitles, "atrack-")
        renderCcList(data.subtitles)
        if (videoError) videoError.style.display = "none"

        currentSubtitle = data.subtitles.some(sub => sub.lang === subtitlePref) ? subtitlePref : ""
        showActive()
        applySubtitle(currentSubtitle)
        if (audioCover) {
            audioCover.hidden = !data.thumbnail_url
            audioCover.src = data.thumbnail_url || ""
        }
        saveNow()
        notifyNow()
        refreshAll()
        updateHost()
    }

    // Makes the element for the chosen mode visible and makes it the one the
    // controls drive. Nothing is paused or started here.
    function showActive() {
        const audioMode = mode === "audio"
        activePlayer = audioMode ? audioPlayer : videoPlayer
        if (playerStage) playerStage.classList.toggle("audio-mode", audioMode)
        if (videoPlayer) videoPlayer.style.display = audioMode ? "none" : ""
        if (audioPlayer) audioPlayer.style.display = audioMode ? "block" : "none"
        updateModeButtons()
        refreshMeta()
    }

    function updateModeButtons() {
        const videoBtn = document.getElementById("mode-video")
        const audioBtn = document.getElementById("mode-audio")
        if (videoBtn) videoBtn.classList.toggle("active", mode === "video")
        if (audioBtn) audioBtn.classList.toggle("active", mode === "audio")
    }

    function playNow() {
        if (!activePlayer) return
        const attempt = activePlayer.play()
        if (attempt && typeof attempt.catch === "function") {
            attempt.catch(err => {
                // Usually the browser's autoplay policy. Reveal the bar so the
                // play button is reachable rather than failing silently.
                showControls()
                console.warn("playback was refused", err)
            })
        }
    }

    /* --- Docking: the watch page slot and the floating mini player ------ */

    function dock(slot) {
        if (host && host.parentNode !== slot) slot.appendChild(host)
        docked = true
        updateHost()
    }

    function undock() {
        if (!docked) return
        docked = false
        if (host) document.body.appendChild(host)
        updateHost()
    }

    function updateHost() {
        if (!host) return
        const show = docked || Boolean(item)
        host.hidden = !show
        host.classList.toggle("is-docked", docked)
        host.classList.toggle("is-mini", show && !docked)
        refreshMini()
    }

    function onPage() {
        // On the first page load this brings back the last video (paused, at
        // its saved position). Later page changes already have an item.
        restoreNow()
        const slot = document.querySelector("[data-player-slot]")
        const source = document.getElementById("fz-video-data")
        if (slot && source) {
            dock(slot)
            const data = parseJson(source.textContent)
            if (data) {
                // Continue straight into the next video when something was
                // already playing, or when a queued one asked for it.
                const autoplay = pendingAutoplay || isPlaying()
                // A saved position only applies to the video it was saved for.
                const resume = item && item.id === data.id ? resumeAt : 0
                pendingAutoplay = false
                setCurrent(data, { autoplay: autoplay, resumeAt: resume })
            }
        } else {
            undock()
        }
        updateHost()
        updateNextButtons()
    }

    /* --- Queue playback --------------------------------------------------- */

    function queueCount() {
        return window.FZ && window.FZ.queue ? window.FZ.queue.count() : 0
    }

    function playItem(entry) {
        // On a watch page the next video gets its own page, the way YouTube
        // does it; elsewhere it simply plays in the mini player.
        if (docked && !(item && item.id === entry.id)) {
            pendingAutoplay = true
            window.FZ.nav.navigate(entry.url)
            return
        }
        if (loadedId === entry.id) {
            playNow()
            return
        }
        playEntry(entry, { autoplay: true })
    }

    function playFromQueue(id) {
        const entry = window.FZ.queue.take(id)
        if (entry) playItem(entry)
    }

    // The "big screen" choice: open the video on its own page instead of
    // starting it in the mini player. The page always starts playing, whether
    // or not something was already playing.
    function watchOnPage(entry) {
        if (!entry) return
        if (docked && item && item.id === entry.id) {
            playNow()
            return
        }
        if (entry.url && window.FZ && window.FZ.nav) {
            pendingAutoplay = true
            window.FZ.nav.navigate(entry.url)
            return
        }
        playItem(entry)
    }

    function playNextFromQueue() {
        const entry = window.FZ.queue.take()
        if (entry) playItem(entry)
    }

    function onEnded() {
        refreshAll()
        saveNow()
        if (queueCount()) playNextFromQueue()
    }

    function close() {
        ;[videoPlayer, audioPlayer].forEach(element => {
            element.pause()
            clearTracks(element)
            setSource(element, "")
        })
        item = null
        loadedId = null
        resumeAt = 0
        clearNow()
        notifyNow()
        updateHost()
        refreshAll()
    }

    /* --- Mode: video or MP3 --------------------------------------------- */

    // Switching modes keeps the position and, if something was playing, the
    // playback too.
    function setMode(next) {
        if (!item || loadedId !== item.id) return
        const wantAudio = next === "audio"
        if (wantAudio && !item.audio_url) return
        if (!wantAudio && item.is_audio_only) return
        if (wantAudio === (mode === "audio")) return

        const from = activePlayer
        const wasPlaying = isPlaying()
        const time = from ? from.currentTime : 0
        from.pause()

        mode = wantAudio ? "audio" : "video"
        setSource(audioPlayer, wantAudio ? item.audio_url : "")
        showActive()

        if (time > 0) {
            try {
                activePlayer.currentTime = time
            } catch (e) { /* not seekable yet; the position is kept as the start */ }
        }
        if (wasPlaying) playNow()
        applySubtitle(currentSubtitle)
        refreshAll()
    }

    /* --- Media Session: the lock screen and notification controls -------- */

    function mediaMetadata() {
        const metadata = {
            title: item ? item.title : "",
            artist: item ? item.channel : "",
            album: "Fazakkir",
        }
        if (item && item.thumbnail_url) {
            metadata.artwork = [{ src: item.thumbnail_url, sizes: "512x512", type: "image/jpeg" }]
        }
        return typeof MediaMetadata === "function" ? new MediaMetadata(metadata) : metadata
    }

    function refreshMeta() {
        if (!mediaSession) return
        try { mediaSession.metadata = mediaMetadata() } catch (e) {}
        syncPlaybackState()
    }

    function syncPlaybackState() {
        if (!mediaSession) return
        try {
            mediaSession.playbackState = isPlaying() ? "playing" : "paused"
        } catch (e) {}
    }

    function bindMediaSession() {
        if (!mediaSession) return

        const seekBy = (delta) => {
            if (!activePlayer) return
            const length = activePlayer.duration
            if (!isFinite(length)) return
            activePlayer.currentTime = Math.max(0, Math.min(length, activePlayer.currentTime + delta))
        }

        const handlers = {
            play: () => activePlayer && activePlayer.play().catch(() => {}),
            pause: () => activePlayer && activePlayer.pause(),
            seekbackward: () => seekBy(-10),
            seekforward: () => seekBy(10),
            nexttrack: () => playNextFromQueue(),
            stop: () => {
                if (!activePlayer) return
                activePlayer.pause()
                try { activePlayer.currentTime = 0 } catch (e) {}
            },
        }
        Object.keys(handlers).forEach(action => {
            // Not every browser supports every action; unsupported ones throw.
            try { mediaSession.setActionHandler(action, handlers[action]) } catch (e) {}
        })
    }

    /* --- Control bar ---------------------------------------------------- */

    function refreshPlayPauseIcon() {
        if (!playPauseBtn || !activePlayer) return
        const playing = isPlaying()
        playPauseBtn.textContent = playing ? "❚❚" : "▶"
        playPauseBtn.setAttribute(
            "aria-label", playing ? labels.pause : labels.play
        )
    }

    function refreshSeekBar() {
        if (!seekBar || !activePlayer || scrubbing) return
        const duration = activePlayer.duration
        if (isFinite(duration) && duration > 0) {
            seekBar.value = String((activePlayer.currentTime / duration) * 100)
        }
    }

    function refreshVolume() {
        if (!activePlayer || !volumeBar || !muteBtn) return
        volumeBar.value = String(activePlayer.volume)
        muteBtn.textContent = activePlayer.muted || activePlayer.volume === 0 ? "🔇" : "🔊"
    }

    function refreshBigPlay() {
        if (!bigPlay) return
        const paused = !activePlayer || activePlayer.paused || activePlayer.ended
        bigPlay.hidden = !paused
    }

    function refreshMini() {
        if (miniTitle) miniTitle.textContent = item ? item.title : ""
        if (miniChannel) miniChannel.textContent = item ? item.channel : ""
        if (miniPlay) {
            const playing = isPlaying()
            miniPlay.textContent = playing ? "❚❚" : "▶"
            miniPlay.setAttribute("aria-label", playing ? labels.pause : labels.play)
        }
    }

    function updateNextButtons() {
        const available = queueCount() > 0
        if (nextBtn) nextBtn.hidden = !available
        if (miniNext) miniNext.hidden = !available
    }

    function refreshAll() {
        refreshPlayPauseIcon()
        refreshSeekBar()
        refreshVolume()
        refreshBigPlay()
        refreshMini()
        updateNextButtons()
        if (activePlayer && isFinite(activePlayer.duration) && durationEl) {
            durationEl.textContent = formatTime(activePlayer.duration)
        }
    }

    function togglePlayPause() {
        if (!item) return
        if (loadedId !== item.id) {
            playEntry(item, { autoplay: true, resumeAt: resumeAt })
            return
        }
        if (activePlayer.paused) {
            playNow()
        } else {
            activePlayer.pause()
        }
    }

    function seekBy(delta) {
        if (!activePlayer) return
        const length = activePlayer.duration
        if (!isFinite(length)) return
        activePlayer.currentTime = Math.max(0, Math.min(length, activePlayer.currentTime + delta))
    }

    function bindControls() {
        if (!playPauseBtn) return

        playPauseBtn.addEventListener("click", togglePlayPause)
        backBtn.addEventListener("click", () => seekBy(-10))
        fwdBtn.addEventListener("click", () => seekBy(10))
        if (nextBtn) nextBtn.addEventListener("click", () => playNextFromQueue())

        // While dragging, the bar follows the pointer rather than the playback.
        seekBar.addEventListener("pointerdown", () => { scrubbing = true })
        seekBar.addEventListener("input", () => {
            if (!activePlayer) return
            const duration = activePlayer.duration
            if (!isFinite(duration)) return
            activePlayer.currentTime = (Number(seekBar.value) / 100) * duration
            currentTimeEl.textContent = formatTime(activePlayer.currentTime)
        })
        const stopScrubbing = () => { scrubbing = false; refreshSeekBar() }
        seekBar.addEventListener("pointerup", stopScrubbing)
        seekBar.addEventListener("change", stopScrubbing)
        seekBar.addEventListener("pointercancel", stopScrubbing)

        volumeBar.addEventListener("input", () => {
            if (!activePlayer) return
            activePlayer.volume = Number(volumeBar.value)
            activePlayer.muted = activePlayer.volume === 0
            refreshVolume()
        })
        muteBtn.addEventListener("click", () => {
            if (!activePlayer) return
            activePlayer.muted = !activePlayer.muted
            refreshVolume()
        })

        rateSelect.addEventListener("change", () => {
            if (!activePlayer) return
            activePlayer.playbackRate = Number(rateSelect.value)
        })

        // Fullscreen. iPhone Safari ships none of the standard API: no
        // Element.requestFullscreen, no webkitRequestFullscreen prefix, and no
        // document.exitFullscreen. The only element iOS will expand is the
        // <video> itself, through its own webkitEnterFullscreen().
        function fullscreenElement() {
            return document.fullscreenElement
                || document.webkitFullscreenElement
                || document.msFullscreenElement
                || null
        }

        function refreshFullscreenButton() {
            if (!fullscreenBtn) return
            const expanded = Boolean(
                fullscreenElement()
                || (videoPlayer && videoPlayer.webkitDisplayingFullscreen)
            )
            fullscreenBtn.textContent = expanded ? "⤡" : "⛶"
            fullscreenBtn.setAttribute(
                "aria-label",
                expanded ? labels.exitFullscreen : labels.fullscreen
            )
        }

        function requestStageFullscreen() {
            // The control bar (and its CC menu) is a sibling of the picture, so
            // expand the whole player box; the fullscreen CSS keeps the bar over
            // the bottom of the picture.
            const target = playerContainer || playerStage
            if (!target) return false
            const request = target.requestFullscreen
                || target.webkitRequestFullscreen
                || target.msRequestFullscreen
            if (!request) return false
            try {
                const result = request.call(target)
                if (result && typeof result.catch === "function") result.catch(() => {})
                return true
            } catch (error) {
                return false
            }
        }

        // The iOS-only path. webkitEnterFullScreen (capital S) is the spelling on
        // very old WebKit builds, so both are accepted.
        function requestVideoFullscreen() {
            if (!videoPlayer) return false
            const enter = videoPlayer.webkitEnterFullscreen
                || videoPlayer.webkitEnterFullScreen
            if (!enter) return false
            try {
                enter.call(videoPlayer)
                return true
            } catch (error) {
                return false
            }
        }

        function toggleFullscreen() {
            if (fullscreenElement()) {
                const exit = document.exitFullscreen
                    || document.webkitExitFullscreen
                    || document.msExitFullscreen
                if (exit) {
                    try {
                        const result = exit.call(document)
                        if (result && typeof result.catch === "function") result.catch(() => {})
                    } catch (error) { /* refused; the controls are already hidden */ }
                }
                return
            }
            if (requestStageFullscreen()) return
            requestVideoFullscreen()
        }

        fullscreenBtn.addEventListener("click", toggleFullscreen)

        // iOS reports its native video fullscreen through private events and a
        // flag rather than the standard ones, so the icon is kept in step with
        // whichever route the browser took.
        const iosFullscreenEvents = ["webkitbeginfullscreen", "webkitendfullscreen"]
        iosFullscreenEvents.forEach(name => {
            if (videoPlayer) videoPlayer.addEventListener(name, refreshFullscreenButton)
        })
        document.addEventListener("webkitfullscreenchange", refreshFullscreenButton)
        document.addEventListener("fullscreenchange", () => {
            refreshFullscreenButton()
            if (fullscreenElement()) showControls()
        })
        refreshFullscreenButton()

        // Picture-in-picture is video only, and not available everywhere.
        if (window.documentPictureInPictureEnabled && !audioPlayer) {
            pipBtn.hidden = false
            pipBtn.addEventListener("click", async () => {
                if (document.pictureInPictureElement) {
                    await document.exitPictureInPicture().catch(() => {})
                    return
                }
                try { await videoPlayer.requestPictureInPicture() } catch (e) {}
            })
        }

        // Space and the usual transport keys, so the page still behaves like a
        // player without reaching for the mouse. They only apply while the
        // player is docked on the watch page, so the home page can still scroll.
        document.addEventListener("keydown", event => {
            if (!docked || !item) return
            const tag = (event.target.tagName || "").toLowerCase()
            if (["input", "select", "textarea", "button"].includes(tag)) return
            if (event.key === " " || event.key === "k") {
                event.preventDefault()
                togglePlayPause()
            } else if (event.key === "ArrowRight") {
                seekBy(10)
            } else if (event.key === "ArrowLeft") {
                seekBy(-10)
            } else if (event.key === "f") {
                fullscreenBtn.click()
            } else if (event.key === "N" && queueCount()) {
                playNextFromQueue()
            }
            showControls()
        })
    }

    function bindMini() {
        if (miniPlay) miniPlay.addEventListener("click", togglePlayPause)
        if (miniNext) miniNext.addEventListener("click", () => playNextFromQueue())
        if (miniQueue) {
            miniQueue.addEventListener("click", () => {
                const panel = document.getElementById("fzQueue")
                if (window.FZ && window.FZ.queue) window.FZ.queue.setDrawer(Boolean(panel && panel.hidden))
            })
        }
        if (miniExpand) {
            miniExpand.addEventListener("click", () => {
                if (item && window.FZ.nav) window.FZ.nav.navigate(item.url)
            })
        }
        if (miniClose) miniClose.addEventListener("click", close)
    }

    /* --- Media events --------------------------------------------------- */

    function bindMediaEvents() {
        if (videoPlayer) {
            videoPlayer.addEventListener("error", () => {
                videoPlayer.style.display = "none"
                if (videoError) videoError.style.display = "flex"
            })
        }

        ;[videoPlayer, audioPlayer].forEach(element => {
            if (!element) return
            ;["play", "pause", "ended", "emptied"].forEach(name =>
                element.addEventListener(name, syncPlaybackState))
            ;["play", "pause", "ended", "loadedmetadata", "durationchange",
              "volumechange", "ratechange"].forEach(name =>
                element.addEventListener(name, refreshAll))
            element.addEventListener("timeupdate", () => {
                refreshSeekBar()
                if (activePlayer && currentTimeEl) {
                    currentTimeEl.textContent = formatTime(activePlayer.currentTime)
                }
                if (Date.now() - lastSave > SAVE_EVERY_MS) saveNow()
                captionTick()
            })
            ;["seeked", "ratechange", "loadedmetadata", "play"].forEach(name =>
                element.addEventListener(name, captionTick))
            element.addEventListener("loadedmetadata", () => {
                if (pendingSeek > 0 && element === activePlayer) {
                    const length = element.duration
                    if (isFinite(length) && pendingSeek < length) element.currentTime = pendingSeek
                    pendingSeek = 0
                }
            })
            element.addEventListener("pause", saveNow)
            element.addEventListener("ended", onEnded)
            element.addEventListener("play", () => {
                refreshBigPlay()
                if (shouldAutoHide()) hideControls()
                refreshPlayPauseIcon()
            })
            element.addEventListener("pause", () => {
                refreshBigPlay()
                showControls()
                refreshPlayPauseIcon()
            })
            element.addEventListener("ended", () => {
                refreshBigPlay()
                showControls()
            })
        })

        window.addEventListener("pagehide", saveNow)
    }

    /* --- Auto-hiding controls ------------------------------------------- */

    // The bar covers the bottom of the picture, so leaving it up while the video
    // plays hides the action. It appears on a tap or click and fades away again
    // once playback has run on for a few seconds without input.
    // Only a device with a real pointer gets hover behaviour. A tablet reports
    // hover: none even with a mouse attached, and without this guard the bar
    // would latch open the moment a tap synthesised a mouseenter.
    const canHover = window.matchMedia
        ? window.matchMedia("(hover: hover) and (pointer: fine)").matches
        : false

    let pointerInsideStage = false
    // The bar floats over the bottom of the picture and is a sibling of the
    // stage, so tracking it separately keeps it from hiding under a pointer.
    let pointerInsideBar = false

    function pointerInsidePlayer() {
        return pointerInsideStage || pointerInsideBar
    }

    // The bar stays up while paused or while the pointer is over it, and fades
    // away only when the video is playing and nobody is aiming at anything.
    function shouldAutoHide() {
        return isPlaying() && !pointerInsidePlayer()
    }

    function showControls() {
        if (!playerBar || !playerStage) return
        playerBar.classList.add("is-visible")
        playerStage.classList.add("controls-visible")
        clearTimeout(hideTimer)
        if (shouldAutoHide()) {
            hideTimer = setTimeout(hideControls, CONTROLS_IDLE_MS)
        }
    }

    function hideControls() {
        if (!playerBar || !playerStage) return
        // Never yank the bar away from a pointer that is still resting on it.
        if (pointerInsidePlayer() && canHover) return
        playerBar.classList.remove("is-visible")
        playerStage.classList.remove("controls-visible")
    }

    function wireAutoHide() {
        if (bigPlay) {
            bigPlay.addEventListener("click", event => {
                event.stopPropagation()
                togglePlayPause()
                showControls()
            })
        }

        if (tapZone) {
            // Tapping the picture plays or pauses it, the way every other player
            // behaves.
            tapZone.addEventListener("click", event => {
                event.preventDefault()
                togglePlayPause()
                showControls()
            })
        }

        if (playerStage) {
            if (canHover) {
                // Entering the video is enough; no wiggling required.
                playerStage.addEventListener("mouseenter", () => {
                    pointerInsideStage = true
                    showControls()
                })
                playerStage.addEventListener("mouseleave", () => {
                    pointerInsideStage = false
                    if (shouldAutoHide()) hideControls()
                })
                playerStage.addEventListener("mousemove", () => showControls())
            }
            playerStage.addEventListener(
                "touchstart", () => showControls(), { passive: true }
            )
        }

        // Working a control counts as activity and restarts the idle countdown.
        if (playerBar) {
            ;["pointerdown", "input", "change", "focusin"].forEach(name =>
                playerBar.addEventListener(name, () => showControls()))

            if (canHover) {
                playerBar.addEventListener("mouseenter", () => {
                    pointerInsideBar = true
                    showControls()
                })
                playerBar.addEventListener("mouseleave", () => {
                    pointerInsideBar = false
                    if (shouldAutoHide()) hideControls()
                })
            }
        }
    }

    /* --- Subtitles and captions ----------------------------------------- */

    // Captions are drawn into `.caption-layer` rather than left to the browser,
    // so they look the same everywhere and stay clear of the control bar.

    /** Parse WebVTT into [{start, end, text}], tolerating CRLF and SRT-ish input. */
    function parseVtt(raw) {
        const cues = []
        const blocks = String(raw).replace(/\r\n?/g, "\n").split(/\n{2,}/)
        for (const block of blocks) {
            const lines = block.split("\n").filter(line => line.trim() !== "")
            if (!lines.length) continue
            // Skip an optional numeric identifier line.
            let index = 0
            if (/^\d+$/.test(lines[0].trim())) index = 1
            const timing = lines[index]
            if (!timing || timing.indexOf("-->") === -1) continue
            const [startRaw, endRaw] = timing.split("-->")
            const start = vttTime(startRaw)
            const end = vttTime((endRaw || "").trim().split(/\s+/)[0])
            if (start === null || end === null) continue
            const text = lines.slice(index + 1).join("\n")
                .replace(/<[^>]+>/g, "")   // strip inline markup and karaoke tags
                .trim()
            if (text) cues.push({ start, end, text })
        }
        return cues
    }

    /** "00:00:01.000" (or "00:01.000") to seconds, or null when unreadable. */
    function vttTime(value) {
        const parts = String(value).trim().split(":")
        if (parts.length < 2) return null
        const seconds = parseFloat(parts.pop().replace(",", "."))
        const minutes = parseInt(parts.pop(), 10)
        if (Number.isNaN(seconds) || Number.isNaN(minutes)) return null
        let hours = 0
        if (parts.length) hours = parseInt(parts.pop(), 10) || 0
        return hours * 3600 + minutes * 60 + seconds
    }

    /** Fetch and cache the cues for one track. */
    async function cuesFor(url) {
        if (cueCache.has(url)) return cueCache.get(url)
        cueCache.set(url, [])   // set first, so a slow load is not fetched twice
        try {
            const response = await fetch(url, { credentials: "same-origin" })
            if (!response.ok) return []
            const cues = parseVtt(await response.text())
            cueCache.set(url, cues)
            return cues
        } catch (e) {
            return []
        }
    }

    function subtitleUrl(lang) {
        const track = item && item.subtitles.find(sub => sub.lang === lang)
        return track ? track.url : ""
    }

    function hideCaption() {
        if (captionLayer) captionLayer.classList.remove("is-visible")
        if (captionText) captionText.textContent = ""
    }

    function showCaption(text) {
        if (!captionText || !captionLayer) return
        if (captionText.textContent === text) return
        captionText.textContent = text
        captionLayer.classList.add("is-visible")
    }

    // A single routine paints the cue covering the playhead. Cheap enough to run
    // on every frame, and it keeps the caption in step when the viewer scrubs or
    // changes speed.
    function paintCaption() {
        if (!currentSubtitle || !activePlayer) return
        // The native iOS player draws them itself while it owns the screen.
        if (nativeVideoFullscreen()) return
        const cues = cueCache.get(subtitleUrl(currentSubtitle))
        if (!cues || !cues.length) return
        const now = activePlayer.currentTime
        for (const cue of cues) {
            if (now >= cue.start && now <= cue.end) {
                showCaption(cue.text)
                return
            }
        }
        hideCaption()
    }

    function captionTick() {
        paintCaption()
    }

    // On an iPhone the only fullscreen available expands the <video> element
    // itself, and the native player covers the whole page. For the duration of
    // that fullscreen the chosen track is handed to the browser instead.
    function nativeVideoFullscreen() {
        return Boolean(videoPlayer && videoPlayer.webkitDisplayingFullscreen)
    }

    function applySubtitle(lang) {
        const mode = nativeVideoFullscreen() ? "showing" : "hidden"
        ;[videoPlayer, audioPlayer].forEach(element => {
            if (!element || !element.textTracks) return
            for (const track of element.textTracks) {
                // "hidden" stops the browser painting its own captions while
                // still giving us the cues; "showing" is only used while the
                // native iOS player is the thing on screen.
                const code = (track.id || "").replace(/^a?track-/, "")
                track.mode = (lang && code === lang) ? mode : "disabled"
            }
        })
        // The native player paints the cues; keep our overlay out of the way.
        if (mode === "showing") hideCaption()
        // The language buttons live both below the player and in the CC menus;
        // keep every copy in step with the choice.
        document.querySelectorAll(".cc-item").forEach(btn => {
            btn.classList.toggle("active", (btn.dataset.lang || "") === lang)
        })
        const label = lang ? ccLabel(lang) : labels.off
        // The button in the control bar reads as pressed and shows the language.
        document.querySelectorAll(".cc-btn").forEach(btn => {
            btn.classList.toggle("active", Boolean(lang))
            btn.title = lang
                ? labels.subtitles + ": " + label
                : labels.subtitles
        })
        // The always-visible button below the player spells the choice out.
        const current = document.getElementById("subtitleCcCurrent")
        if (current) current.textContent = label

        if (!lang) {
            hideCaption()
            return
        }
        // Load the cues, then repaint as soon as they arrive.
        const url = subtitleUrl(lang)
        if (url) cuesFor(url).then(() => paintCaption())
    }

    function ccLabel(lang) {
        const option = document.querySelector(`.cc-item[data-lang="${lang}"]`)
        return option ? option.textContent.trim() : lang
    }

    function setSubtitle(lang) {
        subtitlePref = lang
        try { localStorage.setItem(SUBTITLE_KEY, lang) } catch (e) {}
        currentSubtitle = lang && item && item.subtitles.some(sub => sub.lang === lang) ? lang : ""
        applySubtitle(currentSubtitle)
    }

    function ccOption(lang, text) {
        const option = document.createElement("button")
        option.type = "button"
        option.className = "cc-item"
        option.setAttribute("role", "menuitem")
        option.dataset.lang = lang
        option.textContent = text
        option.addEventListener("click", () => {
            setSubtitle(lang)
            closeCcMenus()
        })
        return option
    }

    // The control bar's language list belongs to the video that is loaded, so
    // it is rebuilt each time one is.
    function renderCcList(subtitles) {
        if (!ccList) return
        ccList.textContent = ""
        ccList.appendChild(ccOption("", labels.off))
        if (!subtitles.length) {
            const note = document.createElement("p")
            note.className = "cc-empty"
            note.textContent = labels.noCaptions
            ccList.appendChild(note)
            return
        }
        subtitles.forEach(sub => {
            ccList.appendChild(ccOption(sub.lang, (sub.is_lyrics ? "♪ " : "") + sub.label))
        })
    }

    // Every CC menu (the one below the player, and the one in the bar) opens its
    // own language list; a click outside or on a language closes them all.
    function closeCcMenus() {
        document.querySelectorAll(".cc-menu.open").forEach(menu => {
            menu.classList.remove("open")
            const btn = menu.querySelector(".cc-btn")
            if (btn) btn.setAttribute("aria-expanded", "false")
        })
    }

    function bindCcMenus() {
        document.addEventListener("click", event => {
            const target = event.target
            if (!(target instanceof Element)) return
            const button = target.closest(".cc-menu > .cc-btn")
            if (button) {
                const menu = button.parentElement
                const wasOpen = menu.classList.contains("open")
                closeCcMenus()
                if (!wasOpen) {
                    menu.classList.add("open")
                    button.setAttribute("aria-expanded", "true")
                }
                return
            }
            // Picking a language closes the menu; clicks elsewhere in an open
            // menu keep it open.
            if (target.closest(".cc-menu .cc-item")) {
                closeCcMenus()
                return
            }
            if (target.closest(".cc-menu")) return
            closeCcMenus()
        })
    }

    /* --- Public API ----------------------------------------------------- */

    function bindTopLevelExports() {
        window.setMode = setMode
        window.setSubtitle = setSubtitle
        window.closeCcMenus = closeCcMenus
        window.FZ = window.FZ || {}
        window.FZ.player = {
            current: () => item,
            isPlaying: isPlaying,
            playItem: playItem,
            playFromQueue: playFromQueue,
            watchOnPage: watchOnPage,
            playNextFromQueue: playNextFromQueue,
            close: close,
            toggle: togglePlayPause,
        }
    }

    /* --- Start-up -------------------------------------------------------- */

    bindTopLevelExports()
    bindMediaEvents()
    bindControls()
    bindMini()
    bindCcMenus()
    bindMediaSession()
    wireAutoHide()

    document.addEventListener("fz:page", onPage)
    document.addEventListener("fz:leave", undock)
    document.addEventListener("fz:queue", updateNextButtons)
    document.addEventListener("fz:queue", refreshMini)

    refreshAll()
    applySubtitle(currentSubtitle)
    showControls()
    onPage()
})()
