import 'package:flutter/material.dart';

import '../core/api.dart';
import 'page_chrome.dart';

/// 平台配置使用完整主页面与固定保存栏。密钥只存在密码框短暂内存中，
/// 每次保存（包括失败）后清空，由 Python 层负责 Windows DPAPI 加密。
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key, required this.api, required this.onChanged});
  final WorkbenchApi api;
  final VoidCallback onChanged;
  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  final _form = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _model = TextEditingController();
  final _base = TextEditingController();
  final _key = TextEditingController();
  final _timeout = TextEditingController();
  JsonMap _registry = {};
  JsonMap _current = {};
  List<JsonMap> _items = [];
  List<JsonMap> _models = [];
  String? _selectedId;
  String _provider = 'openai';
  String? _feedback;
  bool _loading = true;
  bool _dirty = false;
  int _catalogRequest = 0;
  int _selectorRevision = 0;

  static const _protocols = {
    'openai': 'OpenAI / 兼容 API',
    'qwen': '通义千问 Qwen',
    'gemini': 'Google Gemini',
    'none': '停用',
  };
  static const _defaults = {
    'openai': ['gpt-audio-1.5', 'https://api.openai.com/v1'],
    'qwen': [
      'qwen3.8-flash',
      'https://dashscope.aliyuncs.com/compatible-mode/v1',
    ],
    'gemini': [
      'gemini-3.8-flash',
      'https://generativelanguage.googleapis.com/v1beta',
    ],
    'none': ['', ''],
  };

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load({String? selectId}) async {
    if (mounted) setState(() => _loading = true);
    try {
      final registry = Map<String, dynamic>.from(
        await widget.api.getJson('/api/model-platforms') as Map,
      );
      final items = (registry['items'] as List)
          .map((entry) => Map<String, dynamic>.from(entry as Map))
          .toList();
      if (!mounted) return;
      setState(() {
        _registry = registry;
        _items = items;
        _fill(
          items.firstWhere(
            (item) => item['id'] == (selectId ?? _selectedId ?? 'default'),
            orElse: () => items.first,
          ),
        );
      });
      await _catalog();
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _fill(JsonMap? item) {
    _current = item ?? {};
    _selectedId = item?['id']?.toString();
    _provider = item?['provider']?.toString() ?? 'openai';
    _name.text = item?['name']?.toString() ?? '';
    _model.text = item?['model']?.toString() ?? _defaults[_provider]![0];
    _base.text = item?['baseUrl']?.toString() ?? _defaults[_provider]![1];
    _timeout.text = item?['timeoutSeconds']?.toString() ?? '60';
    _key.clear();
    _models = [];
    _dirty = false;
    _catalogRequest++;
  }

  /// 切换平台前明确处理未保存草稿；不会将上一平台的密钥带到另一个平台。
  Future<bool> _canLeaveDraft() async {
    if (!_dirty) return true;
    return await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('放弃未保存的配置？'),
            content: const Text('当前平台的修改还未保存。'),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('继续编辑'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('放弃修改'),
              ),
            ],
          ),
        ) ??
        false;
  }

  Future<void> _select(String? id) async {
    if (!await _canLeaveDraft() || !mounted) {
      // 下拉框先更新内部显示再调用 onChanged。取消离开草稿时重建选择器，
      // 避免它显示新平台、实际表单仍保留旧平台，导致用户误判保存目标。
      if (mounted) setState(() => _selectorRevision++);
      return;
    }
    setState(() {
      _fill(id == null ? null : _items.firstWhere((item) => item['id'] == id));
      _feedback = null;
    });
    await _catalog();
  }

  /// 页面载入只读缓存；只有用户按刷新才访问已保存平台的供应商目录。
  Future<void> _catalog({bool refresh = false}) async {
    final id = _selectedId;
    final request = ++_catalogRequest;
    if (id == null) return;
    try {
      final result = refresh
          ? await widget.api.exclusive(
              () => widget.api.postJson('/api/model-platforms/$id/models'),
            )
          : await widget.api.getJson('/api/model-platforms/$id/models');
      if (!mounted || request != _catalogRequest || id != _selectedId) return;
      setState(() {
        _models = ((result['models'] ?? []) as List)
            .map((value) => Map<String, dynamic>.from(value as Map))
            .toList();
        if (refresh) _feedback = result['message']?.toString() ?? '模型目录已刷新。';
      });
    } catch (error) {
      if (mounted && request == _catalogRequest) {
        setState(() => _feedback = error.toString());
      }
    }
  }

  Future<void> _save({bool disable = false}) async {
    if (!disable && !_form.currentState!.validate()) return;
    final payload = <String, dynamic>{
      if (_selectedId != null) 'id': _selectedId,
      'name': _name.text.trim(),
      'provider': disable ? 'none' : _provider,
      'model': _model.text.trim(),
      'baseUrl': _base.text.trim(),
      'apiKey': disable ? '' : _key.text,
      'timeoutSeconds': double.tryParse(_timeout.text) ?? 60,
      'revision': _selectedId == 'default'
          ? _current['revision']
          : _registry['revision'],
    };
    try {
      final result = await widget.api.exclusive(
        () => widget.api.postJson('/api/model-platforms', payload),
      );
      if (!mounted) return;
      _key.clear();
      await _load(selectId: result['id'].toString());
      if (mounted) {
        setState(
          () => _feedback = disable
              ? '平台已停用，保存的密钥已清除。'
              : '配置已保存，下一次请求立即生效。尚未验证云端连接。',
        );
        widget.onChanged();
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      // 离开设置页后控制器已释放；此时也不回填或重新创建密钥草稿。
      if (mounted) _key.clear();
    }
  }

  Future<void> _setDefault() async {
    try {
      await widget.api.exclusive(
        () => widget.api.postJson('/api/model-platforms/default-selection', {
          'id': _selectedId,
          'revision': _registry['revision'],
        }),
      );
      await _load(selectId: _selectedId);
      if (mounted) {
        setState(() => _feedback = '后续新会话将使用此平台；现有会话保持原选择。');
        widget.onChanged();
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    }
  }

  Future<void> _disable() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('停用此平台并清除密钥？'),
        content: const Text('重新启用时需要填写 API 密钥。此操作不会删除会话。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('停用并清除'),
          ),
        ],
      ),
    );
    if (confirmed == true && mounted) await _save(disable: true);
  }

  /// 固定属性名与输入框分离；密码框不回填，辅助说明按需展开。
  Widget _field(
    String title,
    TextEditingController controller, {
    String? hint,
    bool password = false,
    bool enabled = true,
    String? Function(String?)? validator,
  }) => PageProperty(
    label: title,
    child: TextFormField(
      key: ValueKey('field:$title'),
      controller: controller,
      enabled: enabled && !widget.api.busy,
      obscureText: password,
      autocorrect: false,
      enableSuggestions: !password,
      maxLength: password ? null : 512,
      onChanged: (_) => setState(() => _dirty = true),
      validator: validator,
      decoration: InputDecoration(hintText: hint, counterText: ''),
    ),
  );

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final busy = widget.api.busy;
      final isDefault =
          _selectedId != null && _registry['defaultPlatformId'] == _selectedId;
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          PageToolbar(
            title: '模型设置',
            actions: [
              IconButton(
                tooltip: '重新加载平台配置',
                onPressed: busy || _loading
                    ? null
                    : () async {
                        if (await _canLeaveDraft()) await _load();
                      },
                icon: const Icon(Icons.refresh, size: 18),
              ),
              IconButton(
                tooltip: '新增模型平台',
                onPressed: busy || _loading ? null : () => _select(null),
                icon: const Icon(Icons.add, size: 18),
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
                : SingleChildScrollView(
                    padding: const EdgeInsets.fromLTRB(16, 10, 16, 8),
                    child: Align(
                      alignment: Alignment.topLeft,
                      child: ConstrainedBox(
                        constraints: const BoxConstraints(maxWidth: 820),
                        child: Form(
                          key: _form,
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              PageSection(
                                title: '平台与连接',
                                children: [
                                  PageProperty(
                                    label: '当前平台',
                                    child: DropdownButtonFormField<String>(
                                      style: Theme.of(context)
                                          .textTheme
                                          .bodyMedium,
                                      iconSize: 18,
                                      initialValue: _selectedId ?? 'new',
                                      key: ValueKey(
                                        'platform:$_selectedId:$_selectorRevision',
                                      ),
                                      isExpanded: true,
                                      items: [
                                        for (final item in _items)
                                          DropdownMenuItem(
                                            value: item['id'].toString(),
                                            child: Text(
                                              '${item['name']}${_registry['defaultPlatformId'] == item['id'] ? ' · 新会话默认' : ''}',
                                              overflow: TextOverflow.ellipsis,
                                            ),
                                          ),
                                        const DropdownMenuItem(
                                          value: 'new',
                                          child: Text('新增平台'),
                                        ),
                                      ],
                                      onChanged: busy
                                          ? null
                                          : (value) => _select(
                                              value == 'new' ? null : value,
                                            ),
                                    ),
                                  ),
                                  _field(
                                    '平台名称',
                                    _name,
                                    enabled: _selectedId != 'default',
                                    validator: (value) =>
                                        (value ?? '').trim().isEmpty
                                        ? '请填写平台名称'
                                        : null,
                                  ),
                                  PageProperty(
                                    label: '协议',
                                    child: DropdownButtonFormField<String>(
                                      style: Theme.of(context)
                                          .textTheme
                                          .bodyMedium,
                                      iconSize: 18,
                                      key: ValueKey(
                                        'provider:$_selectedId:$_provider',
                                      ),
                                      initialValue: _provider,
                                      isExpanded: true,
                                      items: [
                                        for (final entry in _protocols.entries)
                                          DropdownMenuItem(
                                            value: entry.key,
                                            child: Text(
                                              entry.value,
                                              overflow: TextOverflow.ellipsis,
                                            ),
                                          ),
                                      ],
                                      onChanged: busy
                                          ? null
                                          : (value) => setState(() {
                                              _provider = value!;
                                              _model.text =
                                                  _defaults[value]![0];
                                              _base.text = _defaults[value]![1];
                                              _key.clear();
                                              _models = [];
                                              _dirty = true;
                                            }),
                                    ),
                                  ),
                                  _field(
                                    'API Base URL',
                                    _base,
                                    hint: 'https://…',
                                    validator: (value) {
                                      if (_provider == 'none') return null;
                                      final uri = Uri.tryParse(value ?? '');
                                      return uri?.scheme != 'https' ||
                                              uri!.host.isEmpty
                                          ? '请输入完整的 HTTPS API 根地址'
                                          : null;
                                    },
                                  ),
                                  _field(
                                    'API 密钥',
                                    _key,
                                    password: true,
                                    hint: _current['keyConfigured'] == true
                                        ? '已保存；同协议和地址下留空保留'
                                        : '输入 API 密钥',
                                  ),
                                ],
                              ),
                              PageSection(
                                title: '模型与请求',
                                trailing: IconButton(
                                  tooltip: '刷新已保存平台的模型目录',
                                  onPressed:
                                      busy || _selectedId == null || _dirty
                                      ? null
                                      : () => _catalog(refresh: true),
                                  icon: const Icon(Icons.sync, size: 17),
                                ),
                                children: [
                                  _field(
                                    '默认模型 ID',
                                    _model,
                                    validator: (value) =>
                                        _provider != 'none' &&
                                            (value ?? '').trim().isEmpty
                                        ? '请填写模型 ID'
                                        : null,
                                  ),
                                  if (_models.isNotEmpty)
                                    PageProperty(
                                      label: '缓存目录',
                                      child: DropdownButtonFormField<String>(
                                        style: Theme.of(context)
                                            .textTheme
                                            .bodyMedium,
                                        iconSize: 18,
                                        key: ValueKey(
                                          'catalog:$_selectedId:${_models.length}',
                                        ),
                                        initialValue: null,
                                        isExpanded: true,
                                        hint: const Text('选择模型（可选）'),
                                        items: [
                                          for (final model in _models)
                                            DropdownMenuItem(
                                              value: model['id'].toString(),
                                              child: Text(
                                                model['id'].toString(),
                                                overflow: TextOverflow.ellipsis,
                                              ),
                                            ),
                                        ],
                                        onChanged: busy
                                            ? null
                                            : (value) => setState(() {
                                                _model.text = value!;
                                                _dirty = true;
                                              }),
                                      ),
                                    ),
                                  _field(
                                    '等待超时（秒）',
                                    _timeout,
                                    validator: (value) {
                                      final seconds = double.tryParse(
                                        value ?? '',
                                      );
                                      return seconds == null ||
                                              !seconds.isFinite ||
                                              seconds < 5 ||
                                              seconds > 180
                                          ? '填写 5～180 秒'
                                          : null;
                                    },
                                  ),
                                ],
                              ),
                              PageSection(
                                title: '使用范围',
                                trailing: _selectedId == null
                                    ? null
                                    : IconButton(
                                        tooltip: '停用平台并清除密钥',
                                        onPressed: busy ? null : _disable,
                                        icon: const Icon(
                                          Icons.power_settings_new,
                                          size: 17,
                                        ),
                                      ),
                                children: [
                                  Align(
                                    alignment: Alignment.centerLeft,
                                    child: OutlinedButton.icon(
                                      onPressed:
                                          busy ||
                                              _dirty ||
                                              _selectedId == null ||
                                              _current['configured'] != true ||
                                              isDefault
                                          ? null
                                          : _setDefault,
                                      icon: Icon(
                                        isDefault
                                            ? Icons.check
                                            : Icons.star_border,
                                        size: 16,
                                      ),
                                      label: Text(
                                        isDefault ? '已设为新会话默认' : '设为新会话默认',
                                      ),
                                    ),
                                  ),
                                  if (_selectedId == 'default')
                                    Padding(
                                      padding: const EdgeInsets.only(top: 8),
                                      child: Text(
                                        '此配置同时用于独立 A/B 听评和 MCP 听评。',
                                        style: Theme.of(context)
                                            .textTheme
                                            .bodySmall,
                                      ),
                                    ),
                                ],
                              ),
                              const PageNotes(
                                children: [
                                  Text(
                                    '保存配置不调用模型，也不验证云端连接。密钥按当前 Windows 账户加密保存；改变协议或地址后需重新填写。',
                                  ),
                                  SizedBox(height: 8),
                                  Text(
                                    '页面只读本地模型目录缓存；点击刷新才访问已保存平台。模型列表不代表完整能力声明，音频与推理支持以平台实际响应为准。',
                                  ),
                                  SizedBox(height: 8),
                                  Text('有效输出会续期等待超时。新会话默认只影响之后的会话；已存在会话保留原选择。'),
                                ],
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
          ),
          if (_feedback != null)
            PageNotice(
              message: _feedback!,
              onDismiss: () => setState(() => _feedback = null),
            ),
          PageFooter(
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    _dirty ? '有未保存的修改' : '本地配置',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
                const SizedBox(width: 12),
                FilledButton.icon(
                  onPressed: busy || _loading ? null : _save,
                  icon: const Icon(Icons.check, size: 16),
                  label: const Text('保存并生效'),
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
    for (final controller in [_name, _model, _base, _key, _timeout]) {
      controller.dispose();
    }
    super.dispose();
  }
}
