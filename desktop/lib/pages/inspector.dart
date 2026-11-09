import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../core/api.dart';
import '../widgets/curve_preview.dart';
import '../widgets/parameter_draft_editor.dart';
import '../widgets/workbench_dropdown.dart';
import '../widgets/workbench_expansion_tile.dart';
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
  JsonMap _status = {}, _project = {}, _selection = {}, _preview = {};
  String? _parameter, _message;
  String _renderMode = 'smooth';
  bool _curve = false, _refreshing = false;
  int _previewEpoch = -1;
  Timer? _timer;
  final _delta = TextEditingController(text: '0.05');
  final _points = TextEditingController(text: '0 0\n0.5 0.05\n1 0');
  final _modeName = TextEditingController();
  final List<String> _draftHistory = [];
  String _category = 'automation';

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
  String _kind(String parameter, JsonMap definition) =>
      parameter == 'pitchCurve'
      ? 'pitch'
      : definition['kind'] == 'vocalMode'
      ? 'vocalMode'
      : 'automation';
  Map<String, JsonMap> get _categoryCatalog => {
    for (final entry in _catalog.entries)
      if (_kind(entry.key, entry.value) == _category) entry.key: entry.value,
  };
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
        _project = {};
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
        _project = {};
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
    });
    try {
      await widget.api.exclusive(action);
    } catch (error) {
      if (mounted) {
        setState(() {
          _message = error.toString();
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
      if (_parameter != null) _category = _kind(_parameter!, _definition);
      _message = (result['noteCount'] as num? ?? 0) > 0
          ? '选区已读取。'
          : '请先在 SynthV 中选中音符，再读取选区。';
      _resetDraft();
      _draftHistory.clear();
    });
    await _refreshStatus();
  });

  /// 工程读取与选区读取分开；查看轨道概况不会自动读取完整音符或产生修改。
  Future<void> _readProject() => _run(() async {
    final result = Map<String, dynamic>.from(
      await widget.api.getJson('/api/project') as Map,
    );
    if (!mounted) return;
    final old = _selection['projectFile'];
    if (old != null && old != result['projectFile']) {
      _invalidate();
      _selection = {};
      _parameter = null;
    }
    setState(() {
      _project = result;
      _message = '工程概况已读取。';
    });
  });

  void _chooseParameter(String? value) {
    _invalidate();
    setState(() {
      _parameter = value;
      _draftHistory.clear();
      if (value != null) _category = _kind(value, _definition);
      _resetDraft();
    });
  }

  /// 草稿撤销与宿主恢复分离：撤销画笔操作只修改本地输入，不提交宿主请求。
  void _changeDraft(List<List<double>> points) {
    final original = _points.text;
    _invalidate();
    setState(() {
      _draftHistory.add(original);
      if (_draftHistory.length > 20) _draftHistory.removeAt(0);
      _points.text = points
          .map((p) => '${p[0].toStringAsFixed(8)} ${p[1].toStringAsFixed(6)}')
          .join('\n');
    });
  }

  void _undoDraft() {
    if (_draftHistory.isEmpty) return;
    _invalidate();
    setState(() => _points.text = _draftHistory.removeLast());
  }

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
            : _definition['kind'] == 'vocalMode'
            ? 5.0
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

  /// 展示字段使用后端实际读回值；缺项保留“—”，不从软件名称猜测版本或声库。
  Widget _property(String label, Object? value) => Padding(
    padding: const EdgeInsets.only(bottom: 7),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 68,
          child: Text(label, style: Theme.of(context).textTheme.bodySmall),
        ),
        Expanded(
          child: SelectableText(
            value == null || '$value'.isEmpty ? '—' : '$value',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ),
      ],
    ),
  );

  Widget _choice(
    String label,
    String? value,
    List<DropdownMenuItem<String>> items,
    ValueChanged<String?>? changed,
  ) => PageProperty(
    label: label,
    child: WorkbenchDropdown<String>(
      key: ValueKey('inspector-choice:$label'),
      value: value,
      items: items,
      onChanged: changed,
      hint: const Text('没有可用参数'),
    ),
  );

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final busy = widget.api.busy, theme = Theme.of(context);
      final validSelection = (_selection['noteCount'] as num? ?? 0) > 0;
      final projectFile =
          '${_selection['projectFile'] ?? _project['projectFile'] ?? ''}';
      final projectName = projectFile.split(RegExp(r'[\\/]')).last;
      final host =
          _selection['hostVersion'] ??
          _project['hostVersion'] ??
          (_status['bridge'] as Map?)?['hostVersion'];
      final notes = (_selection['notes'] as List? ?? [])
          .whereType<Map>()
          .toList();
      final currentValues = _definition['currentValues'];
      final voiceValues = _definition['voiceValues'];
      final available = _categoryCatalog;
      final selected = available.containsKey(_parameter) ? _parameter : null;
      final rawPitch = (_selection['parameters'] as Map?)?['pitchCurve'];
      List<List<double>> draft;
      try {
        draft = _parsePoints();
      } catch (_) {
        draft = [];
      }
      final validPreview =
          _preview.isNotEmpty && _previewEpoch == widget.api.previewEpoch;
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
              key: const PageStorageKey('inspector:viewport'),
              padding: const EdgeInsets.all(12),
              children: [
                Row(
                  children: [
                    Icon(_connected ? Icons.link : Icons.link_off, size: 16),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(_connected ? 'SynthV 已连接' : 'SynthV 未连接'),
                    ),
                  ],
                ),
                if (!_connected) ...[
                  const SizedBox(height: 8),
                  OutlinedButton.icon(
                    onPressed: widget.onConnection,
                    icon: const Icon(Icons.cable_outlined, size: 16),
                    label: const Text('诊断与修复连接'),
                  ),
                ],
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                  title: const Text('允许修改选区', style: TextStyle(fontSize: 12)),
                  value: _writeEnabled,
                  onChanged: busy || !_connected ? null : _setWrite,
                ),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton(
                        onPressed: busy || !_connected ? null : _readProject,
                        child: const Text('读取工程'),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: OutlinedButton(
                        onPressed: busy || !_connected ? null : _readSelection,
                        child: const Text('读取当前选区'),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 14),
                PageSection(
                  title: '工程与选区',
                  children: [
                    _property('工程', projectName.isEmpty ? '尚未读取' : projectName),
                    _property('宿主版本', host),
                    if (_project['trackCount'] != null)
                      _property('轨道数量', _project['trackCount']),
                    _property('音符组', _selection['groupName']),
                    _property(
                      '选中音符',
                      validSelection ? '${_selection['noteCount']} 个' : '尚未读取',
                    ),
                    if (validSelection) ...[
                      _property(
                        '时间范围',
                        '${curveNumber((_selection['startSeconds'] as num?) ?? 0)}–${curveNumber((_selection['endSeconds'] as num?) ?? 0)} 秒',
                      ),
                      _property(
                        '组移调',
                        '${_selection['groupPitchOffset'] ?? 0} 半音',
                      ),
                    ],
                    if (notes.isNotEmpty)
                      WorkbenchExpansionTile(
                        storageId: 'inspector:notes',
                        tilePadding: EdgeInsets.zero,
                        title: Text(
                          '音符明细 · ${notes.length}',
                          style: theme.textTheme.bodySmall,
                        ),
                        children: [
                          ConstrainedBox(
                            constraints: const BoxConstraints(maxHeight: 240),
                            child: ListView.builder(
                              shrinkWrap: true,
                              itemCount: notes.length,
                              itemBuilder: (context, index) {
                                final note = notes[index];
                                final pitch = note['pitch'] is num
                                    ? (note['pitch'] as num) +
                                          ((_selection['groupPitchOffset']
                                                  as num?) ??
                                              0)
                                    : null;
                                return Padding(
                                  padding: const EdgeInsets.symmetric(
                                    vertical: 5,
                                  ),
                                  child: Text(
                                    '${index + 1}. ${note['lyrics'] ?? ''}  ·  MIDI ${pitch ?? '—'}\n'
                                    '${note['onsetSeconds'] is num ? curveNumber(note['onsetSeconds']) : '—'} 秒  /  ${note['durationSeconds'] is num ? curveNumber(note['durationSeconds']) : '—'} 秒',
                                    style: theme.textTheme.bodySmall,
                                  ),
                                );
                              },
                            ),
                          ),
                        ],
                      ),
                    if (_project['tracks'] is List)
                      WorkbenchExpansionTile(
                        storageId: 'inspector:tracks',
                        tilePadding: EdgeInsets.zero,
                        title: Text('轨道概况', style: theme.textTheme.bodySmall),
                        children: [
                          for (final track
                              in (_project['tracks'] as List).whereType<Map>())
                            _property(
                              '${track['index'] ?? ''}',
                              '${track['name'] ?? '未命名轨道'} · ${(track['groups'] as List? ?? []).length} 组',
                            ),
                        ],
                      ),
                  ],
                ),
                if (!validSelection)
                  Text(
                    _connected
                        ? '在 SynthV 选中音符后读取，即可查看可编辑参数。'
                        : '连接后可读取选区与参数目录。',
                    style: theme.textTheme.bodySmall,
                  ),
                if (validSelection) ...[
                  PageSection(
                    title: '参数编辑',
                    children: [
                      _choice(
                        '参数分类',
                        _category,
                        const [
                          DropdownMenuItem(
                            value: 'automation',
                            child: Text('通用参数'),
                          ),
                          DropdownMenuItem(
                            value: 'vocalMode',
                            child: Text('声库声线'),
                          ),
                          DropdownMenuItem(value: 'pitch', child: Text('原生音高')),
                        ],
                        busy
                            ? null
                            : (value) {
                                setState(() => _category = value!);
                                _chooseParameter(
                                  _categoryCatalog.keys.firstOrNull,
                                );
                              },
                      ),
                      if (_category == 'vocalMode') ...[
                        Text(
                          '目录来自当前组设置与手动补充，可能不含声库所有模式。',
                          style: theme.textTheme.bodySmall,
                        ),
                        const SizedBox(height: 8),
                        if (available.isNotEmpty)
                          Wrap(
                            spacing: 6,
                            runSpacing: 6,
                            children: [
                              for (final entry in available.entries)
                                ActionChip(
                                  label: Text(
                                    '${entry.value['modeName'] ?? entry.key}',
                                  ),
                                  avatar: selected == entry.key
                                      ? const Icon(Icons.check, size: 14)
                                      : null,
                                  tooltip: entry.value['source'] == 'user'
                                      ? '用户依据声库面板补充'
                                      : '宿主已保存声线设置',
                                  onPressed: busy
                                      ? null
                                      : () => _chooseParameter(entry.key),
                                ),
                            ],
                          )
                        else
                          Text(
                            '尚未读取到声线。可按宿主面板原名补充，不预设 Airy / Chest / Cute。',
                            style: theme.textTheme.bodySmall,
                          ),
                        const SizedBox(height: 10),
                      ],
                      if (available.isNotEmpty)
                        _choice('调整参数', selected, [
                          for (final entry in available.entries)
                            DropdownMenuItem(
                              value: entry.key,
                              child: Text(
                                '${entry.value['label'] ?? entry.key}',
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                        ], busy ? null : _chooseParameter),
                      if (_category == 'pitch' && available.isEmpty)
                        Text(
                          rawPitch is Map
                              ? '${rawPitch['unavailableReason'] ?? '当前桥接未开放原生音高，请更新桥接并重新读取选区。'}'
                              : '当前桥接未返回原生音高能力，请更新桥接并重新读取选区。',
                          style: theme.textTheme.bodySmall,
                        ),
                      if (selected != null) ...[
                        _property(
                          '允许增量',
                          _nativePitch
                              ? '绝对 MIDI ${curveNumber(_bounds[0])}–${curveNumber(_bounds[1])}'
                              : '±${curveNumber(_bounds[1])} ${_definition['unit'] ?? ''}',
                        ),
                        if (_definition['range'] is List)
                          _property(
                            '宿主范围',
                            (_definition['range'] as List).join(' ～ '),
                          ),
                        if (currentValues is Map &&
                            currentValues['minimum'] is num &&
                            currentValues['maximum'] is num)
                          _property(
                            '当前采样',
                            '${curveNumber(currentValues['minimum'])} ～ ${curveNumber(currentValues['maximum'])} ${_definition['unit'] ?? ''}',
                          ),
                        if (voiceValues is Map && voiceValues.isNotEmpty) ...[
                          Text(
                            '组声线设置（只读，不是自动化曲线）',
                            style: theme.textTheme.bodySmall,
                          ),
                          for (final component in [
                            ('pitch', '音高'),
                            ('timbre', '音色'),
                            ('pronunciation', '发音'),
                          ])
                            if (voiceValues[component.$1] is num)
                              _property(
                                component.$2,
                                '${voiceValues[component.$1]}%',
                              ),
                        ],
                        if (_supportsCurves) ...[
                          _choice(
                            '编辑方式',
                            _nativePitch || _curve ? 'curve' : 'delta',
                            [
                              if (!_nativePitch)
                                const DropdownMenuItem(
                                  value: 'delta',
                                  child: Text('整体增量'),
                                ),
                              const DropdownMenuItem(
                                value: 'curve',
                                child: Text('曲线草稿'),
                              ),
                            ],
                            busy || _nativePitch
                                ? null
                                : (value) {
                                    _invalidate();
                                    setState(() => _curve = value == 'curve');
                                  },
                          ),
                          _choice(
                            '宿主表示',
                            _renderMode,
                            const [
                              DropdownMenuItem(
                                value: 'smooth',
                                child: Text('绘制模式 · 精简曲线'),
                              ),
                              DropdownMenuItem(
                                value: 'points',
                                child: Text('控制点模式'),
                              ),
                            ],
                            busy || _nativePitch
                                ? null
                                : (value) {
                                    _invalidate();
                                    setState(() => _renderMode = value!);
                                  },
                          ),
                        ],
                        if (_nativePitch)
                          Padding(
                            padding: const EdgeInsets.only(bottom: 8),
                            child: Text(
                              '原生音高通过 PitchControlCurve 连续曲线写入；会替换选区内生成音高，不是音分增量。',
                              style: theme.textTheme.bodySmall,
                            ),
                          ),
                        if (_curve || _nativePitch) ...[
                          if (draft.isNotEmpty)
                            ParameterDraftEditor(
                              key: ValueKey('draft:$_parameter'),
                              points: draft,
                              bounds: _bounds,
                              selection: _selection,
                              parameter: _parameter!,
                              enabled: !busy,
                              onChanged: _changeDraft,
                            )
                          else
                            Text(
                              '草稿节点无效，请重置或在精确节点中修正。',
                              style: theme.textTheme.bodySmall,
                            ),
                          const SizedBox(height: 8),
                          Wrap(
                            spacing: 8,
                            runSpacing: 6,
                            children: [
                              OutlinedButton.icon(
                                onPressed: busy || _draftHistory.isEmpty
                                    ? null
                                    : _undoDraft,
                                icon: const Icon(Icons.undo, size: 15),
                                label: const Text('撤销草稿'),
                              ),
                              OutlinedButton.icon(
                                onPressed: busy
                                    ? null
                                    : () {
                                        _invalidate();
                                        setState(() {
                                          _resetDraft();
                                          _draftHistory.clear();
                                        });
                                      },
                                icon: const Icon(Icons.restart_alt, size: 15),
                                label: Text(_nativePitch ? '按音符重置' : '重置草稿'),
                              ),
                            ],
                          ),
                          if (_nativePitch &&
                              (_selection['computedPitch']
                                      as Map?)?['status'] !=
                                  'ready')
                            Padding(
                              padding: const EdgeInsets.only(top: 8),
                              child: Text(
                                (_selection['computedPitch']
                                            as Map?)?['status'] ==
                                        'pending'
                                    ? '宿主尚在计算音高，完成合成后重新读取。'
                                    : '未取得宿主计算音高；图中的音符仅是乐谱参照。',
                                style: theme.textTheme.bodySmall,
                              ),
                            ),
                          WorkbenchExpansionTile(
                            storageId: 'inspector:nodes:$_parameter',
                            tilePadding: EdgeInsets.zero,
                            title: Text(
                              '精确节点 · ${draft.length}/64',
                              style: theme.textTheme.bodySmall,
                            ),
                            children: [
                              const Text(
                                '每行：位置比例 数值。首尾为 0、1，时间严格递增。',
                                style: TextStyle(fontSize: 11),
                              ),
                              const SizedBox(height: 6),
                              TextField(
                                key: const ValueKey('inspector-points'),
                                controller: _points,
                                enabled: !busy,
                                minLines: 3,
                                maxLines: 7,
                                style: const TextStyle(
                                  fontFamily: 'Consolas',
                                  fontSize: 11,
                                ),
                                decoration: const InputDecoration(
                                  hintText: '0 0\n0.5 0.05\n1 0',
                                ),
                                onChanged: (_) => _invalidate(),
                              ),
                            ],
                          ),
                        ] else
                          PageProperty(
                            label: '调整增量',
                            child: TextField(
                              key: const ValueKey('inspector-delta'),
                              controller: _delta,
                              enabled: !busy,
                              onChanged: (_) => _invalidate(),
                            ),
                          ),
                      ],
                      if (_supportsCurves && _category == 'vocalMode')
                        WorkbenchExpansionTile(
                          storageId: 'inspector:add-vocal-mode',
                          tilePadding: EdgeInsets.zero,
                          title: Text(
                            '补充声线模式',
                            style: theme.textTheme.bodySmall,
                          ),
                          children: [
                            TextField(
                              key: const ValueKey('inspector-vocal-mode-name'),
                              controller: _modeName,
                              enabled: !busy,
                              decoration: const InputDecoration(
                                hintText: '按 SynthV 面板原样填写，如 Airy',
                              ),
                            ),
                            const SizedBox(height: 6),
                            Text(
                              '仅扩展本次桥接目录，不修改工程，也不证明该声库确有此模式。',
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
                  ),
                  if ((_selection['capabilityWarnings'] as List? ?? [])
                      .isNotEmpty)
                    WorkbenchExpansionTile(
                      storageId: 'inspector:capabilities',
                      tilePadding: EdgeInsets.zero,
                      title: Text('能力与限制', style: theme.textTheme.bodySmall),
                      children: [
                        for (final warning
                            in (_selection['capabilityWarnings'] as List)
                                .whereType<String>())
                          Padding(
                            padding: const EdgeInsets.only(bottom: 8),
                            child: Text(
                              warning,
                              style: theme.textTheme.bodySmall,
                            ),
                          ),
                      ],
                    ),
                  if (_preview.isNotEmpty)
                    PageSection(
                      title: validPreview ? '宿主实际预览' : '预览已失效',
                      children: [
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
                        WorkbenchExpansionTile(
                          storageId: 'inspector:preview-details',
                          tilePadding: EdgeInsets.zero,
                          title: Text('修改明细', style: theme.textTheme.bodySmall),
                          children: [
                            _property(
                              '参数',
                              _preview['label'] ?? _preview['parameter'],
                            ),
                            _property(
                              '表示方式',
                              _preview['representation'] ??
                                  _preview['renderMode'],
                            ),
                            _property('宿主摘要', _preview['summary']),
                            _property(
                              '时间范围',
                              '${_preview['startSeconds'] ?? '—'}–${_preview['endSeconds'] ?? '—'} 秒',
                            ),
                          ],
                        ),
                      ],
                    ),
                ],
              ],
            ),
          ),
          if (_message != null)
            PageNotice(
              message: _message!,
              onDismiss: () => setState(() => _message = null),
            ),
          // 应用与恢复始终可见，不要求用户滚过整段音符、参数和曲线后寻找确认入口。
          PageFooter(
            child: Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed:
                        busy ||
                            !_connected ||
                            !validSelection ||
                            selected == null
                        ? null
                        : _previewChange,
                    child: const Text('生成预览'),
                  ),
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: FilledButton(
                    onPressed: busy || !_writeEnabled || !validPreview
                        ? null
                        : _applyChange,
                    child: const Text('确认应用'),
                  ),
                ),
                const SizedBox(width: 4),
                IconButton(
                  tooltip: '恢复最近一次宿主修改',
                  onPressed: busy || !_connected || !_writeEnabled
                      ? null
                      : _restore,
                  icon: const Icon(Icons.undo, size: 17),
                ),
              ],
            ),
          ),
        ],
      );
    },
  );
}
