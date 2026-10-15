// Theme switch, phone menu, button ripples and the page-loading animation.
// The pages work fine without this file.
(function () {
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function current() {
    var chosen = root.getAttribute("data-theme");
    if (chosen) return chosen;
    return media && media.matches ? "dark" : "light";
  }

  function show() {
    var mode = current();
    root.setAttribute("data-mode", mode);
    document.querySelectorAll(".theme-toggle:not(.sound-toggle):not(.pin-btn) .label").forEach(function (el) {
      el.textContent = mode === "dark" ? "Light mode" : "Dark mode";
    });
  }

  // Three-dot menu on narrow screens
  var nav = document.querySelector(".nav");
  var menuBtn = document.querySelector(".menu-btn");
  function setMenu(open) {
    if (!nav || !menuBtn) return;
    nav.classList.toggle("open", open);
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    menuBtn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && nav && nav.classList.contains("open")) { setMenu(false); menuBtn.focus(); }
  });

  // Knowledge and Settings groups open and close in place.
  document.querySelectorAll(".nav-group-btn").forEach(function (b) {
    b.addEventListener("click", function () {
      var group = b.closest(".nav-group");
      var open = !group.classList.contains("open");
      group.classList.toggle("open", open);
      b.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });

  // "Keep menu open": pin the menu as a sidebar on wide screens (remembered here).
  var pinBtn = document.querySelector(".pin-btn");
  function showPin() {
    if (!pinBtn) return;
    var pinned = root.classList.contains("nav-pinned");
    pinBtn.setAttribute("aria-pressed", pinned ? "true" : "false");
    pinBtn.querySelector(".label").textContent = pinned ? "Fold menu away" : "Keep menu open";
  }
  if (pinBtn) pinBtn.addEventListener("click", function () {
    var pinned = !root.classList.contains("nav-pinned");
    root.classList.toggle("nav-pinned", pinned);
    try { localStorage.setItem("navPinned", pinned ? "1" : "0"); } catch (err) {}
    setMenu(false);
    showPin();
  });
  showPin();

  document.addEventListener("click", function (e) {
    if (e.target.closest(".menu-btn")) { setMenu(!nav.classList.contains("open")); return; }
    // Picking a page folds the menu away straight away.
    if (nav && nav.classList.contains("open") && e.target.closest(".nav-menu a[href]")) setMenu(false);
    if (nav && nav.classList.contains("open") && !e.target.closest(".nav-menu, .tour")) setMenu(false);
    var toggle = e.target.closest(".theme-toggle:not(.install-btn):not(.sound-toggle):not(.pin-btn)");
    if (toggle) {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (err) {}
      show();
      return;
    }
    var btn = e.target.closest(".btn");
    if (btn) {
      var rect = btn.getBoundingClientRect();
      var size = Math.max(rect.width, rect.height);
      var ring = document.createElement("span");
      ring.className = "ripple";
      ring.style.width = ring.style.height = size + "px";
      ring.style.left = (e.clientX - rect.left - size / 2) + "px";
      ring.style.top = (e.clientY - rect.top - size / 2) + "px";
      btn.appendChild(ring);
      setTimeout(function () { ring.remove(); }, 700);
    }
  });

  if (media && media.addEventListener) media.addEventListener("change", show);
  show();

  // Loading animation while the next page is on its way.
  var slowTimer, giveUpTimer;
  function startLoading() {
    root.classList.add("is-loading");
    clearTimeout(slowTimer); clearTimeout(giveUpTimer);
    slowTimer = setTimeout(function () { root.classList.add("is-slow"); }, 250);
    // Safety net: never leave the page covered if nothing arrives.
    giveUpTimer = setTimeout(stopLoading, 15000);
  }
  function stopLoading() {
    clearTimeout(slowTimer); clearTimeout(giveUpTimer);
    root.classList.remove("is-loading", "is-slow");
  }
  // Coming back with the browser's Back button shows the old page from memory.
  window.addEventListener("pageshow", stopLoading);

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download") || a.hasAttribute("data-no-loader")) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    startLoading();
  });
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented || e.target.hasAttribute("data-no-loader")) return;
    startLoading();
  });

  // Saved replies: drop the chosen text into the reply box, with the person's
  // first name, their company and your name filled in.
  document.addEventListener("change", function (e) {
    var pick = e.target.closest("select.insert-reply");
    if (!pick || !pick.value) return;
    var box = document.getElementById(pick.dataset.target);
    var text = pick.value
      .replace(/\{name\}/g, pick.dataset.name || "")
      .replace(/\{company\}/g, pick.dataset.company || "")
      .replace(/\{tech\}/g, pick.dataset.tech || "");
    if (box) {
      box.value = box.value.trim() ? box.value.replace(/\s*$/, "") + "\n\n" + text : text;
      box.focus();
    }
    pick.value = "";
  });

  // Installing on a home screen or desktop.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }
  // Chrome, Edge and Android offer an install prompt; show our button when they do.
  // (On iPhone and iPad: Share button, then "Add to Home Screen".)
  var installPrompt = null;
  var installButtons = document.querySelectorAll(".install-btn");
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    installPrompt = e;
    installButtons.forEach(function (b) { b.hidden = false; });
  });
  installButtons.forEach(function (b) {
    b.addEventListener("click", function () {
      if (!installPrompt) return;
      installPrompt.prompt();
      installPrompt.userChoice.finally(function () {
        installPrompt = null;
        installButtons.forEach(function (x) { x.hidden = true; });
      });
    });
  });
  window.addEventListener("appinstalled", function () {
    installButtons.forEach(function (b) { b.hidden = true; });
  });
})();
