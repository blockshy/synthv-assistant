import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';

import '../core/api.dart';
import '../widgets/audio_clip.dart';
import 'page_chrome.dart';

/// 素材页保持列表独立滚动，附件确认栏始终可见；导入仅写入本机资料库。
class LibraryPage extends StatefulWidget {
  const LibraryPage({super.key, required this.api, required this.onAttach});
  final WorkbenchApi api;
  final ValueChanged<List<JsonMap>> onAttach;
  @override
  State<LibraryPage> createState() => _LibraryPageState();
}

class _LibraryPageState extends State<LibraryPage> {
  List<JsonMap> _items = [];
  final Set<String> _selected = {};
  final _search = TextEditingController();
  final _name = TextEditingController();
  final _note = TextEditingController();
  String _filter = 'all';
  String? _editing;
  JsonMap? _purging;
  String? _feedback;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  String _key(JsonMap item) => '${item['kind']}:${item['id']}';

  Future<void> _load() async {
    if (mounted) setState(() => _loading = true);
    try {
      final values = await Future.wait([
        widget.api.getJson('/api/uploads'),
        widget.api.getJson('/api/recordings'),
      ]);
      final items = <JsonMap>[];
      for (var i = 0; i < values.length; i++) {
        for (final entry in values[i]['items'] as List) {
          items.add({
            ...Map<String, dynamic>.from(entry as Map),
            'kind': i == 0 ? 'upload' : 'recording',
          });
        }
      }
      items.sort(
        (a, b) => (b['createdAt'] ?? '').toString().compareTo(
          (a['createdAt'] ?? '').toString(),
        ),
      );
      if (mounted) {
        setState(() {
          _items = items;
          _selected.removeWhere(
            (key) => !items.any((item) => _key(item) == key),
          );
        });
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _change(Future<void> Function() operation) async {
    try {
      await widget.api.exclusive(operation);
      await _load();
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    }
  }

  Future<void> _upload() async {
    final file = await openFile(
      acceptedTypeGroups: [
        const XTypeGroup(label: '音频文件', extensions: ['wav', 'mp3']),
      ],
    );
    if (file == null || !mounted) return;
    await _change(() async {
      await widget.api.uploadFile(file.path);
      if (mounted) setState(() => _feedback = '已导入本地素材库，尚未发送给模型。');
    });
  }

  void _toggle(JsonMap item, bool selected) {
    setState(() {
      if (selected && _selected.length >= 2) {
        _feedback = '每次消息最多附带两段音频。';
        return;
      }
      selected ? _selected.add(_key(item)) : _selected.remove(_key(item));
    });
  }

  /// 附件只向会话传递公开展示字段，录音的工程路径等私有索引不离开本页。
  JsonMap _attachment(JsonMap item) => {
    for (final key in [
      'id',
      'kind',
      'name',
      'label',
      'url',
      'durationSeconds',
      'available',
    ])
      if (item.containsKey(key)) key: item[key],
  };

  /// 素材采用连续列表行；选中背景只表达附件选择，不给每条音频重复套卡片。
  Widget _item(JsonMap item, bool busy) {
    final title = (item['label'] ?? item['name'] ?? '音频素材').toString();
    final key = _key(item);
    final duration =
        (item['durationSeconds'] as num?)?.toStringAsFixed(1) ?? '—';
    final selected = _selected.contains(key);
    final theme = Theme.of(context);
    return Container(
      decoration: BoxDecoration(
        color: selected ? theme.colorScheme.surfaceContainerHighest : null,
        border: Border(bottom: BorderSide(color: theme.dividerColor)),
      ),
      padding: const EdgeInsets.fromLTRB(12, 6, 12, 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Checkbox(
                value: selected,
                onChanged: busy ? null : (value) => _toggle(item, value!),
              ),
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
                        style: theme.textTheme.bodyMedium,
                      ),
                    ),
                    Text(
                      '${item['kind'] == 'recording' ? '工程录音' : '上传素材'}  ·  $duration 秒',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                    ),
                  ],
                ),
              ),
              IconButton(
                tooltip: item['starred'] == true ? '取消收藏' : '收藏音频',
                icon: Icon(
                  item['starred'] == true ? Icons.star : Icons.star_border,
                  size: 17,
                ),
                onPressed: busy
                    ? null
                    : () => _change(() async {
                        await widget.api.postJson(
                          '/api/assets/${item['kind']}/${item['id']}/metadata',
                          {'starred': item['starred'] != true},
                        );
                      }),
              ),
              IconButton(
                tooltip: '修改名称和备注',
                icon: const Icon(Icons.edit_note, size: 18),
                onPressed: busy
                    ? null
                    : () => setState(() {
                        _editing = key;
                        _purging = null;
                        _name.text = title;
                        _note.text = (item['note'] ?? '').toString();
                      }),
              ),
              PopupMenuButton<String>(
                tooltip: '删除音频',
                enabled: !busy,
                icon: const Icon(Icons.delete_outline, size: 17),
                itemBuilder: (_) => const [
                  PopupMenuItem(value: 'delete', child: Text('移入回收站')),
                  PopupMenuItem(value: 'purge', child: Text('永久删除')),
                ],
                onSelected: (action) {
                  if (action == 'purge') {
                    setState(() {
                      _purging = item;
                      _editing = null;
                    });
                  } else {
                    _change(() async {
                      await widget.api.postJson(
                        '/api/assets/${item['kind']}/${item['id']}/delete',
                      );
                    });
                  }
                },
              ),
            ],
          ),
          if ((item['note'] ?? '').toString().isNotEmpty && _editing != key)
            Padding(
              padding: const EdgeInsets.only(left: 36, top: 3),
              child: Tooltip(
                message: item['note'].toString(),
                child: Text(
                  item['note'].toString(),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall,
                ),
              ),
            ),
          AudioClip(key: ValueKey('audio:$key'), api: widget.api, clip: item),
          if (_editing == key)
            Padding(
              padding: const EdgeInsets.fromLTRB(8, 6, 8, 8),
              child: Column(
                children: [
                  PageProperty(
                    label: '名称',
                    child: TextField(
                      controller: _name,
                      enabled: !busy,
                      maxLength: 100,
                      decoration: const InputDecoration(counterText: ''),
                    ),
                  ),
                  PageProperty(
                    label: '本地备注',
                    child: TextField(
                      controller: _note,
                      enabled: !busy,
                      minLines: 2,
                      maxLines: 4,
                      maxLength: 2000,
                      decoration: const InputDecoration(counterText: ''),
                    ),
                  ),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: busy
                            ? null
                            : () => setState(() => _editing = null),
                        child: const Text('取消'),
                      ),
                      const SizedBox(width: 6),
                      FilledButton(
                        onPressed: busy
                            ? null
                            : () => _change(() async {
                                await widget.api.postJson(
                                  '/api/assets/${item['kind']}/${item['id']}/metadata',
                                  {
                                    'label': _name.text.trim(),
                                    'note': _note.text,
                                  },
                                );
                                if (mounted) setState(() => _editing = null);
                              }),
                        child: const Text('保存备注'),
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

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final busy = widget.api.busy;
      final query = _search.text.trim().toLowerCase();
      final filtered = _items
          .where(
            (item) =>
                (_filter == 'all' ||
                    (_filter == 'starred'
                        ? item['starred'] == true
                        : item['kind'] == _filter)) &&
                '${item['label']} ${item['name']} ${item['note']}'
                    .toLowerCase()
                    .contains(query),
          )
          .toList();
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          PageToolbar(
            title: '音频素材',
            actions: [
              Tooltip(
                message: '只导入本地素材库；附到会话后需点击发送才交给模型。',
                child: Text(
                  '${_items.length} 项',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
              const SizedBox(width: 10),
              IconButton(
                tooltip: '刷新音频素材',
                onPressed: _loading || busy ? null : _load,
                icon: const Icon(Icons.refresh, size: 18),
              ),
              const SizedBox(width: 6),
              FilledButton.icon(
                onPressed: busy ? null : _upload,
                icon: const Icon(Icons.add, size: 16),
                label: const Text('导入'),
              ),
            ],
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _search,
                    onChanged: (_) => setState(() {}),
                    decoration: const InputDecoration(
                      hintText: '搜索名称或备注',
                      prefixIcon: Icon(Icons.search, size: 17),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                SizedBox(
                  width: 104,
                  child: DropdownButtonFormField<String>(
                    style: Theme.of(context).textTheme.bodyMedium,
                    iconSize: 18,
                    initialValue: _filter,
                    isExpanded: true,
                    decoration: const InputDecoration(
                      contentPadding: EdgeInsets.symmetric(
                        horizontal: 10,
                        vertical: 8,
                      ),
                    ),
                    items: [
                      for (final entry in const {
                        'all': '全部',
                        'upload': '上传',
                        'recording': '录音',
                        'starred': '收藏',
                      }.entries)
                        DropdownMenuItem(
                          value: entry.key,
                          child: Text(entry.value),
                        ),
                    ],
                    onChanged: (value) => setState(() => _filter = value!),
                  ),
                ),
              ],
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: _loading
                ? const Center(
                    child: SizedBox.square(
                      dimension: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  )
                : filtered.isEmpty
                ? const Center(child: Text('暂无匹配的音频素材'))
                : ListView.builder(
                    itemCount: filtered.length,
                    itemBuilder: (context, index) =>
                        _item(filtered[index], busy),
                  ),
          ),
          if (_feedback != null)
            PageNotice(
              message: _feedback!,
              onDismiss: () => setState(() => _feedback = null),
            ),
          if (_purging != null)
            PageFooter(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    '永久删除「${_purging!['label'] ?? _purging!['name'] ?? '该音频'}」？此操作不能撤销。',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: 6),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: busy
                            ? null
                            : () => setState(() => _purging = null),
                        child: const Text('取消'),
                      ),
                      const SizedBox(width: 6),
                      FilledButton(
                        onPressed: busy
                            ? null
                            : () {
                                final target = _purging!;
                                _change(() async {
                                  await widget.api.postJson(
                                    '/api/assets/${target['kind']}/${target['id']}/purge',
                                    {'confirm': true},
                                  );
                                  if (mounted) setState(() => _purging = null);
                                });
                              },
                        child: const Text('确认永久删除'),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          PageFooter(
            child: Row(
              children: [
                Expanded(
                  child: Tooltip(
                    message: '仅加入会话草稿，尚未发送到模型。',
                    child: Text(
                      '已选 ${_selected.length} / 2 段',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                ),
                IconButton(
                  tooltip: '清空附件选择',
                  onPressed: _selected.isEmpty || busy
                      ? null
                      : () => setState(_selected.clear),
                  icon: const Icon(Icons.clear_all, size: 18),
                ),
                const SizedBox(width: 8),
                FilledButton.icon(
                  onPressed: _selected.isEmpty || busy || _loading
                      ? null
                      : () => widget.onAttach(
                          // 保留用户点选次序，A/B 不因素材列表按创建时间排序而交换。
                          _selected
                              .map(
                                (key) => _items.firstWhere(
                                  (item) => _key(item) == key,
                                ),
                              )
                              .map(_attachment)
                              .toList(),
                        ),
                  icon: const Icon(Icons.attach_file, size: 16),
                  label: const Text('附到会话'),
                ),
              ],
            ),
          ),
        ],
      );
    },
  );

  @override
  void dispose() {
    _search.dispose();
    _name.dispose();
    _note.dispose();
    super.dispose();
  }
}
