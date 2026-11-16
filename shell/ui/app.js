"use strict";
/* HomeOS home screen. Talks to the local API in sysapi.py. */

const $ = (id) => document.getElementById(id);
const meta = (name) => (document.querySelector('meta[name="' + name + '"]') || {}).content || "";
const TOKEN = meta("homeos-token");
const WEB = meta("homeos-mode") === "web";   // opened in a remote browser through homeos-web
const state = { boot: null, settings: null, status: null, wifiTarget: null };

async function api(path, body) {
  const opt = { headers: { "X-HomeOS-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  if (WEB && res.status === 401) { location.href = "/login"; throw new Error("Signed out"); }
  if (!res.ok) throw new Error("Request failed (" + res.status + ")");
  return res.json();
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ---------- toast */
let toastTimer = 0;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

/* ---------- sheets */
function openSheet(id) {
  document.querySelectorAll(".sheet").forEach((s) => { s.hidden = true; });
  $(id).hidden = false;
  $("backdrop").hidden = false;
}
function closeSheets() {
  const video = $("clip-video");
  if (video.getAttribute("src")) { video.pause(); video.removeAttribute("src"); video.load(); }
  document.querySelectorAll(".sheet").forEach((s) => { s.hidden = true; });
  $("backdrop").hidden = true;
}
$("backdrop").addEventListener("click", closeSheets);
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeSheets));

let confirmAction = null;
function confirmSheet(title, text, okLabel, action) {
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-ok").textContent = okLabel;
  confirmAction = action;
  openSheet("sheet-confirm");
}
$("confirm-ok").addEventListener("click", () => {
  const fn = confirmAction;
  confirmAction = null;
  closeSheets();
  if (fn) fn();
});

/* ---------- clock */
/* The clock is drawn digit by digit, so a digit that changes can roll in. */
function showClock(text) {
  const box = $("clock");
  if (box.dataset.t === text) return;
  const old = box.dataset.t || "";
  box.dataset.t = text;
  box.setAttribute("aria-label", text);
  box.textContent = "";
  Array.from(text).forEach((ch, i) => {
    const s = document.createElement("span");
    s.textContent = ch;
    s.setAttribute("aria-hidden", "true");
    if (old && old[i] !== ch) s.className = "roll";
    box.appendChild(s);
  });
}
/* Panels slide away before they are hidden. */
function closePanel(node, after) {
  if (node.hidden || node.classList.contains("leaving")) return;
  const done = () => { node.classList.remove("leaving"); node.hidden = true; if (after) after(); };
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { done(); return; }
  node.classList.add("leaving");
  setTimeout(done, 180);
}

function tick() {
  const d = new Date();
  showClock(String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"));
  $("date").textContent = d.toLocaleDateString("en-GB", innerWidth < 420 ? { weekday: "short", day: "numeric", month: "short" } : { weekday: "long", day: "numeric", month: "long" });
}

/* ---------- formatting */
function gb(bytes) {
  const v = bytes / 1073741824;
  return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " GB";
}
function uptime(sec) {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return d + " d " + h + " h";
  if (h) return h + " h " + m + " min";
  return m + " min";
}

/* ---------- tiles */
function hue(text) {            // picks one of the four avatar colours from the name
  let h = 7;
  for (const ch of text) h = (h * 33 + ch.codePointAt(0)) >>> 0;
  return (h >>> 3) % 4;
}

/* Same rules as sysapi.normalize_url; used when the page runs in a remote browser. */
const ENGINES = {
  duckduckgo: "https://duckduckgo.com/?q=",
  google: "https://www.google.com/search?q=",
  wikipedia: "https://en.wikipedia.org/w/index.php?search=",
};
const HOSTLIKE = /^(localhost|\d{1,3}(\.\d{1,3}){3}|[\p{L}\p{N}_-]+(\.[\p{L}\p{N}_-]+)+)(:\d+)?([/?#].*)?$/u;
function normalizeUrl(text) {
  const t = (text || "").trim();
  if (!t) return null;
  const search = (ENGINES[state.settings.search] || ENGINES.duckduckgo) + encodeURIComponent(t);
  if (/^[a-z][a-z0-9+.-]*:/i.test(t) && !HOSTLIKE.test(t)) return /^https?:/i.test(t) ? t : search;
  if (!/\s/.test(t) && HOSTLIKE.test(t)) {
    const host = t.split("/")[0].split(":")[0];
    return (host === "localhost" || /^\d{1,3}(\.\d{1,3}){3}$/.test(host) ? "http://" : "https://") + t;
  }
  return search;
}

function openBrowser(url) {
  if (!WEB) {
    api("/api/open", { target: "browser", url: url || "" }).catch((e) => toast(e.message));
    return;
  }
  const engines = { duckduckgo: "https://duckduckgo.com/", google: "https://www.google.com/", wikipedia: "https://en.m.wikipedia.org/" };
  let target = normalizeUrl(url) || state.settings.homepage || engines[state.settings.search] || engines.duckduckgo;
  let onPi = false;
  try {
    // "localhost" on a tile means the Pi, not the device this browser runs on
    const u = new URL(target);
    if (u.hostname === "localhost" || u.hostname === "127.0.0.1") { u.hostname = location.hostname; target = u.href; }
    onPi = u.hostname === location.hostname;
    // a secure page cannot show a plain http page inside itself
    if (location.protocol === "https:" && u.protocol === "http:") { window.open(target, "_blank", "noopener"); return; }
  } catch (e) { /* keep as typed */ }
  showPage(target, onPi);
}

/* Built-in browser of the web version.
   Pages that allow it are shown in a frame (fast, and their sound plays on this device).
   Most sites refuse frames, so those open in the Pi browser: Chromium runs on the Pi,
   this page shows its picture and sends taps, scrolling and typing back. */
const wv = { url: "", onPi: false, mode: "", canBack: false, timer: 0, seq: 0 };
function wvOpenPanel(target) {
  wv.url = target;
  $("wv-url").value = target;
  $("wv-ext").href = target;
  $("settings").hidden = true;
  $("webview").hidden = false;
}
function wvProgress(on) {
  const p = $("wv-prog");
  p.classList.toggle("run", on);
}
async function showPage(target, onPi) {
  wvOpenPanel(target);
  wv.onPi = onPi;
  if (onPi) { showFrame(target, true); return; }
  const seq = ++wv.seq;
  wvProgress(true);
  let r = { ok: false };
  try { r = await api("/api/web/check", { url: target }); } catch (e) { /* use the Pi browser */ }
  if (seq !== wv.seq) return;           // another page was asked for meanwhile
  if (r.ok && r.framable) showFrame(target, false);
  else showPi(target);
}
function showFrame(target, onPi) {
  rbStop();
  wv.mode = "frame";
  wvProgress(false);
  $("rb").hidden = true;
  $("wv-frame").hidden = false;
  $("wv-kbd").hidden = true;
  $("wv-mode").setAttribute("aria-pressed", "false");
  $("wv-note").hidden = onPi;
  $("wv-frame").src = "about:blank";
  $("wv-frame").src = target;
}

/* ---- the Pi browser */
function rbSize() {
  const box = $("rb").getBoundingClientRect();
  return { w: Math.round(box.width), h: Math.round(box.height), scale: Math.min(2, window.devicePixelRatio || 1) };
}
async function showPi(target) {
  wv.mode = "pi";
  $("wv-frame").src = "about:blank";
  $("wv-frame").hidden = true;
  $("wv-note").hidden = true;
  $("rb").hidden = false;
  $("rb-msg").hidden = true;
  $("wv-kbd").hidden = false;
  $("wv-mode").setAttribute("aria-pressed", "true");
  wvProgress(true);
  const r = await api("/api/rb/open", { url: target, ...rbSize() }).catch((e) => ({ ok: false, error: e.message }));
  if (wv.mode !== "pi") return;
  if (!r.ok) {
    wvProgress(false);
    $("rb-text").textContent = r.error + " You can still open the page in a new tab with the button at the top right.";
    $("rb-msg").hidden = false;
    return;
  }
  $("rb-img").src = "/api/rb/stream?n=" + Date.now();
  clearInterval(wv.timer);
  wv.timer = setInterval(rbState, 1200);
  rbShow(r);
}
function rbShow(r) {
  if (!r || !r.ok) return;
  wvProgress(!!r.loading);
  wv.canBack = !!r.back;
  if (r.url && r.url !== "about:blank") {
    wv.url = r.url;
    $("wv-ext").href = r.url;
    if (document.activeElement !== $("wv-url")) $("wv-url").value = r.url;
  }
}
async function rbState() {
  if ($("webview").hidden || wv.mode !== "pi" || document.hidden) return;
  try { rbShow(await api("/api/rb/state")); } catch (e) { /* next time */ }
}
function rbStop() {
  clearInterval(wv.timer);
  $("rb-img").removeAttribute("src");           // ends the picture stream
  $("rb-keys").blur();
}
async function rbGo(action) {
  wvProgress(true);
  try { rbShow(await api("/api/rb/go", { action })); } catch (e) { toast(e.message); }
}
$("rb-retry").addEventListener("click", () => showPi(wv.url));
$("rb-img").addEventListener("error", () => {
  if (wv.mode !== "pi" || !$("rb-img").getAttribute("src")) return;
  $("rb-text").textContent = "The Pi browser stopped. Try again, or open the page in a new tab.";
  $("rb-msg").hidden = false;
});

/* taps and scrolling on the picture */
const touch = { id: null, x0: 0, y0: 0, x: 0, y: 0, moved: false, dx: 0, dy: 0, sending: false, kind: "" };
function rel(ev) {
  const b = $("rb-touch").getBoundingClientRect();
  return { x: (ev.clientX - b.left) / b.width, y: (ev.clientY - b.top) / b.height, w: b.width, h: b.height };
}
async function rbFlushScroll() {
  if (touch.sending || (!touch.dx && !touch.dy)) return;
  touch.sending = true;
  const dx = touch.dx, dy = touch.dy;
  touch.dx = touch.dy = 0;
  try { await api("/api/rb/scroll", { x: touch.x, y: touch.y, dx, dy }); } catch (e) { /* drop it */ }
  touch.sending = false;
  rbFlushScroll();
}
$("rb-touch").addEventListener("pointerdown", (ev) => {
  const p = rel(ev);
  Object.assign(touch, { id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, x: p.x, y: p.y, moved: false, kind: ev.pointerType });
  $("rb-touch").setPointerCapture(ev.pointerId);
});
$("rb-touch").addEventListener("pointermove", (ev) => {
  if (ev.pointerId !== touch.id) return;
  const p = rel(ev);
  if (!touch.moved && Math.hypot(ev.clientX - touch.x0, ev.clientY - touch.y0) < 8) return;
  if (!touch.moved) { touch.moved = true; touch.lx = ev.clientX; touch.ly = ev.clientY; }
  touch.dx += -(ev.clientX - touch.lx) / p.w;           // the page follows the finger
  touch.dy += -(ev.clientY - touch.ly) / p.h;
  touch.lx = ev.clientX; touch.ly = ev.clientY;
  rbFlushScroll();
});
$("rb-touch").addEventListener("pointerup", (ev) => {
  if (ev.pointerId !== touch.id) return;
  touch.id = null;
  if (touch.moved) return;
  const p = rel(ev);
  api("/api/rb/tap", { x: p.x, y: p.y }).catch(() => {});
  if (touch.kind === "mouse") $("rb-keys").focus({ preventScroll: true });   // a real keyboard types straight into the page
});
$("rb-touch").addEventListener("pointercancel", () => { touch.id = null; });
$("rb-touch").addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const p = rel(ev);
  touch.x = p.x; touch.y = p.y;
  touch.dx += ev.deltaX / p.w;
  touch.dy += ev.deltaY / p.h;
  rbFlushScroll();
}, { passive: false });

/* typing: the keyboard button raises the phone's keyboard; letters go to the page as they are typed */
const RB_KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);
$("wv-kbd").addEventListener("click", () => $("rb-keys").focus({ preventScroll: true }));
$("rb-keys").addEventListener("beforeinput", (ev) => {
  ev.preventDefault();
  if (ev.inputType === "insertText" || ev.inputType === "insertReplacementText" || ev.inputType === "insertFromPaste") {
    const text = ev.data || (ev.dataTransfer && ev.dataTransfer.getData("text/plain")) || "";
    if (text) api("/api/rb/type", { text }).catch(() => {});
  } else if (ev.inputType === "deleteContentBackward") {
    api("/api/rb/key", { key: "Backspace" }).catch(() => {});
  } else if (ev.inputType === "insertLineBreak" || ev.inputType === "insertParagraph") {
    api("/api/rb/key", { key: "Enter" }).catch(() => {});
  }
});
$("rb-keys").addEventListener("keydown", (ev) => {
  if (RB_KEYS.has(ev.key) && ev.key !== "Backspace") {    // Backspace arrives as beforeinput
    ev.preventDefault();
    api("/api/rb/key", { key: ev.key }).catch(() => {});
  }
});
let rbResize = 0;
addEventListener("resize", () => {
  clearTimeout(rbResize);
  rbResize = setTimeout(() => { if (wv.mode === "pi" && !$("webview").hidden) api("/api/rb/open", { url: "", ...rbSize() }).catch(() => {}); }, 300);
});

function closePage() {
  wv.seq++;
  rbStop();
  wv.mode = "";
  closePanel($("webview"), () => { $("wv-frame").src = "about:blank"; });   // stops radio, video and camera streams
}
$("wv-home").addEventListener("click", closePage);
$("wv-reload").addEventListener("click", () => {
  if (wv.mode === "pi") { rbGo("reload"); return; }
  if (wv.url) showFrame(wv.url, wv.onPi);
});
$("wv-back").addEventListener("click", () => {
  if (wv.mode === "pi" && wv.canBack) { rbGo("back"); return; }
  closePage();
});
$("wv-mode").addEventListener("click", () => { if (wv.url) (wv.mode === "pi" ? showFrame(wv.url, wv.onPi) : showPi(wv.url)); });
$("wv-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("wv-url").value.trim();
  $("wv-url").blur();
  if (q) openBrowser(q);
});

