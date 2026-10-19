/* ==========================================================================
   NovaCode site — engine and interactions
   One clock: Lenis is driven by GSAP's ticker, ScrollTrigger reads Lenis.
   Everything page-level is registered in onPage() so it can be reverted.
   ========================================================================== */

(() => {
  "use strict";

  const root = document.documentElement;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const hasGSAP = typeof window.gsap !== "undefined" && typeof window.ScrollTrigger !== "undefined";

  if (hasGSAP) gsap.registerPlugin(ScrollTrigger);

  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  /* --- smooth scroll ---------------------------------------------------- */

  let lenis = null;

  function initScroll() {
    if (reduced || !window.Lenis || !hasGSAP) return;

    lenis = new Lenis({
      duration: 1.05,
      easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      smoothWheel: true,
      touchMultiplier: 1.6,
    });

    lenis.on("scroll", ScrollTrigger.update);
    gsap.ticker.add((time) => lenis.raf(time * 1000));
    gsap.ticker.lagSmoothing(0);
  }

  /* --- nav -------------------------------------------------------------- */

  function initNav() {
    const nav = $(".nav");
    if (!nav) return;

    if (hasGSAP) {
      ScrollTrigger.create({
        start: 0,
        end: "max",
        onUpdate: (self) => nav.classList.toggle("is-stuck", self.scroll() > 12),
      });
    } else {
      const onScroll = () => nav.classList.toggle("is-stuck", window.scrollY > 12);
      window.addEventListener("scroll", onScroll, { passive: true });
      onScroll();
    }

    const toggle = $(".nav__toggle");
    const menu = $(".menu");
    if (!toggle || !menu) return;

    const setOpen = (open) => {
      toggle.setAttribute("aria-expanded", String(open));
      menu.hidden = !open;
      root.classList.toggle("menu-open", open);
      if (lenis) open ? lenis.stop() : lenis.start();
      if (open) $("a", menu)?.focus();
    };

    toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));
    $$("a", menu).forEach((a) => a.addEventListener("click", () => setOpen(false)));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        setOpen(false);
        toggle.focus();
      }
    });
  }

  /* --- reveals ---------------------------------------------------------- */

  function initReveals() {
    const items = $$("[data-reveal]");
    if (!items.length) return;

    if (!hasGSAP || reduced) {
      items.forEach((el) => el.classList.add("is-in"));
      return;
    }

    items.forEach((el) => {
      ScrollTrigger.create({
        trigger: el,
        start: "top 90%",
        once: true,
        onEnter: () => el.classList.add("is-in"),
      });
    });
  }

  /* --- hero intro ------------------------------------------------------- */

  function initIntro() {
    const hero = $("[data-intro]");
    if (!hero || !hasGSAP || reduced) return;

    const claim = $("[data-intro-claim]", hero);
    const aside = $$("[data-intro-aside]", hero);
    const plate = $("[data-intro-plate]", hero);
    const cells = $$(".titleblock__cell", hero);

    const tl = gsap.timeline({ defaults: { ease: "power3.out" } });

    if (claim) {
      tl.from(claim, { y: 26, opacity: 0, duration: 0.9 }, 0.05);
    }
    if (aside.length) {
      tl.from(aside, { y: 18, opacity: 0, duration: 0.8, stagger: 0.08 }, 0.22);
    }
    if (plate) {
      tl.from(plate, { y: 30, opacity: 0, duration: 1, ease: "power2.out" }, 0.3);
    }
    if (cells.length) {
      tl.from(cells, { opacity: 0, duration: 0.5, stagger: 0.05 }, 0.5);
    }
  }

  /* --- the driver: status line, claim marks, stamp ---------------------- */

  function initDriver() {
    const claims = $$("[data-claim]");
    const countEl = $("[data-count-proofs]");
    const totalEl = $("[data-count-claims]");
    const statusEls = $$("[data-status]");
    const total = claims.length;

    if (totalEl) totalEl.textContent = String(total).padStart(2, "0");

    if (!total || !hasGSAP) {
      if (countEl) countEl.textContent = String(total).padStart(2, "0");
      statusEls.forEach((el) => {
        el.textContent = "VERIFIED";
        el.classList.add("statusline__val--accent");
      });
      return;
    }

    const state = new Array(total).fill(false);

    const refresh = () => {
      const n = state.filter(Boolean).length;
      if (countEl) countEl.textContent = String(n).padStart(2, "0");
      const done = n === total;
      statusEls.forEach((el) => {
        el.textContent = done ? "VERIFIED" : "DRAFT";
        el.classList.toggle("statusline__val--accent", done);
      });
      claims.forEach((sec, i) => {
        const mark = $(".claim-mark", sec);
        if (mark) mark.classList.toggle("is-proven", state[i]);
      });
    };

    claims.forEach((sec, i) => {
      ScrollTrigger.create({
        trigger: sec,
        start: "top 62%",
        onEnter: () => {
          state[i] = true;
          refresh();
        },
        onLeaveBack: () => {
          state[i] = false;
          refresh();
        },
      });
    });

    refresh();
  }

  /* --- the stage: a real change, and the test that proves it ------------ */

  function initStage() {
    const stage = $("[data-stage]");
    if (!stage) return;

    const lines = $$(".diff__line", stage);
    const codes = $$(".diff__code, .diff__sign", stage);
    const runs = $$(".test__run", stage);
    const steps = $$(".rail__step", stage);
    const stamp = $(".stamp", stage);
    const verdict = $("[data-verdict]", stage);

    const setPhase = (n) => {
      steps.forEach((s, i) => {
        s.classList.toggle("is-active", i === n);
        s.classList.toggle("is-done", i < n);
      });
      if (verdict) {
        const pass = n >= 2;
        verdict.dataset.state = pass ? "pass" : "fail";
        verdict.textContent = pass ? "36 passed" : "2 failed";
      }
    };

    // No motion, no pin: show the finished proof.
    if (!hasGSAP || reduced) {
      gsap?.set(lines, { x: 0 });
      gsap?.set(codes, { opacity: 1 });
      runs.forEach((r, i) => {
        r.style.opacity = i === runs.length - 1 ? "1" : "0";
        r.style.visibility = i === runs.length - 1 ? "visible" : "hidden";
      });
      if (stamp) {
        stamp.style.opacity = "1";
        stamp.style.transform = "rotate(-11deg) scale(1)";
      }
      if (verdict) verdict.dataset.state = "pass";
      setPhase(steps.length - 1);
      return;
    }

    const wide = window.matchMedia("(min-width: 900px)").matches;

    gsap.set(codes, { opacity: 0.55 });
    gsap.set(lines, { x: -8 });
    gsap.set(runs, { opacity: 0, visibility: "hidden" });
    gsap.set(runs[0], { opacity: 1, visibility: "visible" });
    gsap.set(stamp, { opacity: 0, scale: 0.6, rotate: -11 });

    const tl = gsap.timeline({
      defaults: { ease: "none" },
      scrollTrigger: {
        trigger: stage,
        start: wide ? "top top" : "top 75%",
        end: wide ? "+=130%" : "bottom 40%",
        pin: wide,
        pinSpacing: wide,
        scrub: wide ? 0.6 : true,
        invalidateOnRefresh: true,
        onUpdate: (self) => {
          const p = self.progress;
          setPhase(p < 0.14 ? 0 : p < 0.56 ? 1 : p < 0.86 ? 2 : 3);
        },
      },
    });

    // 1. BEFORE: the failing run is on screen.
    tl.to({}, { duration: 0.14 });

    // 2. CHANGE: the diff applies, line by line.
    tl.to(codes, {
      opacity: 1,
      duration: 0.42,
      stagger: 0.012,
      ease: "power1.out",
    });
    tl.to(lines, { x: 0, duration: 0.42, stagger: 0.012, ease: "power1.out" }, "<");

    // 3. TEST: the failing run gives way to the passing run.
    tl.to(runs[0], { opacity: 0, duration: 0.06 }, ">-0.02");
    tl.set(runs[0], { visibility: "hidden" });
    tl.set(runs[1], { visibility: "visible" });
    tl.to(runs[1], { opacity: 1, duration: 0.08 });
    tl.to({}, { duration: 0.16 });

    // 4. VERIFIED: the stamp lands.
    tl.to(stamp, { opacity: 1, scale: 1, duration: 0.1, ease: "power2.out" });
    tl.to({}, { duration: 0.04 });
  }

  /* --- copy buttons ----------------------------------------------------- */

  function initCopy() {
    $$("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const row = btn.closest(".cmd");
        const text = row ? $(".cmd__text", row).textContent.trim() : "";
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.setAttribute("readonly", "");
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          try {
            document.execCommand("copy");
          } catch {
            /* clipboard unavailable */
          }
          ta.remove();
        }
        const label = $("span", btn);
        const prev = label ? label.textContent : "";
        btn.dataset.copied = "true";
        if (label) label.textContent = "Copied";
        window.setTimeout(() => {
          btn.dataset.copied = "false";
          if (label) label.textContent = prev;
        }, 1600);
      });
    });
  }

  /* --- tabs ------------------------------------------------------------- */

  function initTabs() {
    $$("[data-tabs]").forEach((group) => {
      const tabs = $$('[role="tab"]', group);
      const panels = $$('[role="tabpanel"]', group);
      if (!tabs.length) return;

      const select = (i) => {
        tabs.forEach((t, j) => {
          const on = i === j;
          t.setAttribute("aria-selected", String(on));
          t.tabIndex = on ? 0 : -1;
          if (panels[j]) panels[j].hidden = !on;
        });
      };

      tabs.forEach((tab, i) => {
        tab.addEventListener("click", () => select(i));
        tab.addEventListener("keydown", (e) => {
          const dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
          if (!dir) return;
          e.preventDefault();
          const next = (i + dir + tabs.length) % tabs.length;
          select(next);
          tabs[next].focus();
        });
      });

      select(0);
    });
  }

  /* --- accordion -------------------------------------------------------- */

  function initAccordion() {
    $$(".acc__item").forEach((item) => {
      const btn = $(".acc__btn", item);
      const panel = $(".acc__panel", item);
      if (!btn || !panel) return;

      btn.addEventListener("click", () => {
        const open = btn.getAttribute("aria-expanded") === "true";
        btn.setAttribute("aria-expanded", String(!open));

        if (!hasGSAP || reduced) {
          panel.style.height = open ? "0px" : "auto";
          return;
        }

        if (open) {
          gsap.to(panel, { height: 0, duration: 0.28, ease: "power2.inOut" });
        } else {
          gsap.fromTo(
            panel,
            { height: 0 },
            {
              height: "auto",
              duration: 0.4,
              ease: "power2.out",
              onComplete: () => {
                panel.style.height = "auto";
              },
            },
          );
        }
      });
    });
  }

  /* --- the ledger and the timeline: each row is marked as it is read ---- */

  function initLedger() {
    $$(".ledger__row, .tl").forEach((row) => {
      if (!hasGSAP) {
        row.classList.add("is-proven");
        return;
      }
      ScrollTrigger.create({
        trigger: row,
        start: "top 88%",
        onEnter: () => row.classList.add("is-proven"),
        onLeaveBack: () => row.classList.remove("is-proven"),
      });
    });
  }

  /* --- boot ------------------------------------------------------------- */

  function boot() {
    initScroll();
    initNav();
    initIntro();
    initReveals();
    initDriver();
    initLedger();
    initStage();
    initCopy();
    initTabs();
    initAccordion();

    if (hasGSAP) {
      // Fonts change metrics; recompute once they are in.
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(() => ScrollTrigger.refresh());
      }
      window.addEventListener("load", () => ScrollTrigger.refresh());
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
