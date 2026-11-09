import 'package:flutter/material.dart';

import '../core/api.dart';
import '../theme.dart';

/// 会话模型选择的唯一提交结果；空模型 ID 表示继续跟随该平台的默认模型。
/// 打开管理页时只返回意图，不在弹层内部修改平台配置或当前会话。
class ModelSelectionResult {
  const ModelSelectionResult({
    required this.platformId,
    this.modelId = '',
    this.manage = false,
  });

  final String platformId;
  final String modelId;
  final bool manage;
}

/// 在同一弹层中选择服务与模型，避免先改平台、再改模型造成半完成的会话配置。
/// 首次打开、切换服务与搜索均不访问供应商；只有刷新按钮触发目录联网请求。
class ModelPickerDialog extends StatefulWidget {
  const ModelPickerDialog({
    super.key,
    required this.api,
    required this.platforms,
    required this.platformId,
    required this.modelId,
  });

  final WorkbenchApi api;
  final List<JsonMap> platforms;
  final String platformId;
  final String modelId;

  @override
  State<ModelPickerDialog> createState() => _ModelPickerDialogState();
}

class _ModelPickerDialogState extends State<ModelPickerDialog> {
  final _search = TextEditingController();
  final _manual = TextEditingController();
  late String _platformId;
  late String _selectedModelId;
  List<JsonMap> _models = [];
  JsonMap _catalog = {};
  String? _error;
  bool _loading = false;
  bool _refreshing = false;
  int _generation = 0;

  JsonMap get _platform => widget.platforms.firstWhere(
    (item) => item['id'] == _platformId,
    orElse: () => <String, dynamic>{},
  );

  /// 新版服务提供独立的连接可用标记，不要求先填默认模型才能获取目录。
  /// 兼容旧服务/测试快照时才回退到协议、密钥状态；显式 false 始终优先。
  bool get _connectionReady {
    final platform = _platform;
    if (platform.isEmpty ||
        platform['provider'] == 'none' ||
        platform['enabled'] == false ||
        platform['invalid'] == true) {
      return false;
    }
    if (platform['connectionConfigured'] is bool) {
      return platform['connectionConfigured'] == true;
    }
    return platform['configured'] == true ||
        (platform['keyConfigured'] == true &&
            const {'openai', 'qwen', 'gemini'}.contains(platform['provider']));
  }

  String get _defaultModel => (_platform['model'] ?? '').toString().trim();

  /// 刷新目录持有全局互斥锁；此时不能先关闭弹层再让会话因 busy 拒绝结果。
  /// 所有提交入口共享该条件，包含列表、默认项、手动按钮与键盘 Enter。
  bool get _canSelect => _connectionReady && !_refreshing && !widget.api.busy;

  @override
  void initState() {
    super.initState();
    _platformId = widget.platforms.any((p) => p['id'] == widget.platformId)
        ? widget.platformId
        : (widget.platforms.isEmpty
              ? ''
              : widget.platforms.first['id'].toString());
    _selectedModelId = _platformId == widget.platformId ? widget.modelId : '';
    _manual.text = _selectedModelId;
    _loadCatalog();
  }

  @override
  void dispose() {
    // 递增代次使已发出的异步读取失效；不取消或重试用户已经触发的网络请求。
    _generation++;
    _search.dispose();
    _manual.dispose();
    super.dispose();
  }

  void _switchPlatform(String? id) {
    if (id == null || id == _platformId) return;
    setState(() {
      _platformId = id;
      _selectedModelId = '';
      _search.clear();
      _manual.clear();
      _models = [];
      _catalog = {};
      _error = null;
    });
    _loadCatalog();
  }

  /// 每次读取都捕获平台与代次，旧平台的迟到结果不得覆盖当前目录或错误提示。
  Future<void> _loadCatalog({bool refresh = false}) async {
    final generation = ++_generation;
    final id = _platformId;
    if (!_connectionReady) {
      setState(() {
        _loading = false;
        _refreshing = false;
      });
      return;
    }
    setState(() {
      _loading = true;
      _refreshing = refresh;
      _error = null;
    });
    try {
      final path = '/api/model-platforms/$id/models';
      final response = refresh
          ? await widget.api.exclusive(() => widget.api.postJson(path))
          : await widget.api.getJson(path);
      if (!mounted || generation != _generation || id != _platformId) return;
      if (response is! Map ||
          (response['platformId'] != null && response['platformId'] != id)) {
        throw const ApiException('模型目录返回异常，请重新读取或手动填写模型 ID。');
      }
      if (response['status'] == 'error') {
        throw ApiException(response['message']?.toString() ?? '模型目录刷新失败。');
      }
      final seen = <String>{};
      final entries = response['models'];
      final models = <JsonMap>[];
      if (entries is List) {
        for (final item in entries) {
          if (item is! Map) continue;
          final modelId = (item['id'] ?? '').toString().trim();
          if (modelId.isEmpty || !seen.add(modelId)) continue;
          models.add({
            'id': modelId,
            'label': (item['label'] ?? modelId).toString(),
          });
        }
      }
      setState(() {
        _models = models;
        _catalog = Map<String, dynamic>.from(response);
      });
    } catch (error) {
      if (!mounted || generation != _generation || id != _platformId) return;
      // 目录失败不抹掉已加载列表；不向界面暴露底层异常中的本机路径或凭据。
      setState(() {
        _error = error is ApiException
            ? error.message
            : '无法读取模型目录，可手动填写模型 ID 或到模型服务检查配置。';
      });
    } finally {
      if (mounted && generation == _generation && id == _platformId) {
        setState(() {
          _loading = false;
          _refreshing = false;
        });
      }
    }
  }

