/* The hero's dot field, in WebGL2. Loaded as its own chunk by PublicHome.
 *
 * A halftone of the market at rest: a slow terrain of dots, raised where the
 * odds are busy. When the page opens, one ring travels out from the sizer's
 * corner (a trade) and a smaller one follows it (the copy), then the field is
 * still. The pointer lifts the dots near it, and scrolling the hero away moves
 * the field a little slower than the page.
 *
 * Nothing loops. A frame is drawn only while the entrance plays or the pointer
 * eases, and on a scroll step; off screen nothing is drawn at all. The colour
 * is the --green token, read from the stylesheet, and the field fades out
 * behind the headline so the type never sits on dots.
 */

const VERT = `#version 300 es
in vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }`

const FRAG = `#version 300 es
precision highp float;
uniform vec2 uRes;
uniform float uDpr;
uniform float uTime;
uniform vec2 uOrigin;
uniform vec2 uPointer;
uniform float uPointerAmt;
uniform float uScroll;
uniform vec3 uInk;
out vec4 o;

float hash(vec2 p) {
  p = fract(p * vec2(234.34, 435.345));
  p += dot(p, p + 34.23);
  return fract(p.x * p.y);
}
float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
             mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
float ring(float d, float r, float w) { return exp(-pow((d - r) / w, 2.0)); }

void main() {
  float cell = 18.0 * uDpr;
  float drift = uScroll * 90.0 * uDpr;
  vec2 frag = gl_FragCoord.xy + vec2(0.0, drift);
  vec2 id = floor(frag / cell);
  vec2 center = (id + 0.5) * cell;
  vec2 uv = (center - vec2(0.0, drift)) / uRes;

  // The market at rest: a slow terrain, barely raised.
  float terrain = noise(id * 0.09) * 0.65 + noise(id * 0.023 + 11.0) * 0.35;
  float v = smoothstep(0.45, 0.95, terrain) * 0.28;

  // A trade, then the copy: two rings from one point, the second later and
  // smaller, both fading as they travel.
  float d = distance(center, uOrigin) / uDpr;
  float t1 = uTime;
  float t2 = uTime - 0.9;
  if (t1 > 0.0) v += ring(d, t1 * 460.0, 64.0) * exp(-t1 * 0.85);
  if (t2 > 0.0) v += ring(d, t2 * 460.0, 48.0) * exp(-t2 * 1.0) * 0.55;

  // The pointer lifts the dots near it.
  float pd = distance(center, uPointer) / uDpr;
  v += exp(-pow(pd / 110.0, 2.0)) * 0.65 * uPointerAmt;

  // Quiet behind the headline: the field lives on the right and thins toward
  // the copy and toward the top and bottom edges.
  float mask = smoothstep(0.32, 0.66, uv.x) * smoothstep(0.02, 0.22, uv.y) * (1.0 - smoothstep(0.86, 1.0, uv.y));
  v = clamp(v, 0.0, 1.0) * mask;

  float r = (0.7 + v * 4.6) * uDpr;
  float a = (1.0 - smoothstep(r - 0.8 * uDpr, r + 0.8 * uDpr, length(frag - center))) * (0.16 + 0.6 * v) * mask;
  o = vec4(uInk * a, a);
}`

function compile(gl, type, source) {
  const shader = gl.createShader(type)
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  return gl.getShaderParameter(shader, gl.COMPILE_STATUS) ? shader : null
}

function link(gl) {
  const vs = compile(gl, gl.VERTEX_SHADER, VERT)
  const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG)
  if (!vs || !fs) return null
  const program = gl.createProgram()
  gl.attachShader(program, vs)
  gl.attachShader(program, fs)
  gl.linkProgram(program)
  return gl.getProgramParameter(program, gl.LINK_STATUS) ? program : null
}

/** The --green token as RGB floats; the stylesheet stays the source. */
function ink(node) {
  const hex = getComputedStyle(node).getPropertyValue('--green').trim().replace('#', '')
  if (!/^[0-9a-f]{6}$/i.test(hex)) return [0.043, 0.62, 0.388]
  return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
}

/**
 * @param {HTMLCanvasElement} canvas  sized by CSS to cover the hero stage
 * @param {{ still?: boolean }} options  still: one settled frame, no motion
 * @returns {() => void} teardown
 */
