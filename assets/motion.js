/* Motion: GSAP + ScrollTrigger on top of the self-hosted Lenis ticker.
   - Nothing runs without GSAP or under prefers-reduced-motion, and every hidden
     state is set from here after the gate, so the mint is complete with script
     blocked, reduced motion, or a failed script fetch.
   - The verb is strike and settle: every entrance lands as a die press — a fast
     press, one tiny rebound, then still — and big plates press slower than chips.
   - Markup API: data-motion-text="words", data-image-reveal, data-magnetic.
     [data-fade] and [data-draw] settle through ScrollTrigger; a struck chapter
     divider gets .struck so its engraving hairline can fill (CSS owns the sheen).
   - Reveals run once. The record rail outside this file stays a native
     scroller; pinning it would fight touch, keyboard and the agent view. */
(function () {
  "use strict";
  var doc = document.documentElement;
  var gsap = window.gsap, ST = window.ScrollTrigger;
  var mq = window.matchMedia;
  var reduce = mq && mq("(prefers-reduced-motion: reduce)").matches;
  var fine = mq && mq("(pointer: fine)").matches;
  if (!gsap || !ST || reduce) { doc.classList.remove("enter"); return; }
  gsap.registerPlugin(ST);
  doc.classList.add("has-motion");
  gsap.defaults({ ease: "power3.out", duration: 0.85 });

  /* ---- smooth scroll: Lenis rafs on the GSAP ticker so a reveal never lags the page */
  var lenis = null;
  function tickLenis(time) { if (lenis) lenis.raf(time * 1000); }
  function syncSmoothScroll() {
    if (doc.getAttribute("data-view") === "agent") {
      if (lenis) {
        gsap.ticker.remove(tickLenis);
        lenis.destroy();
        lenis = null;
      }
    } else if (window.Lenis && !lenis) {
      /* anchors ride the same easing as the wheel; the target's own scroll-margin-top
         already clears the sticky masthead, so Lenis adds no offset of its own */
      lenis = new Lenis({ lerp: 0.08, wheelMultiplier: 0.9, anchors: true });
      lenis.on("scroll", ST.update);
      gsap.ticker.add(tickLenis);
      gsap.ticker.lagSmoothing(0);
    }
  }
  syncSmoothScroll();
  document.addEventListener("themechange", function () { ST.refresh(); });

  /* ---- the die press: one press, one rebound, then still. The hero types land after the
     picture starts developing, in reading order; the same press, slower, strikes a chapter. */
  function press(targets, vars) {
    vars = vars || {};
    var rebound = vars.rebound || 2;
    return gsap.timeline()
      .to(targets, { y: rebound, autoAlpha: 1, duration: vars.press || 0.55, stagger: vars.stagger || 0.06, ease: "power4.in" })
      .to(targets, { y: 0, duration: vars.settle || 0.5, stagger: vars.stagger || 0.06, ease: "power3.out" }, "-=" + (vars.overlap || 0.28));
  }
  var hero = document.querySelector(".hero");
  if (hero) {
    var enter = gsap.utils.toArray("[data-enter]").filter(function (el) { return hero.contains(el); });
    if (enter.length) {
      gsap.set(enter, { y: -18, autoAlpha: 0 });
      gsap.timeline({ delay: 0.14, onComplete: function () { doc.classList.remove("enter"); } })
        .add(press(enter, { press: 0.55, stagger: 0.07 }));
    } else {
      doc.classList.remove("enter");
    }
  }

  /* ---- the record rail is a native horizontal scroller (index.html owns the wheel mapping and
     the progress hairline). It is deliberately NOT pinned: translating a scroll container moves
     its own clipping box, so cards slid out of the content column and lost their edges. The only
     thing motion adds here is bringing a tabbed card into view inside the scroller. */
  (function () {
    var rail = document.querySelector("[data-horizontal-track]");
    if (!rail) return;
    rail.addEventListener("focusin", function (e) {
      var card = e.target.closest && e.target.closest("li");
      if (!card) return;
      var r = card.getBoundingClientRect(), v = rail.getBoundingClientRect();
      if (r.left >= v.left && r.right <= v.right) return;
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      rail.scrollTo({ left: card.offsetLeft - rail.offsetLeft, behavior: reduce ? "auto" : "smooth" });
    });
  })();

  /* The screenshot stays still while each explanation selects its source region. */
  gsap.utils.toArray(".tutorial").forEach(function (tutorial) {
    var tutorialMM = gsap.matchMedia();
    function buildTutorial() {
      if (doc.getAttribute("data-view") === "agent") return;
      tutorialMM.add("(min-width: 900px) and (min-height: 650px)", function () {
        tutorial.classList.add("tutorial-live");
        var steps = gsap.utils.toArray(".tutorial-steps li", tutorial);
        var triggers = steps.map(function (step, index) {
          function select() {
            tutorial.setAttribute("data-step", index);
            steps.forEach(function (item, i) {
              if (i === index) item.setAttribute("aria-current", "step");
              else item.removeAttribute("aria-current");
            });
          }
          if (index === 0) select();
          return ST.create({
            trigger: step, start: "top 116px", end: "bottom 116px",
            onEnter: select, onEnterBack: select
          });
        });
        return function () {
          triggers.forEach(function (trigger) { trigger.kill(); });
          tutorial.classList.remove("tutorial-live");
          tutorial.removeAttribute("data-step");
          steps.forEach(function (step) { step.removeAttribute("aria-current"); });
        };
      });
    }
    buildTutorial();
    document.addEventListener("viewchange", function () {
      tutorialMM.revert();
      buildTutorial();
      ST.refresh();
    });
  });

  /* ---- the strike: a chapter plate lands as a die press, slower than the hero chips,
     then its engraving fills — the hairline under the headline draws and holds */
  gsap.utils.toArray(".divider").forEach(function (divider) {
    var inner = divider.querySelector(".wrap");
    if (!inner) return;
    var tl = gsap.timeline({ scrollTrigger: { trigger: divider, start: "top 82%", once: true } });
    gsap.set(inner, { y: -26, autoAlpha: 0 });
    tl.add(press(inner, { press: 0.7, settle: 0.55, stagger: 0.08 }));
    tl.add(function () { divider.classList.add("struck"); });
  });
  /* the slow sheen: the struck headline's hairline breathes, nothing else idles */

  /* ---- type: whole words rise out of their own mask, in reading order */
  function splitWords(el) {
    if (el.getAttribute("data-split") || el.children.length) return null;
    var text = el.textContent, parts = text.split(/(\s+)/);
    el.textContent = "";
    el.setAttribute("aria-label", text.trim());
    el.setAttribute("data-split", "");
    parts.forEach(function (part) {
      if (!part.trim()) { el.appendChild(document.createTextNode(part)); return; }
      var mask = document.createElement("span"), word = document.createElement("span");
      mask.className = "mo-word-mask";
      mask.setAttribute("aria-hidden", "true");
      word.className = "mo-word";
      word.textContent = part;
      mask.appendChild(word);
      el.appendChild(mask);
    });
    return el.querySelectorAll(".mo-word");
  }
  gsap.utils.toArray("[data-motion-text]").forEach(function (el) {
    var words = splitWords(el);
    if (!words || !words.length) return;
    gsap.fromTo(words, { yPercent: 112, autoAlpha: 0 }, {
      yPercent: 0, autoAlpha: 1, duration: 0.75, stagger: 0.05, ease: "power4.inOut",
      scrollTrigger: { trigger: el, start: "top 84%", once: true }
    });
  });

  /* ---- blocks: [data-fade] presses in as it enters, [data-draw] rules draw left to right */
  var fades = gsap.utils.toArray("[data-fade]");
  if (fades.length) {
    gsap.set(fades, { y: -14, autoAlpha: 0 });
    ST.batch(fades, {
      start: "top 88%", once: true,
      onEnter: function (batch) {
        var tl = gsap.timeline({ overwrite: true });
        tl.to(batch, { y: 2, autoAlpha: 1, duration: 0.5, stagger: 0.06, ease: "power4.in" })
          .to(batch, { y: 0, duration: 0.42, ease: "power3.out" }, "-=0.26");
      }
    });
  }
  gsap.utils.toArray("[data-draw]").forEach(function (el) {
    gsap.fromTo(el, { scaleX: 0 }, {
      scaleX: 1, duration: 0.9, ease: "power4.out",
      scrollTrigger: { trigger: el, start: "top 92%", once: true }
    });
  });

  /* ---- media: the window opens by clip, the picture inside drifts slower than the sheet */
  gsap.utils.toArray("[data-image-reveal]").forEach(function (fig) {
    var img = fig.querySelector("img");
    var tl = gsap.timeline({ scrollTrigger: { trigger: fig, start: "top 84%", once: true } });
    tl.fromTo(fig, { clipPath: "inset(0 0 100% 0)" }, { clipPath: "inset(0 0 0% 0)", duration: 1.05, ease: "power4.out" });
    if (img) tl.fromTo(img, { scale: 1.07 }, { scale: 1, duration: 1.2, ease: "power4.out" }, 0);
  });

  /* ---- magnetic: the two controls worth aiming at lean toward the pointer */
  if (fine) {
    gsap.utils.toArray("[data-magnetic]").forEach(function (el) {
      var strength = Number(el.getAttribute("data-magnetic")) || 0.16;
      var xTo = gsap.quickTo(el, "x", { duration: 0.45, ease: "power3.out" });
      var yTo = gsap.quickTo(el, "y", { duration: 0.45, ease: "power3.out" });
      el.addEventListener("pointermove", function (e) {
        var r = el.getBoundingClientRect();
        xTo((e.clientX - r.left - r.width / 2) * strength);
        yTo((e.clientY - r.top - r.height / 2) * strength);
      });
      el.addEventListener("pointerleave", function () { xTo(0); yTo(0); });
    });
  }

  function syncMotionMode() {
    syncSmoothScroll();
    var agent = doc.getAttribute("data-view") === "agent";
    ST.getAll().forEach(function (trigger) {
      if (agent) trigger.disable(); else trigger.enable();
    });
    ST.refresh();
  }
  document.addEventListener("viewchange", syncMotionMode);
  syncMotionMode();
  window.addEventListener("load", function () { ST.refresh(); });
  /* A refresh once the fonts settle would recompute trigger positions for the serif
     swap, but it also re-enters the once-only reveals: an above-the-fold [data-draw]
     that already fired comes back partial or reset, so the strike is left unfinished.
     The load handler above is the one refresh; it runs before those draws complete. */
})();

