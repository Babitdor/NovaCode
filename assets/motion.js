/* Motion: GSAP + ScrollTrigger on top of the self-hosted Lenis ticker.
   - Nothing runs without GSAP or under prefers-reduced-motion, and every hidden
     state is set from here after the gate, so the sheet is complete with script
     blocked, reduced motion, or a failed script fetch.
   - Markup API: data-motion-text="words", data-image-reveal, data-magnetic and the
     [data-cursor] element. [data-fade] and [data-draw] keep their old meaning and now settle
     through ScrollTrigger.
   - Reveals run once. The release strip outside this file stays a native
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
      lenis = new Lenis({ lerp: 0.08, wheelMultiplier: 0.9 });
      lenis.on("scroll", ST.update);
      gsap.ticker.add(tickLenis);
      gsap.ticker.lagSmoothing(0);
    }
  }
  syncSmoothScroll();
  document.addEventListener("themechange", function () { ST.refresh(); });

  /* ---- entrance: the cover plate is already developing, so the type lands after it in reading
     order — headline, claim rule, copy, actions, then the title block's rows */
  var hero = document.querySelector(".hero");
  if (hero) {
    var enter = gsap.utils.toArray("[data-enter]").filter(function (el) { return hero.contains(el); });
    if (enter.length) {
      gsap.set(enter, { y: 22, autoAlpha: 0 });
      gsap.timeline({ delay: 0.14, onComplete: function () { doc.classList.remove("enter"); } })
        .to(enter, { y: 0, autoAlpha: 1, duration: 1.05, stagger: 0.09, ease: "power4.out" });
    } else {
      doc.classList.remove("enter");
    }
  }

  /* ---- the record strip: on a wide screen the sheet pins and the scroll runs the strip
     sideways. Everything narrower, reduced motion and the agent view keep the native scroller,
     and a card reached by Tab is scrolled into view instead of being left off-screen. */
  var gallery = document.querySelector("[data-horizontal-gallery]");
  var track = gallery && gallery.querySelector("[data-horizontal-track]");
  if (gallery && track) {
    var galleryMM = gsap.matchMedia();
    var paintBar = function (self) {
      var bar = gallery.querySelector(".rail-progress i");
      var wrap = gallery.querySelector(".rail-wrap");
      if (bar) bar.style.setProperty("--p", self.progress.toFixed(3));
      if (wrap) {
        wrap.classList.toggle("at-end", self.progress > 0.995);
        wrap.classList.toggle("scrolled", self.progress > 0.004);
      }
    };
    var buildGallery = function () {
      if (doc.getAttribute("data-view") === "agent") return;
      galleryMM.add("(min-width: 1024px)", function () {
        gallery.classList.add("rail-pinned");
        var travel = function () {
          return Math.max(0, track.scrollWidth - track.parentElement.clientWidth);
        };
        var tween = gsap.to(track, {
          x: function () { return -travel(); }, ease: "none",
          scrollTrigger: {
            trigger: gallery, start: "top top",
            end: function () { return "+=" + travel(); },
            scrub: 1, pin: true, anticipatePin: 1, invalidateOnRefresh: true,
            onUpdate: paintBar
          }
        });
        var st = tween.scrollTrigger;
        function focusCard(e) {
          var card = e.target.closest && e.target.closest("li");
          if (!card) return;
          var r = card.getBoundingClientRect();
          if (r.left >= 0 && r.right <= window.innerWidth) return;
          var p = Math.min(1, Math.max(0, card.offsetLeft / (travel() || 1)));
          var y = st.start + p * (st.end - st.start);
          if (lenis) lenis.scrollTo(y, { duration: 0.8 }); else window.scrollTo(0, y);
        }
        gallery.addEventListener("focusin", focusCard);
        return function () {
          gallery.removeEventListener("focusin", focusCard);
          gallery.classList.remove("rail-pinned");
          gsap.set(track, { clearProps: "transform" });
          tween.kill();
        };
      });
    };
    buildGallery();
    document.addEventListener("viewchange", function () {
      galleryMM.revert();
      buildGallery();
      ST.refresh();
    });
  }

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
    gsap.fromTo(words, { yPercent: 108, autoAlpha: 0 }, {
      yPercent: 0, autoAlpha: 1, duration: 0.95, stagger: 0.055, ease: "power4.out",
      scrollTrigger: { trigger: el, start: "top 84%", once: true }
    });
  });

  /* ---- blocks: [data-fade] settles as it enters, [data-draw] rules draw left to right */
  var fades = gsap.utils.toArray("[data-fade]");
  if (fades.length) {
    gsap.set(fades, { y: 26, autoAlpha: 0 });
    ST.batch(fades, {
      start: "top 88%", once: true,
      onEnter: function (batch) {
        gsap.to(batch, { y: 0, autoAlpha: 1, duration: 0.95, stagger: 0.07, overwrite: true });
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

  /* ---- chapter numerals drift against the sheet: the one scroll-linked scene on the page,
     decorative (aria-hidden), transform-only, 8% of its own height */
  gsap.utils.toArray(".divider .num").forEach(function (num) {
    gsap.fromTo(num, { yPercent: 6 }, {
      yPercent: -6, ease: "none",
      scrollTrigger: { trigger: num.closest(".divider"), start: "top bottom", end: "bottom top", scrub: 1.2 }
    });
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

  /* ---- cursor: a ring that widens over anything the visitor can act on. Atmosphere only;
     it never replaces the native pointer and it disappears on touch. */
  var cursor = document.querySelector("[data-cursor]");
  if (cursor && !fine) {
    cursor.remove();
  } else if (cursor) {
    var cx = gsap.quickTo(cursor, "x", { duration: 0.35, ease: "power3.out" });
    var cy = gsap.quickTo(cursor, "y", { duration: 0.35, ease: "power3.out" });
    document.addEventListener("pointermove", function (e) { cx(e.clientX); cy(e.clientY); });
    document.addEventListener("pointerover", function (e) {
      var hot = e.target.closest && e.target.closest("a,button,summary,input,[data-magnetic]");
      gsap.to(cursor, { scale: hot ? 1.9 : 1, duration: 0.35, ease: "power3.out" });
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
})();
