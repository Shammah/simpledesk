// Welcome tour: Desky, a small animated helper, walks a new technician
// through the app. Shows once on the first visit to the dashboard; "Take the
// tour" in the menu replays it.
(function () {
  var body = document.body;
  var replay = new URLSearchParams(location.search).get("tour") === "1";
  if (body.dataset.tour !== "first-time" && !replay) return;
  if (replay) history.replaceState(null, "", location.pathname);

  var STEPS = [
    { title: "Hi, I'm Desky!", wave: true,
      text: "I'll show you around. It takes about a minute, and you can skip any time." },
    { target: '.nav-menu a[href="/companies"]', nav: true, title: "1. Add your clients",
      text: "Add each company you look after. Every company gets its own help link to share with their staff." },
    { target: '.nav-menu a[href="/tickets"]', nav: true, title: "2. Requests arrive here",
      text: "When staff ask for help, by help link, email or WhatsApp, it lands here as a ticket." },
    { target: '.nav-menu a[href="/guides"]', nav: true, title: "3. Answer once, reuse forever",
      text: "Write a fix as a guide. Next time someone has that problem, they get the answer straight away." },
    { target: '.nav-menu a[href="/checklists"]', nav: true, title: "4. Never miss a step",
      text: "Keep lists for new starters and leavers, then tick them off for each person." },
    { target: '.nav-menu a[href="/channels"]', nav: true, title: "5. Email and WhatsApp",
      text: "Switch on email and WhatsApp here so staff can reach you the way they like." },
    { target: ".stat.hero", title: "6. Show your value",
      text: "This shows how many problems were solved without you lifting a finger. Great to show clients." },
    { target: ".theme-toggle:not(.install-btn)", nav: true, title: "7. Make it yours",
      text: "Switch between light and dark whenever you like." },
    { title: "You're all set!", wave: true, last: true,
      text: "Start by adding your first client. You can replay this tour from the menu any time." },
  ].filter(function (s) { return !s.target || document.querySelector(s.target); });

  var BUDDY =
    '<svg viewBox="0 0 64 64" aria-hidden="true">' +
    '<defs><linearGradient id="desky" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="#4f46e5"/><stop offset=".5" stop-color="#3b6fd8"/><stop offset="1" stop-color="#0d9488"/>' +
    "</linearGradient></defs>" +
    '<g class="buddy-body">' +
    '<line x1="32" y1="11" x2="32" y2="5" stroke="#5eead4" stroke-width="2.5" stroke-linecap="round"/>' +
    '<circle class="buddy-bulb" cx="32" cy="4.5" r="3.2" fill="#5eead4"/>' +
    '<rect x="8" y="11" width="48" height="43" rx="14" fill="url(#desky)"/>' +
    '<rect x="8" y="11" width="48" height="20" rx="14" fill="#fff" opacity=".08"/>' +
    '<g class="buddy-eyes"><ellipse cx="24" cy="30" rx="4.2" ry="5.2" fill="#fff"/><ellipse cx="40" cy="30" rx="4.2" ry="5.2" fill="#fff"/>' +
    '<g class="buddy-pupils"><circle cx="24.5" cy="31" r="2.1" fill="#1b1f4b"/><circle cx="40.5" cy="31" r="2.1" fill="#1b1f4b"/></g></g>' +
    '<circle cx="16.5" cy="40" r="2.8" fill="#f9a8d4" opacity=".55"/><circle cx="47.5" cy="40" r="2.8" fill="#f9a8d4" opacity=".55"/>' +
    '<path d="M26 41.5 Q32 46.5 38 41.5" stroke="#fff" stroke-width="2.6" fill="none" stroke-linecap="round"/>' +
    "</g>" +
    '<g class="buddy-hand"><circle cx="58" cy="35" r="4.6" fill="#3b6fd8" stroke="#fff" stroke-width="1.6"/></g>' +
    "</svg>";

  var root = document.createElement("div");
  root.className = "tour";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-labelledby", "tour-title");
  root.innerHTML =
    '<div class="tour-spot"></div>' +
    '<div class="tour-card">' +
    '<div class="tour-head"><div class="tour-buddy">' + BUDDY + "</div>" +
    '<div><h2 id="tour-title"></h2><p class="tour-text"></p></div></div>' +
    '<div class="tour-foot"><div class="tour-dots"></div><div class="tour-actions">' +
    '<button type="button" class="link tour-skip">Skip tour</button>' +
    '<button type="button" class="btn ghost sm tour-back">Back</button>' +
    '<button type="button" class="btn sm tour-next">Next</button>' +
    "</div></div></div>";
  body.appendChild(root);

  var spot = root.querySelector(".tour-spot");
  var card = root.querySelector(".tour-card");
  var buddy = root.querySelector(".tour-buddy");
  var nextBtn = root.querySelector(".tour-next");
  var backBtn = root.querySelector(".tour-back");
  var skipBtn = root.querySelector(".tour-skip");
  var dots = root.querySelector(".tour-dots");
  var nav = document.querySelector(".nav");
  var menuBtn = document.querySelector(".menu-btn");
  var index = 0;
  var saved = false;

  STEPS.forEach(function () { dots.appendChild(document.createElement("span")); });

  function narrow() { return menuBtn && getComputedStyle(menuBtn).display !== "none"; }
  function menuOpen() { return nav && nav.classList.contains("open"); }

  function replayAnimation(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth;          // restart the CSS animation
    el.classList.add(cls);
  }

  function show(i) {
    index = i;
    var step = STEPS[i];
    var wait = 0;
    if (narrow() && menuOpen()) { menuBtn.click(); wait = 300; }
    root.querySelector("#tour-title").textContent = step.title;
    // On phones the menu is folded into the three dots, so point there instead.
    root.querySelector(".tour-text").textContent =
      step.text + (narrow() && step.nav ? " Find it in the menu (the three dots)." : "");
    backBtn.hidden = i === 0;
    nextBtn.textContent = step.last ? "Add my first client" : i === 0 ? "Show me" : "Next";
    skipBtn.textContent = step.last ? "Close" : "Skip tour";
    Array.prototype.forEach.call(dots.children, function (d, n) {
      d.className = n < i ? "done" : n === i ? "on" : "";
    });
    buddy.classList.toggle("wave", !!step.wave);
    replayAnimation(buddy, "hop");
    replayAnimation(card, "in");
    setTimeout(place, wait);
    // Measure again once any sliding menu or scrolling has settled.
    setTimeout(place, wait + 520);
    nextBtn.focus({ preventScroll: true });
  }

  function place() {
    var step = STEPS[index];
    var target = step.target && document.querySelector(narrow() && step.nav ? ".menu-btn" : step.target);
    var vw = window.innerWidth, vh = window.innerHeight;
    var cw = Math.min(360, vw - 24), ch = card.offsetHeight;
    card.style.width = cw + "px";
    var side = "center", left, top;

    if (target) {
      target.scrollIntoView({ block: "nearest", inline: "nearest" });
      var r = target.getBoundingClientRect();
      var pad = 8;
      spot.classList.remove("none");
      spot.style.left = r.left - pad + "px";
      spot.style.top = r.top - pad + "px";
      spot.style.width = r.width + pad * 2 + "px";
      spot.style.height = r.height + pad * 2 + "px";
      var gap = 22;
      if (r.right + gap + cw <= vw - 12) {
        side = "right"; left = r.right + gap; top = r.top + r.height / 2 - ch / 2;
      } else if (r.bottom + gap + ch <= vh - 12) {
        side = "below"; left = r.left + r.width / 2 - cw / 2; top = r.bottom + gap;
      } else if (r.top - gap - ch >= 12) {
        side = "above"; left = r.left + r.width / 2 - cw / 2; top = r.top - gap - ch;
      }
      // Desky looks toward the thing being explained.
      buddy.style.setProperty("--look-x", side === "right" ? "-1.6px" : "0px");
      buddy.style.setProperty("--look-y", side === "below" ? "-1.6px" : side === "above" ? "1.6px" : "0px");
    } else {
      spot.classList.add("none");
      spot.style.left = vw / 2 + "px"; spot.style.top = vh / 2 + "px";
      spot.style.width = "0px"; spot.style.height = "0px";
      buddy.style.setProperty("--look-x", "0px");
      buddy.style.setProperty("--look-y", "0px");
    }
    if (side === "center") { left = vw / 2 - cw / 2; top = vh / 2 - ch / 2; }
    card.style.left = Math.max(12, Math.min(left, vw - cw - 12)) + "px";
    card.style.top = Math.max(12, Math.min(top, vh - ch - 12)) + "px";
    card.dataset.side = side;
  }

  function markDone() {
    if (saved) return;
    saved = true;
    var token = document.querySelector('meta[name="csrf"]');
    fetch("/tour/done", { method: "POST", body: new URLSearchParams({ csrf: token ? token.content : "" }),
                          credentials: "same-origin" }).catch(function () {});
  }

  function sparkle() {
    var r = buddy.getBoundingClientRect();
    var colours = ["#818cf8", "#3b82f6", "#2dd4bf", "#5eead4", "#f9a8d4"];
    for (var n = 0; n < 26; n++) {
      var p = document.createElement("span");
      p.className = "tour-spark";
      var angle = Math.random() * Math.PI * 2, dist = 60 + Math.random() * 90;
      p.style.left = r.left + r.width / 2 + "px";
      p.style.top = r.top + r.height / 2 + "px";
      p.style.background = colours[n % colours.length];
      p.style.setProperty("--dx", Math.cos(angle) * dist + "px");
      p.style.setProperty("--dy", Math.sin(angle) * dist - 30 + "px");
      p.style.animationDelay = Math.random() * 0.12 + "s";
      document.body.appendChild(p);
      setTimeout(function (el) { el.remove(); }, 1300, p);
    }
  }

  function close(then) {
    markDone();
    root.classList.add("out");
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", place);
    setTimeout(function () { root.remove(); if (then) then(); }, 380);
  }

  function next() {
    if (STEPS[index].last) {
      sparkle();
      setTimeout(function () { close(function () { location.href = "/companies"; }); }, 650);
    } else {
      show(index + 1);
    }
  }

  function onKey(e) {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowRight") next();
    else if (e.key === "ArrowLeft" && index > 0) show(index - 1);
    else if (e.key === "Tab") {          // keep keyboard focus inside the tour
      var items = [skipBtn, backBtn, nextBtn].filter(function (b) { return !b.hidden; });
      var at = items.indexOf(document.activeElement);
      e.preventDefault();
      items[(at + (e.shiftKey ? items.length - 1 : 1)) % items.length].focus();
    }
  }

  nextBtn.addEventListener("click", next);
  backBtn.addEventListener("click", function () { show(Math.max(0, index - 1)); });
  skipBtn.addEventListener("click", function () {
    if (STEPS[index].last) sparkle();
    close();
  });
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", place);

  setTimeout(function () { root.classList.add("open"); show(0); }, 450);
})();
