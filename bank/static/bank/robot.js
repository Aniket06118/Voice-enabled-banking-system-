/*
 * EchoMint robot: the face of the voice assistant. A vanilla three.js port of the
 * "robot-hero" component (same body, neck, glass visor, blinking eyes, ears with
 * antennas, heart eyes when tapped), recoloured for EchoMint and wired to the
 * assistant's state:
 *   idle       slow blink, looks at the pointer
 *   listening  visor glows and pulses, antenna tips light up, eyes open wide
 *   thinking   head tilts and scans, eyes narrow
 *   speaking   eyes and visor move with the voice level
 *   happy      heart eyes (payment done, or when tapped)
 *
 * const bot = initRobot(el); bot.setState("listening"); bot.setLevel(0.6); bot.happy();
 */
import * as THREE from "three";

const BLUE = new THREE.Color("#4f74ff");

class HeartCurve extends THREE.Curve {
  getPoint(t, target = new THREE.Vector3()) {
    t *= Math.PI * 2;
    const x = 16 * Math.pow(Math.sin(t), 3);
    const y = 13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t);
    return target.set(x * 0.002, (y + 6) * 0.002, 0);
  }
}

function eyePaths() {
  const w = 0.025, h = 0.035, r = 0.02, g = 0.005, V = (x, y) => new THREE.Vector3(x, y, 0);
  const half = (s) => {
    const p = new THREE.CurvePath();
    p.add(new THREE.LineCurve3(V(-w, s * g), V(-w, s * (h - r))));
    p.add(new THREE.QuadraticBezierCurve3(V(-w, s * (h - r)), V(-w, s * h), V(-w + r, s * h)));
    p.add(new THREE.LineCurve3(V(-w + r, s * h), V(w - r, s * h)));
    p.add(new THREE.QuadraticBezierCurve3(V(w - r, s * h), V(w, s * h), V(w, s * (h - r))));
    p.add(new THREE.LineCurve3(V(w, s * (h - r)), V(w, s * g)));
    return p;
  };
  return [half(1), half(-1)];
}

