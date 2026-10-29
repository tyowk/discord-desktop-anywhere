"use strict";
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const cfg = JSON.parse($("#dda-config").textContent);

  // ---------------------------------------------------------------- helpers
  async function api(path, { method = "GET", body } = {}) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (method !== "GET") {
      opts.headers["X-DDA-Request"] = "1";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body || {});
    }
    const res = await fetch(path, opts);
    if (res.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok && data.ok !== false) data = { ok: false, error: "HTTP " + res.status };
    return data;
  }
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* storage unavailable */ } },
  };
  const fmtBytes = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");

  // ---------------------------------------------------------------- shell
  const desk = $("#desk"), boot = $("#boot"), panel = $("#panel"), fab = $("#fab");
  desk.src = cfg.vnc;
  $("#ver").textContent = "v" + cfg.version;
  $("#logout").hidden = !cfg.auth;

  function setPanel(open) {
    panel.hidden = !open;
    fab.setAttribute("aria-expanded", String(open));
    if (open) refreshShots();
  }
  fab.addEventListener("click", () => setPanel(panel.hidden));
  $("#close").addEventListener("click", () => setPanel(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) setPanel(false); });
  if (cfg.panel) setPanel(true);

  // ---------------------------------------------------------------- status polling
  let lastStatus = null;
  async function pollStatus() {
    try {
      const s = await api("/api/status");
      lastStatus = s;
      const ready = s.phase === "running";
      boot.hidden = ready;
      boot.classList.toggle("error", s.phase === "error");
      $("#boot-msg").textContent = s.message || "Starting...";
      const parts = [ready ? (s.discord ? "Discord is running" : "Discord is restarting...") : (s.message || s.phase)];
      parts.push(s.resolution);
      $("#status").textContent = parts.join(" · ");
      updateAudioAvailability(s.audio);
      setDevButtons(s.devtools);
      $("#shot-sec").hidden = false;
    } catch (_) {
      $("#status").textContent = "Cannot reach the server...";
    }
  }
  function setDevButtons(on) { document.querySelectorAll(".dev").forEach((b) => { b.hidden = !on; }); }
  pollStatus();
  setInterval(pollStatus, 3000);

  // ---------------------------------------------------------------- controls
  document.querySelectorAll("[data-action]").forEach((btn) => btn.addEventListener("click", async () => {
    const msg = $("#action-msg");
    msg.textContent = "Sending...";
    try {
      const r = await api("/api/action", { method: "POST", body: { name: btn.dataset.action } });
      msg.textContent = r.ok ? r.message : "Failed: " + r.error;
    } catch (e) { msg.textContent = "Error: " + e.message; }
  }));

  // ---------------------------------------------------------------- screenshots
  async function refreshShots() {
    const list = $("#shot-list");
    let r;
    try { r = await api("/api/shots"); } catch (_) { return; }
    list.textContent = "";
    for (const s of r.shots || []) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = s.name + " (" + fmtBytes(s.bytes) + ")";
      const view = document.createElement("a");
      view.href = "/shots/" + encodeURIComponent(s.name);
      view.target = "_blank"; view.rel = "noopener"; view.textContent = "View";
      const dl = document.createElement("a");
      dl.href = view.href + "?dl=1"; dl.textContent = "Save";
      const del = document.createElement("button");
      del.type = "button"; del.textContent = "Delete"; del.setAttribute("aria-label", "Delete " + s.name);
      del.addEventListener("click", async () => {
        await api("/api/shots/" + encodeURIComponent(s.name), { method: "DELETE" });
        refreshShots();
      });
      li.append(name, view, dl, del);
      list.append(li);
    }
  }
  document.querySelectorAll("[data-shot]").forEach((btn) => btn.addEventListener("click", async () => {
    const msg = $("#shot-msg");
    btn.disabled = true;
    msg.textContent = "Taking screenshot... (4K/8K can take a few seconds)";
    try {
      const r = await api("/api/shot", { method: "POST", body: { mode: btn.dataset.shot, width: Number(btn.dataset.width) } });
      msg.textContent = r.ok ? "Saved " + r.dims + " (" + fmtBytes(r.bytes) + ")" : "Failed: " + r.error;
      refreshShots();
    } catch (e) { msg.textContent = "Error: " + e.message; }
    btn.disabled = false;
  }));

  // ---------------------------------------------------------------- audio
  const audioBtn = $("#audio-btn"), audioMsg = $("#audio-msg"), vol = $("#vol");
  const player = new Audio();
  player.preload = "none";
  let wanted = false, retries = 0, retryTimer = null, audioOk = cfg.audio;

  vol.value = store.get("dda.volume", "80");
  player.volume = Number(vol.value) / 100;
  vol.addEventListener("input", () => { player.volume = Number(vol.value) / 100; store.set("dda.volume", vol.value); });

  function showAudio(state, text) {
    audioBtn.classList.toggle("on", state === "playing");
    fab.classList.toggle("playing", state === "playing");
    audioBtn.textContent = wanted ? "Mute audio" : "Enable audio";
    audioMsg.textContent = text || "";
  }
  function updateAudioAvailability(a) {
    audioOk = !!(a && a.available);
    audioBtn.disabled = !audioOk;
    if (!audioOk) {
      showAudio("off", "Audio is unavailable" + (a && a.reason ? ": " + a.reason : "."));
    } else if (!wanted && !audioMsg.textContent) {
      showAudio("off", "Tap Enable audio to hear Discord (browsers require a tap).");
    }
  }
  function connectAudio() {
    clearTimeout(retryTimer);
    showAudio("connecting", "Connecting...");
    player.src = "/audio?t=" + Date.now();
    player.play().catch((e) => {
      if (e && e.name === "NotAllowedError") { wanted = false; showAudio("off", "The browser blocked playback: tap Enable audio."); }
    });
  }
  function scheduleRetry() {
    if (!wanted) return;
    retries += 1;
    const delay = Math.min(10000, 500 * 2 ** Math.min(retries, 5));
    showAudio("connecting", "Reconnecting...");
    retryTimer = setTimeout(connectAudio, delay);
  }
  function stopAudio() {
    wanted = false;
    clearTimeout(retryTimer);
    player.pause();
    player.removeAttribute("src");
    player.load();
    showAudio("off", "Audio muted.");
  }
  audioBtn.addEventListener("click", () => {
    if (wanted) { stopAudio(); return; }
    wanted = true; retries = 0; connectAudio();
  });
  player.addEventListener("playing", () => { retries = 0; showAudio("playing", "Playing live audio."); });
  player.addEventListener("error", scheduleRetry);
  player.addEventListener("ended", scheduleRetry);
  // Stay close to live: if the buffer grows (slow network bursts), skip ahead.
  setInterval(() => {
    if (!wanted || player.paused || !player.buffered.length) return;
    const end = player.buffered.end(player.buffered.length - 1);
    if (end - player.currentTime > 3) { try { player.currentTime = end - 0.4; } catch (_) { /* not seekable */ } }
  }, 2000);
  showAudio("off", "");
})();
