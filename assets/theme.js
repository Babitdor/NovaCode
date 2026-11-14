/* Theme: follows the system until the visitor picks one, then remembers the pick.
   Loaded without defer in <head>, so data-theme is set before the first paint and a
   saved choice never flashes the other palette. The CSS reads [data-theme] and falls
   back to prefers-color-scheme, so pages stay themed with this script blocked.
   Buttons marked [data-theme-toggle] ship hidden and are revealed here, because
   without script they could not do anything.

   A manual switch uses native View Transitions: a drafting rule prints the new
   palette down the sheet, reversing for light, after the next images decode. The
   unselected image set is decoded during idle time so that first switch does not
   open with a stall. Browsers without View Transitions fade the palette in place;
   reduced motion switches immediately. A newer choice supersedes an in-flight
   reveal. */
(function () {
  "use strict";
  var root = document.documentElement, KEY = "nova-theme";
  var sys = window.matchMedia("(prefers-color-scheme: dark)");
  var motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var saved = null;
  var transition = null;
  var fadeTimer = 0;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* storage blocked: follow the system */ }
  if (saved !== "light" && saved !== "dark") saved = null;

  var isDark = function () { return saved ? saved === "dark" : sys.matches; };
  var calm = function () { return motion.matches || root.getAttribute("data-view") === "agent"; };

  function apply() {
    if (saved) root.setAttribute("data-theme", saved);
    else root.removeAttribute("data-theme");
    var dark = isDark();
    /* Picture sources follow the saved choice as well as the OS. Without JS,
       their original media query still supplies the appropriate hero. */
    Array.prototype.forEach.call(document.querySelectorAll("source[data-theme-source]"), function (source) {
      source.media = dark ? "all" : "not all";
    });
    /* The browser chrome follows the page, not the OS, once a theme is chosen. */
    Array.prototype.forEach.call(document.querySelectorAll('meta[name="theme-color"]'), function (m) {
      var forDark = saved ? dark : /dark/.test(m.getAttribute("media") || "");
      m.setAttribute("content", forDark ? "#121316" : "#EFEDE7");
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-theme-toggle]"), function (b) {
      b.hidden = false;
      b.setAttribute("aria-pressed", dark ? "true" : "false");
      b.setAttribute("aria-label", dark ? "Light theme" : "Dark theme");
    });
  }

  function changed() {
    apply();
    document.dispatchEvent(new Event("themechange"));
  }

  function themedImages() {
    var images = [];
    Array.prototype.forEach.call(document.querySelectorAll("source[data-theme-source]"), function (source) {
      var picture = source.parentNode;
      var img = picture && picture.querySelector ? picture.querySelector("img") : null;
      if (img && images.indexOf(img) === -1) images.push(img);
    });
    return images;
  }

  function decodeImages(images) {
    return Promise.all(images.map(function (img) {
      return img.decode ? img.decode().catch(function () { /* keep the native image fallback */ }) : Promise.resolve();
    }));
  }

  /* Rasterise both palettes ahead of the first toggle. The picture elements only
     hold one theme at a time, so decode the alternate candidates through detached
     images; the browser reuses them when the real source switches. */
  function warmThemedImages() {
    var pending = [];
    Array.prototype.forEach.call(document.querySelectorAll("picture"), function (picture) {
      var themed = picture.querySelector("source[data-theme-source]");
      if (!themed) return;
      var candidates = [themed], img = picture.querySelector("img");
      if (img) candidates.push(img);
      Array.prototype.forEach.call(candidates, function (candidate) {
        var srcset = candidate.getAttribute("srcset");
        var src = candidate.getAttribute("src");
        if (!srcset && !src) return;
        var loader = new Image();
        var sizes = candidate.getAttribute("sizes");
        if (sizes) loader.sizes = sizes;
        loader.decoding = "async";
        /* Set the descriptor before the source so the viewport-sized candidate
           is the one that gets fetched, not the browser's 100vw default. */
        if (srcset) loader.srcset = srcset;
        else loader.src = src;
        pending.push(loader.decode().catch(function () { /* the selected palette still works */ }));
      });
    });
    return Promise.all(pending);
  }

  function fallback(images) {
    root.classList.add("theme-fade");
    void root.offsetWidth; /* take the transition properties before the palette changes */
    changed();
    window.clearTimeout(fadeTimer);
    fadeTimer = window.setTimeout(function () { root.classList.remove("theme-fade"); }, 280);
    decodeImages(images);
  }

  function switchTheme() {
    if (transition) transition.skipTransition();
    window.clearTimeout(fadeTimer);
    root.classList.remove("theme-fade");
    if (calm()) { changed(); return; }
    var images = themedImages();
    var rule = null;
    var render = function () {
      changed();
      rule = document.createElement("div");
      rule.className = "theme-scan";
      rule.setAttribute("aria-hidden", "true");
      document.body.appendChild(rule);
      /* Let source selection run, then decode the new bitmaps before the new
         snapshot is taken. A timer, not requestAnimationFrame: frames can be
         suspended while a transition waits for this promise. */
      return new Promise(function (resolve) { window.setTimeout(resolve, 0); })
        .then(function () { return decodeImages(images); });
    };
    if (!document.startViewTransition) { fallback(images); return; }
    try {
      var t = document.startViewTransition(render);
      transition = t;
      t.ready.catch(function () { /* skipping also rejects the ready promise */ });
      t.finished.then(function () {
        if (rule) rule.remove();
        if (transition === t) transition = null;
      }, function () {
        if (rule) rule.remove();
        if (transition === t) transition = null;
      });
    } catch (e) {
      if (rule) rule.remove();
      fallback(images);
    }
  }

  apply();
  var onSys = function () { if (!saved) changed(); };
  if (sys.addEventListener) sys.addEventListener("change", onSys);
  else if (sys.addListener) sys.addListener(onSys);

  function wire() {
    apply();
    Array.prototype.forEach.call(document.querySelectorAll("[data-theme-toggle]"), function (b) {
      b.addEventListener("click", function () {
        saved = isDark() ? "light" : "dark";
        try { localStorage.setItem(KEY, saved); } catch (e) { /* the choice lasts for this page only */ }
        switchTheme();
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  function warmWhenIdle() {
    var warm = function () { warmThemedImages(); };
    if (window.requestIdleCallback) window.requestIdleCallback(warm, { timeout: 2000 });
    else window.setTimeout(warm, 900);
  }
  if (document.readyState === "complete") warmWhenIdle();
  else window.addEventListener("load", warmWhenIdle);
})();
