/* Human/Agent view toggle for NovaCode static pages.
   - Persists choice in localStorage (nova-view: human|agent)
   - URL override: ?view=agent|human
   - Sets data-view on <html> and updates [data-view-toggle] controls
   - Dispatches "viewchange" for page-specific adjustments
   - In agent view, expands <details> marked/used as readable content where helpful
   Works without server config; all pages remain usable with JS blocked. */
(function () {
  "use strict";
  var root = document.documentElement, KEY = "nova-view";
  var saved = null, param = null;
  try {
    var q = new URLSearchParams(window.location.search);
    var v = (q.get("view") || "").toLowerCase();
    if (v === "agent" || v === "human") param = v;
  } catch (e) {}
  try { saved = localStorage.getItem(KEY); } catch (e) {}
  if (saved !== "human" && saved !== "agent") saved = null;
  var mode = param || saved || "human";
  var detailStates = new Map();

  function expandDetails(on) {
    if (on) {
      Array.prototype.forEach.call(document.querySelectorAll("main details, details[data-agent-expand]"), function (d) {
        if (!detailStates.has(d)) detailStates.set(d, d.open);
        d.open = true;
      });
    } else {
      detailStates.forEach(function (open, d) { if (d.isConnected) d.open = open; });
      detailStates.clear();
    }
  }

  function apply() {
    root.setAttribute("data-view", mode);
    try {
      Array.prototype.forEach.call(document.querySelectorAll("[data-view-toggle]"), function (b) {
        b.hidden = false;
        var on = mode === "agent";
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.setAttribute("aria-label", on ? "Switch to human view" : "Switch to agent view");
        b.setAttribute("title", on ? "Agent view (click for human)" : "Human view (click for agent)");
        var label = b.querySelector("[data-view-label]");
        if (label) label.textContent = on ? "Agent" : "Human";
      });
      Array.prototype.forEach.call(document.querySelectorAll('meta[name="nova-view"]'), function (m) {
        m.setAttribute("content", mode);
      });
    } catch (e) {}
    expandDetails(mode === "agent");
  }

  function set(next, persist) {
    mode = next === "agent" ? "agent" : "human";
    if (persist) try { localStorage.setItem(KEY, mode); } catch (e) {}
    if (persist && param) try {
      var url = new URL(location.href);
      url.searchParams.set("view", mode);
      history.replaceState(history.state, "", url);
    } catch (e) {}
    apply();
    try { document.dispatchEvent(new CustomEvent("viewchange", { detail: { view: mode } })); } catch (e) {}
  }

  apply();
  function wire() {
    apply();
    Array.prototype.forEach.call(document.querySelectorAll("[data-view-toggle]"), function (b) {
      b.addEventListener("click", function () {
        set(mode === "agent" ? "human" : "agent", true);
      });
    });
    if (mode === "agent") expandDetails(true);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
  window.addEventListener("load", function(){ if (mode === "agent") expandDetails(true); });
  window.NovaView = { get: function(){return mode;}, set: function(v){ set(v, true); }, expandDetails: function(){ expandDetails(mode === "agent"); } };
})();
