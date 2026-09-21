"use strict";

/**
 * 参数目录与曲线表现层：只管理浏览器中的草稿、校验和绘图，不请求 API、不写入工程。
 * 参数能力来自已读取的真实选区；声线名称不预设，原生音高与音高偏移分别处理。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const format = (value) => finite(value) ? String(Number(value.toFixed(3))) : "未提供";
  const LEGACY = Object.freeze({
    breathiness: { label: "气声", unit: "参数值", maxDelta: .2, value: .05 },
    tension: { label: "张力", unit: "参数值", maxDelta: .2, value: .05 },
    loudness: { label: "响度", unit: "dB", maxDelta: 6, value: 1 },
    gender: { label: "性别", unit: "参数值", maxDelta: .2, value: .05 },
    pitchDelta: { label: "音高偏移", unit: "音分", maxDelta: 100, value: 10 },
  });
  const ADDED = Object.freeze({ toneShift: { label: "音色偏移", unit: "音分", maxDelta: 200, value: 0 },
    vibratoEnv: { label: "颤音包络", unit: "参数值", maxDelta: .3, value: 0 },
    pitchCurve: { label: "原生音高曲线", unit: "MIDI 半音", kind: "pitch", value: 60 } });
  const representationNames = { "automation-simplified": "精简自动化曲线", "automation-points": "密集自动化控制点", "native-pitch-curve": "原生音高曲线" };
  const previewData = new WeakMap();
  const node = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  };

  /** 原生音高的安全范围以真实音符及组移调计算；缺少任一音高信息时不猜测。 */
  function nativeRange(selection) {
    const notes = selection?.notes || selection?.selectedNotes;
    if (!Array.isArray(notes) || !notes.length || !finite(selection?.groupPitchOffset) || notes.some((note) => !finite(note.pitch))) return null;
    const pitches = notes.map((note) => note.pitch + selection.groupPitchOffset);
    const low = Math.max(0, Math.min(...pitches) - 2), high = Math.min(127, Math.max(...pitches) + 2);
    return low < high ? { low, high, initial: clamp(pitches[0], low, high) } : null;
  }

  /** 只复制需要显示的公开字段，不把声库指纹或其他内部标识放进目录和 DOM。 */
  function catalog(selection) {
    const source = selection?.parameters || {}, enhanced = selection?.capabilities?.curves === true;
    const pitchRange = nativeRange(selection);
    const entries = new Map();
    for (const [id, fallback] of Object.entries({ ...LEGACY, ...ADDED })) {
      const supplied = source[id] && typeof source[id] === "object" ? source[id] : {};
      const pitch = id === "pitchCurve";
      entries.set(id, { id, label: supplied.label || fallback.label, unit: supplied.unit || fallback.unit,
        kind: pitch ? "pitch" : "automation", maxDelta: finite(supplied.maxDelta) && supplied.maxDelta > 0 ? supplied.maxDelta : fallback.maxDelta,
        value: fallback.value, range: Array.isArray(supplied.range) ? supplied.range : null,
        available: supplied.available !== false && (Object.hasOwn(LEGACY, id) || (enhanced && Object.hasOwn(source, id))) && (!pitch || (selection?.capabilities?.nativePitch === true && Boolean(pitchRange))),
        nativeRange: pitch ? pitchRange : null });
    }
    for (const [id, supplied] of Object.entries(source)) {
      if (!id.startsWith("vocalMode_") || !supplied || typeof supplied !== "object") continue;
      const modeName = typeof supplied.modeName === "string" ? supplied.modeName : id.slice("vocalMode_".length);
      entries.set(id, { id, label: supplied.label || modeName, modeName, unit: supplied.unit || "百分点", kind: "vocalMode",
        source: supplied.source === "user" ? "user" : "host",
        maxDelta: finite(supplied.maxDelta) && supplied.maxDelta > 0 ? supplied.maxDelta : 30,
        value: 0, range: Array.isArray(supplied.range) ? supplied.range : null, available: enhanced && supplied.available !== false });
    }
    return entries;
  }

  /** 数值点是明确的规范化位置，严格检查端点、递增顺序和边界，不默默修正键盘输入。 */
  function parsePoints(text, range) {
    const rows = text.trim().split(/\r?\n/).filter((row) => row.trim());
    if (rows.length < 2 || rows.length > 64) throw new Error("曲线需要 2–64 个点，每行一个“位置比例 值”。");
    const points = rows.map((row, index) => {
      const parts = row.trim().split(/[\s,，]+/);
      if (parts.length !== 2 || parts.some((part) => !Number.isFinite(Number(part)))) throw new Error(`第 ${index + 1} 行需要两个数值：位置比例和参数值。`);
      return parts.map(Number);
    });
    if (points[0][0] !== 0 || points.at(-1)[0] !== 1) throw new Error("第一个点的位置必须为 0，最后一个必须为 1。");
    for (let index = 0; index < points.length; index++) {
      const [position, value] = points[index];
      if (position < 0 || position > 1 || (index && position <= points[index - 1][0])) throw new Error("位置必须在 0–1 之间，并按严格递增顺序填写。");
      if (value < range[0] || value > range[1]) throw new Error(`曲线值必须在 ${format(range[0])}–${format(range[1])} 之间。`);
    }
    return points;
  }

  /** 使用当前主题绘制坐标和系列；null 表示未知基线，必须断开，不能画成零值。 */
  function plot(canvas, series, range, unit) {
    const width = Math.round(canvas.getBoundingClientRect().width);
    if (!width) return;
    const height = 180, scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext("2d"); ctx.scale(scale, scale);
    const css = getComputedStyle(document.documentElement);
    const color = (name) => css.getPropertyValue(name).trim();
    const bounds = { left: 42, right: width - 12, top: 18, bottom: height - 28 };
    const span = range[1] - range[0] || 1;
    const x = (position) => bounds.left + position * (bounds.right - bounds.left);
    const y = (value) => bounds.bottom - (value - range[0]) / span * (bounds.bottom - bounds.top);
    ctx.clearRect(0, 0, width, height); ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let index = 0; index <= 4; index++) {
      const value = range[0] + span * index / 4, at = y(value);
      ctx.strokeStyle = color("--border"); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(bounds.left, at); ctx.lineTo(bounds.right, at); ctx.stroke();
      ctx.fillStyle = color("--subtle"); ctx.fillText(format(value), bounds.left - 6, at);
    }
    ctx.textBaseline = "top"; ctx.textAlign = "left"; ctx.fillText("0%", bounds.left, bounds.bottom + 8);
    ctx.textAlign = "right"; ctx.fillText("100%", bounds.right, bounds.bottom + 8);
    ctx.textAlign = "left"; ctx.fillText(unit || "参数值", bounds.left, 2);
    for (const line of series) {
      ctx.strokeStyle = color(line.before ? "--waveform-b" : "--waveform-a"); ctx.lineWidth = line.before ? 1.5 : 2;
      ctx.setLineDash(line.before ? [4, 4] : []); ctx.beginPath(); let begun = false;
      for (const [position, value] of line.points) {
        if (!finite(value)) { begun = false; continue; }
        if (!begun) { ctx.moveTo(x(position), y(value)); begun = true; } else ctx.lineTo(x(position), y(value));
      }
      ctx.stroke(); ctx.setLineDash([]);
    }
    return bounds;
  }

  function representation(value, renderMode) {
    return representationNames[value] || (value ? String(value) : renderMode === "points" ? "密集控制点" : renderMode === "smooth" ? "精简曲线" : "旧版预览");
  }

  /** 预览图只接受宿主实际返回的曲线采样，不以草稿或模型建议冒充应用效果。 */
  function createPreview(preview) {
    if (!Array.isArray(preview?.curvePreview)) return null;
    const points = preview.curvePreview.filter((item) => finite(item?.position) && item.position >= 0 && item.position <= 1 && finite(item.after));
    if (points.length < 2) return null;
    const figure = node("figure", "curve-preview-figure"), canvas = node("canvas", "curve-canvas curve-preview-canvas");
    const hasBefore = points.some((item) => finite(item.before));
    const values = points.flatMap((item) => finite(item.before) ? [item.before, item.after] : [item.after]);
    let low = Math.min(...values), high = Math.max(...values);
    const padding = (high - low) * .08 || .1; low -= padding; high += padding;
    const unit = preview.unit || "参数值";
    canvas.dataset.curvePreview = "true"; canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `宿主曲线预览，${hasBefore ? "虚线为调整前，实线为调整后" : "仅展示调整后，原曲线未提供"}。调整后起点 ${format(points[0].after)}，终点 ${format(points.at(-1).after)} ${unit}。完整数值见预览明细。`);
    previewData.set(canvas, { points, range: [low, high], unit });
    // 同一预览也会留在已应用的历史建议卡中，不用“尚未写入”覆盖真实操作状态。
    figure.append(canvas, node("figcaption", "field-help", hasBefore ? "宿主预览快照：虚线为调整前，实线为调整后。应用状态见操作提示。" : "宿主预览快照：实线为调整后。未提供原曲线，未绘制基线。应用状态见操作提示。"));
    requestAnimationFrame(() => drawPreview(canvas));
    return figure;
  }
  function drawPreview(canvas) {
    const data = previewData.get(canvas); if (!data || !canvas.isConnected) return;
    plot(canvas, [{ before: true, points: data.points.map((point) => [point.position, point.before]) }, { points: data.points.map((point) => [point.position, point.after]) }], data.range, data.unit);
  }

  function createEditor({ onChange, onValidityChange } = {}) {
    const state = { selection: null, entries: catalog(null), selected: "breathiness", points: [[0, .05], [1, .05]],
      busy: false, connected: false, error: "", stroke: null };
    const definition = () => state.entries.get(state.selected);
    const enhanced = () => state.selection?.capabilities?.curves === true;
    const usingCurve = () => $("parameter-method").value === "curve";
    const valueRange = () => definition()?.kind === "pitch" ? [definition().nativeRange?.low ?? 0, definition().nativeRange?.high ?? 127] : [-definition().maxDelta, definition().maxDelta];
    const initialValue = () => definition()?.kind === "pitch" ? definition().nativeRange?.initial ?? 60 : clamp(Number($("parameter-delta").value) || 0, ...valueRange());
    const writePoints = () => { $("curve-points").value = state.points.map(([position, value]) => `${format(position)} ${format(value)}`).join("\n"); };
    const draw = () => plot($("curve-editor-canvas"), [{ points: state.points }], valueRange(), definition()?.unit);
    function changed() { onChange?.(); onValidityChange?.(); }

    function sync() {
      const entry = definition(), locked = state.busy || !state.connected || !entry?.available;
      $("parameter").disabled = state.busy;
      $("parameter-method").disabled = locked || !enhanced() || entry?.kind === "pitch";
      $("parameter-method").querySelector('option[value="curve"]').disabled = !enhanced();
      $("parameter-render-mode").disabled = locked || !enhanced() || entry?.kind === "pitch";
      $("parameter-render-mode").querySelector('option[value="points"]').disabled = entry?.kind === "pitch";
      $("parameter-delta-field").hidden = usingCurve();
      $("parameter-delta").disabled = locked || usingCurve();
      $("curve-editor").hidden = !usingCurve();
      $("curve-points").disabled = locked; $("curve-reset").disabled = locked;
      $("curve-editor-canvas").setAttribute("aria-disabled", String(locked));
      // 草稿会保留在应用之后；描述编辑行为而非重复声明“未写入”，避免与真实应用结果矛盾。
      $("curve-editor-feedback").textContent = state.error || `草稿包含 ${state.points.length} 个点。编辑仅更新草稿，不会写入工程；应用状态见下方操作提示。`;
      $("curve-editor-feedback").classList.toggle("error", Boolean(state.error));
      $("curve-point-count").textContent = `${state.points.length} / 64 点`;
    }
    function reset(notify = true) {
      if (state.stroke) {
        const pointerId = state.stroke.pointerId; state.stroke = null;
        const canvas = $("curve-editor-canvas"); if (canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId);
      }
      state.points = [[0, initialValue()], [1, initialValue()]]; state.error = ""; writePoints(); sync(); draw();
      if (notify) changed();
    }
    function configureParameter(notify = true) {
      const entry = definition(), range = valueRange();
      const input = $("parameter-delta"); input.min = range[0]; input.max = range[1];
      input.step = entry.unit === "音分" || entry.unit === "百分点" ? 1 : .01; input.value = entry.value || 0;
      $("delta-unit").textContent = entry.unit;
      if (entry.kind === "pitch") { $("parameter-method").value = "curve"; $("parameter-render-mode").value = "smooth"; }
      else if (!enhanced()) { $("parameter-method").value = "delta"; $("parameter-render-mode").value = "smooth"; }
      $("parameter-help").textContent = entry.kind === "pitch"
        ? entry.available ? `原生音高使用绝对 MIDI 半音值，允许 ${format(range[0])}–${format(range[1])}（音符含组移调的范围 ±2 半音）。只支持精简原生曲线；如需自动化控制点请选择音高偏移。` : "原生音高需要新版桥接、有效音符与组移调信息；请更新桥接并重新读取选区。"
        : `${entry.modeName ? entry.source === "user" ? `手动补充名称：${entry.modeName}，请在声库面板核对是否存在。` : `当前声库声线：${entry.modeName}。` : ""}均匀增量与手绘点值均表示相对当前曲线的增量，最多 ±${format(entry.maxDelta)} ${entry.unit}。${entry.id === "pitchDelta" ? "音高偏移是音分自动化参数，不是原生音高曲线。" : ""}`;
      $("curve-value-help").textContent = entry.kind === "pitch" ? "点值为绝对 MIDI 半音。重置草稿为选区第一个音符的音高，不代表当前原生曲线。" : `点值为增量，范围 ${format(range[0])}–${format(range[1])} ${entry.unit}；0 表示不改变。`;
      // 原生音高的示例也使用选区内的有效 MIDI 值，不给出超出允许范围的零值示例。
      $("curve-points-help").textContent = `2–64 点；位置范围 0–1，首尾必须是 0 和 1，位置严格递增。例如“0.5 ${entry.kind === "pitch" ? format(entry.nativeRange?.initial ?? 60) : "0"}”表示选区中点。`;
      reset(false); if (notify) changed();
    }
    function updateCatalog(selection) {
      if (selection === state.selection) return;
      state.selection = selection; state.entries = catalog(selection);
      const previous = state.selected, select = $("parameter"); select.replaceChildren();
      for (const [name, kind] of [["通用参数", "automation"], ["当前声库声线", "vocalMode"], ["原生音高", "pitch"]]) {
        const group = document.createElement("optgroup"); group.label = name;
        for (const entry of state.entries.values()) {
          if (entry.kind !== kind) continue;
          const option = new Option(`${entry.label}${entry.source === "user" ? " · 手动补充" : ""}${entry.available ? "" : " · 不可用"}`, entry.id); option.disabled = !entry.available; group.append(option);
        }
        if (group.children.length) select.append(group);
      }
      state.selected = state.entries.get(previous)?.available ? previous : [...state.entries.values()].find((entry) => entry.available)?.id || "breathiness"; select.value = state.selected;
      $("parameter-capability-help").textContent = enhanced() ? "目录来自已读取选区；手动补充的名称会单独标记。切换声库后请重新读取选区。" : "旧桥接可继续使用原有五项均匀增量。曲线、音色偏移、颤音包络和声线控制需要更新桥接，再读取选区。";
      configureParameter(false);
    }
    $("parameter").addEventListener("change", () => { state.selected = $("parameter").value; configureParameter(); });
    $("parameter-method").addEventListener("change", () => { reset(false); sync(); changed(); });
    $("parameter-render-mode").addEventListener("change", changed);
    $("parameter-delta").addEventListener("input", changed);
    $("curve-reset").addEventListener("click", () => reset());
    $("curve-points").addEventListener("input", () => {
      try { state.points = parsePoints($("curve-points").value, valueRange()); state.error = ""; }
      catch (error) { state.error = error.message; }
      sync(); draw(); changed();
    });

    /** 手绘采样只修改草稿：以 64 个固定位置合并笔迹，始终保留 0/1 端点。 */
    const canvas = $("curve-editor-canvas");
    function pointerPoint(event) {
      const rect = canvas.getBoundingClientRect(), [low, high] = valueRange();
      const position = clamp((event.clientX - rect.left - 42) / Math.max(1, rect.width - 54), 0, 1);
      const value = high - clamp((event.clientY - rect.top - 18) / 134, 0, 1) * (high - low);
      return [Math.round(position * 63) / 63, Number(value.toFixed(3))];
    }
    function paint(event) {
      const point = pointerPoint(event), previous = state.stroke.previous;
      // 快速拖动时填充跨过的采样格，防止采样稀疏使笔迹连接到旧草稿的尖峰。
      if (previous) {
        const from = Math.round(previous[0] * 63), to = Math.round(point[0] * 63), step = to >= from ? 1 : -1;
        for (let index = from; index !== to; index += step) {
          const ratio = from === to ? 0 : (index - from) / (to - from);
          state.stroke.samples.set(index, [index / 63, Number((previous[1] + (point[1] - previous[1]) * ratio).toFixed(3))]);
        }
      }
      state.stroke.samples.set(Math.round(point[0] * 63), point); state.stroke.previous = point;
      state.points = [...state.stroke.samples.values()].sort((a, b) => a[0] - b[0]);
      state.error = ""; writePoints(); sync(); draw(); changed();
    }
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || !event.isPrimary || state.busy || !state.connected || !definition()?.available || !usingCurve()) return;
      event.preventDefault(); canvas.setPointerCapture(event.pointerId);
      const samples = new Map(state.points.map(([position, value]) => [Math.round(position * 63), [Math.round(position * 63) / 63, value]]));
      state.stroke = { pointerId: event.pointerId, samples, original: state.points.map((point) => [...point]), previous: null }; paint(event);
    });
    canvas.addEventListener("pointermove", (event) => { if (state.stroke?.pointerId === event.pointerId) paint(event); });
    function finishStroke(event, cancelled = false) {
      if (state.stroke?.pointerId !== event.pointerId) return;
      if (cancelled) { state.points = state.stroke.original; writePoints(); draw(); changed(); }
      state.stroke = null;
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
      sync();
    }
    canvas.addEventListener("pointerup", (event) => finishStroke(event));
    canvas.addEventListener("pointercancel", (event) => finishStroke(event, true));
    canvas.addEventListener("lostpointercapture", (event) => finishStroke(event, true));
    window.addEventListener("resize", draw); window.addEventListener("synthv:themechange", draw);
    // 首次构建使用显式空目录，随后仅由真实选区读取刷新能力。
    state.selection = undefined; updateCatalog(null);
    return Object.freeze({
      setSelection: updateCatalog,
      // 补充目录后由业务模块显式选择新增项；不通过合成 DOM 事件触发其他业务动作。
      selectParameter: (parameter) => {
        if (!state.entries.get(parameter)?.available) return false;
        state.selected = parameter; $("parameter").value = parameter; configureParameter(); return true;
      },
      setBusy: (busy, connected) => { state.busy = busy; state.connected = connected; sync(); },
      canPreview: () => Boolean(definition()?.available && (!usingCurve() || (enhanced() && !state.error))),
      describe: (parameter) => state.entries.get(parameter),
      getPayload: () => {
        const entry = definition(); if (!entry?.available) throw new Error("当前参数不可用，请重新读取选区或更新桥接。");
        const payload = { parameter: state.selected };
        if (enhanced()) payload.renderMode = entry.kind === "pitch" ? "smooth" : $("parameter-render-mode").value;
        if (usingCurve()) {
          if (!enhanced()) throw new Error("当前桥接未提供曲线能力，请更新桥接并重新读取选区。");
          payload.curve = parsePoints($("curve-points").value, valueRange());
        } else {
          payload.delta = Number($("parameter-delta").value);
          if (!finite(payload.delta) || Math.abs(payload.delta) > entry.maxDelta) throw new Error("调整量超出当前参数允许的范围。");
        }
        return payload;
      },
    });
  }
  window.addEventListener("resize", () => document.querySelectorAll("canvas[data-curve-preview]").forEach(drawPreview));
  window.addEventListener("synthv:themechange", () => document.querySelectorAll("canvas[data-curve-preview]").forEach(drawPreview));
  window.SynthVCurves = Object.freeze({ createEditor, createPreview, representation });
})();
