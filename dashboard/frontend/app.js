// Server dashboard. Every value is inserted as text, never as HTML. Every action calls the
// backend and shows what it actually returned; nothing here pretends an operation succeeded.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var session = null;           // {user, csrf, expiresAt}
  var status = null;            // last /api/status
  var tab = "overview";
  var timers = {};
  var STATUS_LABEL = { applied: "Applied", configured: "Configured", pending: "Restart needed", runtime: "Live", unknown: "Unknown" };

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (attrs[k] === null || attrs[k] === undefined || attrs[k] === false) return;
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k === "onclick") node.addEventListener("click", attrs[k]);
      else node.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return node;
  }
  function clear(node) { node.textContent = ""; return node; }

  // --- Time ---------------------------------------------------------------------------
  function ago(iso) {
    var s = (Date.now() - Date.parse(iso)) / 1000;
    var future = s < 0; s = Math.abs(s);
    var t = s < 60 ? "less than a minute" : s < 3600 ? Math.round(s / 60) + " min" : s < 86400 ? (s / 3600).toFixed(s < 36000 ? 1 : 0) + " h" : Math.round(s / 86400) + " d";
    return future ? "in " + t : (s < 60 ? "just now" : t + " ago");
  }
  function when(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function whenAgo(iso) { return iso ? when(iso) + " (" + ago(iso) + ")" : "—"; }
  function bytes(n) {
    if (n === undefined || n === null) return "—";
    return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
  }

  // --- Feedback -------------------------------------------------------------------------
  var toastTimer;
  function toast(msg) {
    var t = $("toast");
    t.textContent = msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, 2200);
  }
  function banner(text, level) {
    var b = $("banner");
    if (!text) { b.hidden = true; return; }
    b.hidden = false; b.className = "banner " + (level || "info"); b.textContent = text;
  }
  function copy(text, what) {
    function fallback() {
      var ta = el("textarea", { readonly: true });
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      toast(ok ? "Copied " + what : "Copy failed; select the text instead");
    }
    try { navigator.clipboard.writeText(text).then(function () { toast("Copied " + what); }, fallback); } catch (e) { fallback(); }
  }
  function confirmAction(title, text, okLabel) {
    var d = $("confirm");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-ok").textContent = okLabel;
    if (typeof d.showModal !== "function") return Promise.resolve(window.confirm(title + "\n\n" + text));
    return new Promise(function (resolve) {
      d.returnValue = "";
      d.addEventListener("close", function onClose() { d.removeEventListener("close", onClose); resolve(d.returnValue === "ok"); });
      d.showModal();
    });
  }
  function busy(btn, on, label) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.textContent; btn.textContent = label || "Working…"; btn.disabled = true; }
    else { btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }
  }

  // --- API ------------------------------------------------------------------------------
  function api(method, path, body) {
    var headers = { Accept: "application/json" };
    if (method !== "GET") { headers["Content-Type"] = "application/json"; if (session) headers["X-CSRF-Token"] = session.csrf; }
    return fetch(path, { method: method, headers: headers, credentials: "same-origin", cache: "no-store",
                         body: method === "GET" ? undefined : JSON.stringify(body || {}) })
      .catch(function () { throw new Error("The dashboard couldn't be reached. Check your connection."); })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (res.status === 401 && path !== "/api/login" && path !== "/api/session") { showLogin("Your session ended. Log in again."); }
          if (!res.ok) { var err = new Error(data.error || ("Request failed (" + res.status + ").")); err.status = res.status; throw err; }
          return data;
        });
      });
  }

  // --- Login ----------------------------------------------------------------------------
  function showLogin(message) {
    session = null;
    stopTimers();
    $("app-view").hidden = true; $("session-box").hidden = true; $("login-view").hidden = false;
    $("login-error").textContent = message || "";
    var u = document.querySelector("#login-form [name=username]");
    if (u) u.focus();
  }
  function showApp(s) {
    session = s;
    $("login-view").hidden = true; $("app-view").hidden = false; $("session-box").hidden = false;
    $("session-user").textContent = "Logged in as " + s.user;
    route();
  }
  $("login-form") && $("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target, btn = $("login-submit");
    busy(btn, true, "Logging in…"); $("login-error").textContent = "";
    api("POST", "/api/login", { username: f.username.value, password: f.password.value })
      .then(function (s) { f.password.value = ""; showApp(s); })
      .catch(function (err) { $("login-error").textContent = err.message; })
      .then(function () { busy(btn, false); });
  });
  $("logout").addEventListener("click", function () {
    api("POST", "/api/logout").catch(function () {}).then(function () { showLogin("Logged out."); });
  });

  // --- Routing --------------------------------------------------------------------------
  function stopTimers() { Object.keys(timers).forEach(function (k) { clearInterval(timers[k]); clearTimeout(timers[k]); delete timers[k]; }); }
  function route() {
    if (!session) return;
    var name = (location.hash || "#overview").slice(1);
    if (["overview", "schedule", "backups", "logs", "settings"].indexOf(name) < 0) name = "overview";
    tab = name;
    document.querySelectorAll(".tab").forEach(function (t) { t.hidden = t.id !== "tab-" + name; });
    document.querySelectorAll(".tabs a").forEach(function (a) {
      if (a.dataset.tab === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    stopTimers();
    if (name === "overview") {
      loadStatus(); timers.status = setInterval(loadStatus, 30000);
      // CloudWatch has new points every 1-5 minutes and charges per read, so the charts refresh slowly
      loadMetrics(); timers.metrics = setInterval(function () { if (!document.hidden) loadMetrics(); }, 300000);
    }
    if (name === "schedule") loadSchedules();
    if (name === "backups") loadBackups(true);
    if (name === "logs") { loadLogs(); timers.logs = setInterval(function () { if ($("log-auto").checked && !document.hidden) loadLogs(); }, 15000); }
    if (name === "settings") loadSettings();
  }
  window.addEventListener("hashchange", route);

  // --- Overview -------------------------------------------------------------------------
  var HERO_STATES = { ready: "online", starting: "starting", pending: "starting", stopping: "starting", stopped: "stopped", offline: "offline", unknown: "unknown", missing: "unknown" };

  function loadStatus() {
    return api("GET", "/api/status").then(function (s) { status = s; renderStatus(); return s; })
      .catch(function (err) { if (err.status !== 401) banner(err.message, "error"); });
  }

  function card(title, value, detail, extra) {
    return el("div", { class: "card stat" }, [el("div", { class: "k", text: title }), el("div", { class: "v", text: value }),
      detail ? el("p", { class: "muted", text: detail }) : null].concat(extra || []));
  }

  function scheduleLine(s) {
    if (!s || !s.deployed) return ["Not set up", "The schedule isn't deployed."];
    if (!s.enabled) return ["Off", "Recurring " + s.label.toLowerCase() + " is disabled."];
    return [when(s.next), (s.skipNext ? "Will be skipped. " : "") + ago(s.next)];
  }

  function renderStatus() {
    var s = status;
    if (s.serverName) { $("server-name").textContent = s.serverName; document.title = s.serverName + " · Server Dashboard"; }
    var srv = s.server || { state: "unknown", label: "Unknown", detail: "" };
    $("hero").setAttribute("data-state", HERO_STATES[srv.state] || "unknown");
    $("hero-pill").textContent = srv.label;
    $("hero-detail").textContent = srv.detail + (s.instance ? " Machine: " + s.instance.state + "." : "");
    $("address-box").hidden = !(s.instance && s.instance.address);
    $("address").textContent = (s.instance && s.instance.address) || "";
    var state = s.instance && s.instance.state;
    var opRunning = s.operation && (s.operation.status === "queued" || s.operation.status === "running");
    $("act-start").disabled = state !== "stopped" || opRunning;
    $("act-stop").disabled = state !== "running" || opRunning;
    $("act-backup").disabled = state !== "running";
    $("overview-times").textContent = "Status checked " + ago(s.checkedAt) + (s.reportedAt ? " · game server last reported " + ago(s.reportedAt) : "");
    if (s.errors && s.errors.length) banner(s.errors.join(" "), "warn"); else if (!opRunning) banner(null);
    if (opRunning) banner(opText(s.operation), "info");

    var cards = clear($("overview-cards"));
    var stop = scheduleLine(s.schedules && s.schedules.stop);
    cards.appendChild(card("Next scheduled stop", stop[0], stop[1], s.schedules && s.schedules.stop.skipNext ? [el("span", { class: "badge pending", text: "Skip pending" })] : null));
    var b = s.backups;
    if (b) {
      var latest = b.latest;
      cards.appendChild(card("Latest successful backup", latest ? whenAgo(latest.finishedAt) : "None yet", b.text,
        [el("span", { class: "badge " + ({ healthy: "applied", paused: "runtime", none: "unknown", stale: "pending", failing: "unknown" }[b.state] || "runtime"),
                      text: { healthy: "Healthy", paused: "Paused", none: "No backups", stale: "Overdue", failing: "Failing" }[b.state] || b.state })]
          .concat(b.recentFailures ? [el("span", { class: "muted small", text: " " + b.recentFailures + " failed in 24 h" })] : [])));
    }
    if (s.operation) {
      cards.appendChild(card("Last start/stop from the dashboard", s.operation.kind + " · " + s.operation.status, (s.operation.message || "") + " " + whenAgo(s.operation.createdAt)));
    }
    var errBox = el("div", { class: "card wide" }, [el("div", { class: "k", text: "Recent game errors (last hour)" })]);
    if (s.recentErrors === null) errBox.appendChild(el("p", { class: "muted", text: "Logs couldn't be read." }));
    else if (!s.recentErrors.length) errBox.appendChild(el("p", { class: "muted", text: state === "running" ? "No errors logged in the last hour." : "The server isn't running." }));
    else s.recentErrors.forEach(function (l) { errBox.appendChild(el("div", { class: "log-line error" }, [el("time", { text: new Date(l.t).toLocaleTimeString() }), el("span", { text: l.text })])); });
    errBox.appendChild(el("a", { href: "#logs", class: "small", text: "Open logs" }));
    cards.appendChild(errBox);
  }

  // --- Usage charts (Overview) ----------------------------------------------------------
  var metrics = null;
  var SVGNS = "http://www.w3.org/2000/svg";
  var CHARTS = {
    cpu: { title: "CPU usage", note: "Whole machine, 5-minute average, last 6 hours." },
    memory: { title: "Memory usage", note: "Whole machine, 1-minute average, last 6 hours." }
  };

  function loadMetrics() {
    return api("GET", "/api/metrics").then(function (m) { metrics = m; renderMetrics(); })
      .catch(function (err) { if (err.status !== 401) { metrics = { errors: [err.message], series: {} }; renderMetrics(); } });
  }
  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function clock(ms) { return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }

  function renderMetrics() {
    Object.keys(CHARTS).forEach(function (key) { renderChart(key, metrics, (metrics.series || {})[key]); });
  }

  function renderChart(key, m, series) {
    var box = clear($("chart-" + key));
    box.className = "card chart " + key;
    var pts = (series && series.points) || [];
    var running = m.state === "running";
    var t1 = Date.parse(m.to) || Date.now(), t0 = Date.parse(m.from) || t1 - 6 * 3600000, span = (t1 - t0) || 1;
    var last = pts.length ? pts[pts.length - 1] : null;
    var current = !running ? (m.state ? "Off" : "—") : last && t1 - last[0] < series.period * 3000 ? Math.round(last[1]) + "%" : "—";
    box.appendChild(el("div", { class: "chart-head" }, [el("div", { class: "k", text: CHARTS[key].title }), el("div", { class: "v", text: current })]));
    box.appendChild(el("p", { class: "muted small", text: CHARTS[key].note }));

    var plot = el("div", { class: "plot", role: "img",
      "aria-label": CHARTS[key].title + ", last 6 hours" + (running && current !== "—" ? ", now " + current : "") });
    ["100%", "50%", "0%"].forEach(function (t, i) { plot.appendChild(el("span", { class: "y y" + i, text: t, "aria-hidden": "true" })); });
    var svg = svgEl("svg", { viewBox: "0 0 1000 100", preserveAspectRatio: "none", "aria-hidden": "true" });
    [0, 50, 100].forEach(function (y) { svg.appendChild(svgEl("line", { x1: 0, x2: 1000, y1: y, y2: y, class: "grid", "vector-effect": "non-scaling-stroke" })); });

    // Gaps (machine stopped, agent restarting) break the line instead of drawing across them
    var gap = (series ? series.period : 300) * 2500, segments = [], seg = null;
    pts.forEach(function (p, i) {
      if (!seg || p[0] - pts[i - 1][0] > gap) { seg = []; segments.push(seg); }
      seg.push([((p[0] - t0) / span * 1000).toFixed(1), (100 - p[1]).toFixed(1)]);
    });
    segments.filter(function (s) { return s.length > 1; }).forEach(function (s) {
      var line = s.map(function (xy, i) { return (i ? "L" : "M") + xy[0] + "," + xy[1]; }).join("");
      svg.appendChild(svgEl("path", { d: "M" + s[0][0] + ",100" + line.replace(/^M/, "L") + "L" + s[s.length - 1][0] + ",100Z", class: "area" }));
      svg.appendChild(svgEl("path", { d: line, class: "line", "vector-effect": "non-scaling-stroke" }));
    });
    plot.appendChild(svg);

    var empty = null;
    if (m.errors && m.errors.length) empty = m.errors.join(" ");
    else if (!m.state) empty = "No server machine found.";
    else if (pts.length < 2) empty = running ? "Collecting data. The first points appear within a few minutes of starting." : "The server is off. Usage is recorded while it runs.";
    if (empty) plot.appendChild(el("div", { class: "plot-empty", text: empty }));
    else {
      var dot = el("div", { class: "plot-dot", hidden: true }), tip = el("div", { class: "plot-tip", hidden: true });
      plot.appendChild(dot); plot.appendChild(tip);
      var hide = function () { dot.hidden = true; tip.hidden = true; };
      plot.addEventListener("pointermove", function (e) {
        var r = plot.getBoundingClientRect(), t = t0 + (e.clientX - r.left) / r.width * span, best = null;
        pts.forEach(function (p) { if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p; });
        if (!best || Math.abs(best[0] - t) > gap) return hide();
        var fx = (best[0] - t0) / span;
        dot.style.left = (fx * 100) + "%"; dot.style.top = (100 - best[1]) + "%";
        tip.textContent = clock(best[0]) + " · " + best[1].toFixed(1) + "%";
        tip.style.left = (fx * 100) + "%";
        tip.style.transform = fx < 0.15 ? "translate(0, -100%)" : fx > 0.85 ? "translate(-100%, -100%)" : "translate(-50%, -100%)";
        dot.hidden = false; tip.hidden = false;
      });
      plot.addEventListener("pointerleave", hide);
    }
    box.appendChild(plot);
    box.appendChild(el("div", { class: "x-axis", "aria-hidden": "true" },
      [el("span", { text: clock(t0) }), el("span", { text: clock(t0 + span / 2) }), el("span", { text: "now" })]));
  }

  function opText(op) {
    var last = op.steps && op.steps.length ? op.steps[op.steps.length - 1].text : "Starting";
    return (op.kind === "stop" ? "Stopping the server: " : "Starting the server: ") + last + "…";
  }

  // Poll until the machine reaches the target state (or the game is ready, for starts)
  function follow(kind) {
    var started = Date.now(), limit = kind === "stop" ? 12 * 60000 : 15 * 60000;
    clearInterval(timers.follow);
    timers.follow = setInterval(function () {
      loadStatus().then(function (s) {
        if (!s) return;
        var st = s.instance && s.instance.state, op = s.operation;
        var failed = op && op.kind === kind && op.status === "failed";
        var done = kind === "stop" ? st === "stopped" : (s.server && s.server.state === "ready");
        if (failed) { clearInterval(timers.follow); banner((kind === "stop" ? "Stop" : "Start") + " failed: " + (op.message || "unknown error"), "error"); }
        else if (done) { clearInterval(timers.follow); banner(kind === "stop" ? "Server stopped. " + (op && op.message ? op.message : "") : "Server is ready for players.", "success"); }
        else if (Date.now() - started > limit) { clearInterval(timers.follow); banner("Still not " + (kind === "stop" ? "stopped" : "ready") + " after " + limit / 60000 + " minutes. Check the logs.", "warn"); }
        else if (kind === "start" && st === "running") banner("Machine is running; " + (s.server ? s.server.detail : ""), "info");
      });
    }, 5000);
  }

  $("act-refresh").addEventListener("click", function () { var b = this; busy(b, true, "Refreshing…"); Promise.all([loadStatus(), loadMetrics()]).then(function () { busy(b, false); }); });
  $("copy-address").addEventListener("click", function () { copy($("address").textContent, "address"); });
  $("act-start").addEventListener("click", function () {
    var btn = this;
    confirmAction("Start the server?", "The machine starts, the game updates and loads the world. Players can join after a few minutes. The recurring schedule is not changed.", "Start").then(function (ok) {
      if (!ok) return;
      busy(btn, true, "Starting…");
      api("POST", "/api/server/start", { confirm: "start" })
        .then(function (r) { banner((r.operation && r.operation.message) || "Start requested.", "info"); follow("start"); return loadStatus(); })
        .catch(function (err) { banner("Start failed: " + err.message, "error"); })
        .then(function () { busy(btn, false); renderStatus(); });
    });
  });
  $("act-stop").addEventListener("click", function () {
    var btn = this;
    confirmAction("Stop the server?", "Everyone playing is disconnected. The game is stopped, a backup is saved, then the machine shuts down. The recurring schedule is not changed.", "Stop server").then(function (ok) {
      if (!ok) return;
      busy(btn, true, "Stopping…");
      api("POST", "/api/server/stop", { confirm: "stop" })
        .then(function () { banner("Stopping: saving a backup first…", "info"); follow("stop"); return loadStatus(); })
        .catch(function (err) { banner("Stop failed: " + err.message, "error"); })
        .then(function () { busy(btn, false); if (status) renderStatus(); });
    });
  });
  $("act-backup").addEventListener("click", function () { createBackup(this); });

  // --- Backups --------------------------------------------------------------------------
  var backupCursor = null, backupItems = [];

  function createBackup(btn) {
    busy(btn, true, "Starting backup…");
    api("POST", "/api/backups").then(function (r) {
      toast("Backup started");
      banner("Backup " + r.backup.id + " is running…", "info");
      followBackup(r.backup.id);
      if (tab === "backups") loadBackups(true);
    }).catch(function (err) { banner("Backup not started: " + err.message, "error"); })
      .then(function () { busy(btn, false); });
  }
  function followBackup(id) {
    var started = Date.now();
    clearInterval(timers.backup);
    timers.backup = setInterval(function () {
      api("GET", "/api/backups/" + encodeURIComponent(id)).then(function (r) {
        var b = r.backup;
        if (b.status === "in_progress" && Date.now() - started < 8 * 60000) return;
        clearInterval(timers.backup);
        if (b.status === "succeeded") banner("Backup saved (" + bytes(b.size) + ").", "success");
        else if (b.status === "in_progress") banner("The backup is taking unusually long. Check the Backups tab.", "warn");
        else banner("Backup " + b.status + ": " + (b.reason || "no reason given"), "error");
        if (tab === "backups") loadBackups(true);
        if (tab === "overview") loadStatus();
      }).catch(function () {});
    }, 3000);
  }
  $("backups-create").addEventListener("click", function () { createBackup(this); });
  $("backups-refresh").addEventListener("click", function () { loadBackups(true); });
  $("backups-more").addEventListener("click", function () { loadBackups(false); });

  var TYPE_LABEL = { automatic: "Automatic (hourly)", manual: "Manual", shutdown: "Automatic (before stop)" };
  var STATUS_CLASS = { succeeded: "applied", in_progress: "configured", failed: "unknown", skipped: "pending" };

  function loadBackups(reset) {
    if (reset) { backupCursor = null; backupItems = []; }
    var q = backupCursor ? "?cursor=" + encodeURIComponent(backupCursor) : "";
    return api("GET", "/api/backups" + q).then(function (r) {
      backupItems = backupItems.concat(r.backups);
      backupCursor = r.next;
      renderBackups();
      if (r.backups.some(function (b) { return b.status === "in_progress"; }) && !timers.backup) {
        timers.backupList = setTimeout(function () { loadBackups(true); }, 5000);
      }
    }).catch(function (err) {
      if (err.status === 401) return;
      clear($("backups-content")).appendChild(el("div", { class: "empty" }, [el("strong", { text: "Couldn't load backups" }), el("span", { text: err.message })]));
    });
  }

  function renderBackups() {
    var latest = backupItems.filter(function (b) { return b.status === "succeeded"; })[0];
    var box = clear($("backups-latest"));
    if (latest) box.appendChild(card("Latest successful backup", whenAgo(latest.finishedAt), TYPE_LABEL[latest.type] + " · " + bytes(latest.size)));
    var content = clear($("backups-content"));
    if (!backupItems.length) {
      content.appendChild(el("div", { class: "empty" }, [el("strong", { text: "No backups recorded yet" }), el("span", { text: "Create one now, or wait for the next hourly backup." })]));
    } else {
      var table = el("table", { class: "data" }, [el("thead", {}, [el("tr", {}, ["Started", "Type", "Status", "Size", ""].map(function (h) { return el("th", { text: h }); }))])]);
      var tbody = el("tbody");
      backupItems.forEach(function (b) {
        var dl = b.downloadable ? el("a", { href: "/api/backups/" + encodeURIComponent(b.id) + "/download", text: "Download", class: "small" }) : null;
        tbody.appendChild(el("tr", {}, [
          el("td", { "data-label": "Started" }, [when(b.startedAt)]),
          el("td", { "data-label": "Type", text: TYPE_LABEL[b.type] || b.type }),
          el("td", { "data-label": "Status" }, [el("span", { class: "badge " + (STATUS_CLASS[b.status] || "runtime"), text: b.status.replace("_", " ") }),
            b.reason ? el("div", { class: "muted small", text: b.reason }) : null]),
          el("td", { "data-label": "Size", class: "mono", text: bytes(b.size) }),
          el("td", {}, [dl]),
        ]));
      });
      table.appendChild(tbody);
      content.appendChild(el("div", { class: "table-wrap" }, [table]));
    }
    $("backups-more").hidden = !backupCursor;
  }

  // --- Schedule -------------------------------------------------------------------------
  function describeCron(s) {
    var m = /^cron\((\S+) (\S+) [*?] \* [*?] \*\)$/.exec(s.expression || "");
    if (!m) return s.expression;
    var pad = function (x) { return ("0" + x).slice(-2); };
    if (m[2] === "*") return "Every hour at :" + pad(m[1]);
    return "Every day at " + pad(m[2]) + ":" + pad(m[1]) + " (" + s.timezone + ")";
  }

  function loadSchedules() {
    return api("GET", "/api/schedules").then(renderSchedules).catch(function (err) {
      if (err.status === 401) return;
      clear($("schedule-content")).appendChild(el("div", { class: "empty" }, [el("strong", { text: "Couldn't load the schedules" }), el("span", { text: err.message })]));
    });
  }
  $("schedule-refresh").addEventListener("click", loadSchedules);

  function scheduleAction(btn, path, body, done) {
    busy(btn, true);
    api("POST", path, body).then(function () { toast(done); return loadSchedules(); })
      .catch(function (err) { banner(err.message, "error"); busy(btn, false); });
  }

  function renderSchedules(data) {
    $("schedule-times").textContent = "Read from AWS EventBridge Scheduler " + ago(data.checkedAt) + ". Times are shown in your local time zone.";
    var content = clear($("schedule-content"));
    var pending = data.skips.filter(function (k) { return k.status === "pending"; });
    ["stop", "backup"].forEach(function (key) {
      if (!data.schedules[key]) return;
      var s = data.schedules[key];
      var body = el("div", { class: "card schedule" });
      body.appendChild(el("div", { class: "schedule-head" }, [
        el("h3", { text: s.label }),
        s.deployed ? el("span", { class: "badge " + (s.enabled ? "applied" : "runtime"), text: s.enabled ? "On" : "Off" })
                   : el("span", { class: "badge unknown", text: "Not deployed" }),
      ]));
      if (!s.deployed) { body.appendChild(el("p", { class: "muted", text: "This schedule doesn't exist in AWS yet. Run dashboard/deploy.sh." })); content.appendChild(body); return; }
      body.appendChild(el("p", { text: describeCron(s) }));
      var skip = pending.filter(function (k) { return k.schedule === key && k.occurrence === s.next; })[0];
      body.appendChild(el("p", { class: "muted", text: s.enabled ? "Next run: " + whenAgo(s.next) + (skip ? " · will be skipped" : "") : "Turned off in AWS; it won't run." }));
      if (!s.targetsDashboard || !s.hasRole) body.appendChild(el("p", { class: "note", text: "Warning: this schedule doesn't target the dashboard function with a role, so it won't work." }));
      var actions = el("div", { class: "actions" });
      if (s.skippable && s.enabled && s.next) {
        actions.appendChild(skip
          ? el("button", { type: "button", class: "btn", text: "Don't skip", onclick: function () { scheduleAction(this, "/api/schedules/" + key + "/unskip", { occurrence: s.next }, "Skip cancelled"); } })
          : el("button", { type: "button", class: "btn", text: "Skip next (" + when(s.next) + ")", onclick: function () { scheduleAction(this, "/api/schedules/" + key + "/skip", { occurrence: s.next }, "Next run will be skipped"); } }));
      }
      if (actions.childNodes.length) body.appendChild(actions);
      if (s.recentRuns && s.recentRuns.length) {
        var list = el("ul", { class: "runs" });
        s.recentRuns.forEach(function (r) {
          list.appendChild(el("li", {}, [el("span", { class: "badge " + ({ done: "applied", skipped: "pending", failed: "unknown" }[r.result] || "configured"), text: r.result || r.status }),
            " " + when(r.occurrence) + (r.detail ? " · " + r.detail : "")]));
        });
        body.appendChild(el("details", {}, [el("summary", { text: "Recent runs" }), list]));
      }
      content.appendChild(body);
    });
    if (data.skips.length) {
      var t = el("div", { class: "card" }, [el("h3", { text: "Skips" })]);
      var ul = el("ul", { class: "runs" });
      data.skips.forEach(function (k) {
        ul.appendChild(el("li", {}, [el("span", { class: "badge " + ({ pending: "pending", consumed: "applied", cancelled: "runtime", expired: "runtime" }[k.status]), text: k.status }),
          " " + (k.schedule === "stop" ? "Stop" : k.schedule) + " on " + when(k.occurrence)]));
      });
      t.appendChild(ul);
      content.appendChild(t);
    }
  }

  // --- Logs -----------------------------------------------------------------------------
  var logLines = [];
  function loadLogs() {
    var q = "source=" + encodeURIComponent($("log-source").value) + "&minutes=" + encodeURIComponent($("log-minutes").value) +
            "&filter=" + encodeURIComponent($("log-filter").value) + "&limit=500";
    var box = $("logs-content");
    if (!logLines.length) clear(box).appendChild(el("div", { class: "muted pad", text: "Loading logs…" }));
    return api("GET", "/api/logs?" + q).then(function (r) {
      logLines = r.lines;
      var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      clear(box);
      if (!r.lines.length) box.appendChild(el("div", { class: "muted pad", text: "No log lines in this window" + (r.filter ? " matching \"" + r.filter + "\"" : "") + "." }));
      r.lines.forEach(function (l) {
        box.appendChild(el("div", { class: "log-line " + (l.level || "") }, [el("time", { datetime: new Date(l.t).toISOString(), text: new Date(l.t).toLocaleTimeString() }), el("span", { text: l.text })]));
      });
      if (atBottom || box.dataset.first !== "no") box.scrollTop = box.scrollHeight;
      box.dataset.first = "no";
      $("logs-times").textContent = r.lines.length + " lines" + (r.truncated ? " (newest shown; narrow the window or filter)" : "") + " · refreshed " + new Date().toLocaleTimeString();
    }).catch(function (err) {
      if (err.status === 401) return;
      clear(box).appendChild(el("div", { class: "empty" }, [el("strong", { text: "Couldn't load logs" }), el("span", { text: err.message })]));
    });
  }
  $("log-form").addEventListener("submit", function (e) { e.preventDefault(); logLines = []; loadLogs(); });
  ["log-source", "log-minutes"].forEach(function (id) { $(id).addEventListener("change", function () { logLines = []; $("logs-content").dataset.first = ""; loadLogs(); }); });
  $("log-copy").addEventListener("click", function () {
    copy(logLines.map(function (l) { return new Date(l.t).toISOString() + " " + l.text; }).join("\n"), logLines.length + " log lines");
  });

  // --- Settings (secondary; groups collapsed by default) ---------------------------------
  var settingsModel = null;
  function loadSettings() {
    var btn = $("refresh"); busy(btn, true, "Refreshing…");
    return api("GET", "/api/settings").then(function (m) { settingsModel = m; renderSettings(); })
      .catch(function (err) { if (err.status !== 401) clear($("content")).appendChild(el("div", { class: "empty" }, [el("strong", { text: "Couldn't load the settings" }), el("span", { text: err.message })])); })
      .then(function () { busy(btn, false); });
  }
  $("refresh").addEventListener("click", loadSettings);
  $("only-changed").addEventListener("change", function () { if (settingsModel) renderSettings(); });
  $("copy-summary").addEventListener("click", function () { if (settingsModel) copy(settingsSummary(settingsModel), "summary"); });

  function displayOf(r) { return r.time ? whenAgo(r.time) : r.display; }

  function settingsSummary(m) {
    var lines = [(m.server.name || "Enshrouded server") + " — server settings", "Checked " + m.checkedAt + (m.inspectedAt ? ", read from the server " + m.inspectedAt : "")];
    if (m.server.address) lines.push("Address: " + m.server.address);
    m.groups.forEach(function (g) {
      lines.push("", "## " + g.title);
      var section = null;
      g.settings.forEach(function (r) {
        if (r.section && r.section !== section) { section = r.section; lines.push("### " + section); }
        var v = displayOf(r);
        if (r.pending) v += " (after restart: " + r.pending.display + ")";
        if (r.default && !r.default.isDefault) v += " [default " + r.default.display + "]";
        lines.push("- " + r.label + ": " + v);
      });
    });
    lines.push("", "Passwords are not included.");
    return lines.join("\n");
  }

  function settingRow(r) {
    var tip = [r.statusNote, r.source ? "Source: " + r.source : null].filter(Boolean).join("\n");
    var main = el("div", { class: "row-main" }, [
      el("div", { class: "label" }, [r.label, el("span", { class: "badge " + r.status, text: STATUS_LABEL[r.status] || r.status, title: tip })]),
      r.description ? el("p", { class: "desc", text: r.description }) : null,
      (r.status === "pending" || r.status === "unknown") && r.statusNote ? el("p", { class: "note", text: r.statusNote }) : null,
    ]);
    var shown = displayOf(r);
    var value = el("span", { class: "value" }, [shown]);
    if (r.pending) { value.appendChild(el("span", { class: "arrow", text: " → " })); value.appendChild(document.createTextNode(r.pending.display)); }
    var copyBtn = el("button", { type: "button", class: "copy", "aria-label": "Copy " + r.label, text: "Copy",
                                 onclick: function () { copy(r.pending ? r.pending.display : shown, r.label.toLowerCase()); } });
    var meta = [el("code", { text: (r.value !== null && r.value !== undefined && typeof r.value !== "object" && r.status !== "runtime") ? r.key + " = " + JSON.stringify(r.value) : r.key })];
    if (r.default) meta.push(r.default.isDefault ? el("span", { class: "def", text: "Default" }) : el("span", { class: "def changed", text: "Changed · default " + r.default.display }));
    if (r.change === "in-game") meta.push(el("span", { text: "Changes in game" }));
    var col = el("div", { class: "value-col" }, [el("div", { class: "value-line" }, [value, copyBtn])]);
    if (r.chips) col.appendChild(el("div", { class: "chips" }, r.chips.map(function (c) { return el("span", { class: "chip" + (c.on ? "" : " off"), text: c.label }); })));
    col.appendChild(el("div", { class: "meta" }, meta));
    return el("div", { class: "row" }, [main, col]);
  }

  function renderSettings() {
    var m = settingsModel;
    var parts = ["Checked " + ago(m.checkedAt)];
    if (m.inspectedAt) parts.push("read from the server " + whenAgo(m.inspectedAt));
    if (m.pendingRestart) parts.push(m.pendingRestart + (m.pendingRestart === 1 ? " change waits" : " changes wait") + " for a restart");
    $("times").textContent = parts.join(" · ");
    var notices = clear($("notices"));
    m.notices.forEach(function (n) { notices.appendChild(el("div", { class: "notice " + (n.level || ""), text: n.text })); });
    var content = clear($("content"));
    content.setAttribute("aria-busy", "false");
    $("copy-summary").disabled = !m.groups.length;
    if (!m.groups.length) {
      content.appendChild(el("div", { class: "empty" }, [el("strong", { text: "No settings to show yet" }), el("span", { text: "The server hasn't sent its settings yet." })]));
      return;
    }
    var onlyChanged = $("only-changed").checked;
    m.groups.forEach(function (g) {
      var rows = g.settings.filter(function (r) { return !onlyChanged || (r.default && !r.default.isDefault) || r.status === "pending"; });
      var changed = g.settings.filter(function (r) { return r.default && !r.default.isDefault; }).length;
      var pend = g.settings.filter(function (r) { return r.status === "pending"; }).length;
      var cardEl = el("div", { class: "card flush" });
      var section = null;
      rows.forEach(function (r) {
        if (r.section && r.section !== section) { section = r.section; cardEl.appendChild(el("h4", { class: "subhead", text: section })); }
        cardEl.appendChild(settingRow(r));
      });
      if (!rows.length) cardEl.appendChild(el("p", { class: "muted pad", text: "Everything here is at the game's default." }));
      var summary = el("summary", {}, [el("span", { class: "group-title", text: g.title }),
        el("span", { class: "muted small", text: " " + g.settings.length + " settings" + (changed ? " · " + changed + " changed from default" : "") + (pend ? " · " + pend + " need a restart" : "") })]);
      content.appendChild(el("details", { class: "group", id: "group-" + g.id }, [summary, cardEl]));
    });
  }

  // --- Start ----------------------------------------------------------------------------
  document.addEventListener("visibilitychange", function () { if (!document.hidden && session && tab === "overview") loadStatus(); });
  setInterval(function () { if (status && tab === "overview" && session) $("overview-times").textContent = "Status checked " + ago(status.checkedAt) + (status.reportedAt ? " · game server last reported " + ago(status.reportedAt) : ""); }, 30000);
  api("GET", "/api/session").then(showApp).catch(function (err) {
    showLogin(err.status === 401 ? "" : err.message);
  });
})();
