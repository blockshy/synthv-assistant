import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../core/api.dart';

/// 图例和指针使用原单位，只有绘图坐标允许归一化。
String curveNumber(num value) =>
    value.toStringAsFixed(3).replaceFirst(RegExp(r'\.?0+$'), '');

bool _finite(dynamic value) => value is num && value.isFinite;
List<JsonMap> _maps(dynamic value) => value is List
    ? value
          .whereType<Map>()
          .map((item) => Map<String, dynamic>.from(item))
          .toList()
    : <JsonMap>[];

/// 合并图必须来自同一次选区快照，不能拿当前选区填补历史预览缺失的数据。
bool compatiblePreviews(List<JsonMap> previews) {
  if (previews.length < 2) return true;
  final first = previews.first;
  return previews.every(
    (item) =>
        _finite(item['startSeconds']) &&
        _finite(item['endSeconds']) &&
        item['endSeconds'] > item['startSeconds'] &&
        (item['startSeconds'] - first['startSeconds']).abs() < 0.000001 &&
        (item['endSeconds'] - first['endSeconds']).abs() < 0.000001 &&
        jsonEncode(item['notes'] ?? []) == jsonEncode(first['notes'] ?? []),
  );
}

class _Series {
  _Series(this.preview, this.color) {
    points = _maps(preview['curvePreview'])
        .where(
          (p) =>
              _finite(p['position']) &&
              p['position'] >= 0 &&
              p['position'] <= 1 &&
              _finite(p['after']),
        )
        .toList();
    controls = _maps(preview['controlPoints'])
        .where(
          (p) =>
              _finite(p['position']) &&
              p['position'] >= 0 &&
              p['position'] <= 1 &&
              _finite(p['value']),
        )
        .toList();
    final values = <double>[
      for (final p in points) ...[
        (p['after'] as num).toDouble(),
        if (_finite(p['before'])) (p['before'] as num).toDouble(),
      ],
      for (final p in controls) (p['value'] as num).toDouble(),
    ];
    final minimum = values.isEmpty ? 0.0 : values.reduce(math.min);
    final maximum = values.isEmpty ? 1.0 : values.reduce(math.max);
    final padding = math.max(
      (maximum - minimum) * .08,
      preview['parameter'] == 'pitchCurve' ? .5 : .01,
    );
    low = minimum - padding;
    high = maximum + padding;
  }
  final JsonMap preview;
  final Color color;
  late final List<JsonMap> points;
  late final List<JsonMap> controls;
  late final double low;
  late final double high;
  String get label => '${preview['label'] ?? preview['parameter'] ?? '参数'}';
  String get unit => '${preview['unit'] ?? ''}';
  bool get nodes =>
      preview['renderMode'] == 'points' ||
      preview['representation'] == 'automation-points';

  double? sample(double x) {
    for (var i = 0; i < points.length - 1; i++) {
      final a = points[i], b = points[i + 1];
      final ax = (a['position'] as num).toDouble(),
          bx = (b['position'] as num).toDouble();
      if (x >= ax && x <= bx && bx > ax) {
        return (a['after'] as num).toDouble() +
            (b['after'] - a['after']) * (x - ax) / (bx - ax);
      }
    }
    return null;
  }
}

/// 宿主正式预览：乐谱音符和参数曲线上下分轨，共用同一真实时间轴。
/// 连线只来自宿主采样；圆点只来自宿主 controlPoints，绝不补造节点。
class CurvePreview extends StatefulWidget {
  const CurvePreview({super.key, required this.previews});
  final List<JsonMap> previews;
  @override
  State<CurvePreview> createState() => _CurvePreviewState();
}

class _CurvePreviewState extends State<CurvePreview> {
  bool _before = false;
  final Set<String> _hidden = {};
  double? _position;
  static const _colors = [
    Color(0xff5a91c9),
    Color(0xffce9963),
    Color(0xff8c9f68),
    Color(0xffac83be),
    Color(0xffc77f8d),
  ];

