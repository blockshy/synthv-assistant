import 'package:flutter/material.dart';

import '../core/api.dart';
import 'page_chrome.dart';

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

  String _title(JsonMap item) =>
      ((item['title'] ?? '').toString().isNotEmpty
              ? item['title']
              : item['label'] ?? '未命名资料')
          .toString();

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) => Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        PageToolbar(
          title: '回收站',
          actions: [
            IconButton(
              tooltip: '刷新回收站',
              onPressed: _loading || widget.api.busy ? null : _load,
              icon: const Icon(Icons.refresh, size: 18),
            ),
          ],
        ),
        Expanded(
          child: _loading
              ? const Center(
                  child: SizedBox.square(
                    dimension: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                )
              : _items.isEmpty
              ? const Center(child: Text('回收站是空的'))
              : ListView.builder(
                  itemCount: _items.length,
                  itemBuilder: (context, index) {
                    final item = _items[index];
                    final theme = Theme.of(context);
                    final title = _title(item);
                    return Container(
                      decoration: BoxDecoration(
                        color:
                            _confirm?['id'] == item['id'] &&
                                _confirm?['kind'] == item['kind']
                            ? theme.colorScheme.surfaceContainerHighest
                            : null,
                        border: Border(
                          bottom: BorderSide(color: theme.dividerColor),
                        ),
                      ),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 10,
                      ),
                      child: Row(
                        children: [
                          Icon(
                            item['kind'] == 'conversation'
                                ? Icons.chat_bubble_outline
                                : Icons.audio_file_outlined,
                            size: 17,
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Tooltip(
                                  message: title,
                                  child: Text(
                                    title,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                                Text(
                                  item['purgePending'] == true
                                      ? '上次删除未完成，可重试清理'
                                      : item['kind'] == 'conversation'
                                      ? '会话'
                                      : '音频素材',
                                  style: theme.textTheme.bodySmall?.copyWith(
                                    color: theme.colorScheme.onSurfaceVariant,
                                  ),
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
                            icon: const Icon(Icons.restore, size: 18),
                          ),
                          IconButton(
                            tooltip: '永久删除',
                            onPressed: widget.api.busy
                                ? null
                                : () => setState(() => _confirm = item),
                            icon: const Icon(
                              Icons.delete_forever_outlined,
                              size: 18,
                            ),
                          ),
                        ],
                      ),
                    );
                  },
                ),
        ),
        if (_error != null)
          PageNotice(
            message: _error!,
            onDismiss: () => setState(() => _error = null),
          ),
        // 删除确认固定在底部，列表选择高亮；取消不发送请求，恢复仍走原有后端校验。
        PageFooter(
          child: _confirm == null
              ? Text(
                  '${_items.length} 项 · 恢复或永久删除',
                  style: Theme.of(context).textTheme.bodySmall,
                )
              : Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text(
                      '永久删除「${_title(_confirm!)}」？此操作不能撤销。',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                    const SizedBox(height: 6),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.end,
                      children: [
                        TextButton(
                          onPressed: widget.api.busy
                              ? null
                              : () => setState(() => _confirm = null),
                          child: const Text('取消'),
                        ),
                        const SizedBox(width: 6),
                        FilledButton(
                          onPressed: widget.api.busy
                              ? null
                              : () => _change(_confirm!, true),
                          child: const Text('确认永久删除'),
                        ),
                      ],
                    ),
                  ],
                ),
        ),
      ],
    ),
  );
}