function speckleTexture() {
  const size = 256, c = document.createElement("canvas"); c.width = c.height = size;
  const x = c.getContext("2d");
  x.fillStyle = "#e4e8ef"; x.fillRect(0, 0, size, size);
  for (let i = 0; i < 2600; i++) {
    x.beginPath(); x.arc(Math.random() * size, Math.random() * size, 0.4 + Math.random() * 1.1, 0, Math.PI * 2);
    x.fillStyle = Math.random() > 0.2 ? "rgba(30,36,50,.55)" : "#ffffff"; x.fill();
  }
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(6, 3); t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export function initRobot(root) {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" }); }
  catch (e) { root.classList.add("no-webgl"); return { setState() {}, setLevel() {}, happy() {}, dispose() {} }; }
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.25));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  root.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 50);
  camera.position.set(0, 0.16, 2.75);
  camera.lookAt(0, 0.12, 0);
  // Plain lights only (no environment map): starts instantly even on laptops without a graphics card.
  scene.add(new THREE.HemisphereLight(0xffffff, 0xb9c3d6, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 1.8); key.position.set(1.5, 3, 4); scene.add(key);
  const rim = new THREE.DirectionalLight(0xc7d4ff, 0.9); rim.position.set(-3, 2, -2); scene.add(rim);

  // shadow blob under the robot
  const sc = document.createElement("canvas"); sc.width = sc.height = 128;
  const sx = sc.getContext("2d"), gr = sx.createRadialGradient(64, 64, 4, 64, 64, 64);
  gr.addColorStop(0, "rgba(19,36,77,.35)"); gr.addColorStop(1, "rgba(19,36,77,0)"); sx.fillStyle = gr; sx.fillRect(0, 0, 128, 128);
  const shadow = new THREE.Mesh(new THREE.PlaneGeometry(1.4, 0.5), new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(sc), transparent: true, depthWrite: false }));
  shadow.rotation.x = -Math.PI / 2; shadow.position.y = -0.74; scene.add(shadow);

  const speckle = speckleTexture();
  const chassis = new THREE.MeshStandardMaterial({ color: "#dde2ea", map: speckle, bumpMap: speckle, bumpScale: 0.004, roughness: 0.95, metalness: 0.05 });
  const body = new THREE.Group(); body.position.y = -0.3; scene.add(body);

  body.add(new THREE.Mesh(new THREE.SphereGeometry(0.43, 32, 24, 0, Math.PI * 2, Math.PI * 0.15, Math.PI * 0.85), chassis));
  const bevel = new THREE.Mesh(new THREE.TorusGeometry(0.235, 0.025, 24, 48), chassis);
  bevel.position.y = 0.34; bevel.rotation.x = Math.PI / 2; body.add(bevel);
  const np = { baseR: 0.215, baseH: -0.05, midR: 0.28, midH: 0.02, lipBottomR: 0.295, lipBottomH: 0.045, lipTopR: 0.27, lipTopH: 0.055, innerR: 0.1 };
  const neck = new THREE.Mesh(new THREE.LatheGeometry([
    new THREE.Vector2(np.innerR, np.baseH), new THREE.Vector2(np.baseR, np.baseH), new THREE.Vector2(np.midR, np.midH),
    new THREE.Vector2(np.lipBottomR, np.lipBottomH), new THREE.Vector2(np.lipTopR, np.lipTopH), new THREE.Vector2(np.innerR, np.lipTopH)], 48), chassis);
  neck.position.y = 0.38; body.add(neck);

  // chest light: shows the assistant state
  const chestMat = new THREE.MeshBasicMaterial({ color: BLUE.clone(), toneMapped: false, transparent: true, opacity: 0.0 });
  const chest = new THREE.Mesh(new THREE.TorusGeometry(0.07, 0.012, 12, 40), chestMat);
  chest.position.set(0, 0.05, 0.415); body.add(chest);

  const head = new THREE.Group(); head.position.y = 0.6; body.add(head);
  head.add(new THREE.Mesh(new THREE.SphereGeometry(0.28, 32, 24), new THREE.MeshStandardMaterial({ color: "#0d1220", roughness: 0.9 })));
  const visorUniforms = { color: { value: BLUE.clone() }, power: { value: 3.8 }, intensity: { value: 1.0 } };
  head.add(new THREE.Mesh(new THREE.SphereGeometry(0.3, 32, 24), new THREE.ShaderMaterial({
    uniforms: visorUniforms, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
    vertexShader: "varying vec3 vN; varying vec3 vV; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0); vV=-mv.xyz; vN=normalize(normalMatrix*normal); gl_Position=projectionMatrix*mv; }",
    fragmentShader: "uniform vec3 color; uniform float power; uniform float intensity; varying vec3 vN; varying vec3 vV; void main(){ float f = pow(1.0-max(dot(normalize(vV),normalize(vN)),0.0), power); gl_FragColor = vec4(color, f*intensity); }",
  })));

  // eyes
  const eyeMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(1.9, 2.0, 2.2), toneMapped: false });
  const heartMat = new THREE.MeshBasicMaterial({ color: "#ff4f7b", toneMapped: false });
  const [top, bottom] = eyePaths(), heartGeo = new THREE.TubeGeometry(new HeartCurve(), 64, 0.0035, 8, true);
  const eyeRow = new THREE.Group(); eyeRow.position.set(0, -0.02, 0.29); head.add(eyeRow);
  const eyes = [-0.07, 0.07].map((x, i) => {
    const g = new THREE.Group(); g.position.x = x; g.rotation.y = i ? 0.2 : -0.2; eyeRow.add(g);
    const normal = new THREE.Group(); g.add(normal);
    normal.add(new THREE.Mesh(new THREE.TubeGeometry(top, 20, 0.0035, 8, false), eyeMat));
    normal.add(new THREE.Mesh(new THREE.TubeGeometry(bottom, 20, 0.0035, 8, false), eyeMat));
    const heart = new THREE.Mesh(heartGeo, heartMat); heart.visible = false; g.add(heart);
    return { g, normal, heart };
  });

  // ears with antennas
  const mat = (c, r, m = 0, extra = {}) => new THREE.MeshStandardMaterial({ color: c, roughness: r, metalness: m, ...extra });
  const tipMat = new THREE.MeshBasicMaterial({ color: BLUE.clone(), toneMapped: false });
  const tips = [];
  [[-0.29, -1], [0.29, 1]].forEach(([x, dir]) => {
    const ear = new THREE.Group(); ear.position.set(x, 0, 0); ear.scale.setScalar(1.3); head.add(ear);
    const a = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.025, 32), mat("#eef1f6", 0.5)); a.rotation.z = Math.PI / 2; ear.add(a);
    const b = new THREE.Mesh(new THREE.TorusGeometry(0.032, 0.008, 16, 32), mat("#ffffff", 0.3)); b.position.x = dir * 0.012; b.rotation.y = Math.PI / 2; ear.add(b);
    const c = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.005, 32), mat("#c9ced8", 0.8)); c.position.x = dir * 0.012; c.rotation.z = Math.PI / 2; ear.add(c);
    const ant = new THREE.Group(); ant.position.set(dir * 0.015, 0.035, 0); ant.rotation.x = -0.4; ear.add(ant);
    const s1 = new THREE.Mesh(new THREE.CylinderGeometry(0.006, 0.008, 0.02, 16), mat("#8a93a6", 0.4, 0.5)); s1.position.y = 0.01; ant.add(s1);
    const s2 = new THREE.Mesh(new THREE.CylinderGeometry(0.003, 0.003, 0.1, 8), mat("#cfd5df", 0.4, 0.2)); s2.position.y = 0.06; ant.add(s2);
    const tip = new THREE.Mesh(new THREE.SphereGeometry(0.007, 16, 16), tipMat); tip.position.y = 0.11; ant.add(tip); tips.push(tip);
  });

  // ---------------------------------------------------------------- behaviour
  let state = "idle", level = 0, smooth = 0, heartUntil = 0;
  const pointer = { x: 0, y: 0 };
  const onMove = (e) => {
    const r = root.getBoundingClientRect();
    pointer.x = THREE.MathUtils.clamp(((e.clientX - r.left) / r.width) * 2 - 1, -1, 1);
    pointer.y = THREE.MathUtils.clamp(-(((e.clientY - r.top) / r.height) * 2 - 1), -1, 1);
  };
  addEventListener("pointermove", onMove);
  const onTap = () => { heartUntil = performance.now() + 2000; };
  renderer.domElement.addEventListener("pointerdown", onTap);

  function resize() {
    const w = root.clientWidth || 300, h = root.clientHeight || 200;
    renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize); ro.observe(root); resize();
  let visible = true;
  const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; }); io.observe(root);

  const clock = new THREE.Clock(); let running = true, lastDraw = 0;
  const lerp = THREE.MathUtils.lerp;
  function frame() {
    if (!running) return;
    requestAnimationFrame(frame);
    if (!visible || document.hidden) { clock.getDelta(); return; }
    const now = performance.now();
    if (state === "idle" && now - lastDraw < 33) return;      // idle: ~30 fps is plenty
    lastDraw = now;
    const dt = Math.min(clock.getDelta(), 0.1), t = clock.elapsedTime;
    smooth = lerp(smooth, level, Math.min(1, dt * 14));

    // body follows the pointer (as in the original), head looks a bit more
    const tx = reduce ? 0 : pointer.x, ty = reduce ? 0 : pointer.y;
    body.rotation.y = lerp(body.rotation.y, tx * 0.22, dt * 6);
    body.rotation.x = lerp(body.rotation.x, -ty * 0.12, dt * 6);
    let hy = tx * 0.38, hx = -ty * 0.18, hz = 0;     // turns toward you, but the face always stays visible
    if (state === "thinking") { hy = Math.sin(t * 2.2) * 0.45; hz = 0.18; hx = -0.08; }
    if (state === "listening") { hz = Math.sin(t * 1.6) * 0.05; hx -= 0.06; }
    head.rotation.y = lerp(head.rotation.y, hy, dt * 8);
    head.rotation.x = lerp(head.rotation.x, hx, dt * 8);
    head.rotation.z = lerp(head.rotation.z, hz, dt * 6);
    body.position.y = -0.3 + (reduce ? 0 : Math.sin(t * 1.8) * 0.012);

    // eyes: blink, squint, talk, hearts
    const hearts = performance.now() < heartUntil;
    const cycle = t % 3.2;
    let sy = 1;
    if (!hearts && cycle < 0.18) sy = Math.max(0.05, 1 - Math.sin((cycle / 0.18) * Math.PI));
    if (state === "thinking") sy *= 0.45;
    if (state === "listening") sy *= 1.15;
    if (state === "speaking") sy *= 0.7 + smooth * 0.7;
    eyes.forEach((e, i) => {
      e.normal.visible = !hearts; e.heart.visible = hearts;
      e.g.scale.set(1.1, 1.1 * sy, 1.1);
      e.g.position.x = (i ? 0.07 : -0.07) + (state === "thinking" ? Math.sin(t * 2.2) * 0.012 : 0);
    });

    // visor, antennas, chest light
    const want = { idle: 0.9, listening: 1.6 + Math.sin(t * 6) * 0.35, thinking: 1.1 + Math.sin(t * 9) * 0.25,
                   speaking: 1.0 + smooth * 1.4, happy: 1.4 }[state] ?? 0.9;
    visorUniforms.intensity.value = lerp(visorUniforms.intensity.value, want, dt * 8);
    const glow = state === "listening" ? 0.6 + Math.sin(t * 6) * 0.4 : state === "speaking" ? 0.4 + smooth : state === "thinking" ? 0.5 + Math.sin(t * 9) * 0.3 : 0.15;
    tips.forEach((tp) => tp.scale.setScalar(1 + glow * 0.9));
    chestMat.opacity = lerp(chestMat.opacity, state === "idle" ? 0.25 : 0.9, dt * 6);
    chest.scale.setScalar(1 + (state === "speaking" ? smooth * 0.5 : state === "listening" ? Math.sin(t * 6) * 0.12 : 0));
    renderer.render(scene, camera);
  }
  frame();

  return {
    setState(s) { state = s; },
    setLevel(v) { level = Math.max(0, Math.min(1, v || 0)); },
    happy(ms = 2000) { heartUntil = performance.now() + ms; },
    dispose() { running = false; ro.disconnect(); io.disconnect(); removeEventListener("pointermove", onMove); renderer.dispose(); renderer.domElement.remove(); },
  };
}
