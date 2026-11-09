import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import '../core/api.dart';
import 'curve_preview.dart';

/// 乐谱参照使用工程绝对音高与实际秒比例；组移调只在此处叠加一次。
/// 不完整或重叠的音符不会被当作可靠的音高偏移反算基线。
class DraftNote {
  const DraftNote(this.start, this.end, this.pitch, this.lyrics);
  final double start, end, pitch;
  final String lyrics;
}

List<DraftNote> draftNotes(JsonMap selection) {
  final start = selection['startSeconds'], end = selection['endSeconds'];
  final offset = selection['groupPitchOffset'];
  if (start is! num ||
      end is! num ||
      !start.isFinite ||
      !end.isFinite ||
      end <= start) {
    return [];
  }
  final result = <DraftNote>[];
  for (final note in (selection['notes'] as List? ?? []).whereType<Map>()) {
    final onset = note['onsetSeconds'],
        duration = note['durationSeconds'],
        pitch = note['pitch'];
    if (onset is! num ||
        duration is! num ||
        pitch is! num ||
        !onset.isFinite ||
        !duration.isFinite ||
        !pitch.isFinite ||
        duration <= 0) {
      continue;
    }
    final a = ((onset - start) / (end - start)).clamp(0.0, 1.0).toDouble();
    final b = ((onset + duration - start) / (end - start))
        .clamp(0.0, 1.0)
        .toDouble();
    if (b <= a) continue;
    result.add(
      DraftNote(
        a,
        b,
        pitch.toDouble() + (offset is num ? offset.toDouble() : 0),
        '${note['lyrics'] ?? ''}',
      ),
    );
  }
  result.sort((a, b) => a.start.compareTo(b.start));
  return result;
}

/// 有界简化只依据给定采样的线性误差；不能宣称保留采样之间的宿主真实插值。
List<List<double>> simplifyDraft(List<List<double>> input, double tolerance) {
  if (input.length <= 2) return input.map((p) => [...p]).toList();
  final first = input.first, last = input.last;
  double greatest = 0;
  var split = 0;
  for (var i = 1; i < input.length - 1; i++) {
    final expected =
        first[1] +
        (last[1] - first[1]) *
            (input[i][0] - first[0]) /
            math.max(1e-12, last[0] - first[0]);
    final error = (input[i][1] - expected).abs();
    if (error > greatest) {
      greatest = error;
      split = i;
    }
  }
  if (greatest <= tolerance) {
    return [
      [...first],
      [...last],
    ];
  }
  return [
    ...simplifyDraft(input.sublist(0, split + 1), tolerance)..removeLast(),
    ...simplifyDraft(input.sublist(split), tolerance),
  ];
}

/// 手工编辑器仅修改本地曲线草稿；蓝灰虚线是宿主计算音高采样，不是候选曲线。
/// 正式预览仍由宿主生成。原生音高与音符同轴，普通自动化显示独立参数轨。
class ParameterDraftEditor extends StatefulWidget {
  const ParameterDraftEditor({
    super.key,
    required this.points,
    required this.bounds,
    required this.selection,
    required this.parameter,
    required this.enabled,
    required this.onChanged,
    this.canvasHeight,
  });
  final List<List<double>> points;
  final List<double> bounds;
  final JsonMap selection;
  final String parameter;
  final bool enabled;
  final ValueChanged<List<List<double>>> onChanged;

  /// 独立参数页可扩大画布；几何与命中测试仍使用同一个实际尺寸，避免拖动偏差。
  final double? canvasHeight;
  @override
  State<ParameterDraftEditor> createState() => _ParameterDraftEditorState();
}

class _ParameterDraftEditorState extends State<ParameterDraftEditor> {
  final List<List<double>> _stroke = [];
  bool _pointTool = false;
  int? _dragPoint, _selectedPoint;
  List<List<double>>? _moving;
  List<double>? _pointer;
  late _DraftGeometry _geometry;
  bool get _native => widget.parameter == 'pitchCurve';

  void _draw(Offset location) {
    final point = _geometry.valueAt(location);
    if (point == null) return;
    setState(() {
      _pointer = point;
      // 覆盖本笔经过的短时间区间，反向绘制不会保留交叉的旧线段。
      if (_stroke.isNotEmpty) {
        final a = math.min(_stroke.last[0], point[0]),
            b = math.max(_stroke.last[0], point[0]);
        _stroke.removeWhere((p) => p[0] >= a && p[0] <= b);
      }
      _stroke.add(point);
    });
  }

