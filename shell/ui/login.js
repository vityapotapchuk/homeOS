"use strict";
const $ = (id) => document.getElementById(id);
const state = (document.querySelector('meta[name="homeos-state"]') || {}).content;

if (state === "open") {               // the password is switched off (Settings > Sign-in)
  $("password").hidden = true;
  $("password").required = false;
  $("login-form").classList.add("open");
  $("go").setAttribute("aria-label", "Open HomeOS");
  $("go").insertAdjacentText("afterbegin", "Open HomeOS");
}
if (state === "nopassword") {
  $("login-form").hidden = true;
  $("nopass").hidden = false;
}

/* The sign-in page looks like the start screen: the time, the date and the weather. */
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
function tick() {
  const d = new Date();
  showClock(String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"));
  $("date").textContent = d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}
tick();
setInterval(tick, 1000);

const svgNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const n = document.createElementNS(svgNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
function weatherIcon(kind) {      // the same drawings as on the start screen (app.js)
  const svg = svgEl("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
  const line = (x1, y1, x2, y2) => svg.appendChild(svgEl("line", { x1, y1, x2, y2, stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round" }));
  if (kind === "sun") {
    svg.appendChild(svgEl("circle", { cx: 12, cy: 12, r: 4.5 }));
    for (let i = 0; i < 8; i++) {
      const a = (i * Math.PI) / 4;
      line(12 + 7 * Math.cos(a), 12 + 7 * Math.sin(a), 12 + 9.5 * Math.cos(a), 12 + 9.5 * Math.sin(a));
    }
    return svg;
  }
  if (kind === "partly") svg.appendChild(svgEl("circle", { cx: 17, cy: 7, r: 3.5 }));
  const low = kind === "rain" || kind === "snow" || kind === "storm";
  svg.appendChild(svgEl("path", { d: "M7 18a4 4 0 0 1-.5-7.97A5.5 5.5 0 0 1 17.2 9.1 4.5 4.5 0 0 1 17 18H7z", transform: low ? "translate(0,-3)" : "" }));
  if (kind === "rain") for (const x of [8, 12, 16]) line(x, 17.5, x - 1, 21);
  if (kind === "snow") for (const x of [8, 12, 16]) svg.appendChild(svgEl("circle", { cx: x, cy: 19.5, r: 1.2 }));
  if (kind === "storm") svg.appendChild(svgEl("path", { d: "M13 16l-3 4h2.5l-1 3 3.5-4.5h-2.5l1-2.5z" }));
  return svg;
}
fetch("/api/public/weather").then((res) => res.json()).then((w) => {
  if (!w.ok) return;
  $("w-weather-icon").replaceChildren(weatherIcon(w.kind));
  $("w-weather-icon").dataset.kind = w.kind;
  $("w-weather-temp").textContent = w.temp + "°";
  $("w-weather-text").textContent = w.text;
  $("w-weather-sub").textContent = "Feels like " + w.feels + "° · wind " + w.wind + " km/h";
  for (const d of w.days.slice(1)) {
    const cell = document.createElement("span"), day = document.createElement("b");
    day.textContent = new Date(d.date + "T12:00").toLocaleDateString("en-GB", { weekday: "short" });
    cell.append(day, document.createTextNode(d.max + "° / " + d.min + "°"));
    $("w-weather-days").appendChild(cell);
  }
  $("w-weather").hidden = false;
}).catch(() => { /* no weather: the clock is enough */ });

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("error").textContent = "";
  $("go").disabled = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("password").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) { location.href = "/"; return; }
    $("error").textContent = data.error || "Could not sign in (" + res.status + ").";
    $("login-form").classList.remove("shake");
    void $("login-form").offsetWidth;          // restarts the animation on a second wrong try
    $("login-form").classList.add("shake");
    $("password").select();
  } catch (e) {
    $("error").textContent = "The Pi is not answering. Check that Tailscale is connected.";
  } finally {
    $("go").disabled = false;
  }
});
