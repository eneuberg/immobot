// immobot dashboard: small progressive enhancements. Everything works without JS, this only adds comfort.
// Loaded synchronously in <head>; all handlers are delegated on document, so they also cover later elements.
(function () {
  "use strict";

  // CSS can now tell that JS runs (e.g. to show only the chosen provider panel on /settings) - before first paint.
  document.documentElement.classList.add("js");

  // Overlays that open by themselves (coming back to the page): for the first moment after they appear they ignore
  // clicks, Enter and Escape. A tap meant for the page underneath (the browser shows the cached page first, the
  // overlay comes a moment later) must not hit "Später"/"Ja" or the backdrop. window.immobotShowModal is also used
  // by editor.js.
  var MODAL_COOLDOWN_MS = 800;
  window.immobotShowModal = function (dlg) {
    if (dlg.open) return;
    dlg.showModal();
    dlg.setAttribute("data-cooling", "");
    clearTimeout(dlg._coolTimer);
    dlg._coolTimer = setTimeout(function () { dlg.removeAttribute("data-cooling"); }, MODAL_COOLDOWN_MS);
  };
  ["click", "submit", "cancel", "keydown"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      if (type === "keydown" && e.key !== "Enter" && e.key !== " " && e.key !== "Escape") return;
      if (e.target.closest && e.target.closest("dialog[data-cooling]")) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);
  });

  // Confirm buttons: <button data-confirm="Frage?">. Copy buttons: <button data-copy="element-id">.
  document.addEventListener("click", function (e) {
    var ask = e.target.closest("[data-confirm]");
    if (ask && !window.confirm(ask.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); return; }

    var btn = e.target.closest("[data-copy]");
    if (btn) { copy(btn); return; }

    var link = e.target.closest("a[data-lightbox]");
    if (link && !e.metaKey && !e.ctrlKey && !e.shiftKey && e.button === 0) { e.preventDefault(); openLightbox(link); }
  });

  function copy(btn) {
    var source = document.getElementById(btn.dataset.copy);
    if (!source) return;
    var text = source.textContent;
    var done = function () {
      if (!btn.dataset.label) btn.dataset.label = btn.textContent;
      btn.textContent = "Kopiert ✓";
      btn.classList.add("copied");
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(function () { btn.textContent = btn.dataset.label; btn.classList.remove("copied"); }, 1800);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(source) && done(); });
    } else if (fallbackCopy(source)) {
      done();
    }
  }

  function fallbackCopy(source) {  // plain-HTTP dev setups and old browsers
    var range = document.createRange();
    range.selectNodeContents(source);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    sel.removeAllRanges();
    return ok;
  }

  // Every form: spinner on the pressed button and no double submit. The button is not disabled on purpose:
  // a disabled submitter would drop its name/value (e.g. the status buttons) from the request.
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    var form = e.target;
    if (form.dataset.sent) { e.preventDefault(); return; }
    form.dataset.sent = "1";
    var btn = e.submitter || form.querySelector("[type=submit]");
    if (btn) { btn.setAttribute("aria-busy", "true"); btn.setAttribute("aria-disabled", "true"); }
  });
  // Back/forward cache: a restored page must not keep spinning buttons.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-sent]").forEach(function (f) { delete f.dataset.sent; });
    document.querySelectorAll("[aria-busy=true]").forEach(function (b) { b.removeAttribute("aria-busy"); b.removeAttribute("aria-disabled"); });
  });

  // Settings → KI-Anbieter: show the panel of the provider chosen in the select (key status, key form, how-to,
  // models) and say when the choice isn't saved yet. Provider and model are saved with the settings form.
  function syncProviderPanels() {
    var select = document.querySelector("select[data-provider-select]");
    if (!select) return;
    document.querySelectorAll("[data-provider-panel]").forEach(function (panel) {
      panel.classList.toggle("is-active", panel.dataset.providerPanel === select.value);
    });
    var note = document.querySelector("[data-provider-unsaved]");
    if (note) note.hidden = select.value === select.dataset.saved;
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-provider-select]")) syncProviderPanels();
  });
  document.addEventListener("DOMContentLoaded", syncProviderPanels);
  window.addEventListener("pageshow", syncProviderPanels);  // the browser may restore another selection

  // ---- Einstellungen: background proposal, self-saving switches (begin) ----------------------------------
  // Profil → "Verbesserung vorschlagen" runs in the background (minutes): poll its status, then open the proposal
  // or reload to show the error. Without JS a <noscript> meta refresh reloads the page instead.
  function pollRewrite(el) {
    var failures = 0;
    function tick() {
      fetch(el.dataset.rewritePoll, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok || r.redirected) throw new Error("status " + r.status);  // e.g. logged out: reload shows the login
          return r.json();
        })
        .then(function (job) {
          failures = 0;
          if (job.state === "running") { setTimeout(tick, 3000); return; }
          if (job.url && job.url !== location.pathname + location.search) location.href = job.url;
          else location.reload();
        })
        .catch(function () {
          if (++failures >= 5) location.reload(); else setTimeout(tick, 6000);
        });
    }
    setTimeout(tick, 2500);
  }
  document.addEventListener("DOMContentLoaded", function () {
    var el = document.querySelector("[data-rewrite-poll]");
    if (el) pollRewrite(el);
  });
  // <input data-autosubmit> (Verhalten → "wird genutzt" of a learned rule) saves its form on change; without JS a
  // fallback button (class nojs-only) does that.
  document.addEventListener("change", function (e) {
    var input = e.target.closest ? e.target.closest("[data-autosubmit]") : null;
    if (!input || !input.form) return;
    if (input.form.requestSubmit) input.form.requestSubmit(); else input.form.submit();
  });
  // ---- Einstellungen (end) ----------------------------------------------------------------------------------

  // ---- Hauptseite: ❌/↩️ without reload, "Gesehen" (begin) ------------------------------------------------
  // ❌/↩️ on a card (form.lc-toggle: /dismiss or /restore) is sent with fetch: the server answers with the new card
  // and the page's numbers, the card is swapped in place and stays even in a filtered list (↩️ is the undo).
  // Registered after the form handler above, which already set the spinner and form.dataset.sent; a second press
  // meanwhile is blocked there (defaultPrevented). The fresh card brings a fresh form, so the button works again.
  // Logged out (the answer is the login page) or refused: the form is sent the ordinary way; any other failure
  // (the change may have happened) reloads the page to show the real state.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form.matches("form.lc-toggle") || !window.fetch) return;
    e.preventDefault();
    var item = form.closest("li");
    fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json" },
                         credentials: "same-origin" })
      .then(function (r) {
        if (r.redirected || r.status === 401 || r.status === 403) { form.submit(); return null; }
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) { if (data) swapCard(item, data); })
      .catch(function () { location.reload(); });
  });

  function swapCard(item, data) {
    if (!data.card || !item) throw new Error("no card");
    var hadFocus = item.contains(document.activeElement);
    var tpl = document.createElement("template");
    tpl.innerHTML = data.card.trim();
    var fresh = tpl.content.firstElementChild;
    item.replaceWith(fresh);
    if (hadFocus) {  // keyboard and screen reader users stay on the (new) button, which now says what it does
      var target = fresh.querySelector(".lc-toggle button") || fresh.querySelector("a");
      if (target) target.focus();
    }
    setCounts(data.counts || {});
  }

  // Elements with data-count="all|new|seen|drafted|sent|discarded|<reason>|auto" (filter chips, "Inserate" stat).
  function setCounts(counts) {
    document.querySelectorAll("[data-count]").forEach(function (el) {
      if (counts[el.dataset.count] !== undefined) el.textContent = counts[el.dataset.count];
    });
  }
  function countOf(key) {
    var el = document.querySelector('[data-count="' + key + '"]');
    return el ? parseInt(el.textContent, 10) || 0 : 0;
  }

  // Opening a glowing card ("Neu"): the listing page moves it to "Gesehen" on the server; show that here right away
  // (pill and numbers), so going back (back/forward cache) doesn't show the glow again. Middle click / new tab too.
  function markSeen(e) {
    var card = e.target.closest ? e.target.closest("a.listing-card.is-unseen") : null;
    if (!card || (e.type === "auxclick" && e.button !== 1)) return;
    card.classList.remove("is-unseen");
    var hint = card.querySelector(".lc-unseen");
    if (hint) hint.remove();
    var badge = card.querySelector(".badge.st-new");
    if (badge) { badge.classList.replace("st-new", "st-seen"); badge.textContent = "Gesehen"; }
    setCounts({ "new": Math.max(0, countOf("new") - 1), seen: countOf("seen") + 1 });
  }
  document.addEventListener("click", markSeen);
  document.addEventListener("auxclick", markSeen);
  // ---- Hauptseite (end) -------------------------------------------------------------------------------------

  // Thumbnails: an image that fails to load is hidden, so the placeholder underneath shows.
  function markBroken(img) { img.classList.add("is-broken"); }
  document.addEventListener("error", function (e) {
    var img = e.target;
    if (img && img.tagName === "IMG" && img.hasAttribute("data-fallback")) markBroken(img);
  }, true);
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("img[data-fallback]").forEach(function (img) {
      if (img.complete && img.naturalWidth === 0) markBroken(img);
    });
  });

  // Lightbox for <a data-lightbox href="full image" data-caption="…"> (prev/next within the same .gallery).
  var box, boxImg, boxCaption, boxCount, group = [], index = 0;

  function buildLightbox() {
    box = document.createElement("dialog");
    box.className = "lightbox";
    box.setAttribute("aria-label", "Bildansicht");
    box.innerHTML =
      '<figure><img alt="" referrerpolicy="no-referrer"></figure>' +
      '<div class="lb-caption"></div><span class="lb-count"></span>' +
      '<button type="button" class="lb-prev" aria-label="Vorheriges Bild">‹</button>' +
      '<button type="button" class="lb-next" aria-label="Nächstes Bild">›</button>' +
      '<button type="button" class="lb-close" aria-label="Schließen" autofocus>✕</button>';
    document.body.appendChild(box);
    boxImg = box.querySelector("img");
    boxCaption = box.querySelector(".lb-caption");
    boxCount = box.querySelector(".lb-count");
    box.querySelector(".lb-close").addEventListener("click", function () { box.close(); });
    // Tell the link of the picture shown last (the carousel then shows that one too).
    box.addEventListener("close", function () {
      if (group[index]) group[index].dispatchEvent(new CustomEvent("lightboxclose", { bubbles: true }));
    });
    box.querySelector(".lb-prev").addEventListener("click", function () { show(index - 1); });
    box.querySelector(".lb-next").addEventListener("click", function () { show(index + 1); });
    box.addEventListener("click", function (e) { if (e.target === box || e.target.tagName === "FIGURE") box.close(); });
    box.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") show(index - 1);
      if (e.key === "ArrowRight") show(index + 1);
    });
    var startX = null;
    box.addEventListener("touchstart", function (e) { startX = e.touches[0].clientX; }, { passive: true });
    box.addEventListener("touchend", function (e) {
      if (startX === null) return;
      var dx = e.changedTouches[0].clientX - startX;
      if (Math.abs(dx) > 50) show(index + (dx < 0 ? 1 : -1));
      startX = null;
    });
  }

  function captionOf(link) {
    if (link.dataset.caption) return link.dataset.caption;
    var cap = link.querySelector("figcaption, .gallery-caption");
    if (cap) return cap.textContent.trim();
    var img = link.querySelector("img");
    return img ? img.alt : "";
  }

  function show(i) {
    if (!group.length) return;
    index = (i + group.length) % group.length;
    var link = group[index];
    boxImg.src = link.href;
    boxImg.alt = captionOf(link);
    boxCaption.textContent = captionOf(link);
    boxCount.textContent = group.length > 1 ? (index + 1) + " / " + group.length : "";
    box.querySelector(".lb-prev").hidden = box.querySelector(".lb-next").hidden = group.length < 2;
  }

  function openLightbox(link) {
    if (typeof HTMLDialogElement !== "function") { window.open(link.href, "_blank", "noopener"); return; }
    if (!box) buildLightbox();
    // Group: the link's own .gallery or [data-lightbox-group] (the carousel), or the one named in data-gallery.
    var scope = link.closest(".gallery, [data-lightbox-group]") ||
      (link.dataset.gallery ? document.querySelector(link.dataset.gallery) : null);
    group = scope ? Array.prototype.slice.call(scope.querySelectorAll("a[data-lightbox]")) : [link];
    var i = group.indexOf(link);
    if (i < 0) i = group.map(function (a) { return a.href; }).indexOf(link.href);
    if (i < 0) { group = [link]; i = 0; }
    show(i);
    box.showModal();
  }

  // "Inserat per Link hinzufügen": which part of the pasted text is used. The exposé link stays, everything else
  // (share text around it, "?referrer=…" and the like) is shown greyed out. Same pattern as is24.expose_in_text.
  var EXPOSE = /(^|[^a-z0-9.-])((?:https?:\/\/)?(?:[a-z0-9-]+\.)*immobilienscout24\.de\/expose\/[0-9]{1,15})(?![0-9])/i;

  function exposePreview(field) {
    var out = document.getElementById(field.dataset.exposePreview);
    if (!out) return;
    var text = field.value;
    out.textContent = "";
    if (!text.trim()) { out.hidden = true; return; }
    out.hidden = false;
    var m = EXPOSE.exec(text);
    if (!m) {
      out.classList.add("is-missing");
      out.textContent = "Kein ImmoScout24-Exposé-Link erkannt.";
      return;
    }
    out.classList.remove("is-missing");
    var start = m.index + m[1].length, end = start + m[2].length;
    var part = function (cls, value) {
      if (!value) return;
      var span = document.createElement("span");
      span.className = cls;
      span.textContent = value;
      out.appendChild(span);
    };
    part("label", "Verwendet wird nur der Link: ");
    part("gone", text.slice(0, start).trim() ? text.slice(0, start) : "");
    part("kept", text.slice(start, end));
    part("gone", text.slice(end).trim() ? text.slice(end) : "");
  }

  document.addEventListener("input", function (e) {
    if (e.target.matches && e.target.matches("[data-expose-preview]")) exposePreview(e.target);
  });
  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-expose-preview]"), exposePreview);
  });

  // Overlay after "Inserat öffnen" (<a data-asks-after-open>): when the person comes back to this page from the
  // listing (other tab or app), <dialog data-after-open> opens - every time the link was used. The click time sits in
  // sessionStorage, so it also works when the browser reloaded the page meanwhile.
  document.addEventListener("DOMContentLoaded", function () {
    var dlg = document.querySelector("dialog[data-after-open]");
    if (!dlg || typeof dlg.showModal !== "function") return;
    var key = "immobot-opened-" + dlg.dataset.afterOpen;
    var openedAt = function (value) {
      try {
        if (value === undefined) return Number(sessionStorage.getItem(key)) || 0;
        if (value) sessionStorage.setItem(key, String(value)); else sessionStorage.removeItem(key);
      } catch (err) { /* no storage: nothing to remember */ }
      return 0;
    };
    document.querySelectorAll("[data-asks-after-open]").forEach(function (link) {
      link.addEventListener("click", function () { openedAt(Date.now()); });
    });
    var back = function () {
      var since = openedAt();
      if (!since || document.visibilityState !== "visible" || Date.now() - since < 800) return;
      openedAt(null);
      window.immobotShowModal(dlg);
    };
    document.addEventListener("visibilitychange", back);
    window.addEventListener("focus", back);
    window.addEventListener("pageshow", back);
    dlg.querySelectorAll("[data-dialog-close]").forEach(function (btn) {
      btn.addEventListener("click", function () { dlg.close(); });
    });
    dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
    back();
  });

  // ---- Listing page: picture carousel (begin) ----------------------------------------------------------------
  // <div data-carousel> around a scroll-snap strip (.cr-track) of <a data-lightbox> slides (listing.html). The strip
  // scrolls by itself (touch, trackpad, without JS too); this adds the arrow buttons, ←/→/Home/End on a focused
  // picture, one tab stop for the whole strip and loading the next picture ahead. A click opens the lightbox with the
  // whole set (group: data-lightbox-group); after it the carousel shows the picture seen last.
  // The filmstrip above (<nav data-carousel-thumbs="carousel id">, links to #slide-N): a tap shows that picture
  // instead of jumping to the anchor; the current one gets aria-current and stays in view; ←/→/Home/End there too.
  function setupCarousel(root) {
    var track = root.querySelector(".cr-track");
    var slides = Array.prototype.slice.call(track.querySelectorAll(".cr-slide"));
    var prev = root.querySelector("[data-carousel-prev]"), next = root.querySelector("[data-carousel-next]");
    var strip = root.id ? document.querySelector('[data-carousel-thumbs="' + root.id + '"]') : null;
    var thumbs = strip ? Array.prototype.slice.call(strip.querySelectorAll(".cr-thumb")) : [];
    var current = -1;
    if (!slides.length) return;

    function clamp(i) { return Math.max(0, Math.min(slides.length - 1, i)); }
    function update() {
      var i = clamp(Math.round(track.scrollLeft / (track.clientWidth || 1)));
      if (i === current) return;
      current = i;
      slides.forEach(function (slide, k) { slide.tabIndex = k === i ? 0 : -1; });
      var ahead = slides[i + 1] && slides[i + 1].querySelector("img[loading=lazy]");
      if (ahead) ahead.loading = "eager";
      if (prev) prev.hidden = i === 0;
      if (next) next.hidden = i === slides.length - 1;
      thumbs.forEach(function (thumb, k) {
        thumb.tabIndex = k === i ? 0 : -1;
        if (k === i) thumb.setAttribute("aria-current", "true"); else thumb.removeAttribute("aria-current");
      });
      var thumb = thumbs[i];
      // Keep the current one in view (centered) without scrolling the page, as scrollIntoView would.
      if (thumb && (thumb.offsetLeft < strip.scrollLeft ||
                    thumb.offsetLeft + thumb.offsetWidth > strip.scrollLeft + strip.clientWidth)) {
        strip.scrollTo({ left: thumb.offsetLeft - (strip.clientWidth - thumb.offsetWidth) / 2 });
      }
    }
    function go(i, opts) {
      i = clamp(i);
      track.scrollTo({ left: i * track.clientWidth, behavior: opts && opts.instant ? "instant" : "auto" });  // auto: CSS (smooth)
      if (opts && opts.focus) slides[i].focus({ preventScroll: true });
    }
    function step(button, delta) {
      var target = clamp(current + delta);
      // The button disappears at either end: then the picture keeps the focus.
      go(target, { focus: target === 0 || target === slides.length - 1 ? document.activeElement === button : false });
    }

    track.addEventListener("scroll", update, { passive: true });
    if (prev) prev.addEventListener("click", function () { step(prev, -1); });
    if (next) next.addEventListener("click", function () { step(next, 1); });
    root.addEventListener("keydown", function (e) {
      if (!e.target.closest(".cr-slide") || e.altKey || e.ctrlKey || e.metaKey) return;
      var to = { ArrowLeft: current - 1, ArrowRight: current + 1, Home: 0, End: slides.length - 1 }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      go(to, { focus: true });
    });
    if (strip) {
      strip.addEventListener("click", function (e) {
        var thumb = e.target.closest(".cr-thumb");
        if (!thumb || e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        go(thumbs.indexOf(thumb));  // smooth, instant with prefers-reduced-motion (CSS scroll-behavior)
      });
      strip.addEventListener("keydown", function (e) {
        if (!e.target.closest(".cr-thumb") || e.altKey || e.ctrlKey || e.metaKey) return;
        var to = { ArrowLeft: current - 1, ArrowRight: current + 1, Home: 0, End: thumbs.length - 1 }[e.key];
        if (to === undefined) return;
        e.preventDefault();
        go(to);
        thumbs[clamp(to)].focus({ preventScroll: true });
      });
    }
    root.addEventListener("lightboxclose", function (e) {
      var i = slides.indexOf(e.target);
      if (i >= 0) go(i, { instant: true, focus: root.contains(document.activeElement) });
    });
    if (slides.length > 1) [prev, next].forEach(function (b) { if (b) b.hidden = false; });
    update();
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-carousel]").forEach(setupCarousel);
  });
  // ---- Listing page: picture carousel (end) ------------------------------------------------------------------

  // ---- Listing page: map (begin) -----------------------------------------------------------------------------
  // <details data-map-src> ("🗺 Karte", listing.html): Google's iframe goes in only when it is opened, so loading the
  // page sends nothing to Google. "toggle" doesn't bubble: caught in the capture phase.
  document.addEventListener("toggle", function (e) {
    var details = e.target;
    if (!details.open || !details.matches || !details.matches("details[data-map-src]") || details.querySelector("iframe")) return;
    var frame = details.querySelector(".map-frame");
    var iframe = document.createElement("iframe");
    iframe.title = details.dataset.mapTitle || "Karte";
    iframe.setAttribute("referrerpolicy", "no-referrer");
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox");
    iframe.setAttribute("allowfullscreen", "");
    iframe.src = details.dataset.mapSrc;
    frame.textContent = "";
    frame.appendChild(iframe);
  }, true);
  // ---- Listing page: map (end) -------------------------------------------------------------------------------
})();
