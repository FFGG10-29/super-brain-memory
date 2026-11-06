/* v3 星云：纯 WebGL 点精灵 + 加色混合 + 三层视差星尘 + 前端光脉冲插值。
   无外部依赖；数据来自 /v1/graph 快照 + /ws 增量事件。 */
(() => {
  const canvas = document.getElementById('sky');
  const gl = canvas.getContext('webgl', { alpha: false, antialias: true });
  const statusEl = document.getElementById('status');
  const countsEl = document.getElementById('counts');
  if (!gl) { statusEl.textContent = 'WebGL 不可用'; return; }

  const VS = `
    attribute vec2 a_pos; attribute float a_size; attribute vec4 a_color;
    uniform vec2 u_res; uniform vec2 u_offset; uniform float u_scale; uniform float u_rot;
    varying vec4 v_color;
    void main() {
      float c = cos(u_rot), s = sin(u_rot);
      vec2 p = vec2(a_pos.x * c - a_pos.y * s, a_pos.x * s + a_pos.y * c);
      vec2 clip = ((p * u_scale + u_offset) / u_res) * 2.0 - 1.0;
      gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
      gl_PointSize = a_size * u_scale;
      v_color = a_color;
    }`;
  const FS = `
    precision mediump float; varying vec4 v_color;
    void main() {
      vec2 d = gl_PointCoord - vec2(0.5);
      float r = length(d) * 2.0;
      float glow = pow(max(0.0, 1.0 - r), 2.4);
      float core = smoothstep(0.32, 0.0, r);
      gl_FragColor = vec4(v_color.rgb, (glow * 0.55 + core) * v_color.a);
    }`;
  const LINE_VS = `attribute vec2 a_pos; uniform vec2 u_res; uniform vec2 u_offset;
    uniform float u_scale; uniform float u_rot;
    void main(){ float c=cos(u_rot), s=sin(u_rot);
      vec2 p=vec2(a_pos.x*c-a_pos.y*s, a_pos.x*s+a_pos.y*c);
      vec2 clip=((p*u_scale+u_offset)/u_res)*2.0-1.0;
      gl_Position=vec4(clip.x,-clip.y,0.0,1.0); }`;
  const LINE_FS = `precision mediump float; uniform vec4 u_color;
    void main(){ gl_FragColor=u_color; }`;

  function compile(type, src) {
    const sh = gl.createShader(type); gl.shaderSource(sh, src); gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(sh));
    return sh;
  }
  function program(vs, fs) {
    const p = gl.createProgram();
    gl.attachShader(p, compile(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, compile(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p); return p;
  }
  const prog = program(VS, FS), lineProg = program(LINE_VS, LINE_FS);
  const loc = (p, n) => gl.getAttribLocation(p, n);
  const uni = (p, n) => gl.getUniformLocation(p, n);

  function hash(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) / 4294967296;
  }

  let nodes = [], edges = [], pulses = [], stardust = [];
  const CX = 500, CY = 400;
  const TYPE_ANGLE = { fact: 0.3, emotion: 1.7, scenario: 2.8, profile: 3.8, code: 4.9, hit: 5.6 };

  function position(node) {
    const h = hash(node.id || node.content || 'x');
    const base = TYPE_ANGLE[node.memory_type] ?? 0.9;
    const angle = base + (h - 0.5) * 1.5;              // 同类型聚成星团/旋臂
    const radius = 90 + (hash((node.id || '') + 'r') * 2 % 1) * 310;
    return [CX + Math.cos(angle) * radius, CY + Math.sin(angle) * radius];
  }

  function nodeColor(n) {
    if (n.is_emotion || n.memory_type === 'emotion') return [1.0, 0.24, 0.42, 1.0];
    if (n.memory_type === 'profile') return [1.0, 0.78, 0.35, 1.0];
    return [0.25, 0.85, 1.0, 1.0];
  }

  function rebuildStardust(seed) {
    stardust = [];
    for (let i = 0; i < 900; i++) {
      const a = hash('a' + i + seed), b = hash('b' + i + seed), d = hash('d' + i + seed);
      stardust.push({ x: a * 1400 - 200, y: b * 1100 - 150, size: 1 + d * 2.3,
        alpha: 0.12 + d * 0.5, layer: i % 3 });
    }
  }

  function emitPulse(targetId, similarity) {
    const target = nodes.find(n => n.id === targetId);
    if (!target) return;
    pulses.push({ from: [CX, CY], to: target.pos, t: 0, speed: 0.012 + (similarity || 0.5) * 0.022,
      color: [0.75, 0.95, 1.0, 1.0] });
  }

  let loaded = false;
  function applyGraph(data) {
    nodes = (data.nodes || []).map(n => ({ ...n, pos: position(n), color: nodeColor(n),
      radius: (n.is_emotion ? 22 : 14) }));
    edges = (data.edges || []);
    countsEl.textContent = `节点 ${nodes.length} · 突触 ${edges.length}`;
    loaded = true;
  }
  fetch('/v1/graph').then(r => r.json()).then(applyGraph).catch(() => { statusEl.textContent = '快照失败'; });

  // WS 增量：节点新增 / 脉冲事件（光点插值完全在前端）
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.hostname}:8767/ws`);
    ws.onopen = () => { statusEl.textContent = 'WS 已连接'; };
    ws.onclose = () => { statusEl.textContent = 'WS 断开，重连中…'; setTimeout(connect, 1500); };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'node_created' || msg.type === 'node_rejected') {
        const n = { id: msg.id, content: msg.content, memory_type: msg.memory_type,
          tier: msg.tier, pad: msg.pad, is_emotion: msg.memory_type === 'emotion' };
        nodes.push({ ...n, pos: position(n), color: nodeColor(n), radius: 15 });
        emitPulse(msg.id, 0.8);
        countsEl.textContent = `节点 ${nodes.length} · 突触 ${edges.length}`;
      } else if (msg.type === 'pulse') {
        emitPulse(msg.target, msg.similarity || 0.6);
      }
    };
  }
  connect();

  // 交互
  let offset = [0, 0], scale = 1, rot = 0, dragging = false, last = null;
  canvas.addEventListener('mousedown', e => { dragging = true; last = [e.clientX, e.clientY]; });
  window.addEventListener('mouseup', () => { dragging = false; });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    offset[0] += e.clientX - last[0]; offset[1] += e.clientY - last[1]; last = [e.clientX, e.clientY];
  });
  canvas.addEventListener('wheel', e => { scale = Math.max(0.45, Math.min(3, scale * (e.deltaY > 0 ? 0.92 : 1.08))); });

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = innerWidth * dpr; canvas.height = innerHeight * dpr;
    gl.viewport(0, 0, canvas.width, canvas.height);
    offset = [innerWidth * dpr / 2 - CX * scale, innerHeight * dpr / 2 - CY * scale];
  }
  window.addEventListener('resize', resize); resize(); rebuildStardust('v3');

  function buffer(data) {
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.DYNAMIC_DRAW); return b;
  }
  function drawPoints(points, program, res) {
    gl.useProgram(program);
    const b = buffer(points.flat());
    const stride = 7 * 4;
    const aPos = loc(program, 'a_pos'), aSize = loc(program, 'a_size'), aColor = loc(program, 'a_color');
    gl.enableVertexAttribArray(aPos); gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(aSize); gl.vertexAttribPointer(aSize, 1, gl.FLOAT, false, stride, 8);
    gl.enableVertexAttribArray(aColor); gl.vertexAttribPointer(aColor, 4, gl.FLOAT, false, stride, 12);
    gl.uniform2f(uni(program, 'u_res'), res[0], res[1]);
    gl.uniform2f(uni(program, 'u_offset'), offset[0], offset[1]);
    gl.uniform1f(uni(program, 'u_scale'), scale);
    gl.uniform1f(uni(program, 'u_rot'), rot);
    gl.drawArrays(gl.POINTS, 0, points.length);
    gl.deleteBuffer(b);
  }

  function frame() {
    const res = [canvas.width, canvas.height];
    gl.clearColor(0.012, 0.02, 0.043, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE);   // 加色：银河辉光

    rot += 0.00018;
    // 三层视差星尘
    const dust = stardust.map(d => {
      const parallax = 1 + d.layer * 0.08;
      return [d.x * parallax, d.y * parallax, d.size * (1 + d.layer * 0.35),
        0.35, 0.55, 0.95, d.alpha];
    });
    drawPoints(dust, prog, res);

    // 突触连线
    const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
    const lineData = [];
    for (const e of edges) {
      const a = byId[e.source], b2 = byId[e.target];
      if (a && b2) lineData.push(a.pos[0], a.pos[1], b2.pos[0], b2.pos[1]);
    }
    if (lineData.length) {
      gl.useProgram(lineProg);
      const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(lineData), gl.DYNAMIC_DRAW);
      const aPos = loc(lineProg, 'a_pos');
      gl.enableVertexAttribArray(aPos); gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
      gl.uniform2f(uni(lineProg, 'u_res'), res[0], res[1]);
      gl.uniform2f(uni(lineProg, 'u_offset'), offset[0], offset[1]);
      gl.uniform1f(uni(lineProg, 'u_scale'), scale);
      gl.uniform1f(uni(lineProg, 'u_rot'), rot);
      gl.uniform4f(uni(lineProg, 'u_color'), 0.28, 0.66, 1.0, 0.22);
      gl.drawArrays(gl.LINES, 0, lineData.length / 2);
      gl.deleteBuffer(b);
    }

    // 节点：外层光晕 + 内核（两次绘制模拟 bloom）
    const halo = nodes.flatMap(n => [...n.pos, n.radius * 3.2, n.color[0], n.color[1], n.color[2], 0.16]);
    const core = nodes.flatMap(n => [...n.pos, n.radius, n.color[0], n.color[1], n.color[2], 0.95]);
    drawPoints(chunk(halo, 7), prog, res);
    drawPoints(chunk(core, 7), prog, res);

    // 光脉冲：前端 Lerp，抵达终点闪烁
    const live = [];
    const pulsePoints = [];
    for (const p of pulses) {
      p.t += p.speed;
      if (p.t >= 1) continue;
      const x = p.from[0] + (p.to[0] - p.from[0]) * p.t;
      const y = p.from[1] + (p.to[1] - p.from[1]) * p.t;
      pulsePoints.push([x, y, 26, 1, 1, 1, 0.28]);
      pulsePoints.push([x, y, 10, 0.6, 1, 1, 1]);
      live.push(p);
    }
    pulses = live;
    if (pulsePoints.length) drawPoints(pulsePoints, prog, res);

    requestAnimationFrame(frame);
  }
  function chunk(arr, n) { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out; }
  frame();
})();