  @override
  Widget build(BuildContext context) {
    final previews = widget.previews.take(5).toList();
    if (previews.isEmpty) return const SizedBox.shrink();
    if (!compatiblePreviews(previews)) {
      return const Text('预览的音符或时间范围不一致，请重新生成组合预览。');
    }
    final all = [
      for (var i = 0; i < previews.length; i++)
        _Series(previews[i], _colors[i]),
    ];
    final valid = all.where((entry) => entry.points.length >= 2).toList();
    if (valid.isEmpty) return const Text('宿主没有返回可绘制的曲线采样；参数明细仍可查看。');
    final visible = valid
        .where((entry) => !_hidden.contains(entry.label))
        .toList();
    final first = previews.first;
    final colors = Theme.of(context).colorScheme;
    final notes = _maps(first['notes']);
    final at = _position;
    final start = _finite(first['startSeconds'])
        ? (first['startSeconds'] as num).toDouble()
        : 0.0;
    final end = _finite(first['endSeconds'])
        ? (first['endSeconds'] as num).toDouble()
        : 1.0;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 4,
          children: [
            for (final entry in valid)
              FilterChip(
                selected: !_hidden.contains(entry.label),
                avatar: Icon(Icons.show_chart, size: 16, color: entry.color),
                label: Text(
                  '${entry.label} · ${curveNumber(entry.low)}–${curveNumber(entry.high)} ${entry.unit}',
                  style: const TextStyle(fontSize: 11),
                ),
                onSelected: (enabled) => setState(() {
                  enabled
                      ? _hidden.remove(entry.label)
                      : _hidden.add(entry.label);
                }),
              ),
          ],
        ),
        Row(
          children: [
            SizedBox(
              width: 28,
              height: 32,
              child: Checkbox(
                value: _before,
                onChanged: (value) => setState(() => _before = value!),
              ),
            ),
            const Flexible(
              child: Text('显示调整前（虚线）', style: TextStyle(fontSize: 11)),
            ),
          ],
        ),
        LayoutBuilder(
          builder: (context, constraints) => MouseRegion(
            onExit: (_) => setState(() => _position = null),
            onHover: (event) => setState(
              () => _position =
                  ((event.localPosition.dx - 48) /
                          math.max(1, constraints.maxWidth - 60))
                      .clamp(0.0, 1.0),
            ),
            child: Semantics(
              label:
                  '上方为按 MIDI 音高排列的乐谱音符，下方为${visible.map((s) => s.label).join('、')}曲线；虚线调整前、实线调整后。',
              child: SizedBox(
                height: 250,
                child: CustomPaint(
                  painter: _PreviewPainter(
                    series: visible,
                    notes: notes,
                    before: _before,
                    multiple: all.length > 1,
                    textColor: colors.onSurfaceVariant,
                    gridColor: colors.outlineVariant,
                    noteColor: colors.onSurface,
                    start: start,
                    end: end,
                    pointer: at,
                  ),
                ),
              ),
            ),
          ),
        ),
        Text(
          at == null
              ? '移动指针查看原单位数值'
              : [
                  '${curveNumber(start + at * (end - start))} 秒',
                  for (final entry in visible)
                    '${entry.label} ${entry.sample(at) == null ? '—' : curveNumber(entry.sample(at)!)} ${entry.unit}',
                ].join(' · '),
          style: Theme.of(context).textTheme.bodySmall,
        ),
        const SizedBox(height: 8),
        Text(
          all.length > 1
              ? '曲线按各自量程归一化，不能直接比较不同参数的绝对大小。音符为乐谱参考，不是实际演唱基频。'
              : '音符为乐谱参考，下方曲线保留原单位。预览不代表已经应用或试听。',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
    );
  }
}

class _PreviewPainter extends CustomPainter {
  _PreviewPainter({
    required this.series,
    required this.notes,
    required this.before,
    required this.multiple,
    required this.textColor,
    required this.gridColor,
    required this.noteColor,
    required this.start,
    required this.end,
    this.pointer,
  });
  final List<_Series> series;
  final List<JsonMap> notes;
  final bool before, multiple;
  final Color textColor, gridColor, noteColor;
  final double start, end;
  final double? pointer;

