"use strict";
/* HomeOS web terminal: xterm.js in the browser, a real shell on the Pi.
   Output arrives as server-sent events, keystrokes go back as small POSTs. */

const $ = (id) => document.getElementById(id);
const meta = (name) => (document.querySelector('meta[name="' + name + '"]') || {}).content || "";
const TOKEN = meta("homeos-token");
$("title").textContent = "Terminal · " + meta("homeos-host");

function store(key, value) {
  try {
    if (value === undefined) return sessionStorage.getItem(key);
    if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value);
  } catch (e) { /* storage may be unavailable */ }
  return null;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-HomeOS-Token": TOKEN },
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("Signed out"); }
  if (!res.ok) throw new Error("Request failed (" + res.status + ")");
  return res.json();
}

let fontSize = Number(store("homeos-term-font")) || 15;
const term = new Terminal({
  fontFamily: '"Roboto Mono", "DejaVu Sans Mono", "Noto Sans Mono", Menlo, Consolas, monospace',
  fontSize, cursorBlink: true, scrollback: 10000,
  theme: {
    background: "#131314", foreground: "#e3e3e3", cursor: "#a8c7fa", selectionBackground: "#0842a0",
    black: "#202124", red: "#f28b82", green: "#81c995", yellow: "#fdd663", blue: "#8ab4f8", magenta: "#c58af9", cyan: "#78d9ec", white: "#e8eaed",
    brightBlack: "#9aa0a6", brightRed: "#f6aea9", brightGreen: "#a8dab5", brightYellow: "#fde293", brightBlue: "#aecbfa", brightMagenta: "#d7aefb", brightCyan: "#a1e4f2", brightWhite: "#ffffff",
  },
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open($("term"));
fit.fit();

let id = null, source = null;

/* keystrokes: sent in order, batched while a request is in flight */
let pending = "", sending = false;
async function flush() {
  if (sending || !pending || !id) return;
  sending = true;
  const data = pending;
  pending = "";
  try {
    const res = await api("/api/term/input", { id, data });
    if (res.ok === false) ended();
  } catch (e) { $("state").textContent = "Connection lost"; }
  sending = false;
  flush();
}
function send(data) { pending += data; flush(); }

/* Ctrl from the key bar is "sticky": it applies to the next key typed */
let ctrl = false;
function setCtrl(on) {
  ctrl = on;
  $("key-ctrl").setAttribute("aria-pressed", on ? "true" : "false");
}
function ctrlChar(ch) {
  const c = ch.toUpperCase().charCodeAt(0);
  if (c >= 64 && c <= 95) return String.fromCharCode(c - 64);   // Ctrl+A..Z, [ \ ] ^ _
  if (ch === " ") return "\x00";
  if (ch === "?") return "\x7f";
  return ch;
}
term.onData((data) => {
  if (ctrl && data.length === 1) { data = ctrlChar(data); setCtrl(false); }
  send(data);
});

/* ---------- copy and paste */
let noteTimer = 0;
function note(text) {
  const el = $("state"), before = el.dataset.base || el.textContent;
  el.dataset.base = before;
  el.textContent = text;
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { el.textContent = el.dataset.base; delete el.dataset.base; }, 2000);
}
function legacyCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { /* not allowed */ }
  ta.remove();
  return ok;
}
async function copyText(text) {
  // the async clipboard only exists on https/localhost; plain http over Tailscale uses the old way
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
  }
  return legacyCopy(text);
}
function screenText() {
  const buf = term.buffer.active, lines = [];
  for (let i = Math.max(0, buf.length - 400); i < buf.length; i++) {
    const line = buf.getLine(i);
    lines.push(line ? line.translateToString(true) : "");
  }
  while (lines.length && !lines[lines.length - 1]) lines.pop();
  return lines.join("\n");
}
function openSheet(id) {
  document.querySelectorAll(".sheet").forEach((s) => { s.hidden = true; });
  $(id).hidden = false;
  $("backdrop").hidden = false;
}
function closeSheets() {
  document.querySelectorAll(".sheet").forEach((s) => { s.hidden = true; });
  $("backdrop").hidden = true;
  term.focus();
}
$("backdrop").addEventListener("click", closeSheets);
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeSheets));

