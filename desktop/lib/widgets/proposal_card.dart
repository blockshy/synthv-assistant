import 'package:flutter/material.dart';

import '../core/api.dart';
import 'curve_preview.dart';

List<JsonMap> _objects(dynamic value) => value is List
    ? value
          .whereType<Map>()
          .map((item) => Map<String, dynamic>.from(item))
          .toList()
    : <JsonMap>[];

/// 每条消息独立呈现方案，但所有确认票据共享 api.previewEpoch。
/// 只有本次成功生成的批次可确认，历史数据中的 previewId 不自动恢复权限。
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
  String? _batchId;
  int _epoch = -1;
  String? _feedback;
  bool _failed = false;
  List<JsonMap> get _actions => _updated ?? _objects(widget.message['actions']);

  @override
  void didUpdateWidget(covariant ProposalCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.message['id'] != oldWidget.message['id']) {
      _updated = null;
      _batchId = null;
      _errors = [];
      _feedback = null;
    }
  }

  Future<void> _run(Future<void> Function() operation) async {
    setState(() {
      _feedback = null;
      _failed = false;
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
    }
  }

  Future<void> _preview() => _run(() async {
    if (_actions.any((action) => action['status'] == 'unknown')) {
      throw const ApiException('存在未确认的写入结果，请先检查工程；此方案不重复提交。');
    }
    final requested = _actions
        .where(
          (a) => ['proposed', 'previewed', 'applied'].contains(a['status']),
        )
        .map((a) => a['id'])
        .toList();
    if (requested.isEmpty) throw const ApiException('此方案没有可重新预览的参数。');
    widget.api.invalidatePreviews();
    final epoch = widget.api.previewEpoch;
    setState(() {
      _batchId = null;
      _errors = [];
      _feedback = '正在生成宿主预览，尚未修改工程…';
    });
    final response = Map<String, dynamic>.from(
      await widget.api.postJson('/api/assistant/batches/preview', {
        'actionIds': requested,
      }) as Map,
    );
    final returned = _objects(response['actions']);
    // 回复必须覆盖本次请求的动作集合；缺少任意成员都不保留确认资格。
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
      throw const ApiException('组合预览返回不完整，请刷新会话检查；未自动重试。');
    }
    final errors = _objects(response['errors']);
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
      throw const ApiException('宿主预览未返回完整确认凭据，请重新预览。');
    }
    if (!mounted) return;
    setState(() {
      _updated = returned;
      _errors = errors;
      _epoch = epoch;
      _batchId = successful.isEmpty ? null : batchId as String;
      _failed = errors.isNotEmpty || successful.isEmpty;
      _feedback =
          '已生成 ${successful.length} 项预览${errors.isEmpty ? '' : '，${errors.length} 项未通过'}。尚未修改工程。';
    });
    widget.onChanged();
  });

  Future<void> _apply() => _run(() async {
    if (_batchId == null || _epoch != widget.api.previewEpoch) {
      throw const ApiException('此预览已失效，请重新预览。');
    }
    final status = await widget.api.getJson('/api/status') as Map;
    if (status['writeEnabled'] != true) {
      throw const ApiException('请先在工程检查器开启“允许修改选区”，然后确认应用。');
    }
    final batchId = _batchId!;
    final pending = _actions
        .where(
          (action) =>
              action['previewBatchId'] == batchId &&
              action['status'] == 'previewed',
        )
        .map((action) => action['id'])
        .toSet();
    // 发出写请求之前立即销毁本地票据，网络中断或未知结果都不能重复点击。
    widget.api.invalidatePreviews();
    setState(() {
      _batchId = null;
      // 请求一旦发出即存在结果未知的可能；只锁定本批次，保留未通过预览的其他参数。
      _updated = _actions
          .map(
            (action) => pending.contains(action['id'])
                ? {...action, 'status': 'unknown'}
                : action,
          )
          .toList();
      _feedback = '正在应用已确认的参数…';
    });
    final response = await widget.api.postJson('/api/assistant/batches/apply', {
      'batchId': batchId,
    }) as Map;
    final returned = _objects(response['actions']);
    if (!mounted) return;
    final complete =
        returned.length == pending.length &&
        returned.map((action) => action['id']).toSet().length ==
            pending.length &&
        returned.every((action) => pending.contains(action['id']));
    setState(() {
      if (complete) {
        _updated = _actions
            .map(
              (action) => returned.firstWhere(
                (replacement) => replacement['id'] == action['id'],
                orElse: () => action,
              ),
            )
            .toList();
      }
      _failed = !complete || returned.any((a) => a['status'] != 'applied');
      _feedback = _failed
          ? '应用结果未全部确认，请检查 SynthV 工程；不要重复提交。'
          : '参数已应用。可录制新片段并对比试听。';
    });
    widget.onChanged();
  });

  Future<void> _reuse() => _run(() async {
    widget.api.invalidatePreviews();
    setState(() {
      _batchId = null;
      _feedback = '正在将方案复用到当前选区…';
    });
    await widget.api.postJson(
      '/api/conversations/${widget.conversationId}/reuse',
      {'messageId': widget.message['id']},
    );
    if (!mounted) return;
    setState(() => _feedback = '已创建当前选区的新方案，请在新消息中预览。');
    widget.onChanged();
  });

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final actions = _actions;
      if (actions.isEmpty) return const SizedBox.shrink();
      final previews = [
        for (final a in actions)
          if (a['preview'] is Map &&
              (_batchId == null || a['previewBatchId'] == _batchId))
            Map<String, dynamic>.from(a['preview'] as Map),
      ];
      final warnings = <String>{
        for (final preview in previews)
          if (preview['capabilityWarnings'] is List)
            ...(preview['capabilityWarnings'] as List).whereType<String>(),
      };
      final canPreview =
          !actions.any((a) => a['status'] == 'unknown') &&
          actions.any(
            (a) => ['proposed', 'previewed', 'applied'].contains(a['status']),
          );
      final canApply = _batchId != null && _epoch == widget.api.previewEpoch;
      final theme = Theme.of(context);
      return Container(
        margin: const EdgeInsets.only(top: 14),
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          border: Border.all(color: theme.colorScheme.outlineVariant),
          borderRadius: BorderRadius.circular(12),
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
                    '参数方案 · ${actions.length} 项',
                    style: theme.textTheme.titleSmall,
                  ),
                ),
                const Tooltip(
                  message: '先生成真实宿主预览，再确认应用。',
                  child: Icon(Icons.info_outline, size: 16),
                ),
              ],
            ),
            const SizedBox(height: 10),
            if (previews.isNotEmpty) ...[
              CurvePreview(previews: previews),
              const SizedBox(height: 12),
            ],
            // 音高覆盖等重要说明始终可见，不能藏在默认折叠的参数详情中。
            for (final warning in warnings)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(warning, style: theme.textTheme.bodySmall),
              ),
            for (final action in actions) _actionDetail(context, action),
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed: widget.api.busy || !canPreview ? null : _preview,
                  icon: const Icon(Icons.visibility_outlined, size: 16),
                  label: Text(previews.isEmpty ? '生成预览' : '重新预览'),
                ),
                FilledButton.icon(
                  onPressed: widget.api.busy || !canApply ? null : _apply,
                  icon: const Icon(Icons.check, size: 16),
                  label: const Text('确认应用'),
                ),
                TextButton.icon(
                  onPressed:
                      widget.api.busy ||
                          actions.any((a) => a['status'] == 'unknown')
                      ? null
                      : _reuse,
                  icon: const Icon(Icons.copy_all_outlined, size: 16),
                  label: const Text('复用到当前选区'),
                ),
              ],
            ),
            if (_feedback != null)
              Padding(
                padding: const EdgeInsets.only(top: 10),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(
                      _failed ? Icons.error_outline : Icons.info_outline,
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
            if (_batchId != null && !canApply)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  '其他操作已使确认资格失效，请重新预览。',
                  style: theme.textTheme.bodySmall,
                ),
              ),
          ],
        ),
      );
    },
  );

  Widget _actionDetail(BuildContext context, JsonMap action) {
    const statusNames = {
      'proposed': '待预览',
      'previewed': '已预览',
      'applied': '已应用',
      'unknown': '结果未知',
    };
    final preview = action['preview'] is Map
        ? action['preview'] as Map
        : const {};
    final label = '${preview['label'] ?? action['parameter']}';
    final detail = action['curve'] is List
        ? '${(action['curve'] as List).length} 个草稿点'
        : '增量 ${action['delta'] ?? '—'}';
    final error = _errors
        .where((e) => e['actionId'] == action['id'])
        .map((e) => e['message'])
        .join('\n');
    return ExpansionTile(
      key: ValueKey(action['id']),
      tilePadding: EdgeInsets.zero,
      childrenPadding: const EdgeInsets.only(bottom: 10),
      title: Text(
        '$label · ${statusNames[action['status']] ?? '未知状态'}',
        style: Theme.of(context).textTheme.bodyMedium,
      ),
      subtitle: Text(
        error.isNotEmpty ? error : detail,
        style: Theme.of(context).textTheme.bodySmall,
      ),
      children: [
        Align(
          alignment: Alignment.centerLeft,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SelectableText('${action['reason'] ?? ''}'),
              if (preview.isNotEmpty)
                Text(
                  '原有 ${preview['beforePointCount'] ?? '—'} 点 → 预览 ${preview['pointCount'] ?? '—'} 点；精简 ${preview['pointReduction'] ?? '—'} 点',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              if (action['curve'] is List)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: SelectableText(
                    (action['curve'] as List)
                        .map(
                          (point) =>
                              point is List ? point.join('  ') : '$point',
                        )
                        .join('\n'),
                    style: const TextStyle(
                      fontFamily: 'Consolas',
                      fontSize: 11,
                    ),
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}
