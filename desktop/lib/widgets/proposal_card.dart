import 'package:flutter/material.dart';

import '../core/api.dart';
import '../theme.dart';
import 'curve_preview.dart';
import 'workbench_expansion_tile.dart';

List<JsonMap> _objects(dynamic value) => value is List
    ? value
          .whereType<Map>()
          .map((item) => Map<String, dynamic>.from(item))
          .toList()
    : <JsonMap>[];

/// 一条助手回复对应一份方案。主动作随待预览、待确认、已应用三个阶段变化，
/// 不要求用户到另一个页面开启权限，也不把部分成功悄悄当成完整方案应用。
/// 预览票据仍受全局 previewEpoch 约束，历史快照只能查看，不能恢复确认资格。
class ProposalCard extends StatefulWidget {
  const ProposalCard({
    super.key,
    required this.api,
    required this.message,
    required this.conversationId,
    required this.onChanged,
  });
  final WorkbenchApi api;
  final JsonMap message;
  final String conversationId;
  final VoidCallback onChanged;
  @override
  State<ProposalCard> createState() => _ProposalCardState();
}

class _ProposalCardState extends State<ProposalCard> {
  List<JsonMap>? _updated;
  List<JsonMap> _errors = [];
  final Set<String> _excluded = {};
  String? _batchId, _feedback, _operation;
  int _epoch = -1;
  bool _failed = false, _restoreUncertain = false;
  List<JsonMap> get _actions => _updated ?? _objects(widget.message['actions']);
  List<JsonMap> get _included =>
      _actions.where((a) => !_excluded.contains(a['id'])).toList();
  bool get _unknown =>
      _restoreUncertain || _actions.any((a) => a['status'] == 'unknown');
  bool get _ready =>
      !_unknown &&
      _errors.isEmpty &&
      _batchId != null &&
      _epoch == widget.api.previewEpoch &&
      _included.isNotEmpty &&
      _included.every(
        (a) => a['status'] == 'previewed' && a['previewBatchId'] == _batchId,
      );

  /// 撤销按已应用批次绑定，不能把历史卡片的按钮接到“撤销最后一次全局修改”。
  String? get _appliedBatch {
    if (_unknown) return null;
    final applied = _actions.where((a) => a['status'] == 'applied').toList();
    if (applied.isEmpty ||
        applied.any(
          (a) => a['result'] is! Map || a['result']['verified'] != true,
        )) {
      return null;
    }
    final ids = applied.map((a) => a['previewBatchId']).toSet();
    return ids.length == 1 &&
            ids.single is String &&
            RegExp(r'^[0-9a-f]{32}$').hasMatch(ids.single as String)
        ? ids.single as String
        : null;
  }

  String _label(JsonMap action) {
    // 两种音高修改语义在预览前即明确展示，不能只依赖相似的后端标签或折叠说明。
    if (action['parameter'] == 'pitchCurve') return '原生音高（替换）';
    if (action['parameter'] == 'pitchDelta') return '音高偏差（叠加）';
    final preview = action['preview'] is Map
        ? action['preview'] as Map
        : const {};
    const names = {
      'breathiness': '气声',
      'tension': '张力',
      'gender': '性别',
      'loudness': '响度',
      'toneShift': '音色偏移',
      'vibratoEnv': '颤音包络',
      'voicing': '浊音 / 清音',
    };
    return '${preview['label'] ?? action['label'] ?? names[action['parameter']] ?? action['parameter']}';
  }