/* ---- the starfield: its own gate, because it must also work without GSAP and
   with reduced motion on (then it builds once and never moves). Pointer movement
   parallaxes each star by its depth, the nearest star takes a halo, and a click
   sends a ring out from that point. Pure transforms, one rAF loop, passive
   listeners; nothing here paints on scroll. */
(function () {
  var field = document.querySelector("[data-starfield]");
  if (!field) return;

  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var count = window.innerWidth < 760 ? 40 : 110;
  var stars = [];
  var mx = 0, my = 0, cx = 0, cy = 0, frame = 0;

  function build() {
    field.textContent = "";
    stars = [];
    var w = field.clientWidth || 600, h = field.clientHeight || 400;
    for (var i = 0; i < count; i++) {
      var el = document.createElement("span");
      var depth = 0.25 + Math.random() * 0.75;
      el.className = "star" + (i % 4 === 0 ? " star--cool" : "") + (depth > 0.72 ? " star--deep" : "");
      el.style.width = el.style.height = (1.4 + depth * 1.2).toFixed(1) + "px";
      el.style.opacity = (0.32 + depth * 0.5).toFixed(2);
      var x = Math.random() * w, y = Math.random() * h;
      el.style.transform = "translate3d(" + x.toFixed(1) + "px," + y.toFixed(1) + "px,0)";
      field.appendChild(el);
      stars.push({ el: el, x: x, y: y, d: depth });
    }
  }

  function paint() {
    frame = 0;
    cx += (mx - cx) * 0.07;
    cy += (my - cy) * 0.07;
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i], k = 16 * s.d;
      s.el.style.transform = "translate3d(" + (s.x - cx * k).toFixed(1) + "px," + (s.y - cy * k).toFixed(1) + "px,0)";
    }
    if (Math.abs(mx - cx) > 0.002 || Math.abs(my - cy) > 0.002) queue();
  }
  function queue() { if (!frame) frame = requestAnimationFrame(paint); }

  build();
  if (calm) return;

  var lit = -1;
  function light(px, py) {
    var next = -1, best = 130 * 130;
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i], dx = px - s.x, dy = py - s.y, dist = dx * dx + dy * dy;
      if (dist < best) { best = dist; next = i; }
    }
    if (next === lit) return;
    if (lit > -1) stars[lit].el.classList.remove("star--halo");
    if (next > -1) stars[next].el.classList.add("star--halo");
    lit = next;
  }

  window.addEventListener("pointermove", function (e) {
    mx = (e.clientX / window.innerWidth - 0.5) * 2;
    my = (e.clientY / window.innerHeight - 0.5) * 2;
    light(e.clientX, e.clientY);
    queue();
  }, { passive: true });

  document.addEventListener("mouseleave", function () { mx = my = 0; queue(); });

  window.addEventListener("click", function (e) {
    // a click on a real control is the visitor's business, not the field's
    if (e.target.closest && e.target.closest("a,button,summary,input,textarea,select")) return;
    var px = e.clientX, py = e.clientY;
    var ring = document.createElement("span");
    ring.className = "starwave";
    ring.style.left = px + "px";
    ring.style.top = py + "px";
    field.appendChild(ring);
    ring.addEventListener("animationend", function () { ring.remove(); });
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i], dx = s.x - px, dy = s.y - py;
      if (dx * dx + dy * dy < 90000) { (function (el) {
        if (el.classList.contains("star--halo")) return;
        el.classList.add("star--halo");
        setTimeout(function () { el.classList.remove("star--halo"); }, 900);
      })(s.el); }
    }
  });

  var resize;
  window.addEventListener("resize", function () {
    clearTimeout(resize);
    resize = setTimeout(build, 220);
  });
})();
