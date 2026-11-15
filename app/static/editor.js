// immobot draft editor (/editor/{id}): comfort on top of plain forms, which work without JS.
// Autosize, "Kopieren" for the text field, "Kopieren & Inserat öffnen", autosave of the text (with a warning on leaving while it isn't saved),
// a live word diff against the newest version ("Verlauf"), the ad in a bottom drawer, polling while Claude looks for rules,
// and the progress bar while Claude writes the first draft, revises the letter or answers a question. Loaded with defer.
(function () {
  "use strict";

  var form = document.querySelector("form[data-editor]");
  // The page's own reloads are no return to it (app.js: no "Uninteressant?" for them); false: the form stayed.
  var ownNavigation = function (on) { if (window.immobotOwnNavigation) window.immobotOwnNavigation(on); };
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

  // --- "Kopieren & Inserat öffnen" ---------------------------------------------------------------------
  // The one action of an open text: copy it and open the ad in a new tab - both only work during the tap itself -,
  // then the button's form posts /finish as usual (submitInPlace below), with copied=1 when copying worked, so the
  // finished page says what happened. Without JS the button only finishes; the next page has the text and the link.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-open]");
    if (!btn || !text || btn.form !== form || (form && form.dataset.sent)) return;
    var ok = false;
    if (navigator.clipboard && window.isSecureContext) {
      try { navigator.clipboard.writeText(text.value); ok = true; } catch (err) { ok = false; }
    }
    if (!ok) {
      var start = text.selectionStart, end = text.selectionEnd;
      text.select();
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      text.setSelectionRange(start, end);
    }
    var flag = form.querySelector("input[name=copied]");
    if (!flag) {
      flag = document.createElement("input");
      flag.type = "hidden";
      flag.name = "copied";
      form.appendChild(flag);
    }
    flag.value = ok ? "1" : "";
    btn.dataset.busyLabel = ok ? "Kopiert ✓" : "Speichert …";
    if (btn.dataset.go) {
      var tab = window.open(btn.dataset.go, "_blank");
      if (tab) { try { tab.opener = null; } catch (err) { /* other origin already */ } }
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
      btn.dataset.idleLabel = btn.textContent;
      btn.textContent = btn.dataset.busyLabel;
      e.target.querySelectorAll("textarea").forEach(function (field) { field.readOnly = true; });
    }
    submitInPlace(e);
  });

  // Editor forms are sent with fetch and the page is *replaced* by the result (location.replace), so every action
  // keeps one entry in the browser history: one "back" returns to Telegram. The server answers a redirect as
  // {"location": …} (header X-Editor-Nav: replace, see editor.editor_headers); any other answer (an error page) is
  // shown as it is. Without fetch/FormData the form is sent normally.
  function submitInPlace(e) {
    var form = e.target, btn = e.submitter;
    var method = btn && btn.hasAttribute("formmethod") ? btn.formMethod : form.method;
    if (!window.fetch || !window.FormData || (method || "").toLowerCase() !== "post") return;
    // the pressed button's formaction ("Fertig", "Überarbeiten lassen", …) wins over the form's action
    var action = new URL(btn && btn.hasAttribute("formaction") ? btn.formAction : form.action, location.href);
    if (action.origin !== location.origin || action.pathname.indexOf("/editor/") !== 0) return;
    e.preventDefault();
    var data;
    try { data = new FormData(form, e.submitter || undefined); } catch (err) { data = new FormData(form); }
    if (e.submitter && e.submitter.name && !data.has(e.submitter.name)) data.append(e.submitter.name, e.submitter.value);
    fetch(action.href, { method: "POST", body: data, credentials: "same-origin", headers: { "X-Editor-Nav": "replace" } })
      .then(function (res) {
        var type = res.headers.get("content-type") || "";
        if (type.indexOf("application/json") === 0) {
          return res.json().then(function (body) {
            if (!body || typeof body.location !== "string" || body.location.charAt(0) !== "/") { location.reload(); return; }
            var target = new URL(body.location, location.href);
            // Same path and query (only "#…" differs): replace() would just jump to the anchor without loading
            // the page, and the buttons would keep spinning. So set the address, then reload.
            if (target.pathname + target.search === location.pathname + location.search) {
              history.replaceState(history.state, "", target.href);
              location.reload();
            } else {
              location.replace(target.href);
            }
          });
        }
        return res.text().then(function (html) { document.open(); document.write(html); document.close(); });
      })
      .catch(function () {
        leaving = false;
        ownNavigation(false);
        delete form.dataset.sent;
        form.querySelectorAll("[aria-busy=true]").forEach(function (b) { b.removeAttribute("aria-busy"); b.removeAttribute("aria-disabled"); });
        form.querySelectorAll("textarea").forEach(function (field) { field.readOnly = false; });
        form.querySelectorAll("[data-idle-label]").forEach(function (b) { b.textContent = b.dataset.idleLabel; });
        window.alert("Keine Verbindung – bitte noch einmal versuchen.");
      });
  }
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    leaving = false;
    if (dirty()) scheduleSave(SAVE_DELAY);
    document.querySelectorAll("textarea").forEach(function (field) { field.readOnly = false; });
  });

  // --- live word diff against the newest version ("Verlauf": "Du – jetzt"; same look as textdiff.word_diff + trim_equal)
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
      p.textContent = "Keine Änderungen seit der letzten Fassung.";
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
          else { ownNavigation(true); window.location.reload(); }
        })
        .catch(function () { if (tries < 240) setTimeout(check, 5000); });
    };
    setTimeout(check, 2000);
  }

  // --- the progress bar (the first draft, a revision, a question) ------------------------------------------------
  // Runs to ~93 % at the expected time (the mean of the last ones), then creeps on; "Dauert länger als gewöhnlich"
  // comes slowAfter ms after that. Reduced motion: no smooth fill, the seconds as text instead.
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var SLOW_TEXT = "Dauert länger als gewöhnlich – bitte noch einen Moment Geduld.";
  function progressBar(bar, clock, note, slowBox) {
    var fill = bar.querySelector(".draftbar-fill");
    var self = { expected: 40000, slowAfter: 15000, t0: null, frame: null, running: false };
    var progress = function (ms) {  // percent of the bar for ms since the start
      if (ms <= self.expected) return 93 * (1 - Math.pow(1 - ms / self.expected, 1.5));
      return 93 + 5 * (1 - Math.exp(-(ms - self.expected) / 30000));  // never quite 100 before it is done
    };
    var setFill = function (percent) {
      fill.style.transform = "scaleX(" + (percent / 100).toFixed(4) + ")";
      bar.setAttribute("aria-valuenow", String(Math.round(percent)));
    };
    var stop = function () {
      if (self.frame !== null) { if (reduce) clearTimeout(self.frame); else cancelAnimationFrame(self.frame); self.frame = null; }
    };
    var tick = function () {
      self.frame = null;
      if (!self.running) return;
      var ms = performance.now() - self.t0;
      setFill(progress(ms));
      clock.textContent = reduce || ms > self.expected ? "seit " + Math.floor(ms / 1000) + " s" : "≈ " + Math.max(1, Math.ceil((self.expected - ms) / 1000)) + " s";
      if (ms > self.expected + self.slowAfter && !slowBox.textContent) slowBox.textContent = SLOW_TEXT;
      self.frame = reduce ? setTimeout(tick, 1000) : requestAnimationFrame(tick);
    };
    // data: {elapsed_ms, expected_ms, slow_after_ms} from the server; follows the server's clock
    self.run = function (data) {
      if (data.expected_ms) { self.expected = data.expected_ms; note.textContent = "Dauert meist etwa " + Math.round(self.expected / 1000) + " s."; }
      if (data.slow_after_ms) self.slowAfter = data.slow_after_ms;
      var since = performance.now() - (data.elapsed_ms || 0);
      // follows the server's clock - not after a restart, where the job started anew but she has waited since
      if (!self.running || self.t0 === null || (!data.keep_clock && Math.abs(since - self.t0) > 3000)) self.t0 = since;
      if (!self.running) {
        self.running = true;
        bar.classList.remove("is-error", "is-done");
        bar.hidden = false;
        slowBox.textContent = "";
        setFill(0);
      }
      if (self.frame === null) tick();
    };
    self.finish = function (then) {  // a short run to the end, then `then`
      self.running = false;
      stop();
      clock.textContent = "";
      slowBox.textContent = "";
      if (reduce) { setFill(100); then(); return; }
      bar.classList.add("is-done");
      bar.getBoundingClientRect();  // the current width first, so the run to 100 % is animated
      setFill(100);
      setTimeout(then, 500);
    };
    self.fail = function () {
      self.running = false;
      stop();
      bar.classList.add("is-error");
      clock.textContent = "";
      slowBox.textContent = "";
    };
    return self;
  }

  // Polls a status URL every second while `isRunning()`; a tab in the background stops and checks again when it
  // comes back. apply(data) gets {state: …} (403: "denied").
  function poller(url, isRunning, apply) {
    var timer = null, failures = 0;
    var self = {};
    self.schedule = function (delay) {
      clearTimeout(timer);
      if (document.hidden || !isRunning()) return;  // visibilitychange polls again
      timer = setTimeout(self.poll, delay);
    };
    self.poll = function () {
      fetch(url, { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.status === 403 ? { state: "denied" } : (r.ok ? r.json() : Promise.reject(r.status)); })
        .then(function (data) { failures = 0; apply(data); })
        .catch(function () { failures += 1; self.schedule(Math.min(5000, 1000 + 1000 * failures)); });
    };
    self.cancel = function () { clearTimeout(timer); };
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && isRunning()) { clearTimeout(timer); self.poll(); }
    });
    window.addEventListener("pageshow", function (e) { if (e.persisted && isRunning()) self.poll(); });
    return self;
  }

  // --- the first draft: progress bar while Claude writes it in the background (drafting.py) ------------------------
  // "idle": start it right away (POST /draft with the form token, as the button would). "running": poll
  // /draft/status every second. Done: a short run to the end, then the page with the letter. Error: the bar stops,
  // the text and "Nochmal versuchen" show.
  var job = document.querySelector("[data-draft-job]");
  if (job) {
    var title = job.querySelector("[data-draft-title]");
    var errorBox = job.querySelector("[data-draft-error]");
    var startForm = job.closest("form[data-draft-start]");  // around both columns: the level and the line go with it
    var startButton = startForm.querySelector("[type=submit]");
    var draftBar = progressBar(job.querySelector("[data-draft-bar]"), job.querySelector("[data-draft-clock]"),
                               job.querySelector("[data-draft-note]"), job.querySelector("[data-draft-slow]"));
    draftBar.expected = Number(job.dataset.expectedMs) || 40000;
    draftBar.slowAfter = Number(job.dataset.slowMs) || 15000;
    // A restart of the server (a deploy) loses the job (jobs live in memory): the status then says "idle" from another
    // process (boot). The draft is simply asked for again - the bar keeps running, no error. The same process saying
    // "idle" gets one more try, as before; only after that it is an error.
    var restarts = 0, resumed = 0, state = job.dataset.draftJob, knownBoot = job.dataset.boot || null;
    var draftPoll = poller(job.dataset.statusUrl, function () { return state === "running"; }, function (data) { apply(data); });

    var running = function (data) {
      if (data.boot) knownBoot = data.boot;
      if (state !== "running") {
        state = job.dataset.draftJob = "running";
        title.textContent = "Claude schreibt dein Anschreiben …";
        errorBox.hidden = true;
      }
      draftBar.run(resumed ? Object.assign({}, data, { keep_clock: true }) : data);
      draftPoll.schedule(1000);
    };
    var done = function () {
      state = "done";
      draftPoll.cancel();
      title.textContent = "Fertig – dein Anschreiben kommt …";
      draftBar.finish(function () { ownNavigation(true); window.location.replace(job.dataset.doneUrl); });
    };
    var failed = function (message) {
      state = job.dataset.draftJob = "error";
      draftPoll.cancel();
      draftBar.fail();
      title.textContent = "Claude konnte nicht schreiben";
      errorBox.textContent = message || "Unerwarteter Fehler – bitte nochmal versuchen.";
      errorBox.hidden = false;
      startButton.textContent = "Nochmal versuchen";
      startButton.removeAttribute("aria-busy");
      startButton.removeAttribute("aria-disabled");
      delete startForm.dataset.sent;
    };
    var apply = function (data) {
      if (state === "done") return;
      if (data.state === "running") running(data);
      else if (data.state === "done") done();
      else if (data.state === "error") failed(data.error);
      else if (data.state === "denied") failed(data.error || "Sitzung abgelaufen – bitte die Seite neu laden.");
      else if (data.state === "idle" && (!knownBoot || data.boot !== knownBoot) && resumed < 2) {
        resumed += 1;  // the server restarted: the same request again, quietly
        knownBoot = data.boot || null;
        start(true);
      } else if (data.state === "idle" && restarts < 1) { restarts += 1; start(true); }
      else failed("Claude hat aufgehört zu schreiben – bitte nochmal versuchen.");
    };
    // resume: the bar goes on (a restart). Without an answer (the server is just restarting) the status is asked
    // until it answers; that decides.
    var start = function (resume) {
      if (!resume) running({ elapsed_ms: 0 });
      draftPoll.cancel();  // the answer to the start says how it stands
      fetch(startForm.action, { method: "POST", body: new FormData(startForm), credentials: "same-origin",
                                headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok || r.status < 500 ? r.json() : Promise.reject(r.status); })
        .then(apply)
        .catch(function () { knownBoot = null; draftPoll.schedule(2000); });
    };

    startForm.addEventListener("submit", function (e) { e.preventDefault(); restarts = 0; resumed = 0; start(); });
    if (state === "idle") start();
    else if (state === "running") running({ elapsed_ms: Number(job.dataset.elapsedMs) || 0 });
  }

  // --- revisions and "Nur fragen" in the background (drafting.start_editor_job) -------------------------------------
  // Buttons with data-job post the form as JSON (the server saves the hand edits, starts the job and answers with
  // its status); the bar under the buttons follows /job/status. A revision makes the letter read-only meanwhile and
  // replaces the page with the result when it is done (the review with its diff, as before); a question leaves the
  // letter alone and puts the answer into the box under the card, the question field empties (a question is no
  // critique: "Fertig" must not keep it as a note). Errors: the bar turns red, the buttons work again.
  var panel = document.querySelector("[data-editor-job]");
  if (panel && form) {
    var jobTitle = panel.querySelector("[data-job-title]");
    var jobError = panel.querySelector("[data-job-error]");
    var badge = document.querySelector("[data-revising-badge]");
    var critiqueField = document.getElementById("ed-critique");
    var answerBox = document.querySelector("[data-answer]");
    var askHint = document.querySelector("[data-ask-hint]");
    var jobBar = progressBar(panel.querySelector("[data-job-bar]"), panel.querySelector("[data-job-clock]"),
                             panel.querySelector("[data-job-note]"), panel.querySelector("[data-job-slow]"));
    var jobState = panel.dataset.editorJob, jobKind = panel.dataset.jobKind || "custom";
    var calmBox = panel.querySelector("[data-job-calm]");
    // After a restart of the server (another boot, the job lost): a revision that had finished already (the newest
    // revision changed: revision_mark) just shows; otherwise the request is sent again as it was - only when this
    // page still has it (the form data of the press, `request`), else a calm note to send it again.
    var knownBoot = panel.dataset.boot || null, mark = panel.dataset.revisionMark || "0", request = null, resumed = 0;
    var DONE_MESSAGES = { custom: "revised", shorter: "shorter", formal: "formal", regenerate: "regenerate" };
    var jobPoll = poller(panel.dataset.statusUrl, function () { return jobState === "running"; }, function (data) { jobApply(data); });
    var TITLES = { ask: ["Claude beantwortet deine Frage …", "Claude konnte nicht antworten"],
                   other: ["Claude überarbeitet deinen Text …", "Claude konnte nicht überarbeiten"] };
    var titles = function () { return TITLES[jobKind === "ask" ? "ask" : "other"]; };
    var lockText = function (on) {
      if (!text) return;
      text.readOnly = on;
      if (on) text.setAttribute("aria-describedby", "ed-job-title"); else text.removeAttribute("aria-describedby");
      if (badge) badge.hidden = !on;
    };
    var release = function () {  // the form's buttons work again (app.js marked it as sent)
      delete form.dataset.sent;
      form.querySelectorAll("[aria-busy=true]").forEach(function (b) { b.removeAttribute("aria-busy"); b.removeAttribute("aria-disabled"); });
      form.querySelectorAll("[data-idle-label]").forEach(function (b) { b.textContent = b.dataset.idleLabel; delete b.dataset.idleLabel; });
      leaving = false;
    };
    var jobRunning = function (data) {
      if (data.kind) jobKind = data.kind;
      if (data.boot) knownBoot = data.boot;
      if (resumed) data = Object.assign({}, data, { keep_clock: true });
      if (calmBox) calmBox.hidden = true;
      if (jobState !== "running") {
        jobState = panel.dataset.editorJob = "running";
        jobError.hidden = true;
      }
      panel.dataset.jobKind = jobKind;
      panel.hidden = false;
      jobTitle.textContent = titles()[0];
      form.dataset.sent = "1";  // no second action meanwhile
      lockText(jobKind !== "ask");
      jobBar.run(data);
      jobPoll.schedule(1000);
    };
    var jobFailed = function (message) {
      jobState = panel.dataset.editorJob = "error";
      jobPoll.cancel();
      jobBar.fail();
      panel.hidden = false;
      jobTitle.textContent = titles()[1];
      jobError.textContent = message || "Unerwarteter Fehler – bitte nochmal versuchen.";
      jobError.hidden = false;
      lockText(false);
      release();
    };
    var showAnswer = function (data) {
      if (!answerBox) return;
      var question = critiqueField ? critiqueField.value.trim() : "";
      answerBox.querySelector("[data-answer-q]").textContent = question || answerBox.querySelector("[data-answer-q]").textContent;
      answerBox.querySelector("[data-answer-a]").innerHTML = data.answer_html || "";  // server-rendered, escaped markdown
      answerBox.hidden = false;
      if (critiqueField) { critiqueField.value = ""; autosize(critiqueField); }
      answerBox.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
    };
    var jobDone = function (data) {
      jobState = "done";
      jobPoll.cancel();
      if (jobKind === "ask") {
        jobBar.finish(function () {
          panel.hidden = true;
          panel.dataset.editorJob = "idle";
          showAnswer(data);
          release();
        });
        return;
      }
      jobTitle.textContent = "Fertig – dein Text kommt …";
      leaving = true;
      jobBar.finish(function () { ownNavigation(true); window.location.replace(data.done_url || location.pathname + "#text"); });
    };
    var jobCalm = function (message) {  // no error of hers or Claude's: just send it again
      jobState = panel.dataset.editorJob = "error";
      jobPoll.cancel();
      jobBar.fail();
      panel.querySelector("[data-job-bar]").hidden = true;
      jobTitle.textContent = jobKind === "ask" ? "Keine Antwort bekommen" : "Nicht überarbeitet";
      jobError.hidden = true;
      if (calmBox) { calmBox.textContent = message; calmBox.hidden = false; }
      lockText(false);
      release();
    };
    var jobIdle = function (data) {
      var restarted = !knownBoot || (data.boot && data.boot !== knownBoot);
      if (jobKind !== "ask" && data.revision_mark !== undefined && String(data.revision_mark) !== String(mark)) {
        var kind = DONE_MESSAGES[jobKind] || "revised";  // it was done before the restart
        jobDone({ done_url: location.pathname + "?m=" + kind + "#text" });
      } else if (restarted && request && resumed < 2) {
        resumed += 1;
        knownBoot = data.boot || null;
        send(request);
      } else if (restarted) {
        jobCalm("Der Server wurde neu gestartet – bitte nochmal abschicken.");
      } else {
        jobFailed("Claude hat aufgehört – bitte nochmal versuchen.");
      }
    };
    var jobApply = function (data) {
      if (jobState === "done") return;
      if (data.state === "running") jobRunning(data);
      else if (data.state === "done") jobDone(data);
      else if (data.state === "error" || data.state === "stale" || data.state === "denied") {
        jobFailed(data.error || "Sitzung abgelaufen – bitte die Seite neu laden.");
      } else jobIdle(data);
    };
    // One press, sent (again after a restart): no answer (the server is just restarting) - the status is asked until
    // it answers, and that decides.
    var send = function (req) {
      jobPoll.cancel();  // the answer to the start says how it stands
      fetch(req.action, { method: "POST", body: req.data, credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok || r.status < 500 ? r.json() : Promise.reject(r.status); })
        .then(function (res) { jobState = "running"; jobApply(res); })
        .catch(function () { jobState = "running"; knownBoot = null; jobPoll.schedule(2000); });
    };
    var startJob = function (btn) {
      if (text) { clearTimeout(saveTimer); savedValue = text.value; }  // the start saves the text (server side)
      jobKind = btn.dataset.job;
      if (askHint) askHint.hidden = true;
      var data;
      try { data = new FormData(form, btn); } catch (err) { data = new FormData(form); }
      if (btn.name && !data.has(btn.name)) data.append(btn.name, btn.value);
      request = { action: btn.formAction, data: data };  // what was asked, as it was asked
      resumed = 0;
      jobState = "starting";
      jobRunning({ elapsed_ms: 0, kind: jobKind });
      send(request);
    };

    // "Überarbeiten lassen" with something that reads like a question (and nothing else changed): ask first.
    var QUESTION = /^\s*(wo|was|wann|wie|wer|wieso|warum|welche[rsmn]?|ist|sind|hat|haben|gibt|gibt's|kann|muss|soll)\b[^]*\?\s*$/i;
    var looksLikeQuestion = function (btn) {
      return btn.dataset.job === "custom" && critiqueField && QUESTION.test(critiqueField.value) && !btn.dataset.forced;
    };

    // Registered after app.js (spinner, no double submit) and before submitInPlace below: a data-job button never
    // reaches it.
    form.addEventListener("submit", function (e) {
      var btn = e.submitter;
      if (!btn || !btn.dataset.job || !window.fetch || !window.FormData) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      ownNavigation(false);  // the page stays (app.js marked the submit as leaving)
      if (jobState === "running" || jobState === "starting") return;  // one at a time
      if (looksLikeQuestion(btn)) {
        release();
        askHint.hidden = false;
        askHint.querySelector("[data-ask-hint-ask]").focus();
        return;
      }
      delete btn.dataset.forced;
      if (btn.dataset.idleLabel) { btn.textContent = btn.dataset.idleLabel; delete btn.dataset.idleLabel; }
      startJob(btn);
    });
    if (askHint) {
      askHint.addEventListener("click", function (e) {
        var ask = e.target.closest("[data-ask-hint-ask]"), revise = e.target.closest("[data-ask-hint-revise]");
        if (!ask && !revise) return;
        askHint.hidden = true;
        var btn = form.querySelector(ask ? "[data-job=ask]" : "[data-job=custom]");
        if (revise) btn.dataset.forced = "1";
        if (form.requestSubmit) form.requestSubmit(btn); else btn.click();
      });
      if (critiqueField) critiqueField.addEventListener("input", function () { askHint.hidden = true; });
    }

    if (jobState === "running") jobRunning({ elapsed_ms: Number(panel.dataset.elapsedMs) || 0,
                                             expected_ms: Number(panel.dataset.expectedMs) || 0,
                                             slow_after_ms: Number(panel.dataset.slowMs) || 0 });
    else if (jobState === "error") jobBar.fail();
  }

  if (form && text && window.location.hash === "#text" && window.matchMedia("(min-width: 768px)").matches) {
    text.focus({ preventScroll: true });
  }

  // --- the ad in a drawer at the bottom (editor.html listing_drawer) -----------------------------------------------
  // The handle opens the sheet as a modal <dialog>: the page behind stays put and inert, focus goes into the sheet and
  // back to the handle afterwards. Closing: the grip, ✕, Escape or a tap beside the sheet; it slides (250 ms, none
  // with reduced motion). Without JS (or <dialog>) the sheet is a section at the end and the handle links to it.
  // Openers: the handle under the right column (wide screen; a bar of its own on a phone), and on a phone with the
  // open text the "Inserat" beside "Kopieren & Inserat öffnen".
  var sheet = document.getElementById("inserat");
  var openers = Array.prototype.slice.call(document.querySelectorAll("[data-drawer-open]"));
  if (sheet && openers.length && typeof sheet.showModal === "function") {
    var opener = openers[0];
    var expanded = function (on) { openers.forEach(function (o) { o.setAttribute("aria-expanded", on ? "true" : "false"); }); };
    var openSheet = function () {
      if (sheet.open) return;
      sheet.removeAttribute("data-anim");
      sheet.showModal();
      expanded(true);
      if (!reduce) { void sheet.offsetWidth; sheet.setAttribute("data-anim", "in"); }
      var close = sheet.querySelector(".ed-sheet-close");
      if (close) close.focus({ preventScroll: true });
    };
    var closed = function () {
      sheet.removeAttribute("data-anim");
      expanded(false);
    };
    var closeSheet = function () {
      if (!sheet.open || sheet.getAttribute("data-anim") === "out") return;
      if (reduce) { sheet.close(); return; }
      sheet.setAttribute("data-anim", "out");
      setTimeout(function () { sheet.close(); }, 240);
    };
    openers.forEach(function (o) {
      o.setAttribute("role", "button");
      o.addEventListener("click", function (e) { e.preventDefault(); opener = o; openSheet(); });
      o.addEventListener("keydown", function (e) { if (e.key === " ") { e.preventDefault(); opener = o; openSheet(); } });  // a link-button
    });
    sheet.addEventListener("cancel", function (e) { e.preventDefault(); closeSheet(); });  // Escape
    sheet.addEventListener("close", function () { closed(); opener.focus({ preventScroll: true }); });
    sheet.addEventListener("click", function (e) { if (e.target === sheet) closeSheet(); });  // the overlay beside it
    sheet.querySelectorAll("[data-drawer-close]").forEach(function (btn) { btn.addEventListener("click", closeSheet); });
    if (sheet.hasAttribute("data-open")) openSheet();  // back from "Bilder laden" / "Inserat neu laden"
  }
})();