  void _commit(List<List<double>> points) {
    final sorted = points.map((p) => [...p]).toList()
      ..sort((a, b) => a[0].compareTo(b[0]));
    final unique = <List<double>>[];
    for (final p in sorted) {
      if (unique.isNotEmpty && (unique.last[0] - p[0]).abs() < 1e-8) {
        unique[unique.length - 1] = p;
      } else {
        unique.add(p);
      }
    }
    if (unique.length < 2 || unique.length > 64) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('曲线需保留 2～64 个节点，本次操作未替换草稿。')),
      );
      return;
    }
    widget.onChanged(unique);
  }

  void _finishStroke() {
    if (_stroke.isEmpty) return;
    final stroke = [..._stroke]..sort((a, b) => a[0].compareTo(b[0]));
    final reduced = simplifyDraft(
      stroke,
      _native ? .01 : (widget.bounds[1] - widget.bounds[0]) * .003,
    );
    final merged = [
      ...widget.points.where(
        (p) => p[0] < stroke.first[0] || p[0] > stroke.last[0],
      ),
      ...reduced,
    ]..sort((a, b) => a[0].compareTo(b[0]));
    if (merged.first[0] > 0) merged.insert(0, [0, widget.points.first[1]]);
    if (merged.last[0] < 1) merged.add([1, widget.points.last[1]]);
    setState(() => _stroke.clear());
    _commit(merged);
  }

  int? _near(Offset location) {
    int? index;
    var distance = 13.0;
    for (var i = 0; i < widget.points.length; i++) {
      final at = _geometry.pixel(widget.points[i]);
      final candidate = (at - location).distance;
      if (candidate < distance) {
        distance = candidate;
        index = i;
      }
    }
    return index;
  }

  void _move(Offset location) {
    final index = _dragPoint, point = _geometry.valueAt(location);
    if (index == null || point == null || _moving == null) return;
    // 首尾时间固定；内部控制点不允许越过相邻节点，保证后端单调时间契约。
    final x = index == 0
        ? 0.0
        : index == _moving!.length - 1
        ? 1.0
        : point[0].clamp(
            _moving![index - 1][0] + 1e-7,
            _moving![index + 1][0] - 1e-7,
          );
    setState(() {
      _moving![index] = [x, point[1]];
      _pointer = [x, point[1]];
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final notes = draftNotes(widget.selection);
    final editable = widget.enabled && widget.points.length >= 2;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                '曲线草稿 · ${widget.points.length}/64',
                style: theme.textTheme.bodySmall,
              ),
            ),
            IconButton(
              tooltip: '画笔绘制',
              isSelected: !_pointTool,
              onPressed: editable
                  ? () => setState(() {
                      _pointTool = false;
                      _selectedPoint = null;
                    })
                  : null,
              icon: const Icon(Icons.gesture, size: 16),
            ),
            IconButton(
              tooltip: '编辑控制点',
              isSelected: _pointTool,
              onPressed: editable
                  ? () => setState(() => _pointTool = true)
                  : null,
              icon: const Icon(Icons.timeline, size: 16),
            ),
            IconButton(
              tooltip: '删除选中的内部节点',
              onPressed:
                  editable &&
                      _pointTool &&
                      _selectedPoint != null &&
                      _selectedPoint! > 0 &&
                      _selectedPoint! < widget.points.length - 1
                  ? () {
                      final points = widget.points.map((p) => [...p]).toList()
                        ..removeAt(_selectedPoint!);
                      setState(() => _selectedPoint = null);
                      _commit(points);
                    }
                  : null,
              icon: const Icon(Icons.remove_circle_outline, size: 16),
            ),
          ],
        ),
        LayoutBuilder(
          builder: (context, constraints) {
            _geometry = _DraftGeometry(
              Size(
                constraints.maxWidth,
                widget.canvasHeight ?? (_native ? 222 : 242),
              ),
              widget.bounds,
              notes,
              _native,
            );
            return Semantics(
              label: '本地曲线草稿编辑区，可用下方数值节点进行精确编辑',
              child: MouseRegion(
                cursor: editable
                    ? SystemMouseCursors.precise
                    : SystemMouseCursors.basic,
                onHover: (event) => setState(
                  () => _pointer = _geometry.valueAt(event.localPosition),
                ),
                onExit: (_) => setState(() => _pointer = null),
                child: GestureDetector(
                  key: const ValueKey('parameter-draft-canvas'),
                  // 起点使用按下位置，不能在超过拖动阈值后再寻找控制点，否则小节点会被错过。
                  dragStartBehavior: DragStartBehavior.down,
                  onTapUp: editable && _pointTool
                      ? (event) {
                          final index = _near(event.localPosition),
                              point = _geometry.valueAt(event.localPosition);
                          if (index != null) {
                            setState(() => _selectedPoint = index);
                          } else if (point != null &&
                              point[0] > 0 &&
                              point[0] < 1) {
                            _commit([...widget.points, point]);
                            setState(() => _selectedPoint = null);
                          }
                        }
                      : null,
                  onPanStart: !editable
                      ? null
                      : (event) {
                          if (_pointTool) {
                            _dragPoint = _near(event.localPosition);
                            _selectedPoint = _dragPoint;
                            _moving = widget.points.map((p) => [...p]).toList();
                            _move(event.localPosition);
                          } else {
                            _stroke.clear();
                            _draw(event.localPosition);
                          }
                        },
                  onPanUpdate: !editable
                      ? null
                      : (event) => _pointTool
                            ? _move(event.localPosition)
                            : _draw(event.localPosition),
                  onPanEnd: !editable
                      ? null
                      : (_) {
                          if (_pointTool) {
                            if (_dragPoint != null && _moving != null) {
                              _commit(_moving!);
                            }
                            setState(() {
                              _moving = null;
                              _dragPoint = null;
                            });
                          } else {
                            _finishStroke();
                          }
                        },
                  onPanCancel: () => setState(() {
                    _stroke.clear();
                    _moving = null;
                    _dragPoint = null;
                  }),
                  child: CustomPaint(
                    size: _geometry.size,
                    painter: _DraftPainter(
                      geometry: _geometry,
                      points: _moving ?? widget.points,
                      stroke: [..._stroke],
                      selectedPoint: _selectedPoint,
                      showPoints: _pointTool,
                      computed: _native
                          ? widget.selection['computedPitch'] as Map?
                          : null,
                      start: widget.selection['startSeconds'],
                      end: widget.selection['endSeconds'],
                      foreground: theme.colorScheme.onSurface,
                      background: theme.colorScheme.surface,
                      border: theme.colorScheme.outlineVariant,
                    ),
                  ),
                ),
              ),
            );
          },
        ),
        const SizedBox(height: 5),
        Text(
          _pointer == null
              ? (_native ? '实线：重绘草稿 · 虚线：宿主计算音高采样' : '上轨：乐谱音符 · 下轨：相对原参数的增量')
              : '位置 ${curveNumber(_pointer![0] * 100)}% · ${curveNumber(_pointer![1])}${_native ? ' MIDI' : ''}',
          style: theme.textTheme.bodySmall,
        ),
      ],
    );
  }
}