  void _select(String modelId) {
    if (!_canSelect || (modelId.isEmpty && _defaultModel.isEmpty)) return;
    Navigator.of(context)
        .pop(ModelSelectionResult(platformId: _platformId, modelId: modelId));
  }

  void _manage() =>
      Navigator.of(context)
          .pop(ModelSelectionResult(platformId: _platformId, manage: true));

  /// 时间来自服务返回的缓存元数据，不能用弹层打开时间伪装最近刷新时间。
  String get _catalogSummary {
    if (_loading && _catalog.isEmpty) {
      return _refreshing ? '正在向服务获取模型目录…' : '正在读取本地模型缓存…';
    }
    final source = switch (_catalog['source']) {
      'api' => '已刷新目录',
      'cache' => '本地缓存',
      _ => '暂无缓存',
    };
    final date = DateTime.tryParse((_catalog['cachedAt'] ?? '').toString());
    final dateText = date == null
        ? ''
        : date.toLocal().toString().substring(0, 16);
    return [
      source,
      if (dateText.isNotEmpty) dateText,
      if (_models.isNotEmpty) '${_models.length} 个模型',
      if (_catalog['stale'] == true) '可按需刷新',
    ].join(' · ');
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, child) => _buildDialog(context),
  );

  /// 全局互斥状态变化时同步更新整张目录，避免仅刷新按钮更新而模型行仍可提交。
  Widget _buildDialog(BuildContext context) {
    final theme = Theme.of(context);
    return Dialog(
      insetPadding: const EdgeInsets.all(16),
      clipBehavior: Clip.antiAlias,
      child: SizedBox(
        width: 680,
        height: 480,
        child: LayoutBuilder(
          builder: (context, constraints) {
            final narrow = constraints.maxWidth < 500;
            return Column(
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 6, 8, 6),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text('选择模型', style: theme.textTheme.titleMedium),
                      ),
                      IconButton(
                        tooltip: '管理模型服务',
                        onPressed: _manage,
                        icon: const Icon(Icons.settings_outlined),
                      ),
                      IconButton(
                        tooltip: '取消选择',
                        onPressed: () => Navigator.of(context).pop(),
                        icon: const Icon(Icons.close),
                      ),
                    ],
                  ),
                ),
                const Divider(height: 1),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 6),
                  child: narrow
                      ? Column(
                          children: [
                            _platformField(),
                            const SizedBox(height: 8),
                            _searchField(),
                          ],
                        )
                      : Row(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            Expanded(flex: 4, child: _platformField()),
                            const SizedBox(width: 12),
                            Expanded(flex: 5, child: _searchField()),
                          ],
                        ),
                ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: SizedBox(
                    height: 24,
                    child: Row(
                      children: [
                        if (_loading) ...[
                          const SizedBox(
                            width: 11,
                            height: 11,
                            child: CircularProgressIndicator(strokeWidth: 1.5),
                          ),
                          const SizedBox(width: 6),
                        ],
                        Expanded(
                          child: Text(
                            _connectionReady ? _catalogSummary : '服务尚未就绪',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: theme.textTheme.bodySmall,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
                    child: Text(
                      _error!,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: theme.textTheme.bodySmall,
                    ),
                  ),
                Expanded(child: _modelList()),
                const Divider(height: 1),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
                  child: _manualField(narrow),
                ),
              ],
            );
          },
        ),
      ),
    );
  }

  Widget _platformField() => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      const Text('模型服务'),
      const SizedBox(height: 4),
      SizedBox(
        height: WorkbenchTheme.controlHeight,
        child: DropdownButtonFormField<String>(
          key: ValueKey('model-picker-platform-$_platformId'),
          initialValue: _platformId.isEmpty ? null : _platformId,
          isExpanded: true,
          decoration: const InputDecoration(
            contentPadding: EdgeInsets.symmetric(horizontal: 10),
          ),
          hint: const Text('尚未添加模型服务'),
          items: widget.platforms.map((platform) {
            final id = platform['id'].toString();
            final name = (platform['name'] ?? id).toString();
            return DropdownMenuItem(
              value: id,
              child: Text(name, overflow: TextOverflow.ellipsis),
            );
          }).toList(),
          onChanged: widget.platforms.isEmpty ? null : _switchPlatform,
        ),
      ),
    ],
  );

  Widget _searchField() => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      const Text('搜索模型'),
      const SizedBox(height: 4),
      Row(
        children: [
          Expanded(
            child: SizedBox(
              height: WorkbenchTheme.controlHeight,
              child: TextField(
                key: const ValueKey('model-picker-search'),
                controller: _search,
                enabled: _connectionReady,
                onChanged: (_) => setState(() {}),
                decoration: const InputDecoration(hintText: '名称或模型 ID'),
              ),
            ),
          ),
          const SizedBox(width: 6),
          ListenableBuilder(
            listenable: widget.api,
            builder: (context, child) => IconButton(
              tooltip: '刷新模型目录（访问服务）',
              onPressed: _connectionReady && !_loading && !widget.api.busy
                  ? () => _loadCatalog(refresh: true)
                  : null,
              icon: const Icon(Icons.refresh),
            ),
          ),
        ],
      ),
    ],
  );

  Widget _modelList() {
    if (!_connectionReady) {
      return ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text('请先启用模型服务并配置 API 地址与密钥。'),
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(onPressed: _manage, child: const Text('前往模型服务')),
          ),
        ],
      );
    }
    final query = _search.text.trim().toLowerCase();
    final models = _models.where((item) {
      return item['id'].toString().toLowerCase().contains(query) ||
          item['label'].toString().toLowerCase().contains(query);
    }).toList();
    final showDefault =
        _defaultModel.isNotEmpty &&
        (_defaultModel.toLowerCase().contains(query) ||
            '平台默认模型'.contains(query));
    return ListView(
      key: const ValueKey('model-picker-list'),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      children: [
        if (showDefault)
          _modelRow(modelId: '', title: _defaultModel, subtitle: '平台默认模型'),
        for (final model in models)
          _modelRow(
            modelId: model['id'] as String,
            title: model['id'] as String,
            subtitle: model['label'] == model['id']
                ? null
                : model['label'] as String,
          ),
        if (!showDefault && models.isEmpty && !_loading)
          Padding(
            padding: const EdgeInsets.all(16),
            child: Text(
              query.isEmpty
                  ? '暂无模型目录。可刷新目录，或在下方填写模型 ID。'
                  : '未找到匹配模型，可在下方填写模型 ID。',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
      ],
    );
  }

  /// 模型行是明确提交点；高亮只表示传入会话的当前选择，不持久化浏览草稿。
  Widget _modelRow({
    required String modelId,
    required String title,
    String? subtitle,
  }) {
    final selected = _selectedModelId == modelId;
    final theme = Theme.of(context);
    return Tooltip(
      message: title,
      child: Material(
        color: selected
            ? theme.colorScheme.surfaceContainerHighest
            : Colors.transparent,
        child: InkWell(
          key: ValueKey(
            modelId.isEmpty
                ? 'model-picker-default'
                : 'model-picker-model-$modelId',
          ),
          onTap: _canSelect ? () => _select(modelId) : null,
          child: SizedBox(
            height: 44,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: Row(
                children: [
                  SizedBox(
                    width: 22,
                    child: selected ? const Icon(Icons.check, size: 16) : null,
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: _canSelect
                              ? null
                              : TextStyle(color: theme.disabledColor),
                        ),
                        if (subtitle != null)
                          Text(
                            subtitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: theme.textTheme.bodySmall,
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _manualField(bool narrow) {
    final field = SizedBox(
      height: WorkbenchTheme.controlHeight,
      child: TextField(
        key: const ValueKey('model-picker-manual'),
        controller: _manual,
        enabled: _connectionReady,
        onChanged: (_) => setState(() {}),
        onSubmitted: (value) {
          if (value.trim().isNotEmpty) _select(value.trim());
        },
        decoration: const InputDecoration(hintText: '输入服务支持的模型 ID'),
      ),
    );
    final submit = FilledButton(
      onPressed: _canSelect && _manual.text.trim().isNotEmpty
          ? () => _select(_manual.text.trim())
          : null,
      child: const Text('使用此模型'),
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('手动模型 ID'),
        const SizedBox(height: 4),
        if (narrow) ...[
          field,
          const SizedBox(height: 8),
          Align(alignment: Alignment.centerRight, child: submit),
        ] else
          Row(
            children: [
              Expanded(child: field),
              const SizedBox(width: 8),
              submit,
            ],
          ),
      ],
    );
  }
}
