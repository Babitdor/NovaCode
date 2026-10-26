/* Theme: follows the system until the visitor picks one, then remembers the pick.
   Loaded without defer in <head>, so data-theme is set before the first paint and a
   saved choice never flashes the other palette. The CSS reads [data-theme] and falls
   back to prefers-color-scheme, so pages stay themed with this script blocked.
   Buttons marked [data-theme-toggle] ship hidden and are revealed here, because
   without script they could not do anything.

   A manual switch uses native View Transitions: the new palette is revealed as a
   circle growing from the toggle, after the next image set has decoded. Browsers
   without View Transitions fade the palette in place; reduced motion switches
   immediately. A newer choice supersedes an in-flight reveal. */
(function () {
  "use strict";
  var root = document.documentElement, KEY = "nova-theme";
  var sys = window.matchMedia("(prefers-color-scheme: dark)");
  var motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var saved = null;
  var transition = null;
  var fadeTimer = 0;
  var artAnimation = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* storage blocked: follow the system */ }
  if (saved !== "light" && saved !== "dark") saved = null;

  var isDark = function () { return saved ? saved === "dark" : sys.matches; };
  var calm = function () { return motion.matches; };

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
      m.setAttribute("content", forDark ? "#0e0e10" : "#fafafa");
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

  function revealOrigin(button) {
    var r = button && button.getBoundingClientRect ? button.getBoundingClientRect() : null;
    var x = r ? r.left + r.width / 2 : window.innerWidth - 48;
    var y = r ? r.top + r.height / 2 : 48;
    var dx = Math.max(x, window.innerWidth - x);
    var dy = Math.max(y, window.innerHeight - y);
    return { x: x, y: y, r: Math.ceil(Math.sqrt(dx * dx + dy * dy)) + 40 };
  }

  function fallback(images) {
    var art = document.querySelector(".art");
    root.classList.add("theme-fade");
    void root.offsetWidth; /* take the transition properties before the palette changes */
    changed();
    window.clearTimeout(fadeTimer);
    fadeTimer = window.setTimeout(function () { root.classList.remove("theme-fade"); }, 520);
    decodeImages(images).then(function () {
      if (!art || !art.animate || calm()) return;
      if (artAnimation) artAnimation.cancel();
      artAnimation = art.animate(
        [{ opacity: 0.72, filter: "blur(3px)" }, { opacity: 1, filter: "blur(0px)" }],
        { duration: 600, easing: "cubic-bezier(.16, 1, .3, 1)" }
      );
    });
  }

  function switchTheme(button) {
    if (calm()) { changed(); return; }
    var origin = revealOrigin(button);
    root.style.setProperty("--theme-x", origin.x + "px");
    root.style.setProperty("--theme-y", origin.y + "px");
    root.style.setProperty("--theme-r", origin.r + "px");
    var images = themedImages();
    var render = function () {
      changed();
      /* Let source selection run, then decode the new bitmaps before the new
         snapshot is taken. A timer, not requestAnimationFrame: frames can be
         suspended while a transition waits for this promise. */
      return new Promise(function (resolve) { window.setTimeout(resolve, 0); })
        .then(function () { return decodeImages(images); });
    };
    if (!document.startViewTransition) { fallback(images); return; }
    if (transition) transition.skipTransition();
    try {
      var t = document.startViewTransition(render);
      transition = t;
      t.ready.catch(function () { /* skipping also rejects the ready promise */ });
      t.finished.then(function () {
        if (transition === t) transition = null;
      }, function () {
        if (transition === t) transition = null;
      });
    } catch (e) {
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
        switchTheme(b);
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