async function doCopy() {
  const sel = term.getSelection();
  if (sel) {
    note((await copyText(sel)) ? "Copied" : "Could not copy");
    return;
  }
  // nothing selected (selecting is hard on a touch screen): offer the text itself
  $("copy-text").value = screenText();
  openSheet("sheet-copy");
  $("copy-text").scrollTop = $("copy-text").scrollHeight;
}
$("copy-all").addEventListener("click", async () => {
  const ok = await copyText($("copy-text").value);
  closeSheets();
  note(ok ? "Copied" : "Could not copy");
});

async function doPaste() {
  if (navigator.clipboard && window.isSecureContext && navigator.clipboard.readText) {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { term.paste(text); return; }
    } catch (e) { /* not allowed: use the box below */ }
  }
  $("paste-text").value = "";
  openSheet("sheet-paste");
  $("paste-text").focus();
}
$("paste-send").addEventListener("click", () => {
  const text = $("paste-text").value;
  closeSheets();
  if (text) term.paste(text);
});

/* Ctrl+Shift+C copies the selection (Ctrl+C must stay "interrupt"); the browser's own paste keeps working */
term.attachCustomKeyEventHandler((ev) => {
  if (ev.type === "keydown" && ev.ctrlKey && ev.shiftKey && ev.code === "KeyC") {
    const sel = term.getSelection();
    if (sel) copyText(sel).then((ok) => note(ok ? "Copied" : "Could not copy"));
    ev.preventDefault();
    return false;
  }
  return true;
});

/* ---------- key bar */
const ARROWS = { up: "A", down: "B", right: "C", left: "D" };
function pressKey(key) {
  if (key === "ctrl") { setCtrl(!ctrl); return; }
  if (key === "copy") { doCopy(); return; }
  if (key === "paste") { doPaste(); return; }
  if (key === "esc") send("\x1b");
  else if (key === "tab") send("\t");
  else if (ARROWS[key]) send("\x1b" + (term.modes.applicationCursorKeysMode ? "O" : "[") + ARROWS[key]);
  setCtrl(false);
}
document.querySelectorAll("#keys button").forEach((b) => {
  // pointerdown + preventDefault keeps the focus in the terminal, so a phone keyboard stays open
  b.addEventListener("pointerdown", (ev) => { ev.preventDefault(); pressKey(b.dataset.key); });
  b.addEventListener("click", (ev) => { if (ev.detail === 0) pressKey(b.dataset.key); });   // keyboard activation
});

let resizeTimer = 0;
function sendSize() {
  fit.fit();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (id) api("/api/term/resize", { id, cols: term.cols, rows: term.rows }).catch(() => {}); }, 150);
}
addEventListener("resize", sendSize);

function ended() {
  if (source) { source.close(); source = null; }
  id = null;
  store("homeos-term-id", null);
  $("state").textContent = "Ended";
  $("ended").hidden = false;
}

function attach(tid) {
  id = tid;
  store("homeos-term-id", tid);
  $("ended").hidden = true;
  source = new EventSource("/api/term/stream?id=" + encodeURIComponent(tid));
  source.onopen = () => { $("state").textContent = "Connected"; sendSize(); };
  source.onmessage = (ev) => {
    const bin = atob(ev.data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    term.write(bytes);
  };
  source.addEventListener("ended", ended);
  source.onerror = () => {
    if (!source) return;
    if (source.readyState === EventSource.CLOSED) {
      // the server refused the stream: the shell is gone (for example after a restart)
      source = null;
      start(true);
    } else {
      $("state").textContent = "Reconnecting…";
    }
  };
}

async function start(fresh) {
  const old = fresh ? null : store("homeos-term-id");
  if (old) { attach(old); return; }      // re-attach after visiting the home screen
  term.reset();
  try {
    const res = await api("/api/term/open", { cols: term.cols, rows: term.rows });
    if (!res.ok) { $("state").textContent = res.error || "Could not start"; $("ended").hidden = false; return; }
    attach(res.id);
  } catch (e) { $("state").textContent = e.message; }
}

$("again").addEventListener("click", () => start(true));
function zoom(d) {
  fontSize = Math.max(9, Math.min(32, fontSize + d));
  term.options.fontSize = fontSize;
  store("homeos-term-font", String(fontSize));
  sendSize();
}
$("smaller").addEventListener("click", () => { zoom(-1); term.focus(); });
$("bigger").addEventListener("click", () => { zoom(1); term.focus(); });

start(false);
term.focus();
