import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../core/api.dart';
import '../widgets/curve_preview.dart';
import '../widgets/parameter_draft_editor.dart';
import '../widgets/workbench_dropdown.dart';
import '../widgets/workbench_expansion_tile.dart';
import 'page_chrome.dart';

/// 独立工程与参数页只在用户点击读取时取得选区，离开页面后保留本地草稿。
/// active 只控制状态轮询，不读取完整工程、模型配置或音频。
class Inspector extends StatefulWidget {
  const Inspector({
    super.key,
    required this.api,
    this.onConnection,
    this.active = true,
  });
  final WorkbenchApi api;
  final VoidCallback? onConnection;
  final bool active;
  @override
  State<Inspector> createState() => _InspectorState();
}

class _InspectorState extends State<Inspector> {
  JsonMap _status = {}, _project = {}, _selection = {}, _preview = {};
  String? _parameter, _message;
  String _renderMode = 'smooth';
  bool _curve = false, _refreshing = false;
  bool _confirming = false;
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
    if (widget.active) _refreshStatus();
    _timer = Timer.periodic(const Duration(seconds: 4), (_) {
      if (widget.active && !widget.api.busy) _refreshStatus();
    });
  }

  @override
  void didUpdateWidget(covariant Inspector oldWidget) {
    super.didUpdateWidget(oldWidget);
    // 重入仅检查桥接身份；参数草稿继续保留，实际选区仍由用户显式读取。
    if (!oldWidget.active && widget.active) _refreshStatus();
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

  Future<void> _applyChange() async {
    if (_confirming ||
        _preview.isEmpty ||
        _previewEpoch != widget.api.previewEpoch) {
      return;
    }
    _confirming = true;
    final previewId = _preview['previewId'], epoch = _previewEpoch;
    final needsPermission = !_writeEnabled;
    var allowed = _writeEnabled;
    bool? confirmed;
    try {
      confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => StatefulBuilder(
          builder: (context, change) => AlertDialog(
            title: const Text('应用参数预览'),
            scrollable: true,
            content: SizedBox(
              width: 440,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    '${_preview['label'] ?? _definition['label'] ?? _parameter} · $_scopeSummary',
                  ),
                  const SizedBox(height: 10),
                  Text(_nativePitch ? '将替换此范围内的原生演唱音高。' : '将按预览中的结果调整此范围内的参数。'),
                  if (needsPermission) ...[
                    const SizedBox(height: 12),
                    CheckboxListTile(
                      contentPadding: EdgeInsets.zero,
                      controlAffinity: ListTileControlAffinity.leading,
                      value: allowed,
                      title: const Text('允许修改当前选区'),
                      onChanged: (value) =>
                          change(() => allowed = value == true),
                    ),
                    Text(
                      '开启后保持可写，可在页面底部关闭。每次应用仍需确认。',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ],
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('取消'),
              ),
              FilledButton(
                onPressed: allowed ? () => Navigator.pop(context, true) : null,
                child: const Text('确定应用'),
              ),
            ],
          ),
        ),
      );
    } finally {
      _confirming = false;
    }
    if (confirmed != true || !mounted) return;
    await _run(() async {
      // 弹窗不持有操作锁。确认后先核对同一预览仍有效，再开启权限和提交，
      // 不能将用户对旧范围的确认用于后台刷新或其他页面生成的新票据。
      if (_preview['previewId'] != previewId ||
          epoch != widget.api.previewEpoch) {
        throw const ApiException('预览已变化，请重新生成并确认。');
      }
      if (!_writeEnabled) {
        await widget.api.postJson('/api/write-mode', {'enabled': true});
        await _refreshStatus();
        if (!_writeEnabled || !_connected || epoch != widget.api.previewEpoch) {
          throw const ApiException('连接或权限已变化，请重新生成预览。');
        }
      }
      _invalidate();
      final result = await widget.api.postJson('/api/apply', {
        'previewId': previewId,
      }) as Map;
      if (result['verified'] != true) {
        throw const ApiException('宿主未确认写入结果，请检查工程后再继续。');
      }
      if (mounted) {
        setState(
          () => _message =
              '${result['summary'] ?? result['message'] ?? '参数已应用。'}',
        );
      }
    });
  }

  Future<void> _restore() async {
    // 恢复记录属于宿主全局，可能来自会话页；不能暗示只撤销本页草稿或本项参数。
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('撤销最近一次宿主修改？'),
        scrollable: true,
        content: const Text('恢复助手最近一次已应用的修改，可能来自会话或工程与参数页。仅在宿主确认没有后续冲突编辑时执行。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('确认撤销'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    await _run(() async {
      _invalidate();
      final result = await widget.api.postJson('/api/restore') as Map;
      if (result['verified'] != true) {
        throw const ApiException('宿主未确认恢复结果，请检查工程。');
      }
      if (mounted) {
        setState(
          () => _message =
              '${result['summary'] ?? result['message'] ?? '最近一次修改已撤销，可重新生成预览。'}',
        );
      }
    });
  }

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

  /// 手动调参的选区摘要与当前草稿分开：预览前显示读取范围，预览后以宿主结果为准。
  String get _scopeSummary {
    final source = _preview.isEmpty ? _selection : _preview;
    final start = source['startSeconds'], end = source['endSeconds'];
    if (start is! num || end is! num) return '尚未读取选区';
    return '${_selection['noteCount'] ?? 0} 个音符 · ${curveNumber(start)}–${curveNumber(end)} 秒';
  }

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
      Widget overview() => PageSection(
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
            _property('组移调', '${_selection['groupPitchOffset'] ?? 0} 半音'),
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
                                ((_selection['groupPitchOffset'] as num?) ?? 0)
                          : null;
                      return Padding(
                        padding: const EdgeInsets.symmetric(vertical: 5),
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
      );

      Widget controls() => PageSection(
        title: '参数编辑',
        children: [
          _choice(
            '参数分类',
            _category,
            const [
              DropdownMenuItem(value: 'automation', child: Text('通用参数')),
              DropdownMenuItem(value: 'vocalMode', child: Text('声库声线')),
              DropdownMenuItem(value: 'pitch', child: Text('原生音高')),
            ],
            busy
                ? null
                : (value) {
                    setState(() => _category = value!);
                    _chooseParameter(_categoryCatalog.keys.firstOrNull);
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
                      label: Text('${entry.value['modeName'] ?? entry.key}'),
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
              _property('宿主范围', (_definition['range'] as List).join(' ～ ')),
            if (currentValues is Map &&
                currentValues['minimum'] is num &&
                currentValues['maximum'] is num)
              _property(
                '当前采样',
                '${curveNumber(currentValues['minimum'])} ～ ${curveNumber(currentValues['maximum'])} ${_definition['unit'] ?? ''}',
              ),
            if (voiceValues is Map && voiceValues.isNotEmpty) ...[
              Text('组声线设置（只读，不是自动化曲线）', style: theme.textTheme.bodySmall),
              for (final component in [
                ('pitch', '音高'),
                ('timbre', '音色'),
                ('pronunciation', '发音'),
              ])
                if (voiceValues[component.$1] is num)
                  _property(component.$2, '${voiceValues[component.$1]}%'),
            ],
            if (_supportsCurves) ...[
              _choice(
                '编辑方式',
                _nativePitch || _curve ? 'curve' : 'delta',
                [
                  if (!_nativePitch)
                    const DropdownMenuItem(value: 'delta', child: Text('整体增量')),
                  const DropdownMenuItem(value: 'curve', child: Text('曲线草稿')),
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
                  DropdownMenuItem(value: 'smooth', child: Text('绘制模式 · 精简曲线')),
                  DropdownMenuItem(value: 'points', child: Text('控制点模式')),
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
                  '原生重绘会替换选区内已有演唱音高。若只需轻微修饰，请使用通用参数中的音高偏移。',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            if (!(_curve || _nativePitch))
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
              title: Text('补充声线模式', style: theme.textTheme.bodySmall),
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
      );

      Widget draftEditor(bool wide) => PageSection(
        title: _nativePitch ? '音高重绘草稿' : '参数曲线草稿',
        children: [
          if (draft.isNotEmpty)
            ParameterDraftEditor(
              key: ValueKey('draft:$_parameter'),
              points: draft,
              bounds: _bounds,
              selection: _selection,
              parameter: _parameter!,
              enabled: !busy,
              canvasHeight: wide ? 340 : null,
              onChanged: _changeDraft,
            )
          else
            Text('草稿节点无效，请重置或在精确节点中修正。', style: theme.textTheme.bodySmall),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            children: [
              OutlinedButton.icon(
                onPressed: busy || _draftHistory.isEmpty ? null : _undoDraft,
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
              (_selection['computedPitch'] as Map?)?['status'] != 'ready')
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                (_selection['computedPitch'] as Map?)?['status'] == 'pending'
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
                style: const TextStyle(fontFamily: 'Consolas', fontSize: 11),
                decoration: const InputDecoration(
                  hintText: '0 0\n0.5 0.05\n1 0',
                ),
                onChanged: (_) => _invalidate(),
              ),
            ],
          ),
        ],
      );

      Widget hostPreview() => PageSection(
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
                in (_preview['capabilityWarnings'] as List).whereType<String>())
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(warning, style: theme.textTheme.bodySmall),
              ),
          WorkbenchExpansionTile(
            storageId: 'inspector:preview-details',
            tilePadding: EdgeInsets.zero,
            title: Text('修改明细', style: theme.textTheme.bodySmall),
            children: [
              _property('参数', _preview['label'] ?? _preview['parameter']),
              _property(
                '表示方式',
                _preview['representation'] ?? _preview['renderMode'],
              ),
              _property('宿主摘要', _preview['summary']),
              _property(
                '时间范围',
                '${_preview['startSeconds'] ?? '—'}–${_preview['endSeconds'] ?? '—'} 秒',
              ),
            ],
          ),
        ],
      );

      List<Widget> contextContent() => [
        overview(),
        if (validSelection) controls(),
        if ((_selection['capabilityWarnings'] as List? ?? []).isNotEmpty)
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
                  child: Text(warning, style: theme.textTheme.bodySmall),
                ),
            ],
          ),
      ];

      List<Widget> editorContent(bool wide) => [
        if (!validSelection)
          _emptyEditor(
            _connected ? '读取选区后开始编辑' : '连接 SynthV 后开始编辑',
            _connected
                ? '先在 SynthV 选择音符，再点击上方“读取当前选区”。读取工程只查看轨道概况。'
                : '前往 SynthV 连接完成桥接，再读取工程或选区。',
          )
        else ...[
          if (selected != null && (_curve || _nativePitch)) draftEditor(wide),
          if (selected != null && !(_curve || _nativePitch))
            PageSection(
              title: '整体调整',
              children: [
                Text('${_definition['label'] ?? selected} · $_scopeSummary'),
                const SizedBox(height: 8),
                Text(
                  '在参数区填写增量，然后生成预览。正值增加、负值减少；预览会显示实际修改后的曲线。',
                  style: theme.textTheme.bodySmall,
                ),
              ],
            ),
          if (_preview.isNotEmpty)
            hostPreview()
          else if (selected != null)
            _emptyEditor('尚未生成预览', '草稿只保存在工作台。生成预览后，检查修改范围和曲线，再确认应用。'),
        ],
      ];

      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          PageToolbar(
            title: '工程与参数',
            actions: [
              IconButton(
                tooltip: '刷新桥接状态',
                onPressed: busy ? null : _refreshStatus,
                icon: const Icon(Icons.refresh, size: 18),
              ),
            ],
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
            child: Wrap(
              spacing: 10,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                OutlinedButton.icon(
                  onPressed: busy || !_connected ? null : _readSelection,
                  icon: const Icon(Icons.select_all, size: 17),
                  label: const Text('读取当前选区'),
                ),
                OutlinedButton.icon(
                  onPressed: busy || !_connected ? null : _readProject,
                  icon: const Icon(Icons.account_tree_outlined, size: 17),
                  label: const Text('读取工程'),
                ),
                Text(
                  _connected
                      ? (validSelection ? _scopeSummary : 'SynthV 已连接')
                      : 'SynthV 未连接',
                  style: theme.textTheme.bodySmall,
                ),
                if (!_connected)
                  TextButton(
                    onPressed: widget.onConnection,
                    child: const Text('诊断与修复连接'),
                  ),
              ],
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: LayoutBuilder(
              builder: (context, bounds) {
                final wide = bounds.maxWidth >= 880;
                if (!wide) {
                  return _scrollPanel('single', const EdgeInsets.all(16), [
                    ...contextContent(),
                    ...editorContent(false),
                  ]);
                }
                // 属性区与曲线区独立滚动。画布不再被旧侧栏的固定窄宽度挤压。
                return Row(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    SizedBox(
                      width: 318,
                      child: _scrollPanel(
                        'properties',
                        const EdgeInsets.all(16),
                        contextContent(),
                      ),
                    ),
                    const VerticalDivider(width: 1),
                    Expanded(
                      child: _scrollPanel(
                        'editor',
                        const EdgeInsets.all(20),
                        editorContent(true),
                      ),
                    ),
                  ],
                );
              },
            ),
          ),
          if (_message != null)
            PageNotice(
              message: _message!,
              onDismiss: () => setState(() => _message = null),
            ),
          // 固定确认栏不依赖滚动位置；写入权限集中在具体应用确认中。
          PageFooter(
            child: LayoutBuilder(
              builder: (context, bounds) {
                final permission = _writeEnabled
                    ? TextButton.icon(
                        onPressed: busy ? null : () => _setWrite(false),
                        icon: const Icon(Icons.lock_open_outlined, size: 16),
                        label: const Text('已允许修改 · 关闭'),
                      )
                    : Text('只读 · 预览不会修改工程', style: theme.textTheme.bodySmall);
                final actions = Row(
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
                    const SizedBox(width: 8),
                    Expanded(
                      child: FilledButton(
                        onPressed: busy || !_connected || !validPreview
                            ? null
                            : _applyChange,
                        child: const Text('确认应用'),
                      ),
                    ),
                    const SizedBox(width: 4),
                    IconButton(
                      tooltip: '撤销最近一次宿主修改',
                      onPressed: busy || !_connected || !_writeEnabled
                          ? null
                          : _restore,
                      icon: const Icon(Icons.undo, size: 17),
                    ),
                  ],
                );
                if (bounds.maxWidth < 620) {
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    mainAxisSize: MainAxisSize.min,
                    children: [permission, const SizedBox(height: 6), actions],
                  );
                }
                return Row(
                  children: [
                    Expanded(child: permission),
                    SizedBox(width: 340, child: actions),
                  ],
                );
              },
            ),
          ),
        ],
      );
    },
  );

  /// 属性、草稿与预览数量有界，整体保留子树以便定位字段和恢复滚动；
  /// 大量音符明细仍使用独立的有界列表。展开状态由共享控件隔离存储。
  Widget _scrollPanel(String id, EdgeInsets padding, List<Widget> children) =>
      SingleChildScrollView(
        key: PageStorageKey('parameter-page:$id'),
        padding: padding,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: children,
        ),
      );

  /// 空状态在工作区说明下一步，不在所有输入框旁重复粘贴操作提示。
  Widget _emptyEditor(String title, String detail) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 20),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(title, style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        Text(detail, style: Theme.of(context).textTheme.bodySmall),
      ],
    ),
  );
}
