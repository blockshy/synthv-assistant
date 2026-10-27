import 'package:flutter/material.dart';

import '../core/api.dart';

/// 回收站恢复与永久删除沿用后端资源编号和确认契约，不推断文件路径。
class TrashPage extends StatefulWidget {
  const TrashPage({super.key, required this.api, required this.onChanged});
  final WorkbenchApi api;
  final VoidCallback onChanged;
  @override
  State<TrashPage> createState() => _TrashPageState();
}

class _TrashPageState extends State<TrashPage> {
  List<JsonMap> _items = [];
  bool _loading = true;
  JsonMap? _confirm;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (mounted) setState(() => _loading = true);
    try {
      final result = await widget.api.getJson('/api/trash');
      if (mounted) {
        setState(() {
          _items = (result['items'] as List)
              .map((item) => Map<String, dynamic>.from(item as Map))
              .toList();
          _error = null;
        });
      }
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _change(JsonMap item, bool purge) async {
    try {
      await widget.api.exclusive(
        () => widget.api.postJson(
          '/api/trash/${item['kind']}/${item['id']}/${purge ? 'purge' : 'restore'}',
          purge ? {'confirm': true} : {},
        ),
      );
      if (mounted) setState(() => _confirm = null);
      await _load();
      if (mounted) widget.onChanged();
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    }
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) => Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.all(24),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '回收站',
                      style: Theme.of(context).textTheme.headlineSmall,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      '恢复资料，或永久清理不再需要的内容。',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ],
                ),
              ),
              IconButton(
                tooltip: '刷新回收站',
                onPressed: _loading ? null : _load,
                icon: const Icon(Icons.refresh_rounded),
              ),
            ],
          ),
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 8),
            child: Text(_error!),
          ),
        Expanded(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : _items.isEmpty
              ? const Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(Icons.delete_outline_rounded, size: 36),
                      SizedBox(height: 12),
                      Text('回收站是空的'),
                    ],
                  ),
                )
              : ListView.separated(
                  padding: const EdgeInsets.fromLTRB(24, 0, 24, 24),
                  itemCount: _items.length,
                  separatorBuilder: (_, _) => const SizedBox(height: 8),
                  itemBuilder: (context, index) {
                    final item = _items[index];
                    final title =
                        ((item['title'] ?? '').toString().isNotEmpty
                                ? item['title']
                                : item['label'] ?? '未命名资料')
                            .toString();
                    return Card(
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Row(
                          children: [
                            Icon(
                              item['kind'] == 'conversation'
                                  ? Icons.chat_bubble_outline_rounded
                                  : Icons.audio_file_outlined,
                              size: 20,
                            ),
                            const SizedBox(width: 12),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    title,
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                  const SizedBox(height: 4),
                                  Text(
                                    item['purgePending'] == true
                                        ? '上次永久删除未完成，可重试清理'
                                        : item['kind'] == 'conversation'
                                        ? '会话'
                                        : '音频素材',
                                    style: Theme.of(context)
                                        .textTheme
                                        .bodySmall,
                                  ),
                                ],
                              ),
                            ),
                            IconButton(
                              tooltip: '恢复资料',
                              onPressed:
                                  widget.api.busy ||
                                      item['restorable'] == false ||
                                      item['permanent'] == true
                                  ? null
                                  : () => _change(item, false),
                              icon: const Icon(Icons.restore_rounded, size: 20),
                            ),
                            IconButton(
                              tooltip: '永久删除',
                              onPressed: widget.api.busy
                                  ? null
                                  : () => setState(() => _confirm = item),
                              icon: const Icon(
                                Icons.delete_forever_outlined,
                                size: 20,
                              ),
                            ),
                          ],
                        ),
                      ),
                    );
                  },
                ),
        ),
        if (_confirm != null)
          Container(
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              border: Border(
                top: BorderSide(color: Theme.of(context).dividerColor),
              ),
            ),
            child: Wrap(
              spacing: 12,
              runSpacing: 8,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                const Text('永久删除选中资料？此操作不能撤销。'),
                TextButton(
                  onPressed: widget.api.busy
                      ? null
                      : () => setState(() => _confirm = null),
                  child: const Text('取消'),
                ),
                FilledButton(
                  onPressed: widget.api.busy
                      ? null
                      : () => _change(_confirm!, true),
                  child: const Text('确认永久删除'),
                ),
              ],
            ),
          ),
      ],
    ),
  );
}
