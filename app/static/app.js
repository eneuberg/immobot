// immobot dashboard: small progressive enhancements. Everything works without JS, this only adds comfort.
// Loaded synchronously in <head>; all handlers are delegated on document, so they also cover later elements.
(function () {
  "use strict";

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
    var scope = link.closest(".gallery");
    group = scope ? Array.prototype.slice.call(scope.querySelectorAll("a[data-lightbox]")) : [link];
    show(group.indexOf(link));
    box.showModal();
  }
})();
