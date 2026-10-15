// Makes pages feel alive: live updates, sounds, typing, countdown rings,
// celebrations and the greeting. Every page still works without this file.
(function () {
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var csrfMeta = document.querySelector('meta[name="csrf"]');
  function csrf() { return csrfMeta ? csrfMeta.content : ""; }
  function store(key, value) {
    try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; }
  }
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }

  // ---- Sound: a soft two-note chime for new tickets (can be switched off) ----
  var audio = null;
  var soundOn = store("sound") !== "off";
  function showSound() {
    document.querySelectorAll(".sound-toggle").forEach(function (b) {
      b.setAttribute("aria-pressed", soundOn ? "true" : "false");
      b.querySelector(".label").textContent = soundOn ? "Sound on" : "Sound off";
    });
  }
  document.addEventListener("click", function (e) {
    // Browsers only allow sound after the person has clicked something.
    if (!audio && (window.AudioContext || window.webkitAudioContext)) {
      try { audio = new (window.AudioContext || window.webkitAudioContext)(); } catch (err) {}
    }
    var b = e.target.closest(".sound-toggle");
    if (b) { soundOn = !soundOn; store("sound", soundOn ? "on" : "off"); showSound(); if (soundOn) chime(); }
  });
  function chime() {
    if (!soundOn || !audio) return;
    try {
      if (audio.state === "suspended") audio.resume();
      [[659.25, 0], [880, 0.13]].forEach(function (n) {
        var osc = audio.createOscillator(), gain = audio.createGain(), t = audio.currentTime + n[1];
        osc.type = "sine"; osc.frequency.value = n[0];
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.45);
        osc.connect(gain).connect(audio.destination);
        osc.start(t); osc.stop(t + 0.5);
      });
    } catch (err) {}
  }
  showSound();

  // ---- Toasts ----
  var toasts = document.querySelector(".toasts");
  function toast(ev) {
    if (!toasts) return;
    var el = document.createElement(ev.url ? "a" : "div");
    el.className = "toast kind-" + ev.kind;
    if (ev.url) el.href = ev.url;
    el.innerHTML = '<span class="avatar sm" style="--h:' + ev.hue + '">' + esc(ev.initials) + "</span><span>" + esc(ev.text) + "</span>";
    toasts.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("in"); });
    setTimeout(function () { el.classList.remove("in"); setTimeout(function () { el.remove(); }, 400); }, 7000);
  }

  // ---- Live updates for technicians: new activity, menu badge, feed ----
  var body = document.body;
  var liveUrl = body.getAttribute("data-live");
  var lastActivity = parseInt(body.getAttribute("data-last-activity") || "0", 10);
  // These pop up and chime; the rest just appear in the feed.
  var LOUD = { needs_tech: 1, reply: 1, rated: 1, payment: 1 };
  function setOpen(n) {
    document.querySelectorAll("[data-live-open]").forEach(function (b) { b.textContent = n; b.hidden = !n; });
    document.querySelectorAll("[data-live-open-count]").forEach(function (b) { b.textContent = n; });
    document.title = document.title.replace(/^\(\d+\) /, "");
  }
  function addToFeed(ev) {
    var feed = document.querySelector("[data-feed]");
    if (!feed) return;
    var empty = feed.querySelector("[data-feed-empty]");
    if (empty) empty.remove();
    var li = document.createElement("li");
    li.className = "feed-item kind-" + ev.kind + " fresh";
    var text = ev.url ? '<a href="' + esc(ev.url) + '">' + esc(ev.text) + "</a>" : esc(ev.text);
    li.innerHTML = '<span class="avatar sm" style="--h:' + ev.hue + '">' + esc(ev.initials) + "</span><div><div>" +
      text + '</div><span class="small muted">' + esc(ev.when) + "</span></div>";
    feed.insertBefore(li, feed.firstChild);
    while (feed.children.length > 15) feed.lastChild.remove();
  }
  function pollLive() {
    if (!liveUrl || document.hidden) return;
    fetch(liveUrl + "?after=" + lastActivity, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        setOpen(data.open);
        var loud = false;
        (data.events || []).forEach(function (ev) {
          addToFeed(ev);
          if (LOUD[ev.kind]) { toast(ev); loud = true; }
        });
        if (loud) chime();
        lastActivity = Math.max(lastActivity, data.last || 0);
      }).catch(function () {});
  }
  if (liveUrl) {
    setInterval(pollLive, 15000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) pollLive(); });
  }

  // ---- A conversation that updates itself, with "is typing…" ----
  var thread = document.getElementById("thread");
  var typingBox = document.querySelector("[data-typing]");
  function refreshThread() {
    fetch(location.pathname, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html) return;
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("thread");
        if (!fresh) return;
        thread.innerHTML = fresh.innerHTML;
        thread.setAttribute("data-key", fresh.getAttribute("data-key"));
        var msgs = thread.querySelectorAll(".msg");
        if (msgs.length) msgs[msgs.length - 1].classList.add("fresh");
        updateRings();
      }).catch(function () {});
  }
  function pollThread() {
    if (!thread || document.hidden) return;
    fetch(thread.getAttribute("data-state"), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        if (!s) return;
        if (typingBox) {
          typingBox.hidden = !s.typing;
          var who = typingBox.querySelector("[data-typing-name]");
          if (who) who.textContent = s.typing || "";
        }
        if (s.key !== thread.getAttribute("data-key")) { refreshThread(); if (!liveUrl) chime(); }
      }).catch(function () {});
  }
  if (thread) {
    setInterval(pollThread, 5000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) pollThread(); });
  }
  // Tell the other side we're typing (at most every 4 seconds).
  var lastTyping = 0;
  document.addEventListener("input", function (e) {
    var box = e.target.closest("textarea[data-typing-url]");
    if (!box || Date.now() - lastTyping < 4000 || !box.value.trim()) return;
    lastTyping = Date.now();
    var form = new FormData();
    form.append("csrf", csrf());
    fetch(box.getAttribute("data-typing-url"), { method: "POST", body: form, credentials: "same-origin" }).catch(function () {});
  });

  // ---- Countdown rings: drain as the reply deadline approaches ----
  function updateRings() {
    var now = Date.now();
    document.querySelectorAll(".ring[data-due]").forEach(function (r) {
      var start = Date.parse(r.getAttribute("data-start")), due = Date.parse(r.getAttribute("data-due"));
      if (isNaN(start) || isNaN(due) || due <= start) return;
      var left = Math.max(0, Math.min(1, (due - now) / (due - start)));
      r.style.setProperty("--left", (left * 100).toFixed(1));
      r.classList.toggle("soon", left > 0 && left < 0.25);
      r.classList.toggle("over", left <= 0);
      r.title = left <= 0 ? "Reply is late" : Math.round(left * 100) + "% of the time left";
    });
  }
  updateRings();
  setInterval(updateRings, 30000);

  // ---- Confetti ----
  function confetti(amount) {
    if (reduceMotion || store("celebrate") === "off") return;
    var colors = ["#4f46e5", "#3b6fd8", "#0d9488", "#5eead4", "#f59e0b", "#ec4899"];
    var layer = document.createElement("div");
    layer.className = "confetti";
    for (var i = 0; i < (amount || 90); i++) {
      var p = document.createElement("i");
      p.style.left = Math.random() * 100 + "vw";
      p.style.background = colors[i % colors.length];
      p.style.setProperty("--x", (Math.random() * 40 - 20) + "vw");
      p.style.setProperty("--r", (Math.random() * 900 - 450) + "deg");
      p.style.animationDelay = Math.random() * 0.35 + "s";
      p.style.animationDuration = 1.6 + Math.random() * 1.2 + "s";
      if (i % 3 === 0) p.style.borderRadius = "50%";
      layer.appendChild(p);
    }
    document.body.appendChild(layer);
    setTimeout(function () { layer.remove(); }, 3500);
  }
  if (document.querySelector("[data-celebrate]")) confetti(110);
  if (document.querySelector("[data-sent]")) confetti(60);
  // Inbox zero: celebrate once a day, not on every visit.
  if (document.querySelector("[data-inbox-zero]")) {
    var today = new Date().toDateString();
    if (store("inboxZero") !== today) { store("inboxZero", today); confetti(70); }
  }

  // ---- Greeting by the time of day where you are ----
  var greet = document.querySelector("[data-greeting]");
  if (greet) {
    var h = new Date().getHours();
    var part = h < 5 ? ["Working late", "🌙", "night"] : h < 12 ? ["Good morning", "☀️", "morning"]
      : h < 17 ? ["Good afternoon", "🌤", "afternoon"] : h < 21 ? ["Good evening", "🌇", "evening"] : ["Good evening", "🌙", "night"];
    greet.textContent = part[0];
    var icon = document.querySelector("[data-greeting-icon]");
    if (icon) icon.textContent = part[1];
    document.documentElement.setAttribute("data-daytime", part[2]);
  }

  // ---- Settings page: theme, sound and celebrations, saved on this device ----
  var themePick = document.querySelector('[data-pref="theme"]');
  if (themePick) {
    var showTheme = function () {
      var chosen = store("theme") || "system";
      themePick.querySelectorAll("button").forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-value") === chosen ? "true" : "false");
      });
    };
    themePick.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-value]");
      if (!b) return;
      var value = b.getAttribute("data-value");
      if (value === "system") {
        try { localStorage.removeItem("theme"); } catch (err) {}
        document.documentElement.removeAttribute("data-theme");
      } else {
        store("theme", value);
        document.documentElement.setAttribute("data-theme", value);
      }
      // Let the menu's light/dark button catch up.
      var media = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
      var mode = value === "system" ? (media && media.matches ? "dark" : "light") : value;
      document.documentElement.setAttribute("data-mode", mode);
      document.querySelectorAll(".theme-toggle:not(.install-btn):not(.sound-toggle):not(.pin-btn) .label").forEach(function (l) {
        l.textContent = mode === "dark" ? "Light mode" : "Dark mode";
      });
      showTheme();
    });
    showTheme();
  }
  var soundBox = document.querySelector('input[data-pref="sound"]');
  if (soundBox) {
    soundBox.checked = soundOn;
    soundBox.addEventListener("change", function () {
      soundOn = soundBox.checked; store("sound", soundOn ? "on" : "off"); showSound();
    });
  }
  var partyBox = document.querySelector('input[data-pref="celebrate"]');
  if (partyBox) {
    partyBox.checked = store("celebrate") !== "off";
    partyBox.addEventListener("change", function () {
      store("celebrate", partyBox.checked ? "on" : "off");
      if (partyBox.checked) confetti(60);
    });
  }
  var testChime = document.querySelector("[data-test-chime]");
  if (testChime) testChime.addEventListener("click", function () {
    var was = soundOn; soundOn = true; setTimeout(function () { chime(); soundOn = was; }, 30);
  });

  // ---- Help page: quick-pick topics, and remembering who you are ----
  var problem = document.getElementById("problem");
  document.querySelectorAll(".topic").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".topic.on").forEach(function (x) { x.classList.remove("on"); });
      b.classList.add("on");
      if (problem) {
        var start = b.getAttribute("data-start");
        if (!problem.value.trim() || document.querySelector(".topic[data-used]")) problem.value = start;
        document.querySelectorAll(".topic[data-used]").forEach(function (x) { x.removeAttribute("data-used"); });
        b.setAttribute("data-used", "");
        problem.focus();
        problem.setSelectionRange(problem.value.length, problem.value.length);
      }
    });
  });
  if (problem) {
    problem.addEventListener("input", function () {
      document.querySelectorAll(".topic[data-used]").forEach(function (x) { if (problem.value.length > x.getAttribute("data-start").length + 3) x.removeAttribute("data-used"); });
    });
    ["name", "email"].forEach(function (field) {
      var input = document.getElementById(field);
      if (!input) return;
      if (!input.value && store("portal-" + field)) input.value = store("portal-" + field);
      input.addEventListener("change", function () { store("portal-" + field, input.value); });
    });
  }
})();
