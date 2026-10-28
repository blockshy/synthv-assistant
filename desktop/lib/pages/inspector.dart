import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../core/api.dart';
import '../widgets/curve_preview.dart';
import 'page_chrome.dart';

/// 工程检查器只在用户点击读取时取得选区。状态轮询不读取完整工程、模型配置或音频。
class Inspector extends StatefulWidget {
  const Inspector({super.key, required this.api, this.onConnection});
  final WorkbenchApi api;
  final VoidCallback? onConnection;
  @override
  State<Inspector> createState() => _InspectorState();
}

class _InspectorState extends State<Inspector> {
  JsonMap _status = {}, _selection = {}, _preview = {};
  String? _parameter, _message;
  String _renderMode = 'smooth';
  bool _curve = false, _error = false, _refreshing = false;
  int _previewEpoch = -1;
  Timer? _timer;
  final _delta = TextEditingController(text: '0.05');
  final _points = TextEditingController(text: '0 0\n0.5 0.05\n1 0');
  final _modeName = TextEditingController();

  @override
  void initState() {
    super.initState();
    _refreshStatus();
    _timer = Timer.periodic(const Duration(seconds: 4), (_) {
      if (!widget.api.busy) _refreshStatus();
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    _delta.dispose();
    _points.dispose();
    _modeName.dispose();
    super.dispose();
  }

  bool get _connected =>
      _status['bridge'] is Map && _status['bridge']['connected'] == true;
  bool get _writeEnabled => _status['writeEnabled'] == true;
  bool get _supportsCurves =>
      _selection['capabilities'] is Map &&
      _selection['capabilities']['curves'] == true;

  /// 旧桥接只回退五个已支持的增量，新目录缺项则保持不可用。
  Map<String, JsonMap> get _catalog {
    if (_selection['parameters'] is Map) {
      final raw = _selection['parameters'] as Map;
      return {
        for (final entry in raw.entries)
          if (entry.value is Map &&
              entry.value['available'] != false &&
              (entry.key != 'pitchCurve' ||
                  (_supportsCurves &&
                      _selection['capabilities']['nativePitch'] == true)))
            '${entry.key}': Map<String, dynamic>.from(entry.value as Map),
      };
    }
    if (_selection.isEmpty ||
        _selection['capabilities'] is Map &&
            (_selection['capabilities'] as Map).containsKey('curves')) {
      return {};
    }
    return {
      'breathiness': {'label': '气声', 'unit': '', 'maxDelta': .3},
      'tension': {'label': '张力', 'unit': '', 'maxDelta': .3},
      'gender': {'label': '性别', 'unit': '', 'maxDelta': .3},
      'loudness': {'label': '响度', 'unit': 'dB', 'maxDelta': 6},
      'pitchDelta': {'label': '音高偏移', 'unit': '音分', 'maxDelta': 100},
    };
  }

  JsonMap get _definition => _catalog[_parameter] ?? {};
  bool get _nativePitch => _parameter == 'pitchCurve';
  List<double> get _bounds {
    if (_nativePitch) {
      final notes = _selection['notes'];
      final offset = (_selection['groupPitchOffset'] as num?)?.toDouble() ?? 0;
      final pitches = notes is List
          ? notes
                .whereType<Map>()
                .where((n) => n['pitch'] is num)
                .map((n) => (n['pitch'] as num).toDouble() + offset)
                .toList()
          : <double>[];
      if (pitches.isNotEmpty) {
        return [
          math.max(0, pitches.reduce(math.min) - 2),
          math.min(127, pitches.reduce(math.max) + 2),
        ];
      }
      return [0, 127];
    }
    final limit = (_definition['maxDelta'] as num?)?.toDouble() ?? .3;
    return [-limit, limit];
  }

  Future<void> _refreshStatus() async {
    // 请求未结束时不追加轮询，避免离线/退出阶段累计大量等待中的请求。
    if (_refreshing || !mounted) return;
    _refreshing = true;
    try {
      final status = Map<String, dynamic>.from(
        await widget.api.getJson('/api/status') as Map,
      );
      if (!mounted) return;
      final oldBridge = _status['bridge'] as Map? ?? {};
      final newBridge = status['bridge'] as Map? ?? {};
      final targetChanged =
          oldBridge.isNotEmpty &&
          (oldBridge['session'] != newBridge['session'] ||
              oldBridge['ipcDirectory'] != newBridge['ipcDirectory']);
      if (targetChanged || _connected && newBridge['connected'] != true) {
        widget.api.invalidatePreviews();
        _selection = {};
        _preview = {};
        _parameter = null;
      }
      setState(() => _status = status);
    } catch (_) {
      if (!mounted) return;
      if (_connected) widget.api.invalidatePreviews();
      setState(() {
        _status = {};
        _selection = {};
        _preview = {};
        _parameter = null;
      });
    } finally {
      _refreshing = false;
    }
  }

  Future<void> _run(Future<void> Function() action) async {
    setState(() {
      _message = null;
      _error = false;
    });
    try {
      await widget.api.exclusive(action);
    } catch (error) {
      if (mounted) {
        setState(() {
          _message = error.toString();
          _error = true;
        });
      }
    }
  }

  void _invalidate() {
    widget.api.invalidatePreviews();
    setState(() {
      _preview = {};
      _message = null;
    });
  }

  Future<void> _readSelection() => _run(() async {
    _invalidate();
    setState(() => _selection = {});
    final result = Map<String, dynamic>.from(
      await widget.api.getJson('/api/selection') as Map,
    );
    if (!mounted) return;
    setState(() {
      _selection = result;
      final names = _catalog.keys;
      _parameter = names.contains(_parameter) ? _parameter : names.firstOrNull;
      _message = (result['noteCount'] as num? ?? 0) > 0
          ? '选区已读取。'
          : '请先在 SynthV 中选中音符，再读取选区。';
      _resetDraft();
    });
    await _refreshStatus();
  });

  void _resetDraft() {
    if (_nativePitch) {
      _curve = true;
      _renderMode = 'smooth';
      // 原生草稿以已读取乐谱为参照，绝不以全句第一音高拉成水平直线。
      final start = (_selection['startSeconds'] as num?)?.toDouble();
      final end = (_selection['endSeconds'] as num?)?.toDouble();
      final offset = (_selection['groupPitchOffset'] as num?)?.toDouble() ?? 0;
      final raw = _selection['notes'];
      final nodes = <List<double>>[];
      if (start != null && end != null && end > start && raw is List) {
        final notes =
            raw
                .whereType<Map>()
                .where(
                  (n) =>
                      n['onsetSeconds'] is num &&
                      n['durationSeconds'] is num &&
                      n['pitch'] is num,
                )
                .toList()
              ..sort(
                (a, b) => (a['onsetSeconds'] as num).compareTo(
                  b['onsetSeconds'] as num,
                ),
              );
        for (final note in notes) {
          final a = ((note['onsetSeconds'] - start) / (end - start) as num)
              .toDouble()
              .clamp(0.0, 1.0);
          final b =
              ((note['onsetSeconds'] + note['durationSeconds'] - start) /
                          (end - start)
                      as num)
                  .toDouble()
                  .clamp(0.0, 1.0);
          final pitch = (note['pitch'] as num).toDouble() + offset;
          if (b <= a) continue;
          final margin = math.min(.01, (b - a) / 4);
          nodes.addAll([
            [a + margin, pitch],
            [b - margin, pitch],
          ]);
        }
        if (nodes.isNotEmpty) {
          nodes.first[0] = 0;
          nodes.last[0] = 1;
          if (nodes.length <= 64 &&
              List.generate(
                nodes.length - 1,
                (i) => nodes[i][0] < nodes[i + 1][0],
              ).every((v) => v)) {
            _points.text = nodes
                .map((p) => '${p[0].toStringAsFixed(6)} ${curveNumber(p[1])}')
                .join('\n');
            return;
          }
        }
      }
      _points.clear();
      _message = '无法生成可靠的乐谱草稿，请缩小到不重叠的短选区，或手动填写绝对 MIDI 曲线。';
    } else {
      if (!_supportsCurves) _curve = false;
      final limit = _bounds[1];
      final amount = math.min(
        limit,
        _parameter == 'pitchDelta'
            ? 10.0
            : _parameter == 'loudness'
            ? 1.0
            : .05,
      );
      _delta.text = curveNumber(amount);
      _points.text = '0 0\n0.5 ${curveNumber(amount)}\n1 0';
    }
  }

  List<List<double>> _parsePoints() {
    final points = <List<double>>[];
    for (final line in _points.text.trim().split(RegExp(r'[\r\n]+'))) {
      final values = line.trim().split(RegExp(r'[\s,，]+'));
      if (values.length != 2) {
        throw const ApiException('每行输入“位置 数值”，例如 0.5 0.05。');
      }
      final x = double.tryParse(values[0]), y = double.tryParse(values[1]);
      if (x == null ||
          y == null ||
          !x.isFinite ||
          !y.isFinite ||
          x < 0 ||
          x > 1 ||
          y < _bounds[0] ||
          y > _bounds[1]) {
        throw const ApiException('曲线存在无效数字或超出当前参数范围。');
      }
      if (points.isNotEmpty && x <= points.last[0]) {
        throw const ApiException('曲线位置需要严格递增。');
      }
      points.add([x, y]);
    }
    if (points.length < 2 ||
        points.length > 64 ||
        points.first[0] != 0 ||
        points.last[0] != 1) {
      throw const ApiException('曲线需要 2～64 个点，首尾位置分别为 0 和 1。');
    }
    return points;
  }

  Future<void> _previewChange() => _run(() async {
    if (_parameter == null) throw const ApiException('请先读取选区并选择参数。');
    final payload = <String, dynamic>{
      'parameter': _parameter,
      'renderMode': _renderMode,
    };
    if (_curve || _nativePitch) {
      payload['curve'] = _parsePoints();
    } else {
      final delta = double.tryParse(_delta.text.trim());
      if (delta == null ||
          !delta.isFinite ||
          delta == 0 ||
          delta < _bounds[0] ||
          delta > _bounds[1]) {
        throw const ApiException('请输入当前范围内的非零增量。');
      }
      payload['delta'] = delta;
    }
    _invalidate();
    final epoch = widget.api.previewEpoch;
    final preview = Map<String, dynamic>.from(
      await widget.api.postJson('/api/preview', payload) as Map,
    );
    if (preview['previewId'] is! String) throw const ApiException('宿主未返回有效预览。');
    if (!mounted) return;
    setState(() {
      _preview = preview;
      _previewEpoch = epoch;
      _message = '预览已生成，尚未修改工程。';
    });
  });

  Future<void> _applyChange() => _run(() async {
    if (_preview.isEmpty || _previewEpoch != widget.api.previewEpoch) {
      throw const ApiException('请先重新生成预览。');
    }
    final id = _preview['previewId'];
    _invalidate();
    final result =
        await widget.api.postJson('/api/apply', {'previewId': id}) as Map;
    if (result['verified'] != true) {
      throw const ApiException('宿主未确认写入结果，请检查工程后再继续。');
    }
    if (mounted) {
      setState(
        () =>
            _message = '${result['summary'] ?? result['message'] ?? '参数已应用。'}',
      );
    }
  });

  Future<void> _restore() => _run(() async {
    _invalidate();
    final result = await widget.api.postJson('/api/restore') as Map;
    if (result['verified'] != true) {
      throw const ApiException('宿主未确认恢复结果，请检查工程。');
    }
    if (mounted) {
      setState(
        () => _message =
            '${result['summary'] ?? result['message'] ?? '最近一次修改已恢复。'}',
      );
    }
  });

  Future<void> _setWrite(bool value) => _run(() async {
    _invalidate();
    await widget.api.postJson('/api/write-mode', {'enabled': value});
    await _refreshStatus();
    if (mounted) {
      setState(() => _message = value ? '已允许修改选区，每次应用仍需确认。' : '已切换为只读。');
    }
  });

  Future<void> _addMode() => _run(() async {
    final name = _modeName.text.trim();
    if (name.isEmpty) throw const ApiException('请填写声库面板中实际存在的声线原名。');
    _invalidate();
    final result = Map<String, dynamic>.from(
      await widget.api.postJson('/api/parameters/vocal-mode', {
        'name': name,
        'selection': _selection,
      }) as Map,
    );
    if (!mounted) return;
    setState(() {
      _selection = result;
      _parameter = 'vocalMode_$name';
      _resetDraft();
      _modeName.clear();
      _message = '声线已加入本次桥接目录。';
    });
  });

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final busy = widget.api.busy;
      final theme = Theme.of(context);
      final catalog = _catalog;
      final selected = catalog.containsKey(_parameter) ? _parameter : null;
      final validSelection = (_selection['noteCount'] as num? ?? 0) > 0;
      List<List<double>> draft;
      try {
        draft = _parsePoints();
      } catch (_) {
        draft = [];
      }
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          PageToolbar(
            title: '工程检查器',
            actions: [
              IconButton(
                tooltip: '刷新桥接状态',
                onPressed: busy ? null : _refreshStatus,
                icon: const Icon(Icons.refresh, size: 18),
              ),
            ],
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.all(12),
              children: [
                Row(
                  children: [
                    Icon(_connected ? Icons.link : Icons.link_off, size: 16),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        _connected ? 'SynthV 已连接' : 'SynthV 未连接',
                        style: theme.textTheme.bodyMedium,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                if (!_connected)
                  OutlinedButton.icon(
                    onPressed: widget.onConnection,
                    icon: const Icon(Icons.cable_outlined, size: 16),
                    label: const Text('诊断与修复连接'),
                  ),
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                  title: const Text('允许修改选区', style: TextStyle(fontSize: 12)),
                  value: _writeEnabled,
                  onChanged: busy || !_connected ? null : _setWrite,
                ),
                OutlinedButton.icon(
                  onPressed: busy || !_connected ? null : _readSelection,
                  icon: const Icon(Icons.select_all, size: 16),
                  label: const Text('读取当前选区'),
                ),
                if (validSelection)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Text(
                      '${_selection['noteCount']} 个音符 · ${_selection['startSeconds']}–${_selection['endSeconds']} 秒',
                      style: theme.textTheme.bodySmall,
                    ),
                  ),
                const SizedBox(height: 18),
                if (!validSelection)
                  Text(
                    _connected ? '读取选区后显示可编辑参数。' : '连接后可读取选区和声库参数。',
                    style: theme.textTheme.bodySmall,
                  ),
                if (validSelection) ...[
                  Text('参数编辑', style: theme.textTheme.titleSmall),
                  const SizedBox(height: 10),
                  DropdownButtonFormField<String>(
                    key: ValueKey('parameter:$selected'),
                    initialValue: selected,
                    isExpanded: true,
                    decoration: const InputDecoration(labelText: '参数 / 声线'),
                    items: [
                      for (final entry in catalog.entries)
                        DropdownMenuItem(
                          value: entry.key,
                          child: Text(
                            '${entry.value['label'] ?? entry.key}',
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: busy
                        ? null
                        : (value) {
                            _invalidate();
                            setState(() {
                              _parameter = value;
                              _resetDraft();
                            });
                          },
                  ),
                  if (selected != null) ...[
                    const SizedBox(height: 10),
                    Text(
                      _nativePitch
                          ? '绝对 MIDI · ${curveNumber(_bounds[0])}–${curveNumber(_bounds[1])}'
                          : '增量范围 ±${curveNumber(_bounds[1])} ${_definition['unit'] ?? ''}',
                      style: theme.textTheme.bodySmall,
                    ),
                    const SizedBox(height: 10),
                    if (_supportsCurves) ...[
                      DropdownButtonFormField<String>(
                        key: ValueKey('input:$_parameter:$_curve'),
                        initialValue: _nativePitch
                            ? 'curve'
                            : _curve
                            ? 'curve'
                            : 'delta',
                        decoration: const InputDecoration(labelText: '输入方式'),
                        items: [
                          if (!_nativePitch)
                            const DropdownMenuItem(
                              value: 'delta',
                              child: Text('整体增量'),
                            ),
                          const DropdownMenuItem(
                            value: 'curve',
                            child: Text('绘制曲线'),
                          ),
                        ],
                        onChanged: busy || _nativePitch
                            ? null
                            : (value) {
                                _invalidate();
                                setState(() => _curve = value == 'curve');
                              },
                      ),
                      const SizedBox(height: 10),
                      DropdownButtonFormField<String>(
                        key: ValueKey('render:$_parameter:$_renderMode'),
                        initialValue: _renderMode,
                        decoration: const InputDecoration(labelText: '宿主表示方式'),
                        items: const [
                          DropdownMenuItem(
                            value: 'smooth',
                            child: Text('绘制模式 · 精简曲线'),
                          ),
                          DropdownMenuItem(
                            value: 'points',
                            child: Text('控制点模式'),
                          ),
                        ],
                        onChanged: busy || _nativePitch
                            ? null
                            : (value) {
                                _invalidate();
                                setState(() => _renderMode = value!);
                              },
                      ),
                    ],
                    const SizedBox(height: 12),
                    if (_curve || _nativePitch) ...[
                      if (draft.isNotEmpty)
                        SizedBox(
                          height: 145,
                          child: _DraftPad(
                            points: draft,
                            bounds: _bounds,
                            enabled: !busy,
                            onChanged: (points) {
                              _invalidate();
                              setState(
                                () => _points.text = points
                                    .map(
                                      (p) =>
                                          '${p[0].toStringAsFixed(6)} ${p[1].toStringAsFixed(6)}',
                                    )
                                    .join('\n'),
                              );
                            },
                          ),
                        ),
                      const SizedBox(height: 8),
                      Text(
                        _nativePitch
                            ? '拖动绘制绝对 MIDI 音高；原生曲线会替换该段生成音高。首尾覆盖选区，手绘自动精简。'
                            : '拖动绘制增量；首尾保持覆盖选区，手绘自动精简。',
                        style: theme.textTheme.bodySmall,
                      ),
                      const SizedBox(height: 8),
                      ExpansionTile(
                        key: ValueKey('nodes:$_parameter'),
                        tilePadding: EdgeInsets.zero,
                        title: Text(
                          '精确节点 · ${draft.length} / 64',
                          style: theme.textTheme.bodySmall,
                        ),
                        children: [
                          TextField(
                            controller: _points,
                            enabled: !busy,
                            minLines: 3,
                            maxLines: 7,
                            style: const TextStyle(
                              fontFamily: 'Consolas',
                              fontSize: 11,
                            ),
                            decoration: const InputDecoration(
                              labelText: '曲线节点 · 每行 位置 数值',
                              alignLabelWithHint: true,
                            ),
                            onChanged: (_) => _invalidate(),
                          ),
                        ],
                      ),
                    ] else
                      TextField(
                        controller: _delta,
                        enabled: !busy,
                        decoration: const InputDecoration(labelText: '调整增量'),
                        onChanged: (_) => _invalidate(),
                      ),
                    const SizedBox(height: 12),
                    FilledButton.icon(
                      onPressed: busy || !_connected || !validSelection
                          ? null
                          : _previewChange,
                      icon: const Icon(Icons.visibility_outlined, size: 16),
                      label: const Text('生成宿主预览'),
                    ),
                  ],
                  if (_supportsCurves) ...[
                    const SizedBox(height: 12),
                    ExpansionTile(
                      tilePadding: EdgeInsets.zero,
                      title: const Text(
                        '补充声库专属参数',
                        style: TextStyle(fontSize: 12),
                      ),
                      children: [
                        TextField(
                          controller: _modeName,
                          enabled: !busy,
                          decoration: const InputDecoration(
                            labelText: '声线面板原名，如 Airy',
                          ),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          '请核对当前声库面板，补充目录不会修改工程。',
                          style: theme.textTheme.bodySmall,
                        ),
                        Align(
                          alignment: Alignment.centerRight,
                          child: TextButton(
                            onPressed: busy ? null : _addMode,
                            child: const Text('补充参数'),
                          ),
                        ),
                      ],
                    ),
                  ],
                  if (_preview.isNotEmpty) ...[
                    const Divider(height: 28),
                    Text('宿主实际预览', style: theme.textTheme.titleSmall),
                    const SizedBox(height: 10),
                    CurvePreview(previews: [_preview]),
                    const SizedBox(height: 8),
                    Text(
                      '原有 ${_preview['beforePointCount'] ?? '—'} 点 → ${_preview['pointCount'] ?? '—'} 点，精简 ${_preview['pointReduction'] ?? '—'} 点',
                      style: theme.textTheme.bodySmall,
                    ),
                    if (_preview['capabilityWarnings'] is List)
                      for (final warning
                          in (_preview['capabilityWarnings'] as List)
                              .whereType<String>())
                        Padding(
                          padding: const EdgeInsets.only(top: 6),
                          child: Text(
                            warning,
                            style: theme.textTheme.bodySmall,
                          ),
                        ),
                    const SizedBox(height: 10),
                    FilledButton.icon(
                      onPressed:
                          busy ||
                              !_writeEnabled ||
                              _previewEpoch != widget.api.previewEpoch
                          ? null
                          : _applyChange,
                      icon: const Icon(Icons.check, size: 16),
                      label: const Text('确认应用此预览'),
                    ),
                  ],
                  const SizedBox(height: 16),
                  OutlinedButton.icon(
                    onPressed: busy || !_connected || !_writeEnabled
                        ? null
                        : _restore,
                    icon: const Icon(Icons.undo, size: 16),
                    label: const Text('恢复最近一次修改'),
                  ),
                ],
                if (_message != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 14),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(
                          _error ? Icons.error_outline : Icons.info_outline,
                          size: 15,
                        ),
                        const SizedBox(width: 6),
                        Expanded(
                          child: SelectableText(
                            _message!,
                            style: theme.textTheme.bodySmall,
                          ),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ],
      );
    },
  );
}

/// 手绘只编辑本地草稿，不请求宿主。拖动结束后精简近似共线点，保留范围外草稿。
class _DraftPad extends StatefulWidget {
  const _DraftPad({
    required this.points,
    required this.bounds,
    required this.enabled,
    required this.onChanged,
  });
  final List<List<double>> points;
  final List<double> bounds;
  final bool enabled;
  final ValueChanged<List<List<double>>> onChanged;
  @override
  State<_DraftPad> createState() => _DraftPadState();
}

class _DraftPadState extends State<_DraftPad> {
  final List<List<double>> _stroke = [];
  double _width = 1;

  void _record(Offset position) {
    final x = ((position.dx - 6) / math.max(1, _width - 12)).clamp(0.0, 1.0);
    final y =
        widget.bounds[1] -
        ((position.dy - 8) / 129).clamp(0.0, 1.0) *
            (widget.bounds[1] - widget.bounds[0]);
    setState(() {
      _stroke.removeWhere((point) => (point[0] - x).abs() < .005);
      _stroke.add([x, y]);
    });
  }

  List<List<double>> _simplify(List<List<double>> points) {
    if (points.length <= 2) return points;
    final first = points.first, last = points.last;
    double maximum = 0;
    var index = 0;
    for (var i = 1; i < points.length - 1; i++) {
      final expected =
          first[1] +
          (last[1] - first[1]) *
              (points[i][0] - first[0]) /
              math.max(1e-12, last[0] - first[0]);
      final error = (points[i][1] - expected).abs();
      if (error > maximum) {
        maximum = error;
        index = i;
      }
    }
    if (maximum <= (widget.bounds[1] - widget.bounds[0]) * .005) {
      return [first, last];
    }
    return [
      ..._simplify(points.sublist(0, index + 1))
          .takeWhile((p) => !identical(p, points[index])),
      ..._simplify(points.sublist(index)),
    ];
  }

  void _finish() {
    if (_stroke.isEmpty) return;
    _stroke.sort((a, b) => a[0].compareTo(b[0]));
    final merged = [
      ...widget.points.where(
        (p) => p[0] < _stroke.first[0] || p[0] > _stroke.last[0],
      ),
      ..._stroke,
    ]..sort((a, b) => a[0].compareTo(b[0]));
    if (merged.first[0] != 0) merged.insert(0, [0, merged.first[1]]);
    if (merged.last[0] != 1) merged.add([1, merged.last[1]]);
    final reduced = _simplify(merged);
    setState(() => _stroke.clear());
    if (reduced.length > 64) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('手绘复杂度超过 64 点，未替换草稿；请分段减少起伏。')),
      );
      return;
    }
    widget.onChanged(reduced);
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      _width = constraints.maxWidth;
      return DecoratedBox(
        decoration: BoxDecoration(
          border: Border.all(
            color: Theme.of(context).colorScheme.outlineVariant,
          ),
          borderRadius: BorderRadius.circular(8),
        ),
        child: MouseRegion(
          cursor: widget.enabled
              ? SystemMouseCursors.precise
              : SystemMouseCursors.basic,
          child: GestureDetector(
            onPanStart: widget.enabled
                ? (event) {
                    _stroke.clear();
                    _record(event.localPosition);
                  }
                : null,
            onPanUpdate: widget.enabled
                ? (event) => _record(event.localPosition)
                : null,
            onPanEnd: widget.enabled ? (_) => _finish() : null,
            onPanCancel: () => setState(() => _stroke.clear()),
            child: CustomPaint(
              size: Size.infinite,
              painter: _DraftPainter(
                points: _stroke.isEmpty
                    ? widget.points
                    : (_stroke.toList()..sort((a, b) => a[0].compareTo(b[0]))),
                bounds: widget.bounds,
                color: Theme.of(context).colorScheme.onSurface,
              ),
            ),
          ),
        ),
      );
    },
  );
}

class _DraftPainter extends CustomPainter {
  _DraftPainter({
    required this.points,
    required this.bounds,
    required this.color,
  });
  final List<List<double>> points;
  final List<double> bounds;
  final Color color;
  @override
  void paint(Canvas canvas, Size size) {
    final rect = Rect.fromLTRB(6, 8, size.width - 6, size.height - 8);
    for (var i = 1; i <= 3; i++) {
      canvas.drawLine(
        Offset(rect.left, rect.top + rect.height * i / 4),
        Offset(rect.right, rect.top + rect.height * i / 4),
        Paint()..color = color.withValues(alpha: .08),
      );
    }
    final path = Path();
    for (var i = 0; i < points.length; i++) {
      final x = rect.left + points[i][0] * rect.width;
      final y =
          rect.bottom -
          (points[i][1] - bounds[0]) / (bounds[1] - bounds[0]) * rect.height;
      if (i == 0) {
        path.moveTo(x, y);
      } else {
        path.lineTo(x, y);
      }
    }
    canvas.drawPath(
      path,
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.8,
    );
  }

  @override
  bool shouldRepaint(covariant _DraftPainter oldDelegate) => true;
}