  @override
  void didUpdateWidget(covariant ProposalCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.message['id'] != oldWidget.message['id']) {
      _updated = null;
      _batchId = null;
      _errors = [];
      _excluded.clear();
      _feedback = null;
      _restoreUncertain = false;
      _epoch = -1;
    }
  }

  Future<void> _run(String stage, Future<void> Function() operation) async {
    setState(() {
      _feedback = null;
      _failed = false;
      _operation = stage;
    });
    try {
      await widget.api.exclusive(operation);
    } catch (error) {
      if (mounted) {
        setState(() {
          _feedback = error.toString();
          _failed = true;
        });
      }
    } finally {
      if (mounted) setState(() => _operation = null);
    }
  }

  Future<void> _preview({List<String>? only}) => _run('正在预览方案', () async {
    if (_unknown) throw const ApiException('写入结果尚未确认，请检查 SynthV 后刷新方案状态。');
    final requested = only ?? _actions.map((a) => '${a['id']}').toList();
    if (requested.isEmpty) throw const ApiException('此方案没有可预览的参数。');
    widget.api.invalidatePreviews();
    final epoch = widget.api.previewEpoch;
    setState(() {
      _batchId = null;
      _errors = [];
      _excluded
        ..clear()
        ..addAll(
          _actions
              .map((a) => '${a['id']}')
              .where((id) => !requested.contains(id)),
        );
    });
    final response = Map<String, dynamic>.from(
      await widget.api.postJson('/api/assistant/batches/preview', {
        'actionIds': requested,
      }) as Map,
    );
    final returned = _objects(response['actions']);
    // 数量、唯一身份、状态及错误条目都需对应本次请求，不能从不完整结果拼出可写票据。
    if (returned.length != requested.length ||
        returned.map((a) => a['id']).toSet().length != requested.length ||
        returned.any(
          (a) =>
              !requested.contains(a['id']) ||
              ![
                'proposed',
                'previewed',
                'applied',
                'unknown',
              ].contains(a['status']),
        )) {
      throw const ApiException('预览结果不完整，请刷新方案状态；尚未修改工程。');
    }
    final errors = _objects(response['errors']);
    if (errors.any((e) => !requested.contains(e['actionId']))) {
      throw const ApiException('预览失败项与本次方案不一致，请刷新方案状态。');
    }
    final batchId = response['batchId'];
    final successful = returned
        .where(
          (a) => a['status'] == 'previewed' && a['previewBatchId'] == batchId,
        )
        .toList();
    if (successful.isNotEmpty &&
        (batchId is! String ||
            !RegExp(r'^[0-9a-f]{32}$').hasMatch(batchId) ||
            successful.any(
              (a) =>
                  a['preview'] is! Map ||
                  a['preview']['previewId'] is! String ||
                  errors.any((e) => e['actionId'] == a['id']),
            ))) {
      throw const ApiException('预览缺少确认信息，请重新预览；尚未修改工程。');
    }
    if (!mounted) return;
    final complete = successful.length == requested.length && errors.isEmpty;
    setState(() {
      _updated = _actions
          .map(
            (a) =>
                returned.firstWhere((r) => r['id'] == a['id'], orElse: () => a),
          )
          .toList();
      _errors = errors;
      _epoch = epoch;
      // 部分预览只供查看。用户必须显式排除失败项并重新生成完整子方案，才可确认。
      _batchId = complete ? batchId as String : null;
      _failed = !complete;
      _feedback = complete
          ? '预览已就绪，确认后应用。'
          : '${successful.length}/${requested.length} 项预览通过，尚未修改工程。请重试，或明确只保留通过的参数。';
    });
    widget.onChanged();
  });

  String _scope(List<JsonMap> actions) {
    final previews = [
      for (final a in actions)
        if (a['preview'] is Map) Map<String, dynamic>.from(a['preview'] as Map),
    ];
    if (previews.isEmpty || !compatiblePreviews(previews)) {
      return '范围以本次已确认的预览为准';
    }
    final first = previews.first;
    final start = first['startSeconds'], end = first['endSeconds'];
    final notes = first['notes'];
    return [
      if (start is num && end is num)
        '${curveNumber(start)}–${curveNumber(end)} 秒',
      if (notes is List) '${notes.length} 个音符',
    ].join(' · ');
  }

  /// 确认弹窗仅收集明确意图，关闭/取消绝不调用权限或写入接口。
  /// 权限是当前桥接开关，不能把持续开启的真实行为描述成自动失效的“单次许可”。
  Future<bool> _confirm(
    List<JsonMap> actions, {
    required bool restore,
    required bool writeEnabled,
  }) async {
    var allowed = writeEnabled;
    return await showDialog<bool>(
          context: context,
          builder: (dialogContext) => StatefulBuilder(
            builder: (context, update) => AlertDialog(
              title: Text(restore ? '撤销这次应用？' : '应用这份方案？'),
              content: SizedBox(
                width: 440,
                child: SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(actions.map(_label).join('、')),
                      const SizedBox(height: 8),
                      Text(_scope(actions)),
                      const SizedBox(height: 12),
                      Text(
                        restore
                            ? '将这些参数恢复到本次应用之前。若之后已有其他修改，会停止撤销。'
                            : '只写入上方预览中的参数。应用后可在本方案中撤销。',
                      ),
                      if (!restore &&
                          actions.any(
                            (a) => a['parameter'] == 'pitchCurve',
                          )) ...[
                        const SizedBox(height: 12),
                        const Text('原生音高将替换该范围的现有曲线，包括原有演唱细节。'),
                      ],
                      if (!writeEnabled) ...[
                        const SizedBox(height: 12),
                        CheckboxListTile(
                          contentPadding: EdgeInsets.zero,
                          controlAffinity: ListTileControlAffinity.leading,
                          title: const Text('允许修改选区'),
                          subtitle: const Text('开启后保持可写；可在“工程与参数”中关闭。'),
                          value: allowed,
                          onChanged: (value) =>
                              update(() => allowed = value == true),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.pop(dialogContext, false),
                  child: const Text('取消'),
                ),
                FilledButton(
                  onPressed: allowed
                      ? () => Navigator.pop(dialogContext, true)
                      : null,
                  child: Text(restore ? '确认撤销' : '确认应用'),
                ),
              ],
            ),
          ),
        ) ??
        false;
  }

  Future<void> _ensureWrite(bool enabled) async {
    if (enabled) return;
    await widget.api.postJson('/api/write-mode', {'enabled': true});
    final status = await widget.api.getJson('/api/status') as Map;
    if (status['writeEnabled'] != true) {
      throw const ApiException('未能开启修改权限，尚未提交参数修改。');
    }
  }

  Future<void> _apply() => _run('等待应用确认', () async {
    if (!_ready) throw const ApiException('预览已更新，请重新预览后确认。');
    final batchId = _batchId!, epoch = _epoch;
    final pending = _included.map((a) => '${a['id']}').toSet();
    final status = await widget.api.getJson('/api/status') as Map;
    if (!mounted ||
        !await _confirm(
          _included,
          restore: false,
          writeEnabled: status['writeEnabled'] == true,
        )) {
      return;
    }
    if (epoch != widget.api.previewEpoch || batchId != _batchId || !_ready) {
      throw const ApiException('确认期间预览已变化，请重新预览后应用。');
    }
    await _ensureWrite(status['writeEnabled'] == true);
    if (epoch != widget.api.previewEpoch || !_ready) {
      throw const ApiException('预览已变化，请重新预览后应用。');
    }
    // 在发送前锁定本批，传输中断不得通过再次点击重放写入。
    widget.api.invalidatePreviews();
    setState(() {
      _batchId = null;
      _operation = '正在应用方案';
      _updated = _actions
          .map(
            (a) => pending.contains(a['id']) ? {...a, 'status': 'unknown'} : a,
          )
          .toList();
    });
    final response = await widget.api.postJson('/api/assistant/batches/apply', {
      'batchId': batchId,
    }) as Map;
    final returned = _objects(response['actions']);
    final complete =
        returned.length == pending.length &&
        returned.map((a) => a['id']).toSet().length == pending.length &&
        returned.every(
          (a) =>
              pending.contains(a['id']) &&
              (a['status'] == 'unknown' ||
                  a['status'] == 'applied' &&
                      a['result'] is Map &&
                      a['result']['verified'] == true),
        );
    if (!mounted) return;
    setState(() {
      if (complete) {
        _updated = _actions
            .map(
              (a) => returned.firstWhere(
                (r) => r['id'] == a['id'],
                orElse: () => a,
              ),
            )
            .toList();
      }
      _failed = !complete || returned.any((a) => a['status'] != 'applied');
      _feedback = _failed
          ? '应用结果尚未确认，请在 SynthV 检查后刷新状态，不要重复应用。'
          : '已应用。在 SynthV 中试听；需要调整时可撤销本次应用。';
    });
    widget.onChanged();
  });

  Future<void> _restore() => _run('等待撤销确认', () async {
    final batchId = _appliedBatch;
    if (batchId == null) throw const ApiException('没有可确认的应用批次，请刷新方案状态。');
    final applied = _actions
        .where(
          (a) => a['status'] == 'applied' && a['previewBatchId'] == batchId,
        )
        .toList();
    final status = await widget.api.getJson('/api/status') as Map;
    if (!mounted ||
        !await _confirm(
          applied,
          restore: true,
          writeEnabled: status['writeEnabled'] == true,
        )) {
      return;
    }
    if (_appliedBatch != batchId) throw const ApiException('应用记录已变化，请刷新方案状态。');
    await _ensureWrite(status['writeEnabled'] == true);
    widget.api.invalidatePreviews();
    setState(() {
      _batchId = null;
      _restoreUncertain = true;
      _operation = '正在撤销本次应用';
    });
    try {
      final response = await widget.api.postJson(
        '/api/assistant/batches/restore',
        {'batchId': batchId},
      ) as Map;
      final returned = _objects(response['actions']);
      final expected = applied.map((a) => a['id']).toSet();
      final verified = response['verified'] == true;
      final rejected = !verified && response['rejected'] == true;
      if (response['batchId'] != batchId ||
          returned.length != expected.length ||
          returned.map((a) => a['id']).toSet().length != expected.length ||
          returned.any(
            (a) =>
                !expected.contains(a['id']) ||
                (verified
                    ? a['status'] != 'proposed'
                    : rejected
                    ? a['status'] != 'applied' ||
                          a['result'] is! Map ||
                          a['result']['verified'] != true
                    : a['status'] != 'unknown'),
          )) {
        throw const ApiException(
          '撤销结果尚未确认，请检查 SynthV 后刷新方案状态。',
          uncertain: true,
        );
      }
      if (!mounted) return;
      setState(() {
        _updated = _actions
            .map(
              (a) => returned.firstWhere(
                (r) => r['id'] == a['id'],
                orElse: () => a,
              ),
            )
            .toList();
        // 宿主明确拒绝的批次仍为已应用；中途失败/无法核实的批次保持 unknown。
        // 两者都不能用一条成功文案掩盖，只有完整恢复后才重新开放预览。
        _restoreUncertain = !verified && !rejected;
        _failed = !verified;
        if (verified) {
          _errors = [];
          _excluded.clear();
        }
        _feedback = verified
            ? '已撤销。可重新预览这份方案。'
            : '${response['message'] ?? '撤销结果尚未确认，请检查 SynthV 后刷新方案状态。'}';
      });
      widget.onChanged();
    } catch (error) {
      // 明确拒绝表示未执行撤销，保留已应用状态；传输未知必须先刷新，不能盲目重放。
      if (mounted && error is ApiException && !error.uncertain) {
        setState(() => _restoreUncertain = false);
      }
      rethrow;
    }
  });

  Future<void> _refresh() => _run('正在刷新方案状态', () async {
    final document = await widget.api.getJson(
      '/api/conversations/${widget.conversationId}',
    ) as Map;
    final message = _objects(document['messages'])
        .where((m) => m['id'] == widget.message['id'])
        .firstOrNull;
    if (message == null) throw const ApiException('未找到这份方案，请重新打开会话。');
    if (!mounted) return;
    setState(() {
      _updated = _objects(message['actions']);
      _batchId = null;
      _errors = [];
      _excluded.clear();
      _restoreUncertain = false;
      _feedback = '状态已刷新。历史预览需要重新预览后才能应用。';
    });
    widget.onChanged();
  });

  Future<void> _reuse() => _run('正在复用到当前选区', () async {
    if (_unknown) throw const ApiException('先确认当前写入结果，再复用方案。');
    widget.api.invalidatePreviews();
    setState(() => _batchId = null);
    await widget.api.postJson(
      '/api/conversations/${widget.conversationId}/reuse',
      {'messageId': widget.message['id']},
    );
    if (!mounted) return;
    setState(() => _feedback = '已为当前选区创建新方案，请查看会话中的最新方案。');
    widget.onChanged();
  });

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final actions = _actions;
      if (actions.isEmpty) return const SizedBox.shrink();
      final previews = [
        for (final a in _included)
          if (a['preview'] is Map)
            Map<String, dynamic>.from(a['preview'] as Map),
      ];
      final passed = _included
          .where(
            (a) =>
                a['status'] == 'previewed' &&
                a['preview'] is Map &&
                !_errors.any((e) => e['actionId'] == a['id']),
          )
          .map((a) => '${a['id']}')
          .toList();
      final partial = _errors.isNotEmpty;
      final applied = _appliedBatch != null;
      final busy = widget.api.busy;
      final theme = Theme.of(context);
      final title = _unknown
          ? '需要核对结果'
          : applied
          ? '已应用'
          : _ready
          ? '待确认应用'
          : partial
          ? '部分预览未通过'
          : '待预览';
      final warnings = <String>{
        for (final p in previews)
          if (p['capabilityWarnings'] is List)
            ...(p['capabilityWarnings'] as List).whereType<String>(),
      };
      return Container(
        margin: const EdgeInsets.only(top: 14),
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          border: Border.all(color: theme.colorScheme.outlineVariant),
          borderRadius: BorderRadius.circular(WorkbenchTheme.radius),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                const Icon(Icons.tune, size: 18),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '参数方案 · ${_included.length} 项',
                    style: theme.textTheme.titleSmall,
                  ),
                ),
                Text(title, style: theme.textTheme.bodySmall),
                PopupMenuButton<String>(
                  tooltip: '方案操作',
                  enabled: !busy,
                  onSelected: (value) {
                    switch (value) {
                      case 'preview':
                        _preview();
                      case 'reuse':
                        _reuse();
                      case 'refresh':
                        _refresh();
                    }
                  },
                  itemBuilder: (_) => [
                    if (!_unknown)
                      const PopupMenuItem(
                        value: 'preview',
                        child: Text('重新预览全部参数'),
                      ),
                    if (!_unknown)
                      const PopupMenuItem(
                        value: 'reuse',
                        child: Text('复用到当前选区'),
                      ),
                    const PopupMenuItem(
                      value: 'refresh',
                      child: Text('刷新方案状态'),
                    ),
                  ],
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(_included.map(_label).join(' · ')),
            if (_excluded.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  '本次已排除：${actions.where((a) => _excluded.contains(a['id'])).map(_label).join('、')}',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            if (previews.isNotEmpty) ...[
              const SizedBox(height: 12),
              if (!_ready && !applied)
                Text('上次预览 · 仅供查看', style: theme.textTheme.bodySmall),
              CurvePreview(previews: previews),
            ],
            for (final warning in warnings)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(warning, style: theme.textTheme.bodySmall),
              ),
            for (final error in _errors)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  '${_label(actions.firstWhere((a) => a['id'] == error['actionId'], orElse: () => {}))}：${error['message']}',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            if (_unknown)
              const Padding(
                padding: EdgeInsets.only(top: 10),
                child: Text('写入或撤销结果尚未确认。请先在 SynthV 检查，再刷新本方案状态。'),
              ),
            WorkbenchExpansionTile(
              storageId: 'proposal-details-${widget.message['id']}',
              title: Text('参数说明', style: theme.textTheme.bodySmall),
              children: [
                for (final action in actions) _actionDetail(context, action),
              ],
            ),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                if (_unknown)
                  OutlinedButton.icon(
                    onPressed: busy ? null : _refresh,
                    icon: const Icon(Icons.refresh, size: 16),
                    label: const Text('刷新方案状态'),
                  )
                else if (applied)
                  OutlinedButton.icon(
                    onPressed: busy ? null : _restore,
                    icon: const Icon(Icons.undo, size: 16),
                    label: const Text('撤销本次应用'),
                  )
                else
                  FilledButton.icon(
                    onPressed: busy
                        ? null
                        : _ready
                        ? _apply
                        : () => _preview(),
                    icon: Icon(
                      _ready ? Icons.check : Icons.visibility_outlined,
                      size: 16,
                    ),
                    label: Text(
                      _ready
                          ? '应用方案'
                          : partial
                          ? '重新预览全部'
                          : '预览方案',
                    ),
                  ),
                if (_ready)
                  TextButton(
                    onPressed: busy
                        ? null
                        : () => _preview(
                            only: _included.map((a) => '${a['id']}').toList(),
                          ),
                    child: const Text('重新预览'),
                  ),
                if (partial && passed.isNotEmpty && !_unknown)
                  TextButton(
                    onPressed: busy ? null : () => _preview(only: passed),
                    child: Text('仅预览通过的 ${passed.length} 项'),
                  ),
                if (_operation?.startsWith('正在') == true) ...[
                  const SizedBox(
                    width: 14,
                    height: 14,
                    child: CircularProgressIndicator(strokeWidth: 1.5),
                  ),
                  Text(_operation!, style: theme.textTheme.bodySmall),
                ],
              ],
            ),
            if (_feedback != null)
              Padding(
                padding: const EdgeInsets.only(top: 10),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(
                      _failed
                          ? Icons.error_outline
                          : Icons.check_circle_outline,
                      size: 15,
                    ),
                    const SizedBox(width: 6),
                    Expanded(
                      child: SelectableText(
                        _feedback!,
                        style: theme.textTheme.bodySmall,
                      ),
                    ),
                  ],
                ),
              ),
            if (_batchId != null && !_ready)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  '预览已更新，请重新预览后应用。',
                  style: theme.textTheme.bodySmall,
                ),
              ),
          ],
        ),
      );
    },
  );

  Widget _actionDetail(BuildContext context, JsonMap action) {
    const states = {
      'proposed': '待预览',
      'previewed': '已预览',
      'applied': '已应用',
      'unknown': '结果未知',
    };
    final label = _label(action);
    final excluded = _excluded.contains(action['id']);
    final delta = action['delta'];
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '$label · ${excluded ? '本次排除' : states[action['status']] ?? '未知状态'}',
          ),
          if ((action['reason']?.toString() ?? '').isNotEmpty)
            SelectableText(
              '${action['reason']}',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          if (delta is num)
            Text(
              '整体增量：${delta > 0 ? '+' : ''}${curveNumber(delta)} ${action['unit'] ?? ''}',
              style: Theme.of(context).textTheme.bodySmall,
            )
          else if (action['parameter'] == 'pitchCurve')
            Text('替换选区内的原生音高曲线', style: Theme.of(context).textTheme.bodySmall)
          else
            Text(
              '按曲线调整，保留原参数作为基线',
              style: Theme.of(context).textTheme.bodySmall,
            ),
        ],
      ),
    );
  }
}