/* ---------- built-in apps: camera and radio */
// <img> and <audio> cannot send the token header, so on the Pi it goes in the address
const streamUrl = (path) => path + "?n=" + Date.now() + (WEB ? "" : "&t=" + encodeURIComponent(TOKEN));

function openCamera() {
  $("cam-msg").hidden = true;
  $("cam-img").hidden = false;
  $("app-camera").hidden = false;
  $("cam-img").src = streamUrl(detView ? "/api/detect/stream" : "/api/camera/stream");
  setCamTab(camTab);
  loadCameraControls();
  detRefresh(true);
  clearInterval(detTimer);
  detTimer = setInterval(detRefresh, 4000);
}

/* camera controls: light, flip, mirror, picture size, brightness, contrast, frame rate */
const CAM_SIZES = [["qvga", "320"], ["vga", "640"], ["svga", "800"], ["xga", "1024"]];
function paintCamSize(current) {
  const box = $("cam-size");
  box.textContent = "";
  for (const [value, label] of CAM_SIZES) {
    const b = el("button", value === current ? "on" : "", label);
    b.addEventListener("click", async () => { if (await camSet("framesize", value)) paintCamSize(value); });
    box.appendChild(b);
  }
}
async function camSet(name, value) {
  try {
    const r = await api("/api/camera/control", { var: name, val: value });
    if (!r.ok) toast(r.error || "The camera did not take that.");
    return !!r.ok;
  } catch (e) { toast(e.message); return false; }
}
/* tabs under the picture: events and clips from motion detection, and the picture's own settings */
let camTab = "events", camTabChosen = false;
function setCamTab(name, byUser) {
  camTab = name;
  if (byUser) camTabChosen = true;
  const box = $("cam-tabs");
  box.textContent = "";
  for (const [id, label] of [["clips", "Clips"], ["events", "Events"], ["picture", "Picture"]]) {
    const b = el("button", id === camTab ? "on" : "", label);
    b.addEventListener("click", () => setCamTab(id, true));
    box.appendChild(b);
    $("pane-" + id).hidden = id !== camTab;
  }
}
function camFull(on) { $("cam-app").classList.toggle("full", on); }
$("cam-full").addEventListener("click", () => camFull(true));
$("cam-exit").addEventListener("click", () => camFull(false));
$("cam-img").addEventListener("click", () => camFull(!$("cam-app").classList.contains("full")));
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") camFull(false); });

async function loadCameraControls() {
  const card = $("app-camera");
  let r;
  try { r = await api("/api/camera/settings"); } catch (e) { r = { ok: false }; }
  $("cam-controls").hidden = !r.ok;
  $("cam-controls-off").hidden = r.ok;
  $("cam-light").hidden = !r.ok;
  $("cam-photo").hidden = !r.ok || !WEB;      // saving a file only makes sense in a remote browser
  if (!r.ok) return;
  const s = r.settings;
  card.querySelectorAll("button[data-cam]").forEach((b) => b.setAttribute("aria-pressed", String(!!s[b.dataset.cam])));
  card.querySelectorAll("input[data-cam]").forEach((i) => {
    const v = s[i.dataset.cam];
    if (typeof v === "number" && v >= Number(i.min)) i.value = v;
    i.nextElementSibling.textContent = i.dataset.cam === "fps" && !s.fps ? "max" : i.value;
    paintRange(i);
  });
  paintCamSize(s.framesize);
  $("cam-photo").href = "/api/camera/capture";
}
document.querySelectorAll("#app-camera button[data-cam]").forEach((b) => {
  b.addEventListener("click", async () => {
    const on = b.getAttribute("aria-pressed") !== "true";
    if (await camSet(b.dataset.cam, on)) b.setAttribute("aria-pressed", String(on));
  });
});
document.querySelectorAll("#app-camera input[data-cam]").forEach((i) => {
  i.addEventListener("input", () => { i.nextElementSibling.textContent = i.value; paintRange(i); });
  i.addEventListener("change", () => camSet(i.dataset.cam, Number(i.value)));
});
function closeCamera() {
  clearInterval(detTimer);
  camFull(false);
  closePanel($("app-camera"), () => $("cam-img").removeAttribute("src"));    // ends the stream, so the camera can rest
}
$("cam-img").addEventListener("error", () => {
  if ($("app-camera").hidden) return;
  $("cam-img").hidden = true;
  $("cam-chip").hidden = true;
  camFull(false);
  $("cam-text").textContent = "The camera is not answering at " + state.settings.camera +
    ". Check that it is switched on, or change its address in Settings.";
  $("cam-msg").hidden = false;
});
$("cam-retry").addEventListener("click", openCamera);
$("camera-back").addEventListener("click", closeCamera);