/// 绘制与命中测试共用一个几何模型，避免视觉坐标与提交值各自换算后产生偏移。
class _DraftGeometry {
  _DraftGeometry(this.size, this.bounds, this.notes, this.native);
  final Size size;
  final List<double> bounds;
  final List<DraftNote> notes;
  final bool native;
  Rect get plot => Rect.fromLTRB(
    38,
    native ? 12 : 102,
    math.max(39, size.width - 10),
    size.height - 25,
  );
  Rect get roll =>
      native ? plot : Rect.fromLTRB(38, 12, math.max(39, size.width - 10), 80);
  List<double> get pitchRange => native
      ? bounds
      : notes.isEmpty
      ? [59, 61]
      : [
          notes.map((n) => n.pitch).reduce(math.min) - 1,
          notes.map((n) => n.pitch).reduce(math.max) + 1,
        ];
  Offset pixel(List<double> point) => Offset(
    plot.left + point[0] * plot.width,
    plot.bottom -
        (point[1] - bounds[0]) / (bounds[1] - bounds[0]) * plot.height,
  );
  List<double>? valueAt(Offset location) {
    if (!plot.contains(location)) return null;
    return [
      ((location.dx - plot.left) / plot.width).clamp(0.0, 1.0),
      (bounds[1] -
              (location.dy - plot.top) / plot.height * (bounds[1] - bounds[0]))
          .clamp(bounds[0], bounds[1]),
    ];
  }
}