  void label(
    Canvas canvas,
    String value,
    Offset at, {
    double width = 80,
    double size = 10,
  }) {
    final text = TextPainter(
      text: TextSpan(
        text: value,
        style: TextStyle(color: textColor, fontSize: size),
      ),
      textDirection: TextDirection.ltr,
      maxLines: 1,
      ellipsis: '…',
    )..layout(maxWidth: width);
    text.paint(canvas, at);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final left = 48.0, right = math.max(left + 1, size.width - 12);
    final noteRect = Rect.fromLTRB(left, 18, right, 105);
    final curveRect = Rect.fromLTRB(left, 137, right, 219);
    double x(double position) => left + position * (right - left);
    label(canvas, '乐谱', const Offset(0, 0));
    label(
      canvas,
      multiple ? '各自量程 %' : (series.isEmpty ? '参数' : series.first.unit),
      const Offset(0, 117),
      width: size.width,
    );
    final grid = Paint()
      ..color = gridColor.withValues(alpha: .5)
      ..strokeWidth = .5;
    final pitches = notes
        .where((n) => _finite(n['pitch']))
        .map((n) => (n['pitch'] as num).toDouble())
        .toList();
    final minPitch = pitches.isEmpty
        ? 59
        : pitches.reduce(math.min).floor() - 1;
    final maxPitch = pitches.isEmpty ? 65 : pitches.reduce(math.max).ceil() + 1;
    final semitoneHeight = noteRect.height / (maxPitch - minPitch + 1);
    for (var pitch = minPitch; pitch <= maxPitch; pitch++) {
      final y = noteRect.bottom - (pitch - minPitch + 1) * semitoneHeight;
      if ([1, 3, 6, 8, 10].contains(pitch % 12)) {
        canvas.drawRect(
          Rect.fromLTWH(left, y, noteRect.width, semitoneHeight),
          Paint()..color = noteColor.withValues(alpha: .025),
        );
      }
      canvas.drawLine(Offset(left, y), Offset(right, y), grid);
      if (maxPitch - minPitch <= 12 || pitch % 12 == 0) {
        const names = [
          'C',
          'C♯',
          'D',
          'D♯',
          'E',
          'F',
          'F♯',
          'G',
          'G♯',
          'A',
          'A♯',
          'B',
        ];
        label(
          canvas,
          '${names[pitch % 12]}${pitch ~/ 12 - 1}',
          Offset(0, y),
          width: 44,
          size: math.min(10, semitoneHeight),
        );
      }
    }
    canvas.save();
    canvas.clipRect(noteRect);
    for (final note in notes) {
      if (!_finite(note['startPosition']) ||
          !_finite(note['endPosition']) ||
          !_finite(note['pitch'])) {
        continue;
      }
      final y =
          noteRect.bottom - (note['pitch'] - minPitch + 1) * semitoneHeight;
      final rect = Rect.fromLTRB(
        x((note['startPosition'] as num).toDouble()),
        y + 1,
        x((note['endPosition'] as num).toDouble()),
        y + math.max(2, semitoneHeight - 1),
      );
      canvas.drawRRect(
        RRect.fromRectAndRadius(rect, const Radius.circular(2)),
        Paint()..color = noteColor.withValues(alpha: .55),
      );
    }
    canvas.restore();
    for (var i = 0; i <= 4; i++) {
      final y = curveRect.bottom - i / 4 * curveRect.height;
      canvas.drawLine(Offset(left, y), Offset(right, y), grid);
      final value = multiple
          ? i * 25
          : series.isEmpty
          ? i / 4
          : series.first.low + (series.first.high - series.first.low) * i / 4;
      label(canvas, curveNumber(value), Offset(0, y - 5), width: 45);
      final xx = x(i / 4);
      canvas.drawLine(
        Offset(xx, noteRect.top),
        Offset(xx, curveRect.bottom),
        grid,
      );
      label(
        canvas,
        '${curveNumber(start + (end - start) * i / 4)}s',
        Offset((xx - 14).clamp(0.0, math.max(0, size.width - 42)), 229),
        width: 52,
      );
    }
    canvas.save();
    canvas.clipRect(curveRect.inflate(2));
    for (final entry in series) {
      double y(num value) =>
          curveRect.bottom -
          ((value - entry.low) / (entry.high - entry.low)) * curveRect.height;
      void pathFor(String field, bool dashed) {
        final path = Path();
        var drawing = false;
        for (final point in entry.points) {
          if (!_finite(point[field])) {
            drawing = false;
            continue;
          }
          final at = Offset(
            x((point['position'] as num).toDouble()),
            y(point[field] as num),
          );
          if (!drawing) {
            path.moveTo(at.dx, at.dy);
            drawing = true;
          } else {
            path.lineTo(at.dx, at.dy);
          }
        }
        final paint = Paint()
          ..color = entry.color.withValues(alpha: dashed ? .5 : 1)
          ..style = PaintingStyle.stroke
          ..strokeWidth = dashed ? 1 : 1.8;
        if (dashed) {
          for (final metric in path.computeMetrics()) {
            for (var distance = 0.0; distance < metric.length; distance += 8) {
              canvas.drawPath(
                metric.extractPath(
                  distance,
                  math.min(distance + 4, metric.length),
                ),
                paint,
              );
            }
          }
        } else {
          canvas.drawPath(path, paint);
        }
      }

      if (before) pathFor('before', true);
      pathFor('after', false);
      if (entry.nodes) {
        for (final point in entry.controls) {
          canvas.drawCircle(
            Offset(
              x((point['position'] as num).toDouble()),
              y(point['value'] as num),
            ),
            2.5,
            Paint()..color = entry.color,
          );
        }
      }
    }
    if (pointer != null) {
      canvas.drawLine(
        Offset(x(pointer!), curveRect.top),
        Offset(x(pointer!), curveRect.bottom),
        Paint()
          ..color = textColor.withValues(alpha: .4)
          ..strokeWidth = 1,
      );
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant _PreviewPainter oldDelegate) => true;
}
