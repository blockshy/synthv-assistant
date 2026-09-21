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

  /** 音符矩形只接受预览保存的公开坐标，不从当前工程替换历史预览的音符。 */
  function previewNotes(value) {
    if (!Array.isArray(value) || value.length > 128) return [];
    return value.filter((note) => finite(note?.startPosition) && finite(note?.endPosition) && finite(note?.pitch)
      && note.startPosition >= 0 && note.endPosition <= 1 && note.startPosition < note.endPosition && note.pitch >= 0 && note.pitch <= 127)
      .map(({ startPosition, endPosition, pitch }) => ({ startPosition, endPosition, pitch }));
  }

  /** 草稿使用本次读取选区的秒坐标，并明确加上组移调；没有可靠时基时不猜测音符位置。 */
  function selectionNotes(selection) {
    if (!finite(selection?.startSeconds) || !finite(selection?.endSeconds) || selection.endSeconds <= selection.startSeconds || !finite(selection?.groupPitchOffset)) return [];
    const span = selection.endSeconds - selection.startSeconds;
    if (!Array.isArray(selection.notes) || selection.notes.length > 128) return [];
    const normalized = selection.notes.map((note) => ({
      startPosition: finite(note?.onsetSeconds) ? clamp((note.onsetSeconds - selection.startSeconds) / span, 0, 1) : NaN,
      endPosition: finite(note?.onsetSeconds) && finite(note?.durationSeconds) && note.durationSeconds > 0 ? clamp((note.onsetSeconds + note.durationSeconds - selection.startSeconds) / span, 0, 1) : NaN,
      pitch: finite(note?.pitch) ? note.pitch + selection.groupPitchOffset : NaN,
    }));
    return previewNotes(normalized);
  }

  function pitchName(value) {
    const rounded = Math.round(value), names = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];
    return `${names[((rounded % 12) + 12) % 12]}${Math.floor(rounded / 12) - 1}`;
  }

  /** 原生音高草稿逐音符保留稳定段，音符交界仅留短过渡，避免整句变为首音高或长滑音。 */
  function notePitchPoints(selection) {
    const notes = selectionNotes(selection).sort((a, b) => a.startPosition - b.startPosition);
    if (!notes.length || notes.length !== selection?.notes?.length) throw new Error("无法取得完整音符时间与音高，请重新读取选区后建立音高草稿。");
    if (notes.length > 32) throw new Error("当前选区超过 32 个音符，无法在 64 点内保留每个音符的稳定段。请缩小选区后重置草稿。");
    if (notes.some((note, index) => index && note.startPosition < notes[index - 1].endPosition - 1e-9)) throw new Error("选区包含重叠音符，不能自动推断单条音高轮廓；请缩小为连续单声部选区。");
    const duration = selection.endSeconds - selection.startSeconds;
    return notes.flatMap((note, index) => {
      // 每端最多 20 ms，且不超过音符时长的 10%；短音符仍保留独立稳定段。
      const edge = Math.min(.02 / duration, (note.endPosition - note.startPosition) * .1);
      return [[index === 0 ? 0 : note.startPosition + edge, note.pitch],
        [index === notes.length - 1 ? 1 : note.endPosition - edge, note.pitch]];
    });
  }

  /** 仅压缩本次笔迹，按纵向最大误差优先保留转折；未触及的音符锚点始终原样保留。 */
  function fitStroke(points, budget) {
    if (points.length <= budget) return points;
    if (budget < 2) throw new Error("现有草稿已占满 64 点，无法保留原音符并加入这段笔迹。请缩小选区或覆盖更长的一段后重试。");
    const keep = new Set([0, points.length - 1]);
    while (keep.size < budget) {
      const kept = [...keep].sort((a, b) => a - b); let candidate = -1, largest = -1;
      for (let segment = 1; segment < kept.length; segment++) {
        const from = kept[segment - 1], to = kept[segment], a = points[from], b = points[to];
        for (let index = from + 1; index < to; index++) {
          const point = points[index], expected = a[1] + (b[1] - a[1]) * (point[0] - a[0]) / (b[0] - a[0]);
          const error = Math.abs(point[1] - expected);
          if (error > largest) { candidate = index; largest = error; }
        }
      }
      if (candidate < 0) break; keep.add(candidate);
    }
    return [...keep].sort((a, b) => a - b).map((index) => points[index]);
  }

  /**
   * 查找某个时间位置唯一对应的乐谱音符。相邻音符交界归后一音符，末端归结束音符；
   * 休止或重叠没有唯一音高参照，不随意选取最近的音符来投射节点或接受笔迹。
   */
  function pitchNoteAt(notes, position) {
    const active = notes.filter((note) => position >= note.startPosition && position < note.endPosition);
    if (active.length === 1) return active[0];
    if (active.length > 1) {
      // 秒与位置比例换算可能使相邻边界重叠约 1e-14；只容忍数值舍入，不容忍真实重叠。
      active.sort((a, b) => a.startPosition - b.startPosition);
      const last = active.at(-1);
      return active.slice(0, -1).every((note) => note.endPosition <= last.startPosition + 1e-9) ? last : null;
    }
    const endings = notes.filter((note) => Math.abs(position - note.endPosition) < 1e-9);
    return endings.length === 1 ? endings[0] : null;
  }

  /** 仅为显示区间边界插值；未知前值仍保持未知，不外推超出宿主采样的范围。 */
  function sampleLine(points, position) {
    const exact = points.find((point) => Math.abs(point[0] - position) < 1e-10);
    if (exact) return finite(exact[1]) ? exact[1] : null;
    for (let index = 1; index < points.length; index++) {
      const left = points[index - 1], right = points[index];
      if (position > left[0] && position < right[0]) {
        return finite(left[1]) && finite(right[1])
          ? left[1] + (right[1] - left[1]) * (position - left[0]) / (right[0] - left[0]) : null;
      }
    }
    return null;
  }

  /**
   * 把音分参数投影到音符的 MIDI 坐标，仅用于乐谱参考显示，不修改宿主数据。
   * 每个音符分别裁剪采样并插入断线，避免跨休止或音程跳变连出不存在的滑音。
   * 输入是宿主参数总值时显示总偏移，输入是草稿时显示拟增加的偏移；都不冒充合成基频。
   */
  function pitchOverlay(notes, series, controlPoints) {
    const ordered = notes.map((note) => ({ ...note })).sort((a, b) => a.startPosition - b.startPosition);
    if (!ordered.length) return null;
    for (let index = 1; index < ordered.length; index++) {
      const previous = ordered[index - 1], note = ordered[index];
      if (note.startPosition < previous.endPosition - 1e-9) return null;
      // 只在显示副本上对齐舍入误差，既不改宿主快照，也不让普通相邻音符误降级为分轨。
      if (note.startPosition < previous.endPosition) previous.endPosition = note.startPosition;
    }
    const projected = series.map((line) => ({ ...line, points: ordered.flatMap((note) => {
      const segment = [[note.startPosition, sampleLine(line.points, note.startPosition)],
        ...line.points.filter(([position]) => position > note.startPosition && position < note.endPosition),
        [note.endPosition, sampleLine(line.points, note.endPosition)]];
      return [...segment.map(([position, value]) => [position, finite(value) ? note.pitch + value / 100 : null]), [note.endPosition, null]];
    }) }));
    const nodes = controlPoints?.flatMap((point) => {
      const note = pitchNoteAt(ordered, point.position);
      return note ? [{ position: point.position, value: note.pitch + point.value / 100 }] : [];
    }) ?? null;
    return { series: projected, controlPoints: nodes, omittedPoints: (controlPoints?.length || 0) - (nodes?.length || 0) };
  }

  /** 所有参数共用时间轴；音高及音分参考叠加采用 MIDI，非音高参数另设原生单位轨道。 */
  function chartGeometry(width, range, notes, pitch, pitchReference = false) {
    const hasNotes = notes.length > 0, split = hasNotes && !pitch;
    const height = split ? 440 : hasNotes || pitch ? 320 : 180;
    const left = hasNotes || pitch ? 64 : 46, right = Math.max(left + 1, width - 12);
    let roll = null;
    if (hasNotes || pitch) {
      const values = notes.map((note) => note.pitch);
      if (pitch) values.push(...range);
      // 偏移参考可能低于 MIDI 0 或高于 127；扩展显示域才能保留边缘音符的完整增量。
      // 这只是显示坐标，原生 MIDI 的提交范围和宿主参数限幅均保持原来的校验。
      const minimum = pitchReference ? -Infinity : 0, maximum = pitchReference ? Infinity : 127;
      let low = Math.max(minimum, Math.floor(Math.min(...values)) - 1);
      let high = Math.min(maximum, Math.ceil(Math.max(...values)) + 1);
      // 至少展示八个半音行，避免小音程选区里的音符块被拉成半张图。
      if (high - low < 7) { low = Math.max(minimum, low - Math.ceil((7 - high + low) / 2)); high = Math.min(maximum, low + 7); low = Math.max(minimum, high - 7); }
      roll = { left, right, top: 24, bottom: split ? 252 : height - 30, range: [low - .5, high + .5] };
    }
    const parameter = pitch ? roll : { left, right, top: split ? 286 : 24, bottom: height - 30, range };
    return { height, roll, parameter, split };
  }

  /**
   * 钢琴卷帘先画半音行和键盘，再按实际音高安放音符。原生绝对音高与音符共轴；
   * 音高偏移先显式换算成乐谱参考坐标；气声、声线等参数仍使用各自的单位轨道。
   * 连线来自宿主采样；圆点只来自候选实际节点，不能把等距显示采样当成控制点。
   */
  function plot(canvas, series, range, unit, context = {}) {
    const width = Math.round(canvas.getBoundingClientRect().width);
    if (!width) return null;
    const notes = context.notes || [], pitch = Boolean(context.pitch);
    const geometry = chartGeometry(width, range, notes, pitch, context.pitchReference), { roll, parameter: bounds, split, height } = geometry;
    canvas.classList.toggle("curve-with-notes", Boolean(roll));
    canvas.classList.toggle("curve-parameter-lanes", split);
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext("2d"); ctx.scale(scale, scale);
    const css = getComputedStyle(document.documentElement), color = (name) => css.getPropertyValue(name).trim();
    const x = (position) => bounds.left + position * (bounds.right - bounds.left);
    const y = (value, track = bounds) => track.bottom - (value - track.range[0]) / (track.range[1] - track.range[0] || 1) * (track.bottom - track.top);
    ctx.clearRect(0, 0, width, height); ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    if (roll) {
      const rowHeight = (roll.bottom - roll.top) / (roll.range[1] - roll.range[0]);
      for (let midi = Math.ceil(roll.range[0]); midi <= Math.floor(roll.range[1]); midi++) {
        const top = y(midi + .5, roll), black = [1, 3, 6, 8, 10].includes(((midi % 12) + 12) % 12);
        // 键盘、半音背景均使用主题语义色；C 音行加强分界，窄轨也能读出音程方向。
        ctx.fillStyle = color(black ? "--panel-raised" : "--panel");
        ctx.fillRect(roll.left, top, roll.right - roll.left, rowHeight);
        ctx.fillStyle = color(black ? "--surface-selected" : "--input");
        ctx.fillRect(4, top, black ? roll.left - 16 : roll.left - 6, rowHeight);
        ctx.strokeStyle = color(midi % 12 === 0 ? "--border-strong" : "--border");
        ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(4, top); ctx.lineTo(roll.right, top); ctx.stroke();
        if (rowHeight >= 12 || midi % 12 === 0) {
          ctx.fillStyle = color("--muted"); ctx.fillText(pitchName(midi), 8, top + rowHeight / 2);
        }
      }
      for (const note of notes) {
        const left = x(note.startPosition), right = x(note.endPosition), top = y(note.pitch, roll) - rowHeight * .38;
        const noteHeight = Math.max(2, rowHeight * .76);
        ctx.fillStyle = color("--surface-selected"); ctx.strokeStyle = color("--border-strong");
        ctx.fillRect(left, top, Math.max(1, right - left), noteHeight);
        ctx.strokeRect(left, top, Math.max(1, right - left), noteHeight);
        if (right - left > 30 && noteHeight >= 12) {
          ctx.save(); ctx.beginPath(); ctx.rect(left + 2, top, Math.max(0, right - left - 4), noteHeight); ctx.clip();
          ctx.fillStyle = color("--text"); ctx.fillText(pitchName(note.pitch), left + 5, top + noteHeight / 2); ctx.restore();
        }
      }
    }
    if (!pitch) {
      ctx.textAlign = "right";
      for (let index = 0; index <= 4; index++) {
        const value = bounds.range[0] + (bounds.range[1] - bounds.range[0]) * index / 4, at = y(value);
        ctx.strokeStyle = color("--border"); ctx.beginPath(); ctx.moveTo(bounds.left, at); ctx.lineTo(bounds.right, at); ctx.stroke();
        ctx.fillStyle = color("--subtle"); ctx.fillText(format(value), bounds.left - 6, at);
      }
    }
    // 两个轨道使用完全相同的 x 映射；时间网格贯穿各轨，但不跨越标题间隔。
    const hasTime = finite(context.startSeconds) && finite(context.endSeconds) && context.endSeconds > context.startSeconds;
    const divisions = width < 360 ? 2 : 4;
    for (let index = 0; index <= divisions; index++) {
      const position = index / divisions, at = x(position);
      ctx.strokeStyle = color("--border"); ctx.setLineDash([2, 4]);
      for (const track of split ? [roll, bounds] : [bounds]) { ctx.beginPath(); ctx.moveTo(at, track.top); ctx.lineTo(at, track.bottom); ctx.stroke(); }
      ctx.setLineDash([]); ctx.fillStyle = color("--subtle"); ctx.textBaseline = "top";
      ctx.textAlign = index === 0 ? "left" : index === divisions ? "right" : "center";
      ctx.fillText(hasTime ? `${format(context.startSeconds + position * (context.endSeconds - context.startSeconds))} s` : `${Math.round(position * 100)}%`, at, bounds.bottom + 8);
    }
    ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.fillStyle = color("--muted");
    ctx.fillText(roll ? pitch ? context.pitchReference ? "音符与音高偏移 · MIDI 参考" : "音符与原生音高 · MIDI" : "选区音符 · MIDI" : unit || "参数值", bounds.left, 4);
    if (split) ctx.fillText(`${context.label || "参数曲线"} · ${unit || "参数值"}`, bounds.left, bounds.top - 20);
    // 曲线和节点裁剪在各自数值轨中，避免误画到钢琴键或相邻参数轨。
    ctx.save(); ctx.beginPath(); ctx.rect(bounds.left, bounds.top, bounds.right - bounds.left, bounds.bottom - bounds.top); ctx.clip();
    for (const line of series) {
      ctx.strokeStyle = color(line.before ? "--waveform-b" : "--waveform-a"); ctx.lineWidth = line.before ? 1.5 : 2;
      ctx.setLineDash(line.before ? [4, 4] : []); ctx.beginPath(); let begun = false;
      for (const [position, value] of line.points) {
        if (!finite(value)) { begun = false; continue; }
        if (!begun) { ctx.moveTo(x(position), y(value)); begun = true; } else ctx.lineTo(x(position), y(value));
      }
      ctx.stroke(); ctx.setLineDash([]);
    }
    if (context.showPoints && Array.isArray(context.controlPoints)) {
      ctx.fillStyle = color("--panel"); ctx.strokeStyle = color("--waveform-a"); ctx.lineWidth = 1.5;
      for (const point of context.controlPoints) {
        ctx.beginPath(); ctx.arc(x(point.position), y(point.value), 2.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      }
    }
    ctx.restore();
    return geometry;
  }

  function representation(value, renderMode) {
    return representationNames[value] || (value ? String(value) : renderMode === "points" ? "密集控制点" : renderMode === "smooth" ? "精简曲线" : "旧版预览");
  }

  /** 预览只使用宿主保存的采样与音符；音分曲线的坐标投影明确标为乐谱参考。 */
  function createPreview(preview) {
    if (!Array.isArray(preview?.curvePreview)) return null;
    const points = preview.curvePreview.filter((item) => finite(item?.position) && item.position >= 0 && item.position <= 1 && finite(item.after));
    if (points.length < 2) return null;
    const figure = node("figure", "curve-preview-figure"), canvas = node("canvas", "curve-canvas curve-preview-canvas");
    const hasBefore = points.some((item) => finite(item.before));
    const controlPoints = Array.isArray(preview.controlPoints) ? preview.controlPoints.filter((point) =>
      finite(point?.position) && point.position >= 0 && point.position <= 1 && finite(point?.value)) : null;
    const showPoints = preview.renderMode === "points" || preview.representation === "automation-points";
    const notes = previewNotes(preview.notes), nativePitch = preview.parameter === "pitchCurve" || preview.kind === "pitch" || preview.representation === "native-pitch-curve";
    const originalSeries = [{ before: true, points: points.map((point) => [point.position, point.before]) },
      { points: points.map((point) => [point.position, point.after]) }];
    const overlay = preview.parameter === "pitchDelta" ? pitchOverlay(notes, originalSeries, controlPoints) : null;
    const series = overlay?.series || originalSeries, displayPoints = overlay ? overlay.controlPoints : controlPoints;
    const pitch = nativePitch || Boolean(overlay);
    const values = series.flatMap((line) => line.points.map((point) => point[1]).filter(finite));
    if (displayPoints) values.push(...displayPoints.map((point) => point.value));
    if (pitch) values.push(...notes.map((note) => note.pitch));
    let low = Math.min(...values), high = Math.max(...values);
    const padding = pitch ? Math.max((high - low) * .08, .8) : (high - low) * .08 || .1; low -= padding; high += padding;
    const unit = preview.unit || "参数值";
    canvas.dataset.curvePreview = "true"; canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `宿主曲线预览，${hasBefore ? "虚线为调整前，实线为调整后" : "仅展示调整后，原曲线未提供"}。${notes.length ? `包含 ${notes.length} 个乐谱音符矩形。` : ""}调整后起点 ${format(points[0].after)}，终点 ${format(points.at(-1).after)} ${unit}。完整数值见预览明细。`);
    previewData.set(canvas, { series, range: [low, high], unit, notes, pitch, pitchReference: Boolean(overlay), label: preview.label,
      controlPoints: displayPoints, showPoints, startSeconds: preview.startSeconds, endSeconds: preview.endSeconds });
    // 同一预览也会留在已应用的历史建议卡中，不用“尚未写入”覆盖真实操作状态。
    const pointsNote = showPoints ? controlPoints ? `圆点为${overlay ? "映射到音符的 " : "选区内 "}${displayPoints.length} 个宿主实际控制点。${overlay?.omittedPoints ? `另有 ${overlay.omittedPoints} 个节点位于音符间隙，未投射到音符上。` : ""}`
      : "此预览未提供实际控制点；使用新版桥接生成新预览后可显示节点。" : "";
    const referenceNote = overlay ? "音高偏移按“乐谱音高 + 音分 / 100”叠加，与音符共用 MIDI 坐标；这是位置参考，不代表实际演唱音高。"
      : preview.parameter === "pitchDelta" ? `因${notes.length ? "音符重叠" : "缺少音符"}，暂以独立音分轨显示偏移。` : "";
    figure.append(canvas, node("figcaption", "field-help", `${hasBefore ? "虚线为调整前，实线为调整后。" : "实线为调整后；未提供原曲线。"}${pointsNote}${referenceNote}${notes.length ? `矩形为 ${notes.length} 个按音高排列的乐谱音符${pitch ? overlay ? "。" : "，与曲线共用 MIDI 坐标，不代表实际演唱音高。" : "；下方参数轨与上方音符共用时间轴，不代表实际演唱音高。"}` : "此预览未提供音符位置。"}应用状态见操作提示。`));
    canvas.setAttribute("aria-label", `${canvas.getAttribute("aria-label") || "宿主曲线预览"}${referenceNote}${pointsNote}`);
    if (notes.length) {
      const noteDetails = node("details", "message-details curve-note-details"), list = node("ol", "curve-note-list");
      noteDetails.append(node("summary", "", "查看选区音符"));
      const hasTime = finite(preview.startSeconds) && finite(preview.endSeconds) && preview.endSeconds > preview.startSeconds;
      for (const note of notes) {
        const start = hasTime ? preview.startSeconds + note.startPosition * (preview.endSeconds - preview.startSeconds) : note.startPosition * 100;
        const end = hasTime ? preview.startSeconds + note.endPosition * (preview.endSeconds - preview.startSeconds) : note.endPosition * 100;
        list.append(node("li", "", `${pitchName(note.pitch)} · MIDI ${format(note.pitch)} · ${format(start)}–${format(end)} ${hasTime ? "秒" : "%"}`));
      }
      noteDetails.append(list); figure.append(noteDetails);
    }
    requestAnimationFrame(() => drawPreview(canvas));
    return figure;
  }
  function drawPreview(canvas) {
    const data = previewData.get(canvas); if (!data || !canvas.isConnected) return;
    plot(canvas, data.series, data.range, data.unit, data);
  }

  function createEditor({ onChange, onValidityChange } = {}) {
    const state = { selection: null, entries: catalog(null), selected: "breathiness", points: [[0, .05], [1, .05]],
      busy: false, connected: false, error: "", stroke: null };
    const definition = () => state.entries.get(state.selected);
    const enhanced = () => state.selection?.capabilities?.curves === true;
    const usingCurve = () => $("parameter-method").value === "curve";
    const valueRange = () => definition()?.kind === "pitch" ? [definition().nativeRange?.low ?? 0, definition().nativeRange?.high ?? 127] : [-definition().maxDelta, definition().maxDelta];
    const initialValue = () => definition()?.kind === "pitch" ? definition().nativeRange?.initial ?? 60 : clamp(Number($("parameter-delta").value) || 0, ...valueRange());
    // 时间比例保留足够精度，防止较长选区中的短音符被格式化到同一横坐标。
    const writePoints = () => { $("curve-points").value = state.points.map(([position, value]) => `${Number(position.toFixed(9))} ${format(value)}`).join("\n"); };
    let editorGeometry = null, editorPitchReference = false;
    const draw = () => {
      const notes = selectionNotes(state.selection), originalSeries = [{ points: state.points }];
      const controlPoints = state.points.map(([position, value]) => ({ position, value }));
      const overlay = state.selected === "pitchDelta" ? pitchOverlay(notes, originalSeries, controlPoints) : null;
      editorPitchReference = Boolean(overlay);
      // 手绘时使用完整允许增量范围，避免曲线随笔迹改变纵轴，导致指针和曲线来回跳动。
      const range = overlay ? [Math.min(...notes.map((note) => note.pitch)) + valueRange()[0] / 100,
        Math.max(...notes.map((note) => note.pitch)) + valueRange()[1] / 100] : valueRange();
      editorGeometry = plot($("curve-editor-canvas"), overlay?.series || originalSeries, range, definition()?.unit,
        { notes, pitch: definition()?.kind === "pitch" || Boolean(overlay), pitchReference: Boolean(overlay), label: definition()?.label,
          showPoints: $("parameter-render-mode").value === "points", controlPoints: overlay ? overlay.controlPoints : controlPoints,
          startSeconds: state.selection?.startSeconds, endSeconds: state.selection?.endSeconds });
    };
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
      state.error = "";
      try { state.points = definition()?.kind === "pitch" ? notePitchPoints(state.selection) : [[0, initialValue()], [1, initialValue()]]; }
      catch (error) { state.points = []; state.error = error.message; }
      writePoints(); sync(); draw();
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
      $("curve-value-help").textContent = entry.kind === "pitch" ? "点值为绝对 MIDI 半音。草稿按每个音符建立稳定段和短过渡；矩形为含组移调的乐谱音符，不代表实际演唱音高。" : `点值为增量，范围 ${format(range[0])}–${format(range[1])} ${entry.unit}；0 表示不改变。${entry.id === "pitchDelta" ? "有唯一音符参照时，草稿按乐谱音高叠加显示；在音符对应时间内绘制，仍提交音分增量，不是最终演唱音高。" : ""}`;
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
    $("parameter-render-mode").addEventListener("change", () => { draw(); changed(); });
    $("parameter-delta").addEventListener("input", changed);
    $("curve-reset").addEventListener("click", () => reset());
    $("curve-points").addEventListener("input", () => {
      try { state.points = parsePoints($("curve-points").value, valueRange()); state.error = ""; }
      catch (error) { state.error = error.message; }
      sync(); draw(); changed();
    });

    /** 手绘只替换笔迹覆盖的时间段；不要把未绘制的短音符锚点量化到固定网格。 */
    const canvas = $("curve-editor-canvas");
    function pointerPoint(event) {
      const rect = canvas.getBoundingClientRect(), [low, high] = valueRange();
      // 命中测试复用最近一次绘图的真实轨道与音域，避免钢琴卷帘加高后笔迹偏移。
      const bounds = editorGeometry.parameter;
      const position = clamp((event.clientX - rect.left - bounds.left) / Math.max(1, bounds.right - bounds.left), 0, 1);
      let value = bounds.range[1] - clamp((event.clientY - rect.top - bounds.top) / (bounds.bottom - bounds.top), 0, 1) * (bounds.range[1] - bounds.range[0]);
      if (editorPitchReference) {
        const note = pitchNoteAt(selectionNotes(state.selection), position);
        if (!note) return null;
        // 显示使用 MIDI，提交仍是相对增量；不可把屏幕 MIDI 值直接写进音分参数。
        value = (value - note.pitch) * 100;
      }
      value = clamp(value, low, high);
      return [Number(position.toFixed(9)), Number(value.toFixed(3))];
    }
    function paint(event) {
      const point = pointerPoint(event), previous = state.stroke.previous;
      if (!point) { state.stroke.previous = null; return; }
      // 回笔覆盖同一时间段时丢弃该段旧笔迹，以最后一次经过的位置为准。
      if (previous) {
        const from = Math.min(previous[0], point[0]), to = Math.max(previous[0], point[0]);
        for (const position of state.stroke.samples.keys()) if (position > from && position < to) state.stroke.samples.delete(position);
      }
      state.stroke.samples.set(point[0], point); state.stroke.previous = point;
      const painted = [...state.stroke.samples.values()].sort((a, b) => a[0] - b[0]);
      const untouched = state.stroke.original.filter(([position]) => position < painted[0][0] || position > painted.at(-1)[0]);
      try {
        const fitted = fitStroke(painted, 64 - untouched.length);
        state.points = [...untouched, ...fitted].sort((a, b) => a[0] - b[0]);
        state.error = ""; writePoints();
      } catch (error) { state.error = error.message; }
      sync(); draw(); changed();
    }
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || !event.isPrimary || state.busy || !state.connected || !definition()?.available || !usingCurve()) return;
      if (state.points.length < 2 || !editorGeometry) return;
      const rect = canvas.getBoundingClientRect(), bounds = editorGeometry.parameter;
      // 非音高参数只能在下方参数轨绘制；点击上方音符或键盘不能写出一条满幅偏移。
      if (event.clientX < rect.left + bounds.left || event.clientX > rect.left + bounds.right ||
          event.clientY < rect.top + bounds.top || event.clientY > rect.top + bounds.bottom) return;
      if (!pointerPoint(event)) return;
      event.preventDefault(); canvas.setPointerCapture(event.pointerId);
      const samples = new Map();
      state.stroke = { pointerId: event.pointerId, samples, original: state.points.map((point) => [...point]), originalError: state.error, previous: null }; paint(event);
    });
    canvas.addEventListener("pointermove", (event) => { if (state.stroke?.pointerId === event.pointerId) paint(event); });
    function finishStroke(event, cancelled = false) {
      if (state.stroke?.pointerId !== event.pointerId) return;
      if (cancelled) { state.points = state.stroke.original; state.error = state.stroke.originalError; writePoints(); draw(); changed(); }
      state.stroke = null;
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
      sync();
    }
    canvas.addEventListener("pointerup", (event) => finishStroke(event));
    canvas.addEventListener("pointercancel", (event) => finishStroke(event, true));
    canvas.addEventListener("lostpointercapture", (event) => finishStroke(event, true));
    window.addEventListener("resize", draw); window.addEventListener("synthv:themechange", draw);
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(draw); observer.observe(canvas);
    }
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
  if (typeof ResizeObserver === "function") {
    // 观察固定容器而非每个历史 Canvas，避免关闭会话后观察器长期保留已移除的图形。
    const observer = new ResizeObserver(() => document.querySelectorAll("canvas[data-curve-preview]").forEach(drawPreview));
    for (const target of document.querySelectorAll("#page-chat, #manual-inspector")) observer.observe(target);
  }
  window.SynthVCurves = Object.freeze({ createEditor, createPreview, representation });
})();