class _DraftPainter extends CustomPainter {
  _DraftPainter({
    required this.geometry,
    required this.points,
    required this.stroke,
    required this.selectedPoint,
    required this.showPoints,
    required this.computed,
    required this.start,
    required this.end,
    required this.foreground,
    required this.background,
    required this.border,
  });
  final _DraftGeometry geometry;
  final List<List<double>> points, stroke;
  final int? selectedPoint;
  final bool showPoints;
  final Map? computed;
  final dynamic start, end;
  final Color foreground, background, border;
  void _text(Canvas canvas, String text, Offset offset, {double width = 60}) {
    final painter = TextPainter(
      text: TextSpan(
        text: text,
        style: TextStyle(color: foreground.withValues(alpha: .65), fontSize: 9),
      ),
      textDirection: TextDirection.ltr,
      maxLines: 1,
      ellipsis: '…',
    )..layout(maxWidth: width);
    painter.paint(canvas, offset);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final plot = geometry.plot,
        roll = geometry.roll,
        pitches = geometry.pitchRange;
    canvas.drawRect(Offset.zero & size, Paint()..color = background);
    canvas.drawRect(
      Offset.zero & size,
      Paint()
        ..color = border
        ..style = PaintingStyle.stroke,
    );
    double pitchY(double value) =>
        roll.bottom -
        (value - pitches[0]) / (pitches[1] - pitches[0]) * roll.height;
    for (var pitch = pitches[0].ceil(); pitch <= pitches[1].floor(); pitch++) {
      final y = pitchY(pitch.toDouble());
      canvas.drawLine(
        Offset(roll.left, y),
        Offset(roll.right, y),
        Paint()..color = foreground.withValues(alpha: .06),
      );
      if (pitches[1] - pitches[0] < 12 || pitch % 2 == 0) {
        _text(canvas, '$pitch', Offset(4, y - 5), width: 30);
      }
    }
    for (final note in geometry.notes) {
      final x = roll.left + note.start * roll.width,
          finish = roll.left + note.end * roll.width;
      final half = math.max(
        2.0,
        math.min(7.0, roll.height / (pitches[1] - pitches[0]) * .32),
      );
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTRB(
            x,
            pitchY(note.pitch) - half,
            finish,
            pitchY(note.pitch) + half,
          ),
          const Radius.circular(2),
        ),
        Paint()..color = foreground.withValues(alpha: .15),
      );
      if (finish - x > 24) {
        _text(
          canvas,
          note.lyrics,
          Offset(x + 3, pitchY(note.pitch) - 6),
          width: finish - x - 5,
        );
      }
    }
    if (!geometry.native) {
      for (var i = 0; i <= 4; i++) {
        final value =
            geometry.bounds[0] +
            (geometry.bounds[1] - geometry.bounds[0]) * i / 4;
        final y = geometry.pixel([0, value]).dy;
        _text(canvas, curveNumber(value), Offset(2, y - 5), width: 34);
        canvas.drawLine(
          Offset(plot.left, y),
          Offset(plot.right, y),
          Paint()..color = foreground.withValues(alpha: .08),
        );
      }
    }
    for (var i = 0; i <= 2; i++) {
      final x = plot.left + plot.width * i / 2;
      canvas.drawLine(
        Offset(x, roll.top),
        Offset(x, plot.bottom),
        Paint()..color = border,
      );
      final label = start is num && end is num
          ? '${curveNumber(start + (end - start) * i / 2)}s'
          : '${i * 50}%';
      _text(
        canvas,
        label,
        Offset((x - 12).clamp(0, size.width - 40), plot.bottom + 7),
        width: 44,
      );
    }
    canvas.save();
    canvas.clipRect(plot);
    // 缺失帧必须断开，不以乐谱或零值补造原演唱音高。
    if (computed?['status'] == 'ready' && computed?['samples'] is List) {
      Offset? previous;
      for (final item in computed!['samples'] as List) {
        if (item is! List ||
            item.length < 2 ||
            item[0] is! num ||
            item[1] is! num ||
            !(item[1] as num).isFinite) {
          previous = null;
          continue;
        }
        final next = geometry.pixel([
          (item[0] as num).toDouble(),
          (item[1] as num).toDouble(),
        ]);
        if (previous != null) {
          final distance = (next - previous).distance;
          for (double step = 0; step < distance; step += 7) {
            canvas.drawLine(
              Offset.lerp(previous, next, step / math.max(1, distance))!,
              Offset.lerp(
                previous,
                next,
                math.min(step + 3, distance) / math.max(1, distance),
              )!,
              Paint()
                ..color = const Color(0xff6c94b8)
                ..strokeWidth = 1.2,
            );
          }
        }
        previous = next;
      }
    }
    void line(List<List<double>> nodes, Color color) {
      final path = Path();
      for (var i = 0; i < nodes.length; i++) {
        final p = geometry.pixel(nodes[i]);
        if (i == 0) {
          path.moveTo(p.dx, p.dy);
        } else {
          path.lineTo(p.dx, p.dy);
        }
      }
      canvas.drawPath(
        path,
        Paint()
          ..color = color
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.7,
      );
    }

    line(points, foreground);
    if (showPoints) {
      for (var i = 0; i < points.length; i++) {
        canvas.drawCircle(
          geometry.pixel(points[i]),
          i == selectedPoint ? 4.5 : 3,
          Paint()
            ..color = i == selectedPoint ? const Color(0xff6c94b8) : foreground,
        );
      }
    }
    if (stroke.isNotEmpty) line(stroke, const Color(0xff6c94b8));
    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant _DraftPainter oldDelegate) => true;
}
