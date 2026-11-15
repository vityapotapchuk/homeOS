"use strict";
/* The start page sky: a 3D star field drifting towards you, slow nebula
   light, and now and then a comet falling across with a glowing tail.
   One canvas, no libraries. It runs at a lower rate on small Pis, stops
   while the page is hidden and stands still for people who ask for less
   motion. */
(() => {
  const canvas = document.getElementById("space");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d", { alpha: false });
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const small = (navigator.hardwareConcurrency || 4) <= 4 && Math.min(screen.width, screen.height) <= 800;
  let W = 0, H = 0, DPR = 1, stars = [], comets = [], nextComet = 0, last = 0, frameGap = small ? 1000 / 30 : 0;

  const COUNT = () => Math.round(Math.min(900, (W * H) / (small ? 1500 : 850)));
  const DEPTH = 1600, SPEED = 0.045;            // depth units, units per ms

  function star(fresh) {
    return {
      x: (Math.random() - 0.5) * 2 * DEPTH,
      y: (Math.random() - 0.5) * 2 * DEPTH,
      z: fresh ? DEPTH : Math.random() * DEPTH + 1,
      pz: 0,
      hue: Math.random() < 0.18 ? 205 + Math.random() * 40 : Math.random() < 0.08 ? 30 + Math.random() * 20 : 0,
      tw: Math.random() * Math.PI * 2,
    };
  }

  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, small ? 1 : 2);
    W = innerWidth; H = innerHeight;
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    const n = COUNT();
    while (stars.length < n) stars.push(star(false));
    stars.length = n;
  }

  function comet() {
    // enters from the top or the right edge, falls down-left at a steep angle
    const fromTop = Math.random() < 0.6;
    const angle = (25 + Math.random() * 30) * Math.PI / 180;        // below the horizon, falling to the left
    const speed = 0.55 + Math.random() * 0.5;                        // px per ms
    return {
      x: fromTop ? W * (0.3 + Math.random() * 0.8) : W + 20,
      y: fromTop ? -20 : H * Math.random() * 0.5,
      vx: -Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
      len: 200 + Math.random() * 260, life: 0, max: 2600 + Math.random() * 1800,
      hue: Math.random() < 0.5 ? 200 : 170 + Math.random() * 60,
      size: 1.6 + Math.random() * 1.4,
    };
  }

  function nebula(t) {
    // two soft clouds that drift and breathe
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, "#04060d"); g.addColorStop(1, "#0a0d1c");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    const clouds = [
      [0.25 + 0.05 * Math.sin(t / 19000), 0.3 + 0.04 * Math.cos(t / 23000), 0.55, "rgba(90, 70, 200, 0.32)"],
      [0.8 + 0.04 * Math.cos(t / 17000), 0.75 + 0.05 * Math.sin(t / 21000), 0.5, "rgba(20, 140, 190, 0.26)"],
      [0.6 + 0.06 * Math.sin(t / 29000), 0.15, 0.35, "rgba(190, 60, 140, 0.18)"],
    ];
    for (const [cx, cy, r, col] of clouds) {
      const R = Math.max(W, H) * r;
      const rg = ctx.createRadialGradient(cx * W, cy * H, 0, cx * W, cy * H, R);
      rg.addColorStop(0, col); rg.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = rg; ctx.fillRect(0, 0, W, H);
    }
  }

  function draw(t, dt) {
    nebula(t);
    const cx = W / 2, cy = H / 2, f = Math.min(W, H) * 0.9;
    // stars: perspective projection, a short streak from the last position
    ctx.lineCap = "round";
    for (const s of stars) {
      s.pz = s.z;
      s.z -= SPEED * dt;
      if (s.z < 1) { Object.assign(s, star(true)); s.pz = s.z; continue; }
      const sx = cx + (s.x / s.z) * f, sy = cy + (s.y / s.z) * f;
      if (sx < -50 || sx > W + 50 || sy < -50 || sy > H + 50) { Object.assign(s, star(true)); s.pz = s.z; continue; }
      const px = cx + (s.x / s.pz) * f, py = cy + (s.y / s.pz) * f;
      const near = 1 - s.z / DEPTH;
      const tw = 0.75 + 0.25 * Math.sin(t / 600 + s.tw);
      const a = Math.min(1, 0.28 + near * 1.2) * tw;
      const w = 0.8 + near * 2.4;
      ctx.strokeStyle = s.hue ? `hsla(${s.hue}, 80%, 80%, ${a})` : `rgba(255,255,255,${a})`;
      ctx.lineWidth = w;
      ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(sx + 0.01, sy); ctx.stroke();
    }
    // comets
    if (t > nextComet) {
      comets.push(comet());
      nextComet = t + 1800 + Math.random() * 4200;
    }
    for (let i = comets.length - 1; i >= 0; i--) {
      const c = comets[i];
      c.life += dt; c.x += c.vx * dt; c.y += c.vy * dt;
      const fade = Math.min(1, c.life / 300, (c.max - c.life) / 600);
      if (fade <= 0 || c.x < -c.len || c.y > H + c.len) { comets.splice(i, 1); continue; }
      const sp = Math.hypot(c.vx, c.vy), tx = c.x - (c.vx / sp) * c.len, ty = c.y - (c.vy / sp) * c.len;
      const g = ctx.createLinearGradient(c.x, c.y, tx, ty);
      g.addColorStop(0, `hsla(${c.hue}, 100%, 92%, ${0.95 * fade})`);
      g.addColorStop(0.15, `hsla(${c.hue}, 90%, 70%, ${0.5 * fade})`);
      g.addColorStop(1, `hsla(${c.hue}, 90%, 60%, 0)`);
      ctx.strokeStyle = g; ctx.lineWidth = c.size * 1.6;
      ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(tx, ty); ctx.stroke();
      const head = ctx.createRadialGradient(c.x, c.y, 0, c.x, c.y, c.size * 7);
      head.addColorStop(0, `rgba(255,255,255,${fade})`);
      head.addColorStop(0.3, `hsla(${c.hue}, 100%, 80%, ${0.6 * fade})`);
      head.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = head;
      ctx.beginPath(); ctx.arc(c.x, c.y, c.size * 7, 0, Math.PI * 2); ctx.fill();
    }
  }

  function loop(t) {
    requestAnimationFrame(loop);
    if (document.hidden) { last = t; return; }
    if (frameGap && t - last < frameGap) return;
    const dt = last ? Math.min(t - last, 60) : 16;
    last = t;
    draw(t, dt);
  }

  addEventListener("resize", resize);
  resize();
  if (still) { draw(0, 0); return; }
  nextComet = 1200;
  requestAnimationFrame(loop);
})();