/* motion detection inside the Camera app: what it sees, events, recorded clips */
let detTimer = 0, detView = false, detClipsAt = 0;
const clock = (t) => new Date(t * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
function dayClock(t) {
  const d = new Date(t * 1000);
  const today = d.toDateString() === new Date().toDateString();
  return (today ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + ", ") + clock(t);
}
function eventText(e) {
  if (e.type === "clear") return "Nothing in view";
  return (e.classes || []).join(", ") || "Motion";
}
async function detRefresh(force) {
  if ($("app-camera").hidden) return;
  let r;
  try { r = await api("/api/detect/state"); } catch (e) { return; }
  $("det-record").hidden = $("det-view").hidden = !r.ok;
  if (Date.now() > detPowerHold) $("det-power").setAttribute("aria-pressed", String(r.service === "active"));
  const chip = $("cam-chip");
  if (!r.ok) {
    chip.hidden = true;
    $("det-state").textContent = r.service === "not installed"
      ? "Motion detection is not installed on this Pi, so there are no events or clips. It is a separate service on port 8084."
      : r.service === "active" ? "Motion detection is starting…"
      : "Motion detection is switched off. Turn it on with the Motion button.";
    $("det-events").textContent = "";
    $("det-clips").textContent = "";
    if (!camTabChosen && camTab !== "picture") setCamTab("picture");
    if (detView) { detView = false; $("det-view").setAttribute("aria-pressed", "false"); openCamera(); }
    return;
  }
  chip.hidden = $("cam-img").hidden || !(r.recording || r.seen.length);
  chip.classList.toggle("alert", r.recording);
  chip.textContent = r.recording ? "Recording" : r.seen.join(", ");
  $("det-state").textContent = !r.camera ? "Waiting for the camera…"
    : r.recording ? "Recording a clip…"
    : r.seen.length ? "In view now: " + r.seen.join(", ") : "Watching. Nothing in view.";
  const list = $("det-events");
  list.textContent = "";
  if (!r.events.length) list.appendChild(el("li", "", "No events yet."));
  for (const e of r.events) {
    const li = el("li");
    li.append(el("time", "", clock(e.t)), el("span", "", eventText(e)));
    list.appendChild(li);
  }
  if (force || Date.now() - detClipsAt > 20000) loadClips();
}
async function loadClips() {
  detClipsAt = Date.now();
  let r;
  try { r = await api("/api/detect/clips"); } catch (e) { return; }
  if (!r.ok) return;
  const list = $("det-clips");
  list.textContent = "";
  if (!r.clips.length) { const li = el("li", "", "No clips yet."); li.style.padding = "8px 16px"; list.appendChild(li); }
  for (const c of r.clips) {
    const li = el("li"), b = el("button");
    b.append(icon("play"), el("span", "ssid", (c.classes || []).join(", ") || "Clip"),
             el("span", "when", dayClock(c.t)));
    b.addEventListener("click", () => {
      $("clip-title").textContent = ((c.classes || []).join(", ") || "Clip") + " · " + dayClock(c.t);
      $("clip-video").src = "/api/detect/clip?id=" + encodeURIComponent(c.id) + (WEB ? "" : "&t=" + encodeURIComponent(TOKEN));
      openSheet("sheet-clip");
      $("clip-video").play().catch(() => {});
    });
    li.appendChild(b);
    list.appendChild(li);
  }
}
/* the Motion button switches the detection service on and off */
let detPowerHold = 0;       // while the service changes state, the button keeps what was just chosen
$("det-power").addEventListener("click", async () => {
  const btn = $("det-power"), on = btn.getAttribute("aria-pressed") !== "true";
  btn.disabled = true;
  try {
    const r = await api("/api/detect/power", { on });
    if (!r.ok) { toast(r.error); return; }
    btn.setAttribute("aria-pressed", String(on));
    detPowerHold = Date.now() + 6000;
    toast(on ? "Motion detection is starting…" : "Motion detection is off.");
    setTimeout(() => detRefresh(true), 1500);
  } catch (e) { toast(e.message); } finally { btn.disabled = false; }
});

$("det-view").addEventListener("click", () => {
  detView = !detView;
  $("det-view").setAttribute("aria-pressed", String(detView));
  $("cam-img").src = streamUrl(detView ? "/api/detect/stream" : "/api/camera/stream");
});
$("det-record").addEventListener("click", async () => {
  try {
    const r = await api("/api/detect/record", {});
    toast(r.ok ? "Recording 10 seconds…" : r.error);
    setTimeout(loadClips, 14000);
  } catch (e) { toast(e.message); }
});

let radioTimer = 0;
const radioOn = () => !$("radio-audio").paused;
async function radioRefresh() {
  let r;
  try { r = await api("/api/radio/now"); } catch (e) { return; }
  const np = r.ok ? r.nowPlaying : null;
  $("radio-title").textContent = !r.ok ? "Radio is off" : np ? np.title : "The playlist is empty";
  $("radio-sub").textContent = !r.ok ? r.error
    : np ? (np.listeners === 1 ? "1 listener" : (np.listeners || 0) + " listeners") : "Add tracks in the radio's admin page.";
  $("radio-play").disabled = !np && !radioOn();
  $("radio-skip").disabled = !np;
  $("w-radio").hidden = !widgetOn("radio");
  $("w-radio-title").textContent = !r.ok ? "Radio is off" : np ? np.title : "The playlist is empty";
  $("w-radio-sub").textContent = !np ? "Radio" : radioOn() ? "Playing" : np.listeners === 1 ? "1 listener" : (np.listeners || 0) + " listeners";
  $("w-radio-play").disabled = !np && !radioOn();
  $("w-radio-skip").disabled = !np;
}
function openRadio() {
  $("app-radio").hidden = false;
  radioRefresh();
  clearInterval(radioTimer);
  radioTimer = setInterval(radioRefresh, 5000);
}
$("radio-back").addEventListener("click", () => {     // the music keeps playing
  closePanel($("app-radio"));
  clearInterval(radioTimer);
});
function radioStop() {
  const a = $("radio-audio");
  a.pause();
  a.removeAttribute("src");
  a.load();
  $("radio-play").textContent = "Play";
  $("w-radio-play").textContent = "Play";
  document.body.classList.remove("radio-on");
}
function radioToggle() {
  const a = $("radio-audio");
  if (radioOn()) { radioStop(); return; }
  a.src = streamUrl("/api/radio/stream");
  $("radio-play").textContent = "Pause";
  $("w-radio-play").textContent = "Pause";
  document.body.classList.add("radio-on");
  a.play().catch(() => { radioStop(); toast("Could not start the radio."); radioRefresh(); });
}
$("radio-play").addEventListener("click", radioToggle);
$("w-radio-play").addEventListener("click", radioToggle);
$("w-radio-open").addEventListener("click", openRadio);
$("w-radio-skip").addEventListener("click", () => $("radio-skip").click());
$("radio-audio").addEventListener("error", () => { if ($("radio-audio").getAttribute("src")) radioStop(); });
$("radio-skip").addEventListener("click", async () => {
  try {
    const r = await api("/api/radio/skip", {});
    if (!r.ok) toast(r.error || "Could not skip the track.");
    setTimeout(radioRefresh, 1200);
  } catch (e) { toast(e.message); }
});

/* ---------- widgets: weather, radio, camera */
const widgetOn = (name) => (state.settings.widgets || []).includes(name);
const svgNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const n = document.createElementNS(svgNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
const CLOUD = "M7 18a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17.2 9.1 4.5 4.5 0 0 1 17 18H7z";
function weatherIcon(kind) {
  const svg = svgEl("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
  const sun = (cx, cy, r) => svg.appendChild(svgEl("circle", { cx, cy, r }));
  if (kind === "sun") {
    sun(12, 12, 4.5);
    for (let i = 0; i < 8; i++) {
      const a = (i * Math.PI) / 4;
      svg.appendChild(svgEl("line", { x1: 12 + 7 * Math.cos(a), y1: 12 + 7 * Math.sin(a), x2: 12 + 9.5 * Math.cos(a),
        y2: 12 + 9.5 * Math.sin(a), stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round" }));
    }
    return svg;
  }
  if (kind === "partly") sun(17, 7, 3.5);
  const low = kind === "rain" || kind === "snow" || kind === "storm";
  svg.appendChild(svgEl("path", { d: CLOUD, transform: low ? "translate(0,-3)" : "" }));
  if (kind === "rain") for (const x of [8, 12, 16]) svg.appendChild(svgEl("line", { x1: x, y1: 17.5, x2: x - 1, y2: 21, stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round" }));
  if (kind === "snow") for (const x of [8, 12, 16]) svg.appendChild(svgEl("circle", { cx: x, cy: 19.5, r: 1.2 }));
  if (kind === "storm") svg.appendChild(svgEl("path", { d: "M13 16l-3 4h2.5l-1 3 3.5-4.5h-2.5l1-2.5z" }));
  return svg;
}
/* The weather card shows the weather itself: its colour is the sky, and the sky moves —
   sun with turning rays, drifting clouds, rain, snow, lightning, fog, wind streaks, stars at night. */
function weatherScene(r) {
  const box = $("w-weather");
  let scene = box.querySelector(".w-scene");
  if (!scene) {
    scene = document.createElement("span");
    scene.className = "w-scene";
    scene.setAttribute("aria-hidden", "true");
    box.prepend(scene);
  }
  const sky = !r.ok ? "partly" : r.fog ? "fog" : r.kind;
  const windy = r.ok && r.wind >= 25;
  const night = !!(r.ok && r.night);
  const key = sky + "|" + windy + "|" + night;
  box.dataset.sky = sky;
  box.classList.toggle("night", night);
  box.classList.toggle("windy", windy);
  if (scene.dataset.key === key) return;
  scene.dataset.key = key;
  const rnd = (a, b) => a + Math.random() * (b - a);
  const parts = [];
  if (sky === "sun" || sky === "partly") parts.push(night ? "<i class='moon'></i>" : "<i class='sun'><b></b></i>");
  if (night) for (let i = 0; i < 26; i++)
    parts.push(`<i class='star' style='left:${rnd(2, 98)}%;top:${rnd(2, 60)}%;animation-delay:${rnd(-4, 0)}s'></i>`);
  const clouds = { sun: 0, partly: 2, cloud: 4, rain: 3, storm: 3, snow: 3, fog: 1 }[sky] || 0;
  for (let i = 0; i < clouds; i++)
    parts.push(`<i class='cloud' style='top:${rnd(-8, 34)}%;--s:${rnd(0.7, 1.35).toFixed(2)};animation-duration:${rnd(38, 70).toFixed(0)}s;animation-delay:${rnd(-70, 0).toFixed(0)}s'></i>`);
  if (sky === "rain" || sky === "storm")
    for (let i = 0; i < 46; i++)
      parts.push(`<i class='drop' style='left:${rnd(-10, 105)}%;animation-duration:${rnd(0.55, 0.9).toFixed(2)}s;animation-delay:${rnd(-1, 0).toFixed(2)}s;opacity:${rnd(0.35, 0.8).toFixed(2)}'></i>`);
  if (sky === "snow")
    for (let i = 0; i < 40; i++)
      parts.push(`<i class='flake' style='left:${rnd(-5, 100)}%;--d:${rnd(-30, 30).toFixed(0)}px;width:${rnd(3, 7).toFixed(1)}px;animation-duration:${rnd(4, 9).toFixed(1)}s;animation-delay:${rnd(-9, 0).toFixed(1)}s'></i>`);
  if (sky === "storm") parts.push("<i class='flash'></i><i class='bolt'></i>");
  if (sky === "fog") for (let i = 0; i < 4; i++)
    parts.push(`<i class='mist' style='top:${18 + i * 20}%;animation-duration:${rnd(16, 28).toFixed(0)}s;animation-delay:${rnd(-28, 0).toFixed(0)}s'></i>`);
  if (windy) for (let i = 0; i < 9; i++)
    parts.push(`<i class='gust' style='top:${rnd(10, 85).toFixed(0)}%;width:${rnd(30, 70).toFixed(0)}%;animation-duration:${rnd(1.6, 3).toFixed(1)}s;animation-delay:${rnd(-3, 0).toFixed(1)}s'></i>`);
  scene.innerHTML = parts.join("");
}
let weatherAt = 0;
async function weatherRefresh(force) {
  const box = $("w-weather");
  if (!widgetOn("weather")) { box.hidden = true; return; }
  if (!force && Date.now() - weatherAt < 600000) return;
  let r;
  try { r = await api("/api/weather"); } catch (e) { return; }
  weatherAt = Date.now();
  box.hidden = false;
  weatherScene(r);
  $("w-weather-icon").replaceChildren(r.ok ? weatherIcon(r.kind) : weatherIcon("partly"));
  $("w-weather-icon").dataset.kind = r.ok ? r.kind : "partly";
  $("w-weather-temp").textContent = r.ok ? r.temp + "°" : "–";
  $("w-weather-text").textContent = r.ok ? r.text + " · " + r.place : "Weather";
  $("w-weather-sub").textContent = r.ok ? "Feels like " + r.feels + "° · wind " + r.wind + " km/h"
    : r.unset ? "Choose your town in Settings to see the forecast." : r.error;
  const days = $("w-weather-days");
  days.textContent = "";
  for (const d of r.ok ? r.days.slice(1) : []) {
    const cell = el("span");
    cell.append(el("b", "", new Date(d.date + "T12:00").toLocaleDateString("en-GB", { weekday: "short" })),
                document.createTextNode(d.max + "° / " + d.min + "°"));
    cell.title = d.text;
    days.appendChild(cell);
  }
}
async function cameraWidget() {
  const box = $("w-camera");
  const grid = $("dash-grid");
  if (!widgetOn("camera") || isLite()) { box.hidden = true; grid.classList.add("no-cam"); dashMotion(null); return; }
  const img = new Image();
  img.onload = () => { $("w-camera-img").src = img.src; box.hidden = false; grid.classList.remove("no-cam"); };
  img.onerror = () => { box.hidden = true; grid.classList.add("no-cam"); };
  img.src = streamUrl("/api/camera/capture");
  let r = null;
  try { r = await api("/api/detect/state"); } catch (e) { /* keep the old text */ }
  dashMotion(r);
  if (!r) return;
  const last = r.ok && r.events.find((e) => e.type !== "clear");
  $("d-cam-live").textContent = r.ok && r.seen.length ? "In view: " + r.seen.join(", ") : "Camera";
  $("d-cam-live").classList.toggle("alert", !!(r.ok && (r.recording || r.seen.length)));
  $("w-camera-big").textContent = last ? clock(last.t) : "Quiet";
  $("w-camera-text").textContent = last ? "Last motion: " + eventText(last) + ". Tap to open the camera."
    : r.ok ? "No motion seen. Tap to open the camera." : "Tap to open the camera.";
}

/* quick tiles: motion detection on and off, and the Pi's temperature */
function dashMotion(r) {
  const tile = $("d-motion");
  if (isLite() || !r || r.service === "not installed" || r.service === undefined) { tile.hidden = true; return; }
  tile.hidden = false;
  const on = r.service === "active";
  tile.setAttribute("aria-pressed", String(on));
  $("d-motion-text").textContent = on ? "Watching" : "Off";
}
$("d-motion").addEventListener("click", async () => {
  const tile = $("d-motion"), on = tile.getAttribute("aria-pressed") !== "true";
  tile.disabled = true;
  try {
    const r = await api("/api/detect/power", { on });
    if (!r.ok) toast(r.error);
    else { tile.setAttribute("aria-pressed", String(on)); $("d-motion-text").textContent = on ? "Starting…" : "Off"; }
  } catch (e) { toast(e.message); } finally { tile.disabled = false; }
});
/* ---------- what sits behind the widgets' content, so they are not plain coloured squares */
function cardArt(id, html) {
  const card = $(id);
  if (!card || card.querySelector(".d-art")) return null;
  const art = document.createElement("span");
  art.className = "d-art";
  art.setAttribute("aria-hidden", "true");
  art.innerHTML = html;
  card.prepend(art);
  return art;
}
cardArt("w-radio", "<span class='bars'>" + "<i></i>".repeat(32) + "</span><span class='disc'></span>");
cardArt("d-motion", "<span class='rings'></span><span class='sweep'></span>");
cardArt("d-system", "<svg viewBox='0 0 100 40' preserveAspectRatio='none'><path class='area'/><path class='line'/></svg>");
cardArt("w-site", "<span class='hero'></span><b class='letter'></b><span class='dots'></span>");
cardArt("w-printer", "<span class='layers'></span><span class='model'></span>");
cardArt("w-update", "<span class='arcs'></span>");
cardArt("w-map", "");
document.querySelectorAll("#w-radio .bars i").forEach((b, i) => {
  b.style.setProperty("--h", (25 + Math.abs(Math.sin(i * 1.7)) * 75).toFixed(0) + "%");
  b.style.animationDelay = (-(i * 137) % 1100) + "ms";
  b.style.animationDuration = (700 + (i * 97) % 600) + "ms";
});
/* the system tile draws the Pi's temperature over the last minutes */
const sysTemps = [];
function sysSpark(t) {
  if (typeof t !== "number") return;
  sysTemps.push(t);
  if (sysTemps.length > 48) sysTemps.shift();
  if (sysTemps.length < 2) return;
  const lo = Math.min(...sysTemps) - 2, hi = Math.max(...sysTemps) + 2;
  const pts = sysTemps.map((v, i) => [(i / (sysTemps.length - 1)) * 100, 38 - ((v - lo) / (hi - lo)) * 30]);
  const line = pts.map(([x, y], i) => (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1)).join(" ");
  const art = $("d-system").querySelector(".d-art svg");
  art.querySelector(".line").setAttribute("d", line);
  art.querySelector(".area").setAttribute("d", line + " L100 40 L0 40 Z");
}

$("d-system").addEventListener("click", openMonitor);
function dashSystem() {
  const s = state.status || {};
  sysSpark(s.temp);
  $("d-system-temp").textContent = typeof s.temp === "number" ? Math.round(s.temp) + "°C" : "–";
  $("d-system").classList.toggle("hot", typeof s.temp === "number" && s.temp >= 75);
  $("d-system-text").textContent = s.memory ? Math.round((s.memory.used / s.memory.total) * 100) + "% memory" : "System";
}

/* the dock: Home, Camera, Apps, Settings. The button of what is open is lit. */
const DOCK_PANELS = ["app-camera", "settings", "menu", "app-files", "app-monitor", "app-printer", "app-radio", "webview"];
function dockActive() {
  const open = DOCK_PANELS.filter((id) => !$(id).hidden && !$(id).classList.contains("leaving"));
  const top = open.find((id) => id !== "menu") || open[0];
  return !top ? "home" : top === "app-camera" ? "camera" : top === "settings" ? "settings" : "apps";
}
function paintDock() {
  const now = dockActive();
  document.querySelectorAll(".dock-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.dock === now);
    b.setAttribute("aria-current", b.dataset.dock === now ? "page" : "false");
  });
}
function closeEverything() {
  if (!$("app-camera").hidden) closeCamera();
  if (!$("webview").hidden) closePage();
  if (!$("app-monitor").hidden) $("monitor-back").click();
  if (!$("app-radio").hidden) $("radio-back").click();
  if (!$("app-printer").hidden) $("printer-back").click();
  for (const id of ["app-files", "settings"]) if (!$(id).hidden) closePanel($(id));
  if (!$("menu").hidden) closeMenu();
  closeSheets();
}
function dockGo(where) {
  if (where === dockActive() && where !== "home") return;
  closeEverything();
  if (where === "camera") openCamera();
  else if (where === "apps") openMenu();
  else if (where === "settings") openSettings();
}
document.querySelectorAll(".dock-btn").forEach((b) => b.addEventListener("click", () => dockGo(b.dataset.dock)));
new MutationObserver(paintDock).observe(document.body, { subtree: true, attributes: true, attributeFilter: ["hidden", "class"] });
$("w-camera").addEventListener("click", openCamera);
/* Featured site: the first site tile, as a square on Home */
function siteWidget() {
  const box = $("w-site");
  const tile = (state.settings.tiles || [])[0];
  box.hidden = !widgetOn("site") || !tile;
  if (!tile) return;
  $("w-site-name").textContent = tile.name;
  document.querySelector("#w-site .d-site-mark").textContent = tile.name.charAt(0).toUpperCase();
  $("w-site").querySelector(".d-art .letter").textContent = tile.name.charAt(0).toUpperCase();
  let host = "";
  try { host = new URL(tile.url).hostname.replace(/^www\./, ""); } catch (e) { /* not a full address */ }
  $("w-site-host").textContent = host;
  box.onclick = () => openBrowser(tile.url);
  siteHero(box, tile, host);
}
/* the site's hero section: its main picture, headline and the line under it */
async function siteHero(box, tile, host) {
  const key = tile.url + "|" + tile.name;
  if (box.dataset.hero === key) return;
  box.dataset.hero = key;
  box.classList.remove("has-hero");
  const q = "?u=" + encodeURIComponent(tile.url) + (WEB ? "" : "&t=" + encodeURIComponent(TOKEN));
  let h;
  try { h = await (await fetch("/api/site/hero" + q, { credentials: "same-origin" })).json(); } catch (e) { return; }
  if (box.dataset.hero !== key || !h.ok) return;
  if (h.title) $("w-site-name").textContent = h.title;
  $("w-site-host").textContent = h.text || host;
  $("w-site-brand").textContent = tile.name;
  const pic = box.querySelector(".d-art .hero");
  pic.style.backgroundImage = h.image ? "url('/api/site/hero-image" + q + "&v=" + h.v + "')" : "";
  box.classList.toggle("has-hero", true);
  box.classList.toggle("has-pic", !!h.image);
}

/* the map: OpenStreetMap around the town chosen for the weather */
let mapKey = "";
function mapWidget() {
  const box = $("w-map");
  box.hidden = !widgetOn("map");
  if (box.hidden) return;
  const place = state.settings.weather;
  $("w-map-msg").hidden = !!place;
  const layer = $("w-map-tiles");
  if (!place) { layer.textContent = ""; mapKey = ""; box.onclick = openSettings; return; }
  const w = box.clientWidth, h = box.clientHeight;
  const key = [place.lat, place.lon, w, h].join(",");
  box.onclick = () => openBrowser("https://www.openstreetmap.org/?mlat=" + place.lat + "&mlon=" + place.lon + "#map=15/" + place.lat + "/" + place.lon);
  if (key === mapKey || !w) return;
  mapKey = key;
  const z = 14, T = 256, n = 2 ** z, rad = (place.lat * Math.PI) / 180;
  const fx = ((place.lon + 180) / 360) * n;
  const fy = ((1 - Math.asinh(Math.tan(rad)) / Math.PI) / 2) * n;
  const x0 = Math.floor(fx - w / 2 / T), y0 = Math.floor(fy - h / 2 / T);
  const x1 = Math.floor(fx + w / 2 / T), y1 = Math.floor(fy + h / 2 / T);
  layer.textContent = "";
  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      const img = document.createElement("img");
      img.alt = "";
      img.decoding = "async";
      img.src = "/api/map/tile?z=" + z + "&x=" + ((tx % n) + n) % n + "&y=" + ty + (WEB ? "" : "&t=" + encodeURIComponent(TOKEN));
      img.style.left = Math.round(w / 2 + (tx - fx) * T) + "px";
      img.style.top = Math.round(h / 2 + (ty - fy) * T) + "px";
      layer.appendChild(img);
    }
  }
}
addEventListener("resize", () => { mapKey = ""; if (state.settings) mapWidget(); });

/* updates as a small square on Home: tap to check, tap again to install */
let updateAt = 0, updateBusy = false;
function updateWidgetShow(text, available) {
  $("w-update-text").textContent = text;
  $("w-update-dot").hidden = !available;
  $("w-update").classList.toggle("ready", !!available);
}
async function updateCheck(quiet) {
  if (updateBusy) return null;
  updateBusy = true;
  $("w-update").classList.add("busy");
  updateWidgetShow("Checking", false);
  let r;
  try { r = await api("/api/update/check", {}); } catch (e) { r = { ok: false, error: e.message }; }
  updateBusy = false;
  updateAt = Date.now();
  $("w-update").classList.remove("busy");
  if (!r.ok) { updateWidgetShow("Update", false); if (!quiet) toast(r.error); return r; }
  updateWidgetShow(r.available ? "Install" : "Up to date", r.available);
  $("update-apply").hidden = !r.available;
  $("update-text").textContent = r.available ? "An update is ready: " + r.summary : "HomeOS " + state.boot.info.version + " is up to date.";
  if (!quiet && !r.available) toast("HomeOS " + state.boot.info.version + " is up to date.");
  return r;
}
$("w-update").addEventListener("click", async () => {
  if ($("w-update").classList.contains("ready")) { $("update-apply").click(); return; }
  const r = await updateCheck(false);
  if (r && r.ok && r.available) $("update-apply").click();
});
function updateWidget() {
  $("w-update").hidden = !widgetOn("update");
  // a quiet look once after start and then every six hours, so a waiting update shows a dot
  if (!$("w-update").hidden && Date.now() - updateAt > 6 * 3600 * 1000) updateCheck(true);
}

/* ---------- Home layout: columns that always fill the screen, so no space is left empty.
   Each card grows in proportion to its weight; a hidden card simply leaves the column,
   and a column with nothing in it disappears so the others widen. */
const DASH = {
  big:    { width: 1.15, cards: [["w-camera", 1.6], ["w-printer", 1]] },
  wide:   { width: 1.15, cards: [["w-weather", 1], ["w-radio", 1]] },
  narrow: { width: 0.62, cards: [["w-site", 1.3], ["d-motion", 1], ["d-system", 1], ["w-map", 1.1], ["w-update", 0.6]] },
};
let dashSig = "";
function layoutDash() {
  const grid = $("dash-grid");
  const shown = (id) => !$(id).hidden;
  const cols = [];
  const small = DASH.narrow.cards.filter(([id]) => shown(id));
  // a phone held upright: rows that share out the whole screen, so nothing needs scrolling
  const fit = innerWidth <= 760 && innerHeight > innerWidth && innerHeight >= 600;
  grid.classList.toggle("fit", fit);
  if (fit) {
    const row = (weight, ids) => { const cards = ids.filter(shown).map((id) => [id, 1]); if (cards.length) cols.push({ width: weight, cards }); };
    row(1.45, ["w-camera"]);
    row(0.9, ["w-printer"]);
    row(1.05, ["w-weather", "w-radio"]);
    const ids = small.map(([id]) => id), per = ids.length > 4 ? 3 : 2;
    for (let i = 0; i < ids.length; i += per) row(0.92, ids.slice(i, i + per));
  } else
  // a tablet or screen standing upright: one tall column of the big cards, one of the small ones
  if (innerHeight > innerWidth * 1.05 && innerWidth > 760) {
    const tall = [...DASH.big.cards, ...DASH.wide.cards].filter(([id]) => shown(id));
    const side = DASH.narrow.cards.filter(([id]) => shown(id));
    if (tall.length) cols.push({ width: 1.45, cards: tall });
    if (side.length) cols.push({ width: 0.8, cards: side });
  } else {
  for (const key of ["big", "wide"]) {
    const cards = DASH[key].cards.filter(([id]) => shown(id));
    if (cards.length) cols.push({ width: DASH[key].width, cards });
  }
  if (small.length <= 2) {
    if (small.length) cols.push({ width: DASH.narrow.width, cards: small });
  } else {                           // two narrow columns of about the same weight, order kept
    const total = small.reduce((t, [, w]) => t + w, 0);
    let acc = 0, cut = 0;
    while (cut < small.length - 1 && acc + small[cut][1] / 2 < total / 2) acc += small[cut++][1];
    cut = Math.max(1, Math.min(small.length - 1, cut));
    cols.push({ width: DASH.narrow.width, cards: small.slice(0, cut) });
    cols.push({ width: DASH.narrow.width, cards: small.slice(cut) });
  }
  }
  const sig = (fit ? "fit:" : "") + cols.map((c) => c.cards.map(([id]) => id).join("+")).join("|");
  if (sig !== dashSig) {
    dashSig = sig;
    const spare = el("div", "dash-spare");
    spare.hidden = true;
    for (const group of Object.values(DASH)) for (const [id] of group.cards) if (!shown(id)) spare.appendChild($(id));
    const nodes = cols.map((c) => {
      const col = el("div", "dash-col");
      col.style.setProperty("--w", c.width);
      for (const [id, weight] of c.cards) { $(id).style.setProperty("--g", weight); col.appendChild($(id)); }
      return col;
    });
    grid.replaceChildren(...nodes, spare);
    // on a phone the cards flow two to a row: a small card left alone takes the whole row
    const smalls = small.map(([id]) => $(id));
    const wideOne = smalls.length % 2 === 1 ? (smalls.find((n) => n.id === "w-site") || smalls[0]) : null;
    smalls.forEach((n) => n.classList.toggle("span-2", n === wideOne));
    mapKey = "";                     // the map card may have a new size
    requestAnimationFrame(mapWidget);
  }
}
// turning the screen between upright and lying down changes the columns
addEventListener("resize", () => { if (state.settings) layoutDash(); });
// re-lay the columns only when a card itself appears or disappears (not on changes inside a card)
new MutationObserver((list) => {
  if (state.settings && list.some((m) => m.target.classList.contains("dcard") && (m.oldValue !== null) !== m.target.hidden)) layoutDash();
}).observe($("dash-grid"), { subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ["hidden"] });

/* ---------- Printer: a Bambu Lab printer on the home network */
let printerCfg = null, printerTimer = 0, printerLast = null;
const PR_STATES = { RUNNING: "Printing", PAUSE: "Paused", FINISH: "Finished", FAILED: "Failed", PREPARE: "Preparing",
                    SLICING: "Preparing", IDLE: "Ready", OFFLINE: "Offline" };
function prState(r) { return !r.online ? "Offline" : PR_STATES[r.state] || (r.state ? r.state.charAt(0) + r.state.slice(1).toLowerCase() : "Ready"); }
function prLeft(min) {
  if (!min) return "";
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return (h ? h + " h " : "") + m + " min left";
}
function prTemp(v, t) { return typeof v === "number" ? Math.round(v) + "°" + (t ? " / " + Math.round(t) + "°" : "") : "–"; }
const prBusy = (r) => r.online && (r.state === "RUNNING" || r.state === "PAUSE" || r.state === "PREPARE");

async function loadPrinterCfg() {
  try { printerCfg = await api("/api/printer/config"); } catch (e) { return; }
  for (const id of ["pr-form-app", "pr-form-set"]) {
    const f = $(id);
    if (document.activeElement && f.contains(document.activeElement)) continue;
    f.host.value = printerCfg.host || "";
    f.serial.value = printerCfg.serial || "";
    f.code.value = "";
    f.code.placeholder = printerCfg.has_code ? "Saved (type to change)" : "8 letters and digits";
  }
  settingsSummaries();
}
function prSteps(form, steps, busy) {
  const ul = document.querySelector('[data-steps="' + form.id + '"]');
  ul.textContent = "";
  ul.hidden = !steps.length && !busy;
  for (const st of steps) ul.appendChild(el("li", st.ok === true ? "good" : st.ok === false ? "bad" : "warn", st.text));
  if (busy) ul.appendChild(el("li", "busy", "Checking the printer… (up to 20 seconds)"));
}
async function savePrinter(form) {
  const body = { host: form.host.value.trim(), code: form.code.value.trim(), serial: form.serial.value.trim() };
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true;
  prSteps(form, [], true);
  try {
    const r = await api("/api/printer/config", body);
    prSteps(form, r.steps || (r.ok ? [] : [{ ok: false, text: r.error }]), false);
    if (!r.ok) return;                // the typed code stays in the field, so it can be corrected
    toast("Printer connected.");
    await loadPrinterCfg();
    printerRefresh();
    printerWidgets();
  } catch (e) { prSteps(form, [{ ok: false, text: e.message }], false); }
  finally { btn.disabled = false; }
}
for (const id of ["pr-form-app", "pr-form-set"]) {
  $(id).addEventListener("submit", (ev) => { ev.preventDefault(); savePrinter($(id)); });
}

function printerShow(r) {
  printerLast = r;
  // the connection form stays at hand until the printer answers, so a wrong code can be fixed right here
  $("pr-setup").hidden = r.configured && r.online;
  // what the saved connection is doing right now, shown next to the form
  for (const p of document.querySelectorAll(".pr-live")) {
    p.hidden = !r.configured || r.online;
    p.textContent = r.error || "Saved. Connecting to the printer…";
    p.classList.toggle("bad", !!r.error);
  }
  $("pr-main").hidden = !r.configured;
  if (!r.configured) return;
  const st = prState(r);
  $("pr-state-badge").textContent = st;
  $("pr-state-badge").classList.toggle("alert", r.state === "FAILED" || !r.online);
  $("pr-file-label").textContent = st;
  $("pr-file").textContent = r.online ? (r.file || (prBusy(r) ? "Printing" : "Nothing printing")) : (r.error || "Connecting…");
  $("pr-bar").style.width = (prBusy(r) || r.state === "FINISH" ? r.percent : 0) + "%";
  $("pr-pct").textContent = prBusy(r) || r.state === "FINISH" ? r.percent + "%" : "";
  $("pr-layer").textContent = r.layers ? "Layer " + r.layer + " / " + r.layers : "";
  $("pr-left").textContent = prBusy(r) ? prLeft(r.remaining) : "";
  $("pr-nozzle").textContent = prTemp(r.nozzle); $("pr-nozzle-t").textContent = r.nozzle_target ? "target " + Math.round(r.nozzle_target) + "°" : " ";
  $("pr-bed").textContent = prTemp(r.bed); $("pr-bed-t").textContent = r.bed_target ? "target " + Math.round(r.bed_target) + "°" : " ";
  $("pr-chamber").textContent = prTemp(r.chamber);
  $("pr-fan").textContent = typeof r.fan === "number" ? r.fan + "%" : "–";
  $("pr-pause").textContent = r.state === "PAUSE" ? "Resume" : "Pause";
  $("pr-pause").disabled = $("pr-stop").disabled = !prBusy(r);
  $("pr-light").disabled = !r.online;
  $("pr-light").setAttribute("aria-pressed", String(r.light === "on"));
  const box = $("pr-speed");
  box.textContent = "";
  for (const [v, label] of [["1", "Silent"], ["2", "Standard"], ["3", "Sport"], ["4", "Ludicrous"]]) {
    const b = el("button", v === r.speed ? "on" : "", label);
    b.disabled = !prBusy(r);
    b.addEventListener("click", () => printerCmd("speed", v));
    box.appendChild(b);
  }
  prModel(r);
  prCamera(r);
  document.querySelector("#app-printer h1").textContent = r.printer_model ? "Bambu Lab " + r.printer_model : "Printer";
  $("pr-note").textContent = r.online ? "If a button does nothing, turn on LAN Only and Developer Mode on the printer: newer firmware ignores commands from other programs otherwise." : "";
}
// the printer's camera: started once the printer answers, tried again every 12 s while it fails
let prCamTry = 0;
function prCamera(r) {
  const cam = $("pr-cam"), img = $("pr-cam-img");
  if (!r.configured || !r.online) {
    $("pr-cam-msg").textContent = r.configured ? "The camera starts once the printer answers." : "";
    if (!img.getAttribute("src")) cam.classList.add("off");
    return;
  }
  const failed = cam.classList.contains("off") || !img.getAttribute("src");
  if (failed && Date.now() - prCamTry > 12000) {
    prCamTry = Date.now();
    $("pr-cam-msg").textContent = "Connecting to the printer's camera…";
    img.src = streamUrl("/api/printer/camera");
  } else if (failed) {
    $("pr-cam-msg").textContent = r.camera_error || "The printer's camera is not answering. On the printer switch on LAN Only Liveview.";
  }
}
// the model being printed: a picture taken from the print file on the printer
function prModel(r) {
  const m = r.model || { status: "none" };
  $("pr-model").hidden = m.status === "none" && !r.file;
  $("pr-model-name").textContent = (r.file || "–").replace(/(\.gcode)?\.3mf$/i, "");
  const plate = r.state && r.layers ? "Layer " + r.layer + " of " + r.layers : "";
  $("pr-model-sub").textContent = m.status === "ok" ? plate : m.why || plate;
  const img = $("pr-model-img");
  if (m.status === "ok") {
    const src = "/api/printer/model?v=" + m.v + (WEB ? "" : "&t=" + encodeURIComponent(TOKEN));
    if (img.getAttribute("src") !== src) img.src = src;
    img.hidden = false;
    $("pr-model-msg").hidden = true;
  } else {
    img.hidden = true;
    img.removeAttribute("src");
    $("pr-model-msg").hidden = false;
    $("pr-model-msg").textContent = m.status === "loading" ? "Loading the model…" : "No picture";
  }
}
async function printerRefresh() {
  let r;
  try { r = await api("/api/printer"); } catch (e) { return; }
  if (!$("app-printer").hidden) printerShow(r);
  printerWidgetsShow(r);
}
async function printerCmd(action, value) {
  try {
    const r = await api("/api/printer/cmd", { action, value });
    if (!r.ok) toast(r.error); else setTimeout(printerRefresh, 1500);
  } catch (e) { toast(e.message); }
}
$("pr-pause").addEventListener("click", () => printerCmd(printerLast && printerLast.state === "PAUSE" ? "resume" : "pause"));
$("pr-stop").addEventListener("click", () => confirmSheet("Stop the print?", "The print is cancelled and cannot be continued.", "Stop", () => printerCmd("stop")));
$("pr-light").addEventListener("click", () => printerCmd($("pr-light").getAttribute("aria-pressed") === "true" ? "light_off" : "light_on"));
$("pr-cam-img").addEventListener("error", () => { $("pr-cam").classList.add("off"); });
$("pr-cam-img").addEventListener("load", () => { $("pr-cam").classList.remove("off"); });
function openPrinter() {
  $("app-printer").hidden = false;
  loadPrinterCfg();
  prCamTry = 0;
  printerRefresh();
  clearInterval(printerTimer);
  printerTimer = setInterval(printerRefresh, 3000);
}
$("printer-back").addEventListener("click", () => {
  clearInterval(printerTimer);
  closePanel($("app-printer"), () => $("pr-cam-img").removeAttribute("src"));    // the camera rests
});

/* the two Home widgets: the print job, and the temperatures */
/* a pixel-art printer for the card while nothing is printing (an original drawing) */
const PIXEL_PRINTER = [
  "...KKKKKKKKKKKKKKK....",
  "..KGGGGGGGGGGGGGGGK...",
  "..KGDDDDDDDDDDDDDGK...",
  "..KGDWWWWWWWWWWWDGK.ss",
  "..KGDDDDDTTTDDDDDGKsSs",
  "..KGDDDDDTTTDDDDDGKsSs",
  "..KGDDDDDDODDDDDDGK.ss",
  "..KGDbDDDDDDDDDDDGK...",
  "..KGDDbDDDDDDDDDDGK...",
  "..KGDDDDDDNDDDDDDGK...",
  "..KGDDDDDNNNDDDDDGK...",
  "..KGDDDDNNNNNDDDDGK...",
  "..KGDPPPPPPPPPPPDGK...",
  "..KGDDDDDDDDDDDDDGK...",
  "..KGGGGGGGGGGGGGGGK...",
  "..KGGLGGGGGGGGGGGGK...",
  "..KKKKKKKKKKKKKKKKK...",
  "...KK...........KK....",
];
const PIXEL_COLOURS = { K: "#23272e", G: "#454c57", W: "#a4adb8", D: "#18202c", T: "#dfe3e8", O: "#ff8a3d",
  b: "#4f78a8", N: "#3ddc84", P: "#6e7681", s: "#1f9d57", S: "#3ddc84", L: "#7cff9b" };
function pixelPrinter() {
  const w = PIXEL_PRINTER[0].length, h = PIXEL_PRINTER.length;
  let rects = "";
  PIXEL_PRINTER.forEach((row, y) => [...row].forEach((c, x) => {
    if (PIXEL_COLOURS[c]) rects += `<rect x="${x}" y="${y}" width="1.02" height="1.02" fill="${PIXEL_COLOURS[c]}" class="px-${c}"/>`;
  }));
  return `<svg viewBox="0 0 ${w} ${h}" shape-rendering="crispEdges" aria-hidden="true">${rects}</svg>`;
}
(() => {
  const ring = $("w-printer-ring"), px = document.createElement("span");
  px.className = "d-pixel";
  px.innerHTML = pixelPrinter();
  ring.after(px);
})();
function printerWidgetsShow(r) {
  const w = $("w-printer");
  const pic = w.querySelector(".d-art .model"), m = r.model || {};
  const src = m.status === "ok" && r.online ? "/api/printer/model?v=" + m.v + (WEB ? "" : "&t=" + encodeURIComponent(TOKEN)) : "";
  if (pic.dataset.src !== src) { pic.dataset.src = src; pic.style.backgroundImage = src ? "url('" + src + "')" : ""; }
  w.classList.toggle("has-model", !!src);
  w.hidden = !widgetOn("printer");
  $("w-printer-temps").hidden = !r.configured || !r.online;
  if (!r.configured) {
    $("w-printer-state").textContent = "Printer";
    $("w-printer-file").textContent = "Bambu Lab printer";
    $("w-printer-sub").textContent = "Tap to connect it";
    w.classList.add("idle");
    $("w-printer-pct").textContent = "+";
    $("w-printer-ring").style.setProperty("--p", 0);
    return;
  }
  const busy = prBusy(r);
  $("w-printer-state").textContent = prState(r);
  w.classList.toggle("busy", busy);
  w.classList.toggle("idle", !busy && r.state !== "FINISH");
  w.classList.toggle("hot", typeof r.nozzle === "number" && r.nozzle > 60);
  $("w-printer-file").textContent = r.online ? (busy || r.state === "FINISH" ? (r.file || "Printing") : "Ready to print") : "Not connected";
  $("w-printer-sub").textContent = busy ? [prLeft(r.remaining), r.layers ? "layer " + r.layer + "/" + r.layers : ""].filter(Boolean).join(" · ")
    : r.online ? "" : (r.error || "Connecting…");
  const pct = busy || r.state === "FINISH" ? r.percent : 0;
  $("w-printer-pct").textContent = busy || r.state === "FINISH" ? pct + "%" : r.online ? "✓" : "–";
  $("w-printer-ring").style.setProperty("--p", pct);
  // temperatures in the same card; warm colours while hot
  $("w-printer-noz").textContent = prTemp(r.nozzle);
  $("w-printer-bed").textContent = prTemp(r.bed);
  $("w-printer-noz").classList.toggle("hot", typeof r.nozzle === "number" && r.nozzle > 60);
  $("w-printer-bed").classList.toggle("hot", typeof r.bed === "number" && r.bed > 45);
}
function printerWidgets() {
  if (!widgetOn("printer")) { $("w-printer").hidden = true; return; }
  printerRefresh();
}
$("w-printer").addEventListener("click", openPrinter);
setInterval(() => { if (!document.hidden && state.settings && widgetOn("printer") && $("app-printer").hidden) printerRefresh(); }, 15000);

function refreshWidgets(force) {
  if (document.hidden || !state.settings) return;
  printerWidgets();
  updateWidget();
  siteWidget();
  mapWidget();
  weatherRefresh(force);
  $("w-orb").hidden = !widgetOn("animation");
  if (widgetOn("radio")) radioRefresh(); else $("w-radio").hidden = true;
  cameraWidget();
  dashSystem();
  layoutDash();
}

/* ---------- monitor: a day of temperature, processor and memory */
let monDraw = false;      // true for the one render after opening or changing the range: the lines draw themselves
let monTimer = 0, monFullTimer = 0, monHours = 1, monData = null, monRate = 10;
try { monRate = [3, 10, 60].includes(Number(localStorage.getItem("homeos-mon-rate"))) ? Number(localStorage.getItem("homeos-mon-rate")) : 10; } catch (e) { /* no storage: keep 10 s */ }
function drawChart(svg, rows, col, opt) {
  svg.textContent = "";
  const W = 600, H = 200, L = 54, R = 8, T = 10, B = 28;
  const pts = rows.filter((p) => typeof p[col] === "number");
  const t1 = Date.now() / 1000, t0 = t1 - monHours * 3600;
  let lo = opt.min, hi = opt.max;
  if (lo === undefined) {
    const vals = pts.map((p) => p[col]);
    lo = Math.floor((Math.min(...vals, opt.floor) - 2) / 5) * 5;
    hi = Math.ceil((Math.max(...vals, opt.floor + 10) + 2) / 5) * 5;
  }
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  for (const v of [lo, (lo + hi) / 2, hi]) {
    svg.appendChild(svgEl("line", { class: "grid", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
    const label = svgEl("text", { x: L - 8, y: y(v) + 6, "text-anchor": "end" });
    label.textContent = Math.round(v) + opt.unit;
    svg.appendChild(label);
  }
  const a = svgEl("text", { x: L, y: H - 6 }), b = svgEl("text", { x: W - R, y: H - 6, "text-anchor": "end" });
  a.textContent = monHours === 1 ? "1 hour ago" : monHours + " hours ago";
  b.textContent = "now";
  svg.append(a, b);
  if (pts.length < 2) {
    const wait = svgEl("text", { x: (L + W - R) / 2, y: H / 2, "text-anchor": "middle" });
    wait.textContent = "Collecting data…";
    svg.appendChild(wait);
    return;
  }
  // a gap of more than three samples (the Pi was off) breaks the line
  let d = "", prev = null, area = "";
  const segs = [];
  for (const p of pts) {
    const gap = prev && p[0] - prev[0] > 200;
    if (!prev || gap) { if (area) segs.push(area + "L" + x(prev[0]) + " " + y(lo) + "Z"); area = "M" + x(p[0]) + " " + y(lo); }
    d += (prev && !gap ? "L" : "M") + x(p[0]).toFixed(1) + " " + y(p[col]).toFixed(1);
    area += "L" + x(p[0]).toFixed(1) + " " + y(p[col]).toFixed(1);
    prev = p;
  }
  segs.push(area + "L" + x(prev[0]) + " " + y(lo) + "Z");
  svg.appendChild(svgEl("path", { class: "area", d: segs.join("") }));
  svg.appendChild(svgEl("path", { class: "line", d, pathLength: 1 }));
  svg.classList.toggle("draw", monDraw);
}
function renderMonitor() {
  const r = monData;
  if (!r || !r.ok) return;
  const since = Date.now() / 1000 - monHours * 3600;
  // the last hour comes from the live readings (every 3 s), older time from the minute history
  const recent = r.recent || [];
  const edge = recent.length ? recent[0][0] : Infinity;
  const rows = r.points.filter((p) => p[0] < edge).concat(recent).filter((p) => p[0] >= since);
  const charts = [["temp", 1, "°", { floor: 40, unit: "°" }], ["cpu", 2, "%", { min: 0, max: 100, unit: "%" }],
                  ["mem", 3, "%", { min: 0, max: 100, unit: "%" }]];
  for (const [id, col, unit, opt] of charts) {
    const vals = rows.map((p) => p[col]).filter((v) => typeof v === "number");
    $("mon-" + id + "-now").textContent = vals.length ? vals[vals.length - 1].toFixed(id === "temp" ? 1 : 0) + (id === "temp" ? " °C" : " %") : "–";
    $("mon-" + id + "-sub").textContent = vals.length
      ? "Lowest " + Math.min(...vals).toFixed(0) + unit + " · highest " + Math.max(...vals).toFixed(0) + unit : "No data yet";
    drawChart($("mon-" + id), rows, col, opt);
  }
  if (r.disk) {
    const pct = (r.disk.used / r.disk.total) * 100;
    $("mon-disk-now").textContent = pct.toFixed(0) + " %";
    $("mon-disk-bar").style.width = pct + "%";
    $("mon-disk-sub").textContent = gb(r.disk.used) + " used of " + gb(r.disk.total);
  }
  const dl = $("mon-services");
  dl.textContent = "";
  for (const s of r.services) {
    const text = s.state === "active" ? "Running" : s.state === "not installed" ? "Not installed" : s.state === "failed" ? "Failed" : "Stopped";
    dl.append(el("dt", "", s.label), el("dd", s.state === "active" ? "good" : s.state === "failed" ? "bad" : "", text));
  }
  if (r.load) dl.append(el("dt", "", "Load (1, 5, 15 min)"), el("dd", "", r.load.join(" · ")));
}
function renderMonRange() {
  const box = $("mon-range");
  box.textContent = "";
  for (const h of [1, 6, 24]) {
    const b = el("button", h === monHours ? "on" : "", h === 1 ? "1 hour" : h + " hours");
    b.style.textTransform = "none";
    b.addEventListener("click", () => { monHours = h; renderMonRange(); monDraw = true; renderMonitor(); monDraw = false; });
    box.appendChild(b);
  }
  const rates = $("mon-rate");
  rates.textContent = "";
  for (const s of [3, 10, 60]) {
    const b = el("button", s === monRate ? "on" : "", s === 60 ? "1 min" : s + " s");
    b.addEventListener("click", () => {
      monRate = s;
      try { localStorage.setItem("homeos-mon-rate", String(s)); } catch (e) { /* not remembered */ }
      renderMonRange();
      monStart();
    });
    rates.appendChild(b);
  }
}
async function monRefresh(draw) {
  try { monData = await api("/api/monitor"); monDraw = draw === true; renderMonitor(); monDraw = false; } catch (e) { /* try again next time */ }
}
async function monTick() {       // one new reading, added to what is already drawn
  if (document.hidden || !monData || !monData.ok) return;
  let r;
  try { r = await api("/api/monitor/now"); } catch (e) { return; }
  const list = monData.recent, s = r.sample;
  if (!s || (list.length && s[0] <= list[list.length - 1][0])) return;
  list.push(s);
  if (list.length > 1200) list.shift();
  renderMonitor();
}
function monStart() {
  clearInterval(monTimer);
  monTimer = setInterval(monTick, monRate * 1000);
}
function openMonitor() {
  $("app-monitor").hidden = false;
  renderMonRange();
  monRefresh(true);
  monStart();
  clearInterval(monFullTimer);
  monFullTimer = setInterval(monRefresh, 120000);      // storage, services and the full history
}
$("monitor-back").addEventListener("click", () => {
  closePanel($("app-monitor"));
  clearInterval(monTimer);
  clearInterval(monFullTimer);
});

/* ---------- updates (Settings > About) */
let updateTimer = 0;
$("update-check").addEventListener("click", async () => {
  $("update-text").textContent = "Checking…";
  $("update-check").disabled = true;
  try {
    const r = await api("/api/update/check", {});
    $("update-apply").hidden = !(r.ok && r.available);
    $("update-text").textContent = !r.ok ? r.error
      : r.available ? "An update is ready: " + r.summary : "HomeOS " + state.boot.info.version + " is up to date.";
  } catch (e) { $("update-text").textContent = e.message; }
  $("update-check").disabled = false;
});
$("update-apply").addEventListener("click", () => {
  confirmSheet("Install the update?", "HomeOS restarts its web access for a few seconds. Your settings are kept.", "Install", async () => {
    try {
      const r = await api("/api/update/apply", {});
      if (!r.ok) { $("update-text").textContent = r.error; return; }
    } catch (e) { $("update-text").textContent = e.message; return; }
    $("update-apply").hidden = true;
    $("update-check").disabled = true;
    $("update-text").replaceChildren(el("span", "loader"), document.createTextNode("Updating… this takes about a minute."));
    let seen = false;
    clearInterval(updateTimer);
    updateTimer = setInterval(async () => {
      let s;
      try { s = await api("/api/update/status"); } catch (e) { return; }   // the service is restarting
      if (s.running) { seen = true; return; }
      if (s.exit === null && !seen) return;
      clearInterval(updateTimer);
      $("update-check").disabled = false;
      if (s.exit === 0) {
        $("update-text").textContent = WEB ? "Updated. Reloading…" : "Updated. Restart the Pi to finish (Power > Restart).";
        if (WEB) setTimeout(() => location.reload(), 1500);
      } else {
        $("update-text").textContent = "The update did not finish.";
        $("update-log").textContent = s.log;
        $("update-log").hidden = false;
      }
    }, 3000);
  });
});

/* ---------- Bluetooth (Settings > Bluetooth) */
let btBusy = false;
function renderBt(r) {
  $("bt-none").hidden = !!r.available;
  $("bt-box").hidden = !r.available;
  if (!r.available) return;
  $("bt-power").checked = r.on;
  $("bt-scan").disabled = !r.on || btBusy;
  const list = $("bt-list");
  list.textContent = "";
  const devs = r.on ? r.devices : [];
  $("bt-state").textContent = !r.on ? "Bluetooth is off." : devs.length ? "Tap a device to connect or disconnect." : "No devices yet. Press Scan.";
  for (const d of devs) {      // already A to Z from the Pi
    const li = el("li", d.connected ? "active" : ""), b = el("button");
    b.append(icon("bt"), el("span", "ssid", d.name),
             el("span", "meta", d.connected ? "Connected" : d.paired ? "Paired" : "New"));
    b.addEventListener("click", () => btDo(d.connected ? "disconnect" : "connect", d));
    if (d.paired) {
      const x = el("span", "bt-forget");
      x.setAttribute("role", "button");
      x.setAttribute("aria-label", "Forget " + d.name);
      x.appendChild(icon("close"));
      x.addEventListener("click", (ev) => {
        ev.stopPropagation();
        confirmSheet("Forget " + d.name + "?", "You will need to pair it again to use it.", "Forget", () => btDo("forget", d));
      });
      b.appendChild(x);
    }
    li.appendChild(b);
    list.appendChild(li);
  }
}
async function btLoad() {
  try { renderBt(await api("/api/bt")); } catch (e) { /* leave it as it was */ }
}
async function btCall(path, body, busyText) {
  btBusy = true;
  $("bt-scan").disabled = true;
  $("bt-state").replaceChildren(el("span", "loader"), document.createTextNode(busyText));
  try {
    const r = await api(path, body);
    btBusy = false;
    if (!r.ok) { toast(r.error); btLoad(); return; }
    renderBt(r);
    pollStatus();
  } catch (e) { btBusy = false; toast(e.message); btLoad(); }
}
function btDo(action, d) {
  const text = { connect: "Connecting to " + d.name + "…", disconnect: "Disconnecting…", forget: "Forgetting " + d.name + "…" }[action];
  btCall("/api/bt/device", { action, mac: d.mac }, text);
}
$("bt-power").addEventListener("change", () => {
  const on = $("bt-power").checked;
  btCall("/api/bt/power", { on }, on ? "Turning Bluetooth on…" : "Turning Bluetooth off…");
});
$("bt-scan").addEventListener("click", () => btCall("/api/bt/scan", {}, "Looking for devices nearby…"));


/* ---------- Files: the home folder of the Pi */
const files = { path: "", hidden: false, entries: [], current: null, nameAction: null };
const FILE_ICONS = { folder: "folder", image: "image", video: "video", audio: "audio", text: "text", pdf: "file", archive: "archive", file: "file" };
function joinPath(dir, name) { return dir ? dir + "/" + name : name; }
function fileUrl(path, download) {
  return "/api/files/get?path=" + encodeURIComponent(path) + (download ? "&dl=1" : "") +
    (WEB ? "" : "&t=" + encodeURIComponent(TOKEN)) + "&n=" + Date.now();
}
function sizeText(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
}
function whenText(t) {
  const d = new Date(t * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
}
function renderCrumbs() {
  const nav = $("files-crumbs");
  nav.textContent = "";
  const parts = files.path ? files.path.split("/") : [];
  const home = el("button", "", "Home");
  home.addEventListener("click", () => filesGo(""));
  nav.appendChild(home);
  parts.forEach((p, i) => {
    nav.appendChild(el("span", "sep", "›"));
    const b = el("button", "", p);
    const target = parts.slice(0, i + 1).join("/");
    b.addEventListener("click", () => filesGo(target));
    nav.appendChild(b);
  });
  nav.scrollLeft = nav.scrollWidth;
  $("files-title").textContent = parts.length ? parts[parts.length - 1] : "Files";
}
async function filesGo(path) {
  let r;
  try { r = await api("/api/files/list", { path, hidden: files.hidden }); } catch (e) { toast(e.message); return; }
  if (!r.ok) { toast(r.error); return; }
  files.path = r.path;
  files.entries = r.entries;
  renderCrumbs();
  const list = $("files-list");
  list.textContent = "";
  for (const f of r.entries) {      // folders first, then A to Z, sorted on the Pi
    const li = el("li"), b = el("button");
    b.append(icon(FILE_ICONS[f.kind] || "file"), el("span", "ssid", f.name),
             el("span", "meta", f.dir ? whenText(f.mtime) : sizeText(f.size) + " · " + whenText(f.mtime)));
    b.dataset.kind = f.kind;
    b.addEventListener("click", () => (f.dir ? filesGo(joinPath(files.path, f.name)) : openFile(f)));
    li.appendChild(b);
    list.appendChild(li);
  }
  $("files-empty").hidden = r.entries.length > 0;
  const folders = r.entries.filter((f) => f.dir).length;
  $("files-foot").textContent = folders + (folders === 1 ? " folder, " : " folders, ") +
    (r.entries.length - folders) + (r.entries.length - folders === 1 ? " file" : " files") + " · " + sizeText(r.free) + " free";
}
function openFiles() {
  $("app-files").hidden = false;
  filesGo(files.path);
}
$("files-back").addEventListener("click", () => {
  if (files.path) { filesGo(files.path.split("/").slice(0, -1).join("/")); return; }    // up one folder first
  closePanel($("app-files"));
});
$("files-hidden").addEventListener("click", () => {
  files.hidden = !files.hidden;
  $("files-hidden").setAttribute("aria-pressed", String(files.hidden));
  filesGo(files.path);
});

function openFile(f) {
  files.current = f;
  const path = joinPath(files.path, f.name);
  $("file-title").textContent = f.name;
  $("file-meta").textContent = sizeText(f.size) + " · changed " + whenText(f.mtime);
  $("file-download").href = fileUrl(path, true);
  $("file-download").hidden = !WEB;          // on the Pi's own screen the file is already here
  const box = $("file-preview");
  box.textContent = "";
  if (f.kind === "image") {
    const img = document.createElement("img");
    img.src = fileUrl(path);
    img.alt = f.name;
    box.appendChild(img);
  } else if (f.kind === "video" || f.kind === "audio") {
    const m = document.createElement(f.kind);
    m.src = fileUrl(path);
    m.controls = true;
    m.preload = "metadata";
    m.setAttribute("playsinline", "");
    box.appendChild(m);
  } else if (f.kind === "text" && f.size <= 512 * 1024) {
    const pre = el("pre", "file-text", "Loading…");
    box.appendChild(pre);
    fetch(fileUrl(path), { headers: { "X-HomeOS-Token": TOKEN } })
      .then((res) => res.text()).then((t) => { pre.textContent = t; }).catch(() => { pre.textContent = "Could not open the file."; });
  }
  box.hidden = !box.firstChild;
  openSheet("sheet-file");
}
function stopPreview() {
  const m = $("file-preview").querySelector("video, audio");
  if (m) { m.pause(); m.removeAttribute("src"); m.load(); }
}
document.querySelectorAll("#sheet-file [data-close]").forEach((b) => b.addEventListener("click", stopPreview));
$("backdrop").addEventListener("click", stopPreview);

function askName(title, value, okLabel, action) {
  $("name-title").textContent = title;
  $("name-input").value = value;
  $("name-ok").textContent = okLabel;
  files.nameAction = action;
  openSheet("sheet-name");
  setTimeout(() => {
    const input = $("name-input"), dot = value.lastIndexOf(".");
    input.focus();
    input.setSelectionRange(0, dot > 0 ? dot : value.length);     // the name without its extension
  }, 50);
}
$("name-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("name-input").value.trim(), action = files.nameAction;
  if (!name || !action) return;
  try {
    const r = await action(name);
    if (!r.ok) { toast(r.error); return; }
    closeSheets();
    filesGo(files.path);
  } catch (e) { toast(e.message); }
});
$("files-mkdir").addEventListener("click", () =>
  askName("New folder", "", "Create", (name) => api("/api/files/mkdir", { path: files.path, name })));
$("file-rename").addEventListener("click", () => {
  const f = files.current;
  stopPreview();
  askName("Rename", f.name, "Rename", (name) => api("/api/files/rename", { path: joinPath(files.path, f.name), name }));
});
$("file-delete").addEventListener("click", () => {
  const f = files.current;
  stopPreview();
  confirmSheet("Delete " + f.name + "?", "It is deleted for good; there is no bin.", "Delete", async () => {
    try {
      const r = await api("/api/files/delete", { path: joinPath(files.path, f.name) });
      if (!r.ok) toast(r.error);
      filesGo(files.path);
    } catch (e) { toast(e.message); }
  });
});

$("files-upload").addEventListener("click", () => $("files-input").click());
$("files-input").addEventListener("change", async () => {
  const picked = Array.from($("files-input").files);
  $("files-input").value = "";
  if (!picked.length) return;
  const folder = files.path;
  $("files-progress").hidden = false;
  let done = 0, failed = 0;
  for (const f of picked) {
    $("files-progress-text").textContent = "Uploading " + (done + failed + 1) + " of " + picked.length + ": " + f.name;
    try {
      const res = await fetch("/api/files/upload?path=" + encodeURIComponent(folder) + "&name=" + encodeURIComponent(f.name), {
        method: "POST", headers: { "X-HomeOS-Token": TOKEN, "Content-Type": "application/octet-stream" }, body: f,
      });
      const r = await res.json().catch(() => ({ ok: false, error: "Upload failed (" + res.status + ")." }));
      if (r.ok) done++; else { failed++; toast(f.name + ": " + r.error); }
    } catch (e) { failed++; toast(f.name + ": " + e.message); }
  }
  $("files-progress").hidden = true;
  if (done) toast(done === 1 ? "1 file uploaded." : done + " files uploaded.");
  if (folder === files.path) filesGo(folder);
});

function openTerminal() {
  if (!WEB) { api("/api/open", { target: "terminal" }).catch((e) => toast(e.message)); return; }
  if (!state.boot.terminal) { toast("The web terminal is turned off on this Pi."); return; }
  location.href = "/terminal";
}

let pressTimer = 0;
let suppressClick = false;

const isLite = () => !!(state.boot && state.boot.features && state.boot.features.edition === "lite");
function renderTiles() {
  const box = $("tiles");
  box.textContent = "";
  const apps = [
    { name: "Browser", icon: "globe", run: () => openBrowser("") },
    { name: "Camera", icon: "camera", run: openCamera },
    { name: "Files", icon: "folder", run: openFiles },
    { name: "Monitor", icon: "monitor", run: openMonitor },
    { name: "Printer", icon: "printer", run: openPrinter },
    { name: "Radio", icon: "radio", run: openRadio },
    { name: "Settings", icon: "gear", run: openSettings },
    { name: "Terminal", icon: "terminal", run: openTerminal },
  ].filter((a) => !(isLite() && (a.name === "Browser" || a.name === "Camera"))).map((a) => ({ ...a, sys: true }));
  const sites = state.settings.tiles.map((t, i) => ({ name: t.name, url: t.url, index: i }));
  const all = apps.concat(sites).sort((x, y) => x.name.localeCompare(y.name, undefined, { sensitivity: "base" }));
  for (const item of all) {
    if (item.sys) {
      const b = el("button", "tile sys");
      const g = el("span", "glyph");
      g.appendChild(icon(item.icon));
      b.append(g, el("span", "name", item.name));
      b.addEventListener("click", () => { if (!document.body.classList.contains("editing")) item.run(); });
      box.appendChild(b);
      continue;
    }
    const b = el("button", "tile web");
    const g = el("span", "glyph", (Array.from(item.name.trim())[0] || "?").toUpperCase());
    g.dataset.c = hue(item.name);
    const rm = el("span", "remove");
    rm.setAttribute("role", "button");
    rm.setAttribute("aria-label", "Remove " + item.name);
    rm.appendChild(icon("close"));
    rm.addEventListener("click", (ev) => { ev.stopPropagation(); removeTile(item.index); });
    b.append(g, el("span", "name", item.name), rm);
    b.addEventListener("pointerdown", () => {
      clearTimeout(pressTimer);
      pressTimer = setTimeout(() => { suppressClick = true; setEditing(true); }, 600);
    });
    for (const ev of ["pointerup", "pointerleave", "pointercancel"]) {
      b.addEventListener(ev, () => clearTimeout(pressTimer));
    }
    b.addEventListener("click", () => {
      if (suppressClick) { suppressClick = false; return; }
      if (!document.body.classList.contains("editing")) openBrowser(item.url);
    });
    box.appendChild(b);
  }
  const add = el("button", "tile add");
  const g = el("span", "glyph");
  g.appendChild(icon("plus"));
  add.append(g, el("span", "name", "Add site"));
  add.addEventListener("click", () => {
    if (document.body.classList.contains("editing")) return;
    $("tile-form").reset();
    openSheet("sheet-tile");
    $("tile-name").focus();
  });
  box.appendChild(add);
  Array.from(box.children).forEach((node, i) => node.style.setProperty("--i", i));    // order of the entrance animation
}

/* The start screen shows only the time and the weather; this opens everything else. */
function openMenu() { $("menu").hidden = false; refreshWidgets(); }
function closeMenu() { setEditing(false); closePanel($("menu")); }
$("menu-back").addEventListener("click", closeMenu);
function setEditing(on) {
  document.body.classList.toggle("editing", on);
  $("done").hidden = !on;
}
$("done").addEventListener("click", () => setEditing(false));

async function saveSettings(patch) {
  try {
    const res = await api("/api/settings", patch);
    state.settings = res.settings;
    applyTheme(state.settings.theme);
    renderTiles();
    renderBrowserSettings();
    settingsSummaries();
    siteWidget();
    mapWidget();
    return true;
  } catch (e) {
    toast("Could not save: " + e.message);
    return false;
  }
}

function removeTile(i) {
  const tiles = state.settings.tiles.slice();
  tiles.splice(i, 1);
  saveSettings({ tiles });
}

$("tile-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("tile-name").value.trim();
  const url = $("tile-url").value.trim();
  if (!name || !url) return;
  const before = state.settings.tiles.length;
  const ok = await saveSettings({ tiles: state.settings.tiles.concat([{ name, url }]) });
  if (ok && state.settings.tiles.length === before) {
    toast("That address does not look right. Try something like example.com");
    return;
  }
  if (ok) closeSheets();
});

/* ---------- search */
$("search").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  openBrowser(q);
  $("q").value = "";
  $("q").blur();
});

/* ---------- power */
$("btn-power").addEventListener("click", () => openSheet("sheet-power"));
const POWER = {
  reboot: ["Restart now?", "Open pages and terminal sessions will be closed.", "Restart"],
  poweroff: ["Shut down now?", "Wait for the green light to stop blinking before unplugging the Pi.", "Shut down"],
  exit: ["Exit to console?", "The desktop closes and you get a plain text console. Type homeos to come back.", "Exit"],
  signout: ["Sign out?", "Open terminals on this page are closed.", "Sign out"],
};
document.querySelectorAll("[data-power]").forEach((b) => {
  b.addEventListener("click", () => {
    const action = b.dataset.power;
    const [title, text, ok] = POWER[action];
    confirmSheet(title, text, ok, async () => {
      try {
        if (action === "signout") {
          await api("/api/logout", {});
          location.href = "/login";
          return;
        }
        const res = await api("/api/power", { action });
        if (res.ok === false) toast("Could not do that: " + (res.error || "unknown error"));
        else if (res.dev || state.boot.dev) toast("Preview mode: nothing was actually done.");
        else if (WEB) toast(action === "reboot" ? "The Pi is restarting. Reload this page in a minute." : "The Pi is shutting down.");
      } catch (e) { toast(e.message); }
    });
  });
});

$("btn-kbd").addEventListener("click", async () => {
  try {
    const res = await api("/api/keyboard", {});
    if (!res.ok) toast("The on-screen keyboard is not running.");
  } catch (e) { toast(e.message); }
});

/* ---------- status */
function renderStatus() {
  const s = state.status;
  if (!s) return;
  dashSystem();
  const net = $("chip-net");
  if (s.wifi) { $("net").textContent = s.wifi.ssid; net.classList.add("on"); }
  else if (s.ip) { $("net").textContent = s.ip; net.classList.add("on"); }
  else { $("net").textContent = "Offline"; net.classList.remove("on"); }

  $("chip-temp").hidden = s.temp === null;
  if (s.temp !== null) {
    $("temp").textContent = Math.round(s.temp) + "°C";
    $("chip-temp").classList.toggle("hot", s.temp >= 75);
  }
  const bat = s.battery;
  $("chip-battery").hidden = !bat;
  if (bat) {
    $("battery").textContent = bat.level + "%" + (bat.charging ? " ⚡" : "");
    $("chip-battery").classList.toggle("hot", bat.level <= 15 && !bat.charging);
    $("chip-battery").style.setProperty("--level", bat.level + "%");
    $("chip-battery").title = "Battery " + bat.level + "% · " + bat.status;
  }
  $("chip-bt").hidden = s.bluetooth !== true;
  $("chip-mem").hidden = !s.memory;
  if (s.memory) $("mem").textContent = Math.round((s.memory.used / s.memory.total) * 100) + "% RAM";

  if (s.theme && s.theme !== state.settings.theme) {     // changed from the other screen
    state.settings.theme = s.theme;
    applyTheme(s.theme);
    if (!$("settings").hidden) renderBrowserSettings();
  }
  if (!$("settings").hidden) { renderAbout(); settingsSummaries(); }
}

async function pollStatus() {
  if (document.hidden) return;
  try {
    state.status = await api("/api/status");
    renderStatus();
  } catch (e) { /* the host may be busy; try again on the next tick */ }
}

/* ---------- settings */
function openSettings() {
  $("settings").hidden = false;
  renderBrowserSettings();
  renderAbout();
  syncSliders();
  scanWifi();
  btLoad();
  loadPrinterCfg();
  loadSignin();
  settingsSummaries();
}
async function loadSignin() {
  $("set-signin").hidden = !WEB;
  if (!WEB) return;
  try {
    const r = await api("/api/web/password");
    $("ask-password").checked = r.ask;
    $("sum-signin").textContent = r.ask ? "Password asked" : "No password (testing)";
  } catch (e) { /* stays as it was */ }
}
$("ask-password").addEventListener("change", async () => {
  const ask = $("ask-password").checked;
  try {
    const r = await api("/api/web/password", { ask });
    if (!r.ok) { toast(r.error); $("ask-password").checked = !ask; return; }
    toast(ask ? "The password is asked again." : "Password switched off. Anyone in your Tailscale network can open HomeOS.");
    loadSignin();
  } catch (e) { toast(e.message); $("ask-password").checked = !ask; }
});
/* Settings is a list: a row opens its own controls and closes the one that was open. */
/* Settings is a set of widgets: every section is an open card, so all controls are at hand. */
/* On a phone it is a list instead: a row opens into its card and closes the one that was open. */
const setPhone = matchMedia("(max-width: 760px)");
const setItems = Array.from(document.querySelectorAll("#set-list .set-item"));
function settingsMode() {
  for (const item of setItems) item.open = item.id === "set-about" || !setPhone.matches;
}
for (const item of setItems) {
  item.querySelector("summary").addEventListener("click", (ev) => {
    if (!setPhone.matches || item.id === "set-about") { ev.preventDefault(); return; }
    if (!item.open) {
      for (const other of setItems) if (other !== item && other.id !== "set-about") other.open = false;
      setTimeout(() => item.scrollIntoView({ block: "nearest", behavior: "smooth" }), 60);
    }
  });
}
setPhone.addEventListener("change", settingsMode);
settingsMode();
function settingsSummaries() {
  if ($("settings").hidden) return;
  const s = state.status || {}, cfg = state.settings, cap = (x) => x.charAt(0).toUpperCase() + x.slice(1);
  const set = (key, text) => { $("sum-" + key).textContent = text; };
  set("wifi", s.wifi ? s.wifi.ssid : s.ip ? "Connected by cable" : "Not connected");
  set("display", cap(cfg.theme) + " theme · " + cap(cfg.background || "plain") + " background");
  set("printer", printerCfg ? (printerCfg.host ? printerCfg.host + (printerCfg.serial ? " · " + printerCfg.serial : "") : "Not set up") : "");
  set("sound", s.volume && s.volume.value !== undefined && s.volume.value !== null ? "Volume " + s.volume.value + "%" : "");
  set("bluetooth", s.bluetooth === null || s.bluetooth === undefined ? "" : s.bluetooth ? "On" : "Off");
  set("apps", "Camera and radio addresses");
  set("home", (cfg.widgets || []).length + " widgets" + (cfg.weather ? " · " + cfg.weather.name : ""));
  set("updates", "HomeOS " + state.boot.info.version);
  set("about", state.boot.info.model || state.boot.info.hostname || "");
}
$("settings-back").addEventListener("click", () => closePanel($("settings")));

function renderAbout() {
  const i = state.boot.info, s = state.status || {};
  const rows = [
    ["System", "HomeOS " + i.version],
    ["Device", i.model || i.arch],
    ["Base", i.os || "Linux"],
    ["Kernel", i.kernel],
    ["Name", i.hostname],
    ["IP address", s.ip || "Not connected"],
  ];
  if (s.battery) rows.push(["Battery", s.battery.level + "% · " + s.battery.status]);
  if (s.memory) rows.push(["Memory", gb(s.memory.used) + " of " + gb(s.memory.total)]);
  if (s.disk) rows.push(["Storage", gb(s.disk.used) + " of " + gb(s.disk.total)]);
  if (s.temp !== null && s.temp !== undefined) rows.push(["Temperature", s.temp.toFixed(1) + " °C"]);
  if (s.uptime !== null && s.uptime !== undefined) rows.push(["Running for", uptime(s.uptime)]);
  const dl = $("about");
  dl.textContent = "";
  for (const [k, v] of rows) { const d = document.createElement("div"); d.append(el("dt", "", k), el("dd", "", v)); dl.append(d); }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.bg = state.settings.background || "plain";
}

function renderBrowserSettings() {
  const themes = $("themes");
  themes.textContent = "";
  for (const name of ["light", "dark"]) {
    const b = el("button", name === state.settings.theme ? "on" : "", name);
    b.addEventListener("click", () => saveSettings({ theme: name }));
    themes.appendChild(b);
  }
  const bgs = $("backgrounds");
  bgs.textContent = "";
  for (const name of state.boot.backgrounds || ["plain"]) {
    const b = el("button", "swatch" + (name === (state.settings.background || "plain") ? " on" : ""));
    b.dataset.bg = name;
    b.setAttribute("aria-label", "Background: " + name);
    b.title = name;
    b.addEventListener("click", () => saveSettings({ background: name }));
    bgs.appendChild(b);
  }
  const wbox = $("set-widgets");
  wbox.textContent = "";
  const WIDGET_NAMES = { animation: "Animation", camera: "Camera", map: "Map", radio: "Radio", site: "Featured site", update: "Updates", weather: "Weather",
                         printer: "Printer" };
  for (const name of ["animation", "camera", "site", "map", "printer", "radio", "update", "weather"].filter((n) => !(isLite() && n === "camera"))
                       .sort((a, b) => WIDGET_NAMES[a].localeCompare(WIDGET_NAMES[b]))) {
    const row = el("label", "check-row"), box = document.createElement("input");
    box.type = "checkbox";
    box.checked = widgetOn(name);
    box.addEventListener("change", async () => {
      const set = new Set(state.settings.widgets || []);
      if (box.checked) set.add(name); else set.delete(name);
      if (await saveSettings({ widgets: Array.from(set) })) refreshWidgets(true);
    });
    row.append(box, document.createTextNode(WIDGET_NAMES[name]));
    wbox.appendChild(row);
  }
  if (document.activeElement !== $("set-town")) $("set-town").value = state.settings.weather ? state.settings.weather.name : "";
  if (document.activeElement !== $("set-camera")) $("set-camera").value = state.settings.camera || "";
  if (document.activeElement !== $("set-radio")) $("set-radio").value = state.settings.radio || "";
}

$("weather-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("set-town").blur();
  try {
    const r = await api("/api/weather/place", { name: $("set-town").value.trim() });
    if (!r.ok) { toast(r.error); return; }
    state.settings = r.settings;
    renderBrowserSettings();
    toast(r.settings.weather ? "Weather for " + r.settings.weather.name + "." : "Town cleared.");
    refreshWidgets(true);
  } catch (e) { toast(e.message); }
});

$("apps-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fix = (v) => (v && !/^https?:\/\//i.test(v) ? "http://" + v : v);
  const patch = { camera: fix($("set-camera").value.trim()), radio: fix($("set-radio").value.trim()) };
  $("set-camera").blur(); $("set-radio").blur();
  if (await saveSettings(patch)) toast("Saved.");
});

$("edit-tiles").addEventListener("click", () => { $("settings").hidden = true; openMenu(); setEditing(true); });
$("reset-tiles").addEventListener("click", () => {
  confirmSheet("Restore default sites?", "Your own site tiles will be replaced.", "Restore", () => {
    saveSettings({ tiles: [
      { name: "Wikipedia", url: "https://en.wikipedia.org" },
      { name: "YouTube", url: "https://www.youtube.com" },
      { name: "GitHub", url: "https://github.com" },
      { name: "Raspberry Pi", url: "https://www.raspberrypi.com/documentation/" },
    ] });
  });
});

/* sliders */
function paintRange(input) {
  const min = Number(input.min) || 0, max = Number(input.max) || 100;
  input.style.setProperty("--fill", ((Number(input.value) - min) / (max - min)) * 100 + "%");
}
function syncSliders() {
  const f = state.boot.features, s = state.status || {};
  const hasB = f.brightness && s.brightness !== null && s.brightness !== undefined;
  $("row-brightness").hidden = !hasB;
  $("no-brightness").hidden = hasB;
  if (hasB) { $("brightness").value = s.brightness; $("brightness-out").textContent = s.brightness + "%"; paintRange($("brightness")); }
  const hasV = f.volume && !!s.volume;
  $("row-volume").hidden = !hasV;
  $("no-volume").hidden = hasV;
  if (hasV) { $("volume").value = s.volume.value; $("volume-out").textContent = s.volume.value + "%"; paintRange($("volume")); }
}
function bindSlider(id, path) {
  let timer = 0;
  const send = async () => {
    try {
      const res = await api(path, { value: Number($(id).value) });
      if (!res.ok) toast("Could not change it: " + (res.error || "unknown error"));
    } catch (e) { toast(e.message); }
  };
  $(id).addEventListener("input", () => {
    paintRange($(id));
    $(id + "-out").textContent = $(id).value + "%";
    clearTimeout(timer);
    timer = setTimeout(send, 120);
  });
}
bindSlider("brightness", "/api/brightness");
bindSlider("volume", "/api/volume");

/* Wi-Fi */
function bars(signal) {
  const b = el("span", "bars");
  const n = signal >= 75 ? 4 : signal >= 50 ? 3 : signal >= 25 ? 2 : 1;
  for (let i = 0; i < 4; i++) b.appendChild(el("i", i < n ? "on" : ""));
  return b;
}

async function scanWifi() {
  const list = $("wifi-list");
  if (!state.boot.features.wifi) {
    $("wifi-state").textContent = "NetworkManager is not installed, so Wi-Fi cannot be managed here.";
    $("wifi-scan").hidden = true;
    return;
  }
  $("wifi-state").textContent = "Looking for networks…";
  $("wifi-scan").disabled = true;
  try {
    const res = await api("/api/wifi");
    list.textContent = "";
    if (!res.ok) { $("wifi-state").textContent = "Wi-Fi is not available: " + res.error; return; }
    const active = res.networks.find((n) => n.active);
    $("wifi-state").textContent = active ? "Connected to " + active.ssid
      : res.networks.length ? "Not connected. Choose a network." : "No networks found.";
    for (const n of res.networks) {
      const li = el("li", n.active ? "active" : "");
      const b = el("button");
      const meta = el("span", "meta");
      if (n.active) meta.appendChild(el("span", "", "Connected"));
      if (n.secure) meta.appendChild(icon("lock"));
      meta.appendChild(bars(n.signal));
      b.append(icon("wifi"), el("span", "ssid", n.ssid), meta);
      b.addEventListener("click", () => chooseWifi(n));
      li.appendChild(b);
      list.appendChild(li);
    }
  } catch (e) {
    $("wifi-state").textContent = "Could not scan: " + e.message;
  } finally {
    $("wifi-scan").disabled = false;
  }
}
$("wifi-scan").addEventListener("click", scanWifi);

function chooseWifi(n) {
  if (n.active) return;
  state.wifiTarget = n.ssid;
  if (!n.secure) { connectWifi(""); return; }
  $("wifi-title").textContent = "Connect to " + n.ssid;
  $("wifi-form").reset();
  $("wifi-pass").type = "password";
  openSheet("sheet-wifi");
  $("wifi-pass").focus();
}
$("wifi-show").addEventListener("change", () => {
  $("wifi-pass").type = $("wifi-show").checked ? "text" : "password";
});
$("wifi-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  connectWifi($("wifi-pass").value);
});

async function connectWifi(password) {
  const ssid = state.wifiTarget;
  closeSheets();
  $("wifi-state").textContent = "Connecting to " + ssid + "…";
  try {
    const res = await api("/api/wifi/connect", { ssid, password });
    if (res.ok) toast("Connected to " + ssid);
    else toast("Could not connect: " + (res.error || "check the password"));
  } catch (e) { toast(e.message); }
  scanWifi();
  pollStatus();
}

/* ---------- start */
document.addEventListener("contextmenu", (ev) => ev.preventDefault());

tick();
setInterval(tick, 1000);

async function start() {
  try {
    state.boot = await api("/api/boot");
  } catch (e) {
    $("date").textContent = "The system service is not answering. " + e.message;
    setTimeout(start, 3000);
    return;
  }
  state.settings = state.boot.settings;
  applyTheme(state.settings.theme);
  $("preview").hidden = !state.boot.dev;
  const host = state.boot.info.hostname || "HomeOS";
  $("device-name").textContent = host;
  $("device-avatar").textContent = (Array.from(host)[0] || "H").toUpperCase();
  document.querySelector('[data-dock="camera"]').hidden = isLite();
  if (WEB) {
    $("remote").hidden = false;
    $("remote").textContent = "Remote";
    document.querySelector('[data-power="exit"]').hidden = true;
    document.querySelector('[data-power="signout"]').hidden = false;
    $("hint").textContent = "Sites open here, inside HomeOS. Press and hold a site to edit this list.";
  }
  $("btn-kbd").hidden = !state.boot.features.keyboard;
  renderTiles();
  await pollStatus();
  setInterval(pollStatus, 5000);
  // After signing in, the home screen with the clock comes first.
  paintDock();
  refreshWidgets(true);
  setInterval(refreshWidgets, 60000);
  document.addEventListener("visibilitychange", pollStatus);
}
start();
