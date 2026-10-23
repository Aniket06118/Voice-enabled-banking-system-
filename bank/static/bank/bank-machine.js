/*
 * EchoMint bank machine: an interactive 3D model of how one payment travels
 * through EchoMint. A blue coin visits five stations in order:
 *   1 You speak  ->  2 Voice check  ->  3 EchoMint understands  ->  4 You confirm  ->  5 Money moves
 * Procedural three.js only (no models, no images). Inspired by the "Agentic Factory"
 * template from 21st.dev, rebuilt for banking.
 *
 * import { initBankMachine, STEPS } from "./bank-machine.js"
 * const machine = initBankMachine(el, { offset: 0.22, onStep: i => ..., onReady: () => ... })
 * machine.focus(2)   // jump the coin to a station
 * machine.dispose()
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

export const STEPS = [
  { id: "speak", name: "You speak", desc: "Say what you need in Hindi or English. No menus, no small buttons." },
  { id: "verify", name: "Voice check", desc: "EchoMint matches your voice while you read a few fresh digits, so a recording can't pass." },
  { id: "understand", name: "EchoMint understands", desc: "It works out who and how much, then reads it back to you in plain words." },
  { id: "confirm", name: "You confirm", desc: "Say “yes” for small amounts. From ₹500, type your 4-digit PIN on screen." },
  { id: "settle", name: "Money moves", desc: "The payment is recorded once, safely, and you hear your new balance." },
];

const DWELL = 1.7;      // seconds the coin rests at each station
const TRAVEL = 1.5;     // seconds between stations
const SLOT = DWELL + TRAVEL;

export function initBankMachine(root, opts = {}) {
  const options = { offset: 0, interactive: true, onStep: () => {}, onReady: () => {}, ...opts };
  const font = opts.fontFamily || getComputedStyle(root).fontFamily;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const cleanups = [];
  const on = (t, type, fn, o) => { t.addEventListener(type, fn, o); cleanups.push(() => t.removeEventListener(type, fn, o)); };
  const TAU = Math.PI * 2;

  // ------------------------------------------------------------ renderer
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
  } catch (e) {
    root.classList.add("no-webgl");
    return { focus() {}, dispose() {} };
  }
  const W = () => Math.max(1, root.clientWidth), H = () => Math.max(1, root.clientHeight);
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.75));
  renderer.setSize(W(), H());
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.96;   // a touch softer on the eyes
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  root.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, W() / H(), 0.1, 200);
  const HOME = { pos: new THREE.Vector3(11.5, 11.2, 17.5), target: new THREE.Vector3(0.1, 0.4, 0.2) };
  camera.position.copy(HOME.pos);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(HOME.target);
  controls.enableDamping = true; controls.dampingFactor = 0.07;
  controls.enablePan = false; controls.enableZoom = false;
  controls.minPolarAngle = 0.25; controls.maxPolarAngle = Math.PI * 0.46;
  controls.rotateSpeed = 0.5;
  if (!options.interactive || matchMedia("(pointer: coarse)").matches) controls.enableRotate = false;

  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  const env = pmrem.fromScene(room, 0.04);
  scene.environment = env.texture;
  scene.environmentIntensity = 0.55;
  room.dispose(); pmrem.dispose();

  scene.add(new THREE.HemisphereLight(0xeef3ff, 0x1b2232, 1.5));
  const key = new THREE.DirectionalLight(0xffffff, 3.4);
  key.position.set(-5, 13, 8); key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  Object.assign(key.shadow.camera, { left: -10, right: 10, top: 9, bottom: -9, near: 0.5, far: 40 });
  key.shadow.normalBias = 0.03; key.shadow.radius = 5;
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xbcd0ff, 2.4); rim.position.set(6, 6, -9); scene.add(rim);
  const fill = new THREE.DirectionalLight(0xffffff, 0.8); fill.position.set(6, 3, 10); scene.add(fill);

  // ------------------------------------------------------------ materials
  const mat = (color, metalness, roughness, extra = {}) =>
    new THREE.MeshStandardMaterial({ color, metalness, roughness, ...extra });
  const M = {
    porcelain: mat(0xf4f6fa, 0.05, 0.36),
    pearl: mat(0xe3e8f1, 0.15, 0.32),
    ink: mat(0x0a0d14, 0.35, 0.42),
    graphite: mat(0x262c38, 0.7, 0.34),
    steel: mat(0xc3cad6, 0.92, 0.2),
    blue: mat(0x2d5bff, 0.3, 0.3, { emissive: 0x2d5bff, emissiveIntensity: 1.2 }),
    paper: mat(0xffffff, 0, 0.8),
  };
  const geos = new Map();
  const cache = (k, make) => { if (!geos.has(k)) geos.set(k, make()); return geos.get(k); };
  function box(p, w, h, d, x, y, z, m = M.porcelain, r = 0.04) {
    const g = cache(`b${w},${h},${d},${r}`, () => r ? new RoundedBoxGeometry(w, h, d, 3, Math.min(r, w / 3, h / 3, d / 3)) : new THREE.BoxGeometry(w, h, d));
    const o = new THREE.Mesh(g, m); o.position.set(x, y, z); o.castShadow = o.receiveShadow = true; p.add(o); return o;
  }
  function cyl(p, r, h, x, y, z, m = M.steel, r2 = r, seg = 32) {
    const o = new THREE.Mesh(cache(`c${r},${r2},${h},${seg}`, () => new THREE.CylinderGeometry(r, r2, h, seg)), m);
    o.position.set(x, y, z); o.castShadow = o.receiveShadow = true; p.add(o); return o;
  }
  function sphere(p, r, x, y, z, m) {
    const o = new THREE.Mesh(cache(`s${r}`, () => new THREE.SphereGeometry(r, 32, 16)), m);
    o.position.set(x, y, z); o.castShadow = true; p.add(o); return o;
  }
  function torus(p, r, t, x, y, z, m) {
    const o = new THREE.Mesh(cache(`t${r},${t}`, () => new THREE.TorusGeometry(r, t, 12, 64)), m);
    o.position.set(x, y, z); p.add(o); return o;
  }
  function paint(w, h, draw) {
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const ctx = c.getContext("2d");
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
    const obj = { tex, ctx, w, h, draw(...a) { ctx.clearRect(0, 0, w, h); draw(ctx, w, h, ...a); tex.needsUpdate = true; } };
    obj.draw(); return obj;
  }
  function screen(p, w, h, x, y, z, painted) {
    const o = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: painted.tex, toneMapped: false, transparent: true }));
    o.position.set(x, y, z); p.add(o); return o;
  }
  const text = (c, s, x, y, size, color, weight = 500, align = "left") => {
    c.font = `${weight} ${size}px ${font}`; c.fillStyle = color; c.textAlign = align; c.fillText(s, x, y);
  };
  const rr = (c, x, y, w, h, r, fill) => { c.beginPath(); c.roundRect(x, y, w, h, r); c.fillStyle = fill; c.fill(); };
  const INK = "#0a0d14", BLUE = "#2d5bff", SOFT = "#8b93a7", PAPER = "#f6f8fc";

  // ------------------------------------------------------------ plate
  const machine = new THREE.Group(); scene.add(machine);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(80, 80), new THREE.ShadowMaterial({ opacity: 0.16 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = -0.42; floor.receiveShadow = true; scene.add(floor);

  box(machine, 12.9, 0.16, 8.1, 0, -0.33, 0, M.ink, 0.07);
  box(machine, 12.7, 0.36, 7.9, 0, -0.08, 0, M.porcelain, 0.18);
  box(machine, 12.3, 0.03, 0.035, 0, -0.27, 3.98, M.blue, 0.01);
  for (const x of [-5.9, 5.9]) for (const z of [-3.5, 3.5]) {
    cyl(machine, 0.07, 0.03, x, 0.115, z, M.steel, 0.07, 16);
  }
  const engraving = paint(1600, 150, (c, w, h) => {
    rr(c, 0, 0, w, h, 18, "#eef1f7");
    text(c, "EchoMint", 44, 92, 62, INK, 700);
    text(c, "one payment, start to finish", 360, 90, 36, SOFT, 400);
    text(c, "No. 001", w - 44, 90, 30, BLUE, 600, "right");
  });
  const plate = screen(machine, 7.6, 0.72, -0.9, 0.105, 3.45, engraving);
  plate.rotation.x = -Math.PI / 2;

  // ------------------------------------------------------------ stations
  const POS = [[-4.4, 0.55], [-2.0, -1.85], [1.0, -1.95], [3.95, -0.35], [1.55, 1.95]];
  const stations = POS.map(([x, z], i) => {
    const g = new THREE.Group(); g.position.set(x, 0.1, z); machine.add(g);
    const glow = M.blue.clone(); glow.emissiveIntensity = 0.25;
    box(g, 2.05, 0.1, 1.75, 0, 0.05, 0, M.ink, 0.08);
    box(g, 1.99, 0.03, 1.69, 0, 0.115, 0, glow, 0.07);
    box(g, 2.05, 0.14, 1.75, 0, 0.2, 0, M.porcelain, 0.09);
    const plaque = paint(512, 112, (c, w, h) => {
      rr(c, 0, 0, w, h, 16, INK);
      text(c, String(i + 1), 34, 76, 50, BLUE, 700);
      text(c, STEPS[i].name, 86, 73, 38, PAPER, 500);
    });
    screen(g, 1.5, 0.33, 0, 0.2, 0.878, plaque);
    g.traverse(o => { o.userData.station = i; });
    return { g, glow, i, level: 0 };
  });
  const top = 0.27;   // station deck height

  // 1. You speak: a microphone with sound rings
  const s1 = stations[0].g;
  cyl(s1, 0.36, 0.42, 0, top + 0.21, -0.1, M.porcelain, 0.44);
  cyl(s1, 0.05, 0.75, 0, top + 0.8, -0.1, M.steel);
  const mic = new THREE.Group(); mic.position.set(0, top + 1.45, -0.1); s1.add(mic);
  cyl(mic, 0.27, 0.46, 0, 0, 0, M.graphite);
  sphere(mic, 0.27, 0, 0.23, 0, M.graphite);
  for (let k = 0; k < 5; k++) torus(mic, 0.275, 0.012, 0, 0.27 - k * 0.09, 0, M.steel).rotation.x = Math.PI / 2;
  const micBand = torus(mic, 0.28, 0.022, 0, -0.17, 0, stations[0].glow); micBand.rotation.x = Math.PI / 2;
  const waveMat = new THREE.MeshBasicMaterial({ color: 0x2d5bff, transparent: true, opacity: 0, toneMapped: false });
  const waves = [0, 1, 2].map(() => torus(s1, 0.42, 0.014, 0, top + 1.5, -0.1, waveMat.clone()));
  s1.children.forEach(o => o.userData.station = 0); mic.traverse(o => o.userData.station = 0);

  // 2. Voice check: voiceprint screen + spinning platter
  const s2 = stations[1].g;
  box(s2, 1.75, 0.5, 1.2, 0, top + 0.25, 0.1, M.porcelain, 0.1);
  const platter = new THREE.Group(); platter.position.set(-0.35, top + 0.53, 0.25); s2.add(platter);
  cyl(platter, 0.42, 0.05, 0, 0, 0, M.steel, 0.42, 48);
  cyl(platter, 0.3, 0.06, 0, 0.005, 0, M.ink, 0.3, 48);
  sphere(platter, 0.045, 0.22, 0.04, 0, M.blue);
  box(s2, 0.5, 0.08, 0.34, 0.5, top + 0.55, 0.3, M.ink, 0.03);
  box(s2, 1.9, 1.15, 0.1, 0, top + 1.35, -0.42, M.ink, 0.05);
  cyl(s2, 0.05, 0.6, 0, top + 0.6, -0.48, M.steel);
  const printTex = paint(640, 380, (c, w, h, t = 0, active = 0, digits = "4 7 2 9 1 3") => {
    rr(c, 0, 0, w, h, 22, "#0d1322");
    text(c, active > 0.5 ? "It's you" : "Read the digits", 34, 62, 32, active > 0.5 ? "#9db4ff" : "#c9d0de", 600);
    text(c, digits, w - 34, 62, 30, "#6f7a91", 500, "right");
    const mid = 200, amp = 62;
    for (const [col, ph, a] of [["#3b4458", 0.0, 1], [BLUE, 0.35, 0.35 + 0.65 * active]]) {
      c.beginPath(); c.lineWidth = 6; c.strokeStyle = col; c.globalAlpha = a;
      for (let x = 30; x <= w - 30; x += 6) {
        const u = x / w;
        const y = mid + Math.sin(u * 22 + t * 3 + ph) * amp * Math.sin(u * Math.PI) * (0.6 + 0.4 * Math.sin(u * 7 + ph));
        x === 30 ? c.moveTo(x, y) : c.lineTo(x, y);
      }
      c.stroke(); c.globalAlpha = 1;
    }
    rr(c, 34, 318, w - 68, 12, 6, "#1e2740");
    rr(c, 34, 318, (w - 68) * Math.min(1, active), 12, 6, BLUE);
  });
  screen(s2, 1.76, 1.02, 0, top + 1.35, -0.366, printTex);
  s2.traverse(o => o.userData.station = 1);

  // 3. EchoMint understands: console with the conversation
  const s3 = stations[2].g;
  box(s3, 1.8, 0.55, 1.3, 0, top + 0.27, 0.05, M.porcelain, 0.1);
  const desk = new THREE.Group(); desk.position.set(0, top + 0.95, -0.05); desk.rotation.x = -0.42; s3.add(desk);
  box(desk, 1.85, 1.2, 0.1, 0, 0, 0, M.ink, 0.05);
  const chatTex = paint(640, 420, (c, w, h, p = 1) => {
    rr(c, 0, 0, w, h, 22, "#0d1322");
    const say = "Send 300 to Aman";
    const reply = "Send ₹300 to Aman Gupta?";
    const reply2 = "Say yes to confirm.";
    const n1 = Math.round(say.length * Math.min(1, p * 2.2));
    rr(c, w - 40 - 380, 40, 380, 82, 22, "#1f2b4a");
    text(c, say.slice(0, n1), w - 62, 92, 32, "#e6ebf7", 500, "right");
    if (p > 0.5) {
      const q = Math.min(1, (p - 0.5) * 2.2);
      rr(c, 40, 160, 470, 132, 22, "#f2f5fb");
      text(c, reply.slice(0, Math.round(reply.length * q)), 66, 212, 32, INK, 600);
      text(c, reply2.slice(0, Math.round(reply2.length * Math.max(0, q * 1.6 - 0.6))), 66, 258, 28, "#4b5468", 500);
    }
    for (let k = 0; k < 3; k++) { c.beginPath(); c.arc(56 + k * 26, 370, 8, 0, TAU); c.fillStyle = k === Math.floor(p * 9) % 3 ? BLUE : "#2b344a"; c.fill(); }
  });
  screen(desk, 1.72, 1.1, 0, 0, 0.052, chatTex);
  const lamps = [0, 1, 2].map(k => sphere(s3, 0.055, -0.45 + k * 0.45, top + 0.56, 0.62, k === 1 ? stations[2].glow : M.steel));
  s3.traverse(o => o.userData.station = 2);

  // 4. You confirm: keypad (PIN from ₹500) and a "yes" lamp
  const s4 = stations[3].g;
  box(s4, 1.6, 0.38, 1.3, 0, top + 0.19, 0.05, M.porcelain, 0.1);
  const pad = new THREE.Group(); pad.position.set(0, top + 0.42, 0.12); pad.rotation.x = -0.12; s4.add(pad);
  box(pad, 1.2, 0.06, 1.0, 0, 0, 0, M.ink, 0.04);
  const keys = [];
  for (let r = 0; r < 4; r++) for (let q = 0; q < 3; q++) {
    const okKey = r === 3 && q === 2;
    const k = box(pad, 0.3, 0.07, 0.19, -0.36 + q * 0.36, 0.06, -0.33 + r * 0.23, okKey ? stations[3].glow : M.pearl, 0.03);
    k.userData.rest = 0.06; keys.push(k);
  }
  box(s4, 1.5, 0.95, 0.1, 0, top + 1.0, -0.55, M.ink, 0.05);
  const ruleTex = paint(600, 380, (c, w, h, which = 0) => {
    rr(c, 0, 0, w, h, 22, "#0d1322");
    rr(c, 28, 34, w - 56, 140, 18, which === 0 ? "#1f2b4a" : "#141b2c");
    text(c, "Under ₹500", 56, 92, 34, "#e6ebf7", 600);
    text(c, "say “yes”", 56, 142, 30, which === 0 ? "#9db4ff" : "#6f7a91", 500);
    rr(c, 28, 200, w - 56, 140, 18, which === 1 ? "#1f2b4a" : "#141b2c");
    text(c, "₹500 or more", 56, 258, 34, "#e6ebf7", 600);
    text(c, "PIN on screen", 56, 308, 30, which === 1 ? "#9db4ff" : "#6f7a91", 500);
    for (let k = 0; k < 4; k++) { c.beginPath(); c.arc(w - 160 + k * 34, 290, 10, 0, TAU); c.fillStyle = which === 1 ? BLUE : "#2b344a"; c.fill(); }
  });
  screen(s4, 1.38, 0.86, 0, top + 1.0, -0.497, ruleTex);
  s4.traverse(o => o.userData.station = 3);

  // 5. Money moves: vault door + receipt
  const s5 = stations[4].g;
  box(s5, 1.7, 1.45, 1.15, 0, top + 0.73, -0.12, M.porcelain, 0.14);
  const door = new THREE.Group(); door.position.set(0, top + 0.78, 0.47); s5.add(door);
  const d1 = cyl(door, 0.52, 0.1, 0, 0, 0, M.steel, 0.52, 64); d1.rotation.x = Math.PI / 2;
  const d2 = cyl(door, 0.4, 0.12, 0, 0, 0.01, M.graphite, 0.4, 64); d2.rotation.x = Math.PI / 2;
  const wheel = new THREE.Group(); wheel.position.z = 0.09; door.add(wheel);
  for (let k = 0; k < 3; k++) { const s = box(wheel, 0.62, 0.05, 0.05, 0, 0, 0, M.steel, 0.02); s.rotation.z = (k / 3) * Math.PI; }
  const hub = cyl(wheel, 0.08, 0.08, 0, 0, 0, stations[4].glow); hub.rotation.x = Math.PI / 2;
  box(s5, 0.9, 0.05, 0.12, 0, top + 1.47, -0.15, M.ink, 0.02);
  const receipt = new THREE.Group(); receipt.position.set(0, top + 1.47, -0.15); s5.add(receipt);
  const receiptTex = paint(300, 420, (c, w, h) => {
    c.fillStyle = "#ffffff"; c.fillRect(0, 0, w, h);
    text(c, "Paid", 26, 70, 40, INK, 700);
    text(c, "₹300 to Aman", 26, 120, 30, INK, 500);
    c.strokeStyle = "#c8cfdc"; c.setLineDash([6, 7]); c.lineWidth = 2;
    c.beginPath(); c.moveTo(24, 160); c.lineTo(w - 24, 160); c.stroke(); c.setLineDash([]);
    text(c, "New balance", 26, 210, 24, "#5b6478", 500);
    text(c, "₹9,700", 26, 256, 40, INK, 700);
    for (let k = 0; k < 38; k++) { c.fillStyle = INK; c.fillRect(26 + k * 6.5, 310, 1 + (k % 3), 70); }
  });
  const slip = screen(receipt, 0.62, 0.86, 0, 0.43, 0, receiptTex);
  slip.material.side = THREE.DoubleSide;
  receipt.scale.y = 0.001;
  s5.traverse(o => o.userData.station = 4);

  // ------------------------------------------------------------ conveyor + coin
  const curve = new THREE.CatmullRomCurve3([
    [-3.15, 0.6, 1.0], [-2.0, 0.6, -0.7], [1.0, 0.6, -0.8], [2.85, 0.6, -0.1], [1.55, 0.6, 0.85], [-1.4, 0.6, 1.55],
  ].map(p => new THREE.Vector3(...p)), true, "catmullrom", 0.5);
  const belt = new THREE.Mesh(new THREE.TubeGeometry(curve, 240, 0.075, 12, true), M.ink);
  belt.castShadow = true; machine.add(belt);
  const rail = new THREE.Mesh(new THREE.TubeGeometry(curve, 240, 0.02, 8, true), M.steel);
  rail.position.y = 0.075; machine.add(rail);
  const N = 90;
  const treads = new THREE.InstancedMesh(new THREE.BoxGeometry(0.05, 0.03, 0.26), M.pearl, N);
  treads.castShadow = true; machine.add(treads);
  const tmp = new THREE.Object3D(), up = new THREE.Vector3(0, 1, 0);
  function placeTreads(shift) {
    for (let k = 0; k < N; k++) {
      const u = (k / N + shift) % 1;
      const p = curve.getPointAt(u), tg = curve.getTangentAt(u);
      tmp.position.copy(p); tmp.position.y += 0.07;
      tmp.lookAt(p.clone().add(tg)); tmp.rotateOnWorldAxis(up, 0);
      tmp.updateMatrix(); treads.setMatrixAt(k, tmp.matrix);
    }
    treads.instanceMatrix.needsUpdate = true;
  }
  // where each station sits along the belt
  const U = POS.map(([x, z]) => {
    let best = 0, bd = Infinity;
    for (let k = 0; k < 600; k++) { const p = curve.getPointAt(k / 600); const d = (p.x - x) ** 2 + (p.z - z) ** 2; if (d < bd) { bd = d; best = k / 600; } }
    return best;
  });
  const coin = new THREE.Group(); machine.add(coin);
  const coinBody = cyl(coin, 0.24, 0.07, 0, 0, 0, M.blue, 0.24, 48);
  const coinRing = torus(coin, 0.24, 0.02, 0, 0, 0, M.steel); coinRing.rotation.x = Math.PI / 2;
  const rupeeTex = paint(128, 128, (c, w, h) => { text(c, "₹", 64, 96, 88, "#ffffff", 700, "center"); });
  const face = screen(coin, 0.3, 0.3, 0, 0.037, 0, rupeeTex); face.rotation.x = -Math.PI / 2;
  const halo = torus(coin, 0.34, 0.012, 0, 0, 0, new THREE.MeshBasicMaterial({ color: 0x2d5bff, transparent: true, opacity: 0.5, toneMapped: false }));
  halo.rotation.x = Math.PI / 2;

  // ------------------------------------------------------------ DOM labels + tooltip
  const labelLayer = document.createElement("div"); labelLayer.className = "bm-labels"; root.appendChild(labelLayer);
  const labels = STEPS.map((s, i) => {
    const el = document.createElement("div"); el.className = "bm-label";
    el.innerHTML = `<span class="n">${i + 1}</span><span class="t">${s.name}</span>`;
    labelLayer.appendChild(el); return el;
  });
  const tip = document.createElement("div"); tip.className = "bm-tip"; tip.setAttribute("role", "tooltip"); root.appendChild(tip);
  cleanups.push(() => { labelLayer.remove(); tip.remove(); });

  // ------------------------------------------------------------ interaction
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  let hover = -1, pointer = { x: 0, y: 0 }, manual = null;
  const pickables = []; stations.forEach(s => s.g.traverse(o => { if (o.isMesh) pickables.push(o); }));
  on(renderer.domElement, "pointermove", (e) => {
    const r = renderer.domElement.getBoundingClientRect();
    pointer.x = (e.clientX - r.left) / r.width * 2 - 1; pointer.y = (e.clientY - r.top) / r.height * 2 - 1;
    ndc.set(pointer.x, -pointer.y); ray.setFromCamera(ndc, camera);
    const hit = ray.intersectObjects(pickables, false)[0];
    hover = hit ? hit.object.userData.station : -1;
    renderer.domElement.style.cursor = hover >= 0 ? "pointer" : "grab";
    if (hover >= 0) {
      tip.innerHTML = `<strong>${hover + 1}. ${STEPS[hover].name}</strong><p>${STEPS[hover].desc}</p>`;
      tip.style.transform = `translate(${Math.min(e.clientX - r.left + 18, r.width - 280)}px, ${e.clientY - r.top + 18}px)`;
      tip.classList.add("on");
    } else tip.classList.remove("on");
  });
  on(renderer.domElement, "pointerleave", () => { hover = -1; tip.classList.remove("on"); pointer = { x: 0, y: 0 }; });
  on(renderer.domElement, "click", () => { if (hover >= 0) api.focus(hover); });

  // ------------------------------------------------------------ layout
  function layout() {
    const w = W(), h = H();
    renderer.setSize(w, h);
    camera.aspect = w / h;
    const shift = w > 900 ? options.offset : 0;
    if (shift) camera.setViewOffset(w, h, -w * shift, 0, w, h); else camera.clearViewOffset();
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(layout); ro.observe(root); cleanups.push(() => ro.disconnect());
  layout();

  // ------------------------------------------------------------ animation
  const clock = new THREE.Clock();
  let t = 0, running = true, visible = true, lastStage = -1, lastPaint = 0;
  const io = new IntersectionObserver(([en]) => { visible = en.isIntersecting; }, { threshold: 0.01 });
  io.observe(root); cleanups.push(() => io.disconnect());
  const ease = (x) => x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2;
  const v3 = new THREE.Vector3();

  function coinState() {
    if (manual) {
      const age = t - manual.at;
      if (age > 6) manual = null; else return { stage: manual.i, u: U[manual.i], resting: true, local: Math.min(age, DWELL) };
    }
    const cycle = t % (SLOT * 5), stage = Math.floor(cycle / SLOT), local = cycle - stage * SLOT;
    if (local < DWELL) return { stage, u: U[stage], resting: true, local };
    let a = U[stage], b = U[(stage + 1) % 5]; if (b < a) b += 1;
    return { stage, u: (a + (b - a) * ease((local - DWELL) / TRAVEL)) % 1, resting: false, local };
  }

  function frame() {
    if (!running) return;
    requestAnimationFrame(frame);
    if (!visible || document.hidden) { clock.getDelta(); return; }
    const dt = Math.min(clock.getDelta(), 0.05);
    if (!reduce) t += dt;
    const st = coinState();

    // coin
    curve.getPointAt(st.u, v3);
    coin.position.set(v3.x, v3.y + 0.2 + Math.sin(t * 4) * 0.03, v3.z);
    coin.rotation.y += dt * (st.resting ? 1.2 : 3.5);
    halo.scale.setScalar(1 + 0.15 * Math.sin(t * 5));
    if (!reduce) placeTreads((t * 0.035) % 1);

    // station glow: the resting station lights up
    stations.forEach((s, i) => {
      const target = (st.stage === i && st.resting) ? 1 : (hover === i ? 0.55 : 0);
      s.level += (target - s.level) * Math.min(1, dt * 6);
      s.glow.emissiveIntensity = 0.25 + s.level * 2.4;
      s.g.position.y = 0.1 + s.level * 0.06;
      labels[i].classList.toggle("on", s.level > 0.5);
    });
    const L = stations.map(s => s.level);

    // 1 mic waves
    waves.forEach((wv, k) => {
      const ph = ((t * 0.9 + k / 3) % 1);
      wv.scale.setScalar(1 + ph * 2.2);
      wv.material.opacity = (1 - ph) * (0.15 + 0.7 * L[0]);
    });
    mic.rotation.y = Math.sin(t * 0.8) * 0.15;
    // 2 platter
    platter.rotation.y += dt * (0.6 + L[1] * 4);
    // 4 keypad presses
    keys.forEach((k, idx) => {
      const press = L[3] > 0.5 && [1, 6, 8, 3, 11][Math.floor(t * 4) % 5] === idx;
      k.position.y += ((press ? 0.02 : k.userData.rest) - k.position.y) * Math.min(1, dt * 18);
    });
    // 5 vault + receipt
    wheel.rotation.z += dt * (0.2 + L[4] * 3);
    receipt.scale.y += ((L[4] > 0.5 ? 1 : 0.001) - receipt.scale.y) * Math.min(1, dt * 3);
    lamps.forEach((l, k) => l.scale.setScalar(1 + (L[2] > 0.5 && Math.floor(t * 6) % 3 === k ? 0.4 : 0)));

    // screens (repainted ~12 times a second)
    if (t - lastPaint > 0.08 || reduce) {
      lastPaint = t;
      printTex.draw(t, L[1]);
      const p = st.stage === 2 && st.resting ? st.local / DWELL : (st.stage > 2 || (st.stage === 2 && !st.resting) ? 1 : 0.0);
      chatTex.draw(reduce ? 1 : p);
      ruleTex.draw(Math.floor(t / (SLOT * 5)) % 2);
    }

    if (st.stage !== lastStage && st.resting) { lastStage = st.stage; options.onStep(st.stage); }

    // gentle parallax toward the pointer
    machine.rotation.y += ((pointer.x * 0.12) - machine.rotation.y) * Math.min(1, dt * 3);
    machine.rotation.x += ((pointer.y * 0.03) - machine.rotation.x) * Math.min(1, dt * 3);

    controls.update();
    renderer.render(scene, camera);

    // labels follow their stations
    const r = renderer.domElement.getBoundingClientRect();
    stations.forEach((s, i) => {
      s.g.getWorldPosition(v3); v3.y += 2.55; v3.project(camera);
      labels[i].style.transform = `translate(${(v3.x * 0.5 + 0.5) * r.width}px, ${(-v3.y * 0.5 + 0.5) * r.height}px)`;
    });
  }

  const api = {
    focus(i) { manual = { i, at: t }; lastStage = -1; },
    dispose() {
      running = false;
      cleanups.reverse().forEach(f => f());
      controls.dispose(); renderer.dispose(); env.dispose();
      geos.forEach(g => g.dispose());
      renderer.domElement.remove();
    },
  };
  placeTreads(0);
  if (reduce) api.focus(2);
  requestAnimationFrame(() => { frame(); options.onReady(); });
  return api;
}