export function mountHeroField(canvas, { still = false } = {}) {
  const gl = canvas.getContext('webgl2', {
    alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false, powerPreference: 'low-power',
  })
  if (!gl) return () => {}
  const program = link(gl)
  if (!program) return () => {}

  gl.useProgram(program)
  const buffer = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
  const aPos = gl.getAttribLocation(program, 'aPos')
  gl.enableVertexAttribArray(aPos)
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0)
  const u = Object.fromEntries(['uRes', 'uDpr', 'uTime', 'uOrigin', 'uPointer', 'uPointerAmt', 'uScroll', 'uInk']
    .map((name) => [name, gl.getUniformLocation(program, name)]))
  const colour = ink(canvas)

  const stage = canvas.parentElement
  const state = { time: still ? 8 : -0.45, pointer: [-1e5, -1e5], amt: 0, amtTarget: 0, scroll: 0 }
  let width = 0
  let height = 0
  let dpr = 1
  let raf = 0
  let last = 0
  let playing = !still
  let visible = true
  let lost = false
  let shown = false

  // The rings start at the sizer's top-right corner, where they travel through
  // open paper instead of behind the opaque panel; without one, right of centre.
  let origin = [0, 0]
  function placeOrigin() {
    const anchor = stage.querySelector('.sizer')
    const box = canvas.getBoundingClientRect()
    if (!anchor || !box.width) { origin = [width * 0.72 * dpr, height * 0.5 * dpr]; return }
    const a = anchor.getBoundingClientRect()
    origin = [(a.right - box.left) * dpr, (box.bottom - a.top) * dpr]
  }

  function draw() {
    gl.uniform2f(u.uRes, canvas.width, canvas.height)
    gl.uniform1f(u.uDpr, dpr)
    gl.uniform1f(u.uTime, state.time)
    gl.uniform2f(u.uOrigin, origin[0], origin[1])
    gl.uniform2f(u.uPointer, state.pointer[0], state.pointer[1])
    gl.uniform1f(u.uPointerAmt, state.amt)
    gl.uniform1f(u.uScroll, state.scroll)
    gl.uniform3f(u.uInk, colour[0], colour[1], colour[2])
    gl.drawArrays(gl.TRIANGLES, 0, 3)
    if (!shown) { shown = true; canvas.classList.add('is-on') }
  }

  function frame(now) {
    raf = 0
    if (lost || !visible || !width) return
    const dt = last ? Math.min(0.05, (now - last) / 1000) : 1 / 60
    last = now
    let busy = false
    if (playing) {
      state.time += dt
      if (state.time >= 6) playing = false
      else busy = true
    }
    const gap = state.amtTarget - state.amt
    if (Math.abs(gap) > 0.003) { state.amt += gap * Math.min(1, dt * 6); busy = true } else state.amt = state.amtTarget
    draw()
    if (busy) raf = requestAnimationFrame(frame)
    else last = 0
  }

  const kick = () => { if (!raf && visible && !lost) raf = requestAnimationFrame(frame) }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 1.5)
    width = canvas.clientWidth
    height = canvas.clientHeight
    canvas.width = Math.max(1, Math.round(width * dpr))
    canvas.height = Math.max(1, Math.round(height * dpr))
    gl.viewport(0, 0, canvas.width, canvas.height)
    placeOrigin()
    kick()
  }

  const onScroll = () => {
    const rect = stage.getBoundingClientRect()
    const next = Math.min(1, Math.max(0, -rect.top / Math.max(1, rect.height)))
    if (Math.abs(next - state.scroll) < 0.0005) return
    state.scroll = next
    kick()
  }
  const onPointer = (event) => {
    const rect = canvas.getBoundingClientRect()
    state.pointer = [(event.clientX - rect.left) * dpr, (rect.bottom - event.clientY) * dpr]
    state.amtTarget = 1
    kick()
  }
  const onLeave = () => { state.amtTarget = 0; kick() }
  const onLost = (event) => { event.preventDefault(); lost = true; canvas.classList.remove('is-on') }

  const sizer = new ResizeObserver(resize)
  sizer.observe(canvas)
  const seen = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting
    if (visible) kick()
  })
  seen.observe(canvas)
  window.addEventListener('scroll', onScroll, { passive: true })
  if (!still) {
    stage.addEventListener('pointermove', onPointer)
    stage.addEventListener('pointerleave', onLeave)
  }
  canvas.addEventListener('webglcontextlost', onLost)
  resize()

  return () => {
    cancelAnimationFrame(raf)
    sizer.disconnect()
    seen.disconnect()
    window.removeEventListener('scroll', onScroll)
    stage.removeEventListener('pointermove', onPointer)
    stage.removeEventListener('pointerleave', onLeave)
    canvas.removeEventListener('webglcontextlost', onLost)
    gl.getExtension('WEBGL_lose_context')?.loseContext()
  }
}
