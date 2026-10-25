/* Theme: follows the system until the visitor picks one, then remembers the pick.
   Loaded without defer in <head>, so data-theme is set before the first paint and a
   saved choice never flashes the other palette. The CSS reads [data-theme] and falls
   back to prefers-color-scheme, so pages stay themed with this script blocked.
   Buttons marked [data-theme-toggle] ship hidden and are revealed here, because
   without script they could not do anything. */
(function () {
  "use strict";
  var root = document.documentElement, KEY = "nova-theme";
  var sys = window.matchMedia("(prefers-color-scheme: dark)");
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* storage blocked: follow the system */ }
  if (saved !== "light" && saved !== "dark") saved = null;

  var isDark = function () { return saved ? saved === "dark" : sys.matches; };

  function apply() {
    if (saved) root.setAttribute("data-theme", saved);
    else root.removeAttribute("data-theme");
    var dark = isDark();
    /* The browser chrome follows the page, not the OS, once a theme is chosen. */
    Array.prototype.forEach.call(document.querySelectorAll('meta[name="theme-color"]'), function (m) {
      var forDark = saved ? dark : /dark/.test(m.getAttribute("media") || "");
      m.setAttribute("content", forDark ? "#0e0e10" : "#fafafa");
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-theme-toggle]"), function (b) {
      b.hidden = false;
      b.setAttribute("aria-pressed", dark ? "true" : "false");
    });
  }

  function changed() {
    apply();
    document.dispatchEvent(new Event("themechange"));
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
        changed();
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
