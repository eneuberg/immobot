// immobot draft editor (/editor/{id}): comfort on top of plain forms, which work without JS.
// Autosize, "Kopieren" for the text field, autosave of the text (with a warning on leaving while it isn't saved),
// "Claude schreibt …" while revising, a live word diff against Claude's original and polling while Claude looks for
// rules. Loaded with defer, only on this page.
(function () {
  "use strict";

  var form = document.querySelector("form[data-editor]");
  var text = document.getElementById("ed-text");
  var leaving = false;

  // --- autosize -----------------------------------------------------------------------------
  function autosize(field) {
    field.style.height = "auto";
    field.style.height = (field.scrollHeight + 2) + "px";
  }
  document.querySelectorAll("textarea[data-autosize]").forEach(function (field) {
    autosize(field);
    field.addEventListener("input", function () { autosize(field); });
  });
  window.addEventListener("resize", function () {
    document.querySelectorAll("textarea[data-autosize]").forEach(autosize);
  });

  // --- copy the text field (app.js copies elements' text, a textarea needs its current value) -------
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-field]");
    if (!btn) return;
    var field = document.getElementById(btn.dataset.copyField);
    if (!field) return;
    var done = function () {
      if (!btn.dataset.label) btn.dataset.label = btn.textContent;
      btn.textContent = "Kopiert ✓";
      btn.classList.add("copied");
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(function () { btn.textContent = btn.dataset.label; btn.classList.remove("copied"); }, 1800);
    };
    var fallback = function () {
      var start = field.selectionStart, end = field.selectionEnd;
      field.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      field.setSelectionRange(start, end);
      if (ok) done();
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(field.value).then(done, fallback);
    } else {
      fallback();
    }
  });

  // --- autosave ------------------------------------------------------------------------------------
  // The text goes to the form's action (/save) with "Accept: application/json": 1.5 s after typing stops, when the
  // field loses focus and when the tab is hidden. "Überarbeiten lassen" and "Fertig" post the text themselves.
  // Answers: {ok: true}, or {ok: false, error, stale}; 401/403/409 mean this page can't save anymore (session over,
  // another tab started a new round or finished): stop, say so, and for a stale page offer "Neu laden", which posts
  // the form normally - the server then shows the text that wasn't saved.
  var saveState = document.querySelector("[data-save-state]");
  var saveReload = document.querySelector("[data-save-reload]");
  var savedValue = text ? text.defaultValue : "";  // what the server has
  var saveTimer = null, saving = false, saveStopped = false;
  var SAVE_DELAY = 1500, RETRY_DELAY = 10000;
  var SAVE_TEXTS = { saving: "Speichert …", saved: "Gespeichert ✓", error: "Nicht gespeichert – Verbindung?" };

  function dirty() { return !!text && text.value !== savedValue; }

  function showSave(state, message) {
    if (!saveState) return;
    saveState.textContent = message || SAVE_TEXTS[state] || "";
    saveState.dataset.state = state;
  }

  function scheduleSave(delay) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, delay);
  }

  function save(keepalive) {
    clearTimeout(saveTimer);
    if (!form || !text || saveStopped || leaving) return;
    if (!dirty()) {  // nothing new (e.g. typed and deleted again)
      if (!saving && saveState && saveState.dataset.state) showSave("saved");
      return;
    }
    if (saving) { scheduleSave(SAVE_DELAY); return; }  // one request at a time, the next follows
    var value = text.value;
    var data = new FormData();
    data.append("token", form.elements.token.value);
    data.append("session_id", form.elements.session_id.value);
    data.append("text", value);
    saving = true;
    showSave("saving");
    fetch(form.action, {
      method: "POST", body: data, credentials: "same-origin", keepalive: keepalive === true,
      headers: { Accept: "application/json" }
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) { return { status: r.status, body: body }; });
      })
      .then(function (res) {
        saving = false;
        if (res.body.ok) {
          savedValue = value;
          if (dirty()) scheduleSave(SAVE_DELAY); else showSave("saved");
        } else if (res.status === 401 || res.status === 403 || res.status === 409) {
          saveStopped = true;
          showSave("error", "Nicht gespeichert: " + (res.body.error || "Seite veraltet – bitte neu laden."));
          if (saveReload && res.body.stale) saveReload.hidden = false;
        } else if (res.body.error) {  // e.g. too long: try again after the next change
          showSave("error", "Nicht gespeichert: " + res.body.error);
        } else {
          showSave("error");
          scheduleSave(RETRY_DELAY);
        }
      })
      .catch(function () { saving = false; showSave("error"); scheduleSave(RETRY_DELAY); });
  }

  if (form && text) {
    text.addEventListener("input", function () {
      if (saveStopped) return;
      showSave("saving");
      scheduleSave(SAVE_DELAY);
    });
    text.addEventListener("blur", function () { save(); });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") save(true);
    });
  }
  window.addEventListener("beforeunload", function (e) {
    if (leaving || !dirty()) return;  // only while a save is pending or failed
    e.preventDefault();
    e.returnValue = "";
  });

  // Submitting saves the text: no warning, no autosave on top. "Claude schreibt …" on buttons with data-busy-label (app.js already
  // blocks a second submit and shows the spinner); the text stays readable but can't change meanwhile.
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    leaving = true;
    clearTimeout(saveTimer);
    var btn = e.submitter;
    if (btn && btn.dataset.busyLabel) {
      btn.textContent = btn.dataset.busyLabel;
      e.target.querySelectorAll("textarea").forEach(function (field) { field.readOnly = true; });
    }
  });
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    leaving = false;
    if (dirty()) scheduleSave(SAVE_DELAY);
    document.querySelectorAll("textarea").forEach(function (field) { field.readOnly = false; });
  });

  // --- live word diff against Claude's original (same look as textdiff.word_diff + trim_equal) -----------------
  var diffBox = document.querySelector("[data-diff]");
  var diffState = document.querySelector("[data-diff-state]");
  var TOKEN = /[\p{L}\p{N}_]+|[^\p{L}\p{N}_\s]|\s+/gu;
  var KEEP = 160;
  var MAX_CELLS = 2500000;  // beyond that (very long texts) the server's diff after saving has to do

  function tokens(value) { return value.match(TOKEN) || []; }

  function runs(ops) {  // [[op, text], ...] with neighbours of the same op joined
    var out = [];
    ops.forEach(function (op) {
      if (!op[1]) return;
      var last = out[out.length - 1];
      if (last && last[0] === op[0]) last[1] += op[1]; else out.push([op[0], op[1]]);
    });
    return out;
  }

  function wordDiff(oldText, newText) {
    var a = tokens(oldText), b = tokens(newText);
    var start = 0;
    while (start < a.length && start < b.length && a[start] === b[start]) start++;
    var endA = a.length, endB = b.length;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
    var A = a.slice(start, endA), B = b.slice(start, endB), n = A.length, m = B.length;
    if (n * m > MAX_CELLS) return null;
    var w = m + 1, L = new Uint32Array((n + 1) * w), i, j;
    for (i = n - 1; i >= 0; i--) {
      for (j = m - 1; j >= 0; j--) {
        L[i * w + j] = A[i] === B[j] ? L[(i + 1) * w + j + 1] + 1 : Math.max(L[(i + 1) * w + j], L[i * w + j + 1]);
      }
    }
    var raw = [["equal", a.slice(0, start).join("")]];
    i = 0; j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { raw.push(["equal", A[i]]); i++; j++; }
      else if (L[(i + 1) * w + j] >= L[i * w + j + 1]) { raw.push(["delete", A[i]]); i++; }
      else { raw.push(["insert", B[j]]); j++; }
    }
    for (; i < n; i++) raw.push(["delete", A[i]]);
    for (; j < m; j++) raw.push(["insert", B[j]]);
    raw.push(["equal", a.slice(endA).join("")]);
    raw = runs(raw);

    // Like the server: one deletion + one insertion per change; spaces between two changes belong to both.
    var segments = [], del = "", ins = "";
    function flush() {
      if (del) segments.push(["delete", del]);
      if (ins) segments.push(["insert", ins]);
      del = ins = "";
    }
    raw.forEach(function (seg, k) {
      var op = seg[0], value = seg[1];
      if (op === "delete") del += value;
      else if (op === "insert") ins += value;
      else if ((del || ins) && k + 1 < raw.length && raw[k + 1][0] !== "equal" && /^\s+$/.test(value) && value.indexOf("\n") < 0) {
        del += value; ins += value;
      } else { flush(); segments.push(["equal", value]); }
    });
    flush();
    return runs(segments);
  }

  function trim(segments) {
    var changed = segments.map(function (s, k) { return s[0] !== "equal" ? k : -1; }).filter(function (k) { return k >= 0; });
    if (!changed.length) return segments;
    var first = changed[0], last = changed[changed.length - 1];
    return segments.map(function (s, k) {
      var value = s[1];
      if (s[0] === "equal" && value.length > KEEP) {
        if (k < first) value = "…" + value.slice(-KEEP);
        else if (k > last) value = value.slice(0, KEEP) + "…";
        else if (value.length > 2 * KEEP) value = value.slice(0, KEEP) + " … " + value.slice(-KEEP);
      }
      return [s[0], value];
    });
  }

  function renderDiff() {
    if (!diffBox || !text) return;
    var segments = wordDiff(diffBox.dataset.original || "", text.value);
    if (segments === null) return;  // too long to compare live
    var changed = segments.some(function (s) { return s[0] !== "equal"; });
    diffBox.textContent = "";
    if (changed) {
      var box = document.createElement("div");
      box.className = "diff";
      trim(segments).forEach(function (s) {
        if (s[0] === "equal") { box.appendChild(document.createTextNode(s[1])); return; }
        var el = document.createElement(s[0] === "insert" ? "ins" : "del");
        el.textContent = s[1];
        box.appendChild(el);
      });
      diffBox.appendChild(box);
    } else {
      var p = document.createElement("p");
      p.className = "muted ed-nodiff";
      p.textContent = "Noch keine Änderungen – der Text ist Claudes Entwurf.";
      diffBox.appendChild(p);
    }
    if (diffState) {
      diffState.textContent = changed ? "geändert" : "keine";
      diffState.classList.toggle("warn", changed);
    }
  }

  if (diffBox && text) {
    var timer = null, details = diffBox.closest("details");
    text.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(renderDiff, details && !details.open ? 900 : 250);
    });
    if (details) details.addEventListener("toggle", function () { if (details.open) renderDiff(); });
  }

  // --- waiting for Claude's rule proposals ------------------------------------------------------------
  var poll = document.querySelector("[data-rules-poll]");
  if (poll) {
    var tries = 0;
    var check = function () {
      tries += 1;
      fetch(poll.dataset.rulesPoll, { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : { state: "reload" }; })
        .then(function (data) {
          if (data.state === "running" && tries < 240) setTimeout(check, 2500);
          else window.location.reload();
        })
        .catch(function () { if (tries < 240) setTimeout(check, 5000); });
    };
    setTimeout(check, 2000);
  }

  if (form && text && window.location.hash === "#text" && window.matchMedia("(min-width: 768px)").matches) {
    text.focus({ preventScroll: true });
  }
})();
