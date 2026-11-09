import 'package:flutter/material.dart';

import '../core/api.dart';
import '../widgets/workbench_dropdown.dart';
import '../widgets/workbench_expansion_tile.dart';
import 'page_chrome.dart';

/// 模型服务按“平台列表 → 连接配置 → 模型目录”组织。外层工作台通过公开的
/// [SettingsPageState.maybeLeave] 处理导航草稿；所有云端访问都需要明确点击。
class SettingsPage extends StatefulWidget {
  const SettingsPage({
    super.key,
    required this.api,
    required this.onChanged,
    this.initialPlatformId,
  });
  final WorkbenchApi api;
  final VoidCallback onChanged;
  final String? initialPlatformId;
  @override
  State<SettingsPage> createState() => SettingsPageState();
}

/// 状态公开仅用于离开页面前的草稿确认，不向工作台暴露控制器或密钥内容。
class SettingsPageState extends State<SettingsPage> {
  final _form = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _model = TextEditingController();
  final _base = TextEditingController();
  final _key = TextEditingController();
  final _timeout = TextEditingController();
  final _platformSearch = TextEditingController();
  final _modelSearch = TextEditingController();
  JsonMap _registry = {}, _current = {}, _catalogState = {};
  JsonMap? _connectionResult;
  List<JsonMap> _items = [], _models = [];
  String? _selectedId, _feedback;
  String _provider = 'openai';
  bool _loading = true, _dirty = false, _saving = false;
  bool _choosingPreset = false, _showList = false, _changing = false;
  bool _networkBusy = false, _saveUncertain = false;
  int _tab = 0, _catalogRequest = 0, _loadRequest = 0;

  static const _protocols = {
    'openai': 'OpenAI 兼容 API',
    'qwen': '通义千问 Qwen',
    'gemini': 'Google Gemini',
  };

  /// 预设只提供已有协议的地址，不猜测用户账户能调用的默认模型。
  /// 兼容 API 与 OpenAI 共用协议；服务类型与“停用”是不同操作。
  static const _presets = [
    (
      name: 'OpenAI',
      provider: 'openai',
      base: 'https://api.openai.com/v1',
      description: '使用 OpenAI 官方 API',
    ),
    (
      name: '通义千问 Qwen',
      provider: 'qwen',
      base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      description: '使用阿里云百炼，服务区域可在地址中修改',
    ),
    (
      name: 'Google Gemini',
      provider: 'gemini',
      base: 'https://generativelanguage.googleapis.com/v1beta',
      description: '使用 Google Gemini API',
    ),
    (
      name: '自定义兼容服务',
      provider: 'openai',
      base: '',
      description: '填写服务商提供的 OpenAI 兼容 API 地址',
    ),
  ];

  bool get _busy => widget.api.busy || _loading || _saving || _changing;
  bool get _isDefault =>
      _selectedId != null && _registry['defaultPlatformId'] == _selectedId;
  bool get _disabled => _current['provider'] == 'none';
  bool get _connectionConfigured =>
      _current['connectionConfigured'] == true ||
      _current['configured'] == true;
  bool get _identityChanged =>
      _current.isNotEmpty &&
      (_provider != _current['provider'] ||
          _base.text.trim().replaceFirst(RegExp(r'/+$'), '') !=
              (_current['baseUrl']?.toString() ?? '').replaceFirst(
                RegExp(r'/+$'),
                '',
              ));
  String get _title => _selectedId == 'default'
      ? '内置平台'
      : _name.text.trim().isEmpty
      ? '新平台'
      : _name.text.trim();

  @override
  void initState() {
    super.initState();
    _load(selectId: widget.initialPlatformId);
  }

  /// 列表加载与目录加载分别防止迟到响应覆盖当前草稿。页面打开只读取本地数据。
  Future<bool> _load({String? selectId}) async {
    final request = ++_loadRequest;
    setState(() => _loading = true);
    try {
      final registry = Map<String, dynamic>.from(
        await widget.api.getJson('/api/model-platforms') as Map,
      );
      final items = (registry['items'] as List)
          .map((entry) => Map<String, dynamic>.from(entry as Map))
          .toList();
      if (!mounted || request != _loadRequest) return false;
      final id =
          selectId ?? _selectedId ?? registry['defaultPlatformId'] ?? 'default';
      setState(() {
        _registry = registry;
        _items = items;
        _fill(
          items.where((item) => item['id'] == id).firstOrNull ??
              items.firstOrNull,
        );
      });
      await _catalog();
      return true;
    } catch (error) {
      if (mounted && request == _loadRequest) {
        setState(() => _feedback = error.toString());
      }
      return false;
    } finally {
      if (mounted && request == _loadRequest) setState(() => _loading = false);
    }
  }

  void _fill(JsonMap? item) {
    _current = item ?? {};
    _selectedId = item?['id']?.toString();
    final provider = item?['provider']?.toString();
    _provider = _protocols.containsKey(provider) ? provider! : 'openai';
    _name.text = item?['name']?.toString() ?? '';
    _model.text = item?['model']?.toString() ?? '';
    _base.text = item?['baseUrl']?.toString() ?? '';
    if (provider == 'none' && _base.text.isEmpty) {
      _base.text = _presets.first.base;
    }
    _timeout.text = item?['timeoutSeconds']?.toString() ?? '60';
    _key.clear();
    _modelSearch.clear();
    _models = [];
    _catalogState = {};
    _connectionResult = null;
    _dirty = false;
    _saveUncertain = false;
    _choosingPreset = item == null;
    _catalogRequest++;
  }

  /// 离开包括切换平台、页面导航和重新加载。保存失败时保持原页及非密钥草稿，
  /// “保存并继续”不能在一次请求结果未知时自动重发，也不能伪装为保存成功。
  Future<bool> maybeLeave() async {
    if (_saving || _networkBusy || _changing) return false;
    if (!_dirty) return true;
    final decision = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('保存平台修改？'),
        content: Text('“$_title”有未保存的修改。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, 'stay'),
            child: const Text('继续编辑'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, 'discard'),
            child: const Text('放弃修改'),
          ),
          FilledButton(
            onPressed: _saveUncertain
                ? null
                : () => Navigator.pop(context, 'save'),
            child: const Text('保存并继续'),
          ),
        ],
      ),
    );
    if (!mounted || decision == null || decision == 'stay') return false;
    if (decision == 'save') return _save();
    _key.clear();
    setState(() => _dirty = false);
    return true;
  }

  Future<void> _select(String? id) async {
    if (_busy || (id != null && id == _selectedId && !_choosingPreset)) {
      if (mounted && id == _selectedId) setState(() => _showList = false);
      return;
    }
    if (!await maybeLeave() || !mounted) return;
    setState(() {
      _fill(id == null ? null : _items.firstWhere((item) => item['id'] == id));
      _tab = 0;
      _showList = false;
      _feedback = null;
    });
    await _catalog();
  }

  void _preset(int index) {
    final preset = _presets[index];
    setState(() {
      _provider = preset.provider;
      _name.text = preset.name;
      _base.text = preset.base;
      _model.clear();
      _key.clear();
      _choosingPreset = false;
      _dirty = true;
    });
  }

  /// 页面载入读取本地目录。显式获取目录才访问已保存的平台，草稿凭据不会被发送。
  Future<void> _catalog({bool refresh = false}) async {
    final id = _selectedId;
    final request = ++_catalogRequest;
    if (id == null) return;
    if (refresh) setState(() => _networkBusy = true);
    try {
      final result = refresh
          ? await widget.api.exclusive(
              () => widget.api.postJson('/api/model-platforms/$id/models'),
            )
          : await widget.api.getJson('/api/model-platforms/$id/models');
      if (!mounted || request != _catalogRequest || id != _selectedId) return;
      setState(() {
        _catalogState = Map<String, dynamic>.from(result as Map);
        _models = ((result['models'] ?? []) as List)
            .map((value) => Map<String, dynamic>.from(value as Map))
            .toList();
        if (refresh) _feedback = result['message']?.toString() ?? '模型目录已更新。';
      });
    } catch (error) {
      if (mounted && request == _catalogRequest) {
        setState(() => _feedback = error.toString());
      }
    } finally {
      if (mounted && refresh) setState(() => _networkBusy = false);
    }
  }

  Future<void> _testConnection() async {
    final id = _selectedId, revision = _current['revision'];
    if (id == null || _busy || _dirty || _disabled) return;
    setState(() {
      _networkBusy = true;
      _connectionResult = null;
    });
    try {
      final result = await widget.api.exclusive(
        () => widget.api.postJson('/api/model-platforms/$id/test-connection', {
          'revision': revision,
        }),
      );
      if (!mounted || id != _selectedId || revision != _current['revision']) {
        return;
      }
      if (result['configurationChanged'] == true ||
          result['revision'] != revision) {
        setState(
          () => _connectionResult = {'message': '测试期间配置发生变化，请重新加载后再测试。'},
        );
        return;
      }
      setState(() {
        _connectionResult = Map<String, dynamic>.from(result as Map);
        // 成功测试可复用同次目录读取，失败不能把已有缓存伪装成刚验证通过。
        if (result['status'] == 'ok' && result['models'] is List) {
          _catalogState = Map<String, dynamic>.from(result);
          _models = (result['models'] as List)
              .map((value) => Map<String, dynamic>.from(value as Map))
              .toList();
        }
      });
    } catch (error) {
      if (mounted && id == _selectedId) {
        setState(
          () => _connectionResult = {
            'status': 'error',
            'message': error.toString(),
          },
        );
      }
    } finally {
      if (mounted) setState(() => _networkBusy = false);
    }
  }

  /// 默认模型可稍后从目录选择。校验超时和身份变更不依赖当前tab是否挂载表单。
  Future<bool> _save({bool disable = false}) async {
    if (_saving || widget.api.busy || _saveUncertain) return false;
    final seconds = double.tryParse(_timeout.text);
    if (!disable) {
      final formValid = _form.currentState?.validate() ?? true;
      final uri = Uri.tryParse(_base.text.trim());
      final validIdentity = uri?.scheme == 'https' && uri!.host.isNotEmpty;
      final needsKey = _current['keyConfigured'] != true || _identityChanged;
      if (!formValid ||
          _name.text.trim().isEmpty ||
          !validIdentity ||
          (needsKey && _key.text.trim().isEmpty) ||
          seconds == null ||
          !seconds.isFinite ||
          seconds < 5 ||
          seconds > 180) {
        setState(() {
          _tab = 0;
          _feedback = !validIdentity
              ? '请填写完整的 HTTPS API 地址。'
              : needsKey && _key.text.trim().isEmpty
              ? '请填写 API 密钥；切换服务类型或地址后必须重新填写。'
              : seconds == null ||
                    !seconds.isFinite ||
                    seconds < 5 ||
                    seconds > 180
              ? '高级设置中的等待超时应为 5～180 秒。'
              : '请完善平台名称和连接配置。';
        });
        return false;
      }
    }
    final payload = <String, dynamic>{
      if (_selectedId != null) 'id': _selectedId,
      'name': _name.text.trim(),
      'provider': disable ? 'none' : _provider,
      'model': _model.text.trim(),
      'baseUrl': _base.text.trim(),
      'apiKey': disable ? '' : _key.text,
      'timeoutSeconds': seconds ?? 60,
      'revision': _selectedId == 'default'
          ? _current['revision']
          : _registry['revision'],
    };
    setState(() => _saving = true);
    try {
      final result = await widget.api.exclusive(
        () => widget.api.postJson('/api/model-platforms', payload),
      );
      if (!mounted) return false;
      _key.clear();
      // POST 已成功时先记住实际 ID，即使后续读取失败也不会再次创建同一平台。
      _selectedId = result['id'].toString();
      _dirty = false;
      final loaded = await _load(selectId: _selectedId);
      if (!mounted) return false;
      widget.onChanged();
      setState(() {
        _saveUncertain = !loaded;
        _feedback = !loaded
            ? '配置已提交，但列表未读回；请重新加载核对，勿重复创建平台。'
            : disable
            ? '平台已停用，连接配置与保存的密钥已清除。'
            : _model.text.trim().isEmpty
            ? '连接配置已保存。到“模型目录”获取并选择默认模型。'
            : '配置已保存，下一次请求生效。';
        if (loaded && !disable && _model.text.trim().isEmpty) _tab = 1;
      });
      return loaded;
    } catch (error) {
      if (mounted) {
        setState(() {
          _saveUncertain = error is ApiException && error.uncertain;
          _feedback = error.toString();
        });
      }
      return false;
    } finally {
      if (mounted) {
        _key.clear();
        setState(() => _saving = false);
      }
    }
  }

  Future<void> _setDefault() async {
    setState(() => _changing = true);
    try {
      await widget.api.exclusive(
        () => widget.api.postJson('/api/model-platforms/default-selection', {
          'id': _selectedId,
          'revision': _registry['revision'],
        }),
      );
      await _load(selectId: _selectedId);
      if (mounted) {
        setState(() => _feedback = '新会话将使用此平台；现有会话保留原选择。');
        widget.onChanged();
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) setState(() => _changing = false);
    }
  }

  Future<void> _disable() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('停用此平台并清除配置？'),
        content: const Text('保存的密钥、地址和默认模型将被清除。重新配置后才可使用；已有会话不会被删除。'),
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
      enabled: enabled && !_busy,
      obscureText: password,
      autocorrect: false,
      enableSuggestions: !password,
      maxLength: password
          ? null
          : title == '平台名称'
          ? 80
          : 512,
      onChanged: (_) => setState(() {
        _dirty = true;
        _connectionResult = null;
      }),
      validator: validator,
      decoration: InputDecoration(hintText: hint, counterText: ''),
    ),
  );

  String _stateLabel(JsonMap item) => item['provider'] == 'none'
      ? '未配置'
      : item['configured'] == true
      ? '已配置'
      : item['connectionConfigured'] == true
      ? '待选模型'
      : '待配置';

  Widget _badge(String text) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      borderRadius: BorderRadius.circular(3),
    ),
    child: Text(text, style: Theme.of(context).textTheme.labelSmall),
  );

  /// 选中高亮仅表示正在编辑，星形“新会话默认”另行展示，避免两种状态混淆。
  Widget _platformList() {
    final query = _platformSearch.text.trim().toLowerCase();
    final items = _items
        .where(
          (item) => '${item['name']} ${_protocols[item['provider']] ?? ''}'
              .toLowerCase()
              .contains(query),
        )
        .toList();
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: TextField(
            key: const ValueKey('platform-search'),
            controller: _platformSearch,
            onChanged: (_) => setState(() {}),
            decoration: const InputDecoration(
              hintText: '搜索平台',
              prefixIcon: Icon(Icons.search, size: 17),
              prefixIconConstraints: BoxConstraints(minWidth: 32),
            ),
          ),
        ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.symmetric(horizontal: 8),
            children: [
              if (items.isEmpty)
                const Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('没有匹配的平台'),
                ),
              for (final item in items)
                Padding(
                  padding: const EdgeInsets.only(bottom: 4),
                  child: Material(
                    color: !_choosingPreset && item['id'] == _selectedId
                        ? Theme.of(context).colorScheme.surfaceContainerHighest
                        : Colors.transparent,
                    borderRadius: BorderRadius.circular(4),
                    child: InkWell(
                      key: ValueKey('platform-item:${item['id']}'),
                      borderRadius: BorderRadius.circular(4),
                      onTap: _busy
                          ? null
                          : () => _select(item['id'].toString()),
                      child: Padding(
                        padding: const EdgeInsets.all(10),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Row(
                              children: [
                                Expanded(
                                  child: Text(
                                    item['id'] == 'default'
                                        ? '内置平台'
                                        : item['name'].toString(),
                                    overflow: TextOverflow.ellipsis,
                                    style: Theme.of(context)
                                        .textTheme
                                        .titleSmall,
                                  ),
                                ),
                                if (_registry['defaultPlatformId'] ==
                                    item['id'])
                                  const Tooltip(
                                    message: '新会话默认平台',
                                    child: Icon(Icons.star, size: 14),
                                  ),
                              ],
                            ),
                            const SizedBox(height: 4),
                            Text(
                              '${_protocols[item['provider']] ?? '尚未选择服务'} · ${_stateLabel(item)}',
                              style: Theme.of(context).textTheme.bodySmall,
                              maxLines: 2,
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.all(12),
          child: SizedBox(
            width: double.infinity,
            child: OutlinedButton.icon(
              onPressed: _busy ? null : () => _select(null),
              icon: const Icon(Icons.add, size: 16),
              label: const Text('添加平台'),
            ),
          ),
        ),
      ],
    );
  }

  Widget _presetPicker() => ListView(
    padding: const EdgeInsets.all(20),
    children: [
      Text('添加模型平台', style: Theme.of(context).textTheme.titleMedium),
      const SizedBox(height: 8),
      const Text('选择服务类型，再配置连接和模型。'),
      const SizedBox(height: 20),
      for (var index = 0; index < _presets.length; index++)
        Padding(
          padding: const EdgeInsets.only(bottom: 8),
          child: OutlinedButton(
            key: ValueKey('provider-preset:$index'),
            style: OutlinedButton.styleFrom(padding: const EdgeInsets.all(14)),
            onPressed: _busy ? null : () => _preset(index),
            child: Row(
              children: [
                const Icon(Icons.hub_outlined, size: 20),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        _presets[index].name,
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 5),
                      Text(
                        _presets[index].description,
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),
                const Icon(Icons.chevron_right, size: 18),
              ],
            ),
          ),
        ),
    ],
  );

  Widget _detailHeader(bool narrow) => Container(
    padding: const EdgeInsets.fromLTRB(16, 10, 10, 10),
    decoration: BoxDecoration(
      border: Border(bottom: BorderSide(color: Theme.of(context).dividerColor)),
    ),
    child: Row(
      children: [
        if (narrow) ...[
          IconButton(
            tooltip: '平台列表',
            onPressed: () => setState(() => _showList = true),
            icon: const Icon(Icons.arrow_back, size: 17),
          ),
          const SizedBox(width: 6),
        ],
        Expanded(
          child: Wrap(
            spacing: 8,
            runSpacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Text(
                _choosingPreset ? '新增平台' : _title,
                style: Theme.of(context).textTheme.titleSmall,
              ),
              if (!_choosingPreset && _selectedId != null)
                _badge(_stateLabel(_current)),
              if (!_choosingPreset && _isDefault) _badge('新会话默认'),
            ],
          ),
        ),
        if (!_choosingPreset && _selectedId != null) ...[
          IconButton(
            tooltip: _isDefault
                ? '已是新会话默认平台'
                : _dirty
                ? '保存修改后设为默认'
                : '设为新会话默认',
            onPressed:
                _busy ||
                    _dirty ||
                    _isDefault ||
                    _current['configured'] != true ||
                    (_current['model']?.toString() ?? '').isEmpty
                ? null
                : _setDefault,
            icon: Icon(_isDefault ? Icons.star : Icons.star_border, size: 17),
          ),
          if (!_disabled)
            IconButton(
              tooltip: _dirty ? '保存修改后可停用' : '停用平台并清除配置',
              onPressed: _busy || _dirty ? null : _disable,
              icon: const Icon(Icons.power_settings_new, size: 17),
            ),
        ],
      ],
    ),
  );

  Widget _configuration() => SingleChildScrollView(
    key: const PageStorageKey('provider-config-scroll'),
    padding: const EdgeInsets.all(16),
    child: Form(
      key: _form,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_selectedId == 'default')
            Padding(
              padding: const EdgeInsets.only(bottom: 14),
              child: Text(
                '内置平台保留为默认配置兼容入口；新会话默认平台可单独指定。',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
          if (_disabled)
            Padding(
              padding: const EdgeInsets.only(bottom: 14),
              child: Row(
                children: [
                  const Icon(Icons.info_outline, size: 17),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      '此平台尚未配置。填写连接后保存即可启用。',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                ],
              ),
            ),
          PageSection(
            title: '连接配置',
            children: [
              if (_selectedId != 'default')
                _field(
                  '平台名称',
                  _name,
                  hint: '用于区分账户或服务',
                  validator: (value) =>
                      (value ?? '').trim().isEmpty ? '请填写平台名称' : null,
                ),
              PageProperty(
                label: '服务类型',
                child: WorkbenchDropdown<String>(
                  key: ValueKey('provider:$_selectedId:$_provider'),
                  value: _provider,
                  items: [
                    for (final entry in _protocols.entries)
                      DropdownMenuItem(
                        value: entry.key,
                        child: Text(entry.value),
                      ),
                  ],
                  onChanged: _busy
                      ? null
                      : (value) => setState(() {
                          _provider = value!;
                          _base.text = _presets
                              .firstWhere((entry) => entry.provider == value)
                              .base;
                          _model.clear();
                          _key.clear();
                          _models = [];
                          _catalogState = {};
                          _connectionResult = null;
                          _catalogRequest++;
                          _dirty = true;
                        }),
                ),
              ),
              _field(
                'API Base URL',
                _base,
                hint: 'https://…',
                validator: (value) {
                  final uri = Uri.tryParse(value ?? '');
                  return uri?.scheme != 'https' || uri!.host.isEmpty
                      ? '请输入完整的 HTTPS API 根地址'
                      : null;
                },
              ),
              _field(
                'API 密钥',
                _key,
                password: true,
                hint: _current['keyConfigured'] == true && !_identityChanged
                    ? '已保存，留空继续使用'
                    : '填写此服务的 API 密钥',
              ),
              if (_identityChanged)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Text(
                    '服务类型或地址已变化，请重新填写对应密钥。',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
              Wrap(
                spacing: 10,
                runSpacing: 8,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  OutlinedButton.icon(
                    onPressed:
                        _busy ||
                            _networkBusy ||
                            _dirty ||
                            _selectedId == null ||
                            !_connectionConfigured
                        ? null
                        : _testConnection,
                    icon: _networkBusy
                        ? const SizedBox.square(
                            dimension: 14,
                            child: CircularProgressIndicator(strokeWidth: 1.5),
                          )
                        : const Icon(Icons.network_check, size: 16),
                    label: const Text('测试连接'),
                  ),
                  Text(
                    _dirty || _selectedId == null
                        ? '保存后可测试连接'
                        : '仅检查目录接口，不生成回复',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ],
              ),
              if (_connectionResult != null)
                Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: Text(
                    '${_connectionResult!['message'] ?? '连接测试已结束。'}'
                    '${_connectionResult!['elapsedMs'] is num ? ' · ${_connectionResult!['elapsedMs']} ms' : ''}',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
            ],
          ),
          WorkbenchExpansionTile(
            storageId: 'provider-advanced:${_selectedId ?? "new"}',
            tilePadding: EdgeInsets.zero,
            childrenPadding: const EdgeInsets.only(top: 8),
            dense: true,
            shape: const Border(),
            collapsedShape: const Border(),
            title: const Text('高级设置'),
            children: [
              _field(
                '等待超时（秒）',
                _timeout,
                validator: (value) {
                  final seconds = double.tryParse(value ?? '');
                  return seconds == null ||
                          !seconds.isFinite ||
                          seconds < 5 ||
                          seconds > 180
                      ? '填写 5～180 秒'
                      : null;
                },
              ),
              const Align(
                alignment: Alignment.centerLeft,
                child: Text('持续收到模型输出时会延长等待。'),
              ),
            ],
          ),
        ],
      ),
    ),
  );

  /// 目录用带搜索的普通列表，默认模型和目录筛选不共用输入值；点击一项只是
  /// 修改本地草稿，必须保存后才更改平台默认模型。
  Widget _modelDirectory() {
    final query = _modelSearch.text.trim().toLowerCase();
    final models = _models
        .where(
          (model) => '${model['id']} ${model['label'] ?? ''}'
              .toLowerCase()
              .contains(query),
        )
        .toList();
    // 查询区与列表共用滚动视口；很矮的窗口或较长错误提示出现时，固定字段
    // 不能挤占全部剩余高度。页面保存栏仍由外层 Column 固定在底部。
    return CustomScrollView(
      key: const PageStorageKey('provider-model-list'),
      slivers: [
        SliverToBoxAdapter(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 16, 16, 6),
                child: _field('默认模型 ID', _model, hint: '从下方选择，或填写模型 ID'),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                child: Text(
                  '会话可单独选择模型；平台默认用于未指定模型的请求。',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Row(
                  children: [
                    Expanded(
                      child: TextField(
                        key: const ValueKey('model-search'),
                        controller: _modelSearch,
                        onChanged: (_) => setState(() {}),
                        decoration: const InputDecoration(
                          hintText: '搜索模型目录',
                          prefixIcon: Icon(Icons.search, size: 17),
                          prefixIconConstraints: BoxConstraints(minWidth: 32),
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Tooltip(
                      message: _dirty ? '先保存连接配置或模型修改' : '向已保存平台获取模型目录',
                      child: OutlinedButton.icon(
                        onPressed:
                            _busy ||
                                _networkBusy ||
                                _dirty ||
                                _selectedId == null ||
                                !_connectionConfigured
                            ? null
                            : () => _catalog(refresh: true),
                        icon: _networkBusy
                            ? const SizedBox.square(
                                dimension: 14,
                                child: CircularProgressIndicator(
                                  strokeWidth: 1.5,
                                ),
                              )
                            : const Icon(Icons.sync, size: 16),
                        label: const Text('获取模型'),
                      ),
                    ),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
                child: Text(
                  _models.isEmpty
                      ? '模型目录不会自动联网获取；也可直接填写模型 ID。'
                      : '${models.length} / ${_models.length} 个模型 · ${_catalogState['stale'] == true ? '缓存已过期，可手动更新' : '本地目录'}',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
              const Divider(height: 1),
            ],
          ),
        ),
        if (models.isEmpty)
          SliverFillRemaining(
            hasScrollBody: false,
            child: Center(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Text(
                  query.isNotEmpty
                      ? '没有匹配的模型'
                      : _selectedId == null || _dirty
                      ? '先保存连接配置，再获取模型。'
                      : '尚无模型目录，点击“获取模型”。',
                ),
              ),
            ),
          )
        else
          SliverList.builder(
            itemCount: models.length,
            itemBuilder: (context, index) {
              final model = models[index],
                  selected = _model.text == models[index]['id'];
              return ListTile(
                key: ValueKey('model-item:${model['id']}'),
                dense: true,
                selected: selected,
                leading: Icon(
                  selected
                      ? Icons.radio_button_checked
                      : Icons.radio_button_off,
                  size: 17,
                ),
                title: Text(model['id'].toString()),
                subtitle:
                    model['label'] != null && model['label'] != model['id']
                    ? Text(model['label'].toString())
                    : null,
                trailing: selected ? const Text('默认') : null,
                onTap: _busy
                    ? null
                    : () => setState(() {
                        _model.text = model['id'].toString();
                        _dirty = true;
                      }),
              );
            },
          ),
      ],
    );
  }

  Widget _details(bool narrow) => Column(
    children: [
      _detailHeader(narrow),
      if (!_choosingPreset)
        Container(
          height: 42,
          padding: const EdgeInsets.symmetric(horizontal: 16),
          alignment: Alignment.centerLeft,
          decoration: BoxDecoration(
            border: Border(
              bottom: BorderSide(color: Theme.of(context).dividerColor),
            ),
          ),
          child: Row(
            children: [
              for (final entry in [(0, '连接配置'), (1, '模型目录')])
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: TextButton(
                    key: ValueKey('settings-tab:${entry.$1}'),
                    style: TextButton.styleFrom(
                      backgroundColor: _tab == entry.$1
                          ? Theme.of(context)
                                .colorScheme
                                .surfaceContainerHighest
                          : null,
                    ),
                    onPressed: _loading
                        ? null
                        : () => setState(() => _tab = entry.$1),
                    child: Text(entry.$2),
                  ),
                ),
            ],
          ),
        ),
      Expanded(
        child: _choosingPreset
            ? _presetPicker()
            : _tab == 0
            ? _configuration()
            : _modelDirectory(),
      ),
    ],
  );

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) => Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        PageToolbar(
          title: '模型服务',
          actions: [
            IconButton(
              tooltip: '重新加载平台配置',
              onPressed: _busy
                  ? null
                  : () async {
                      if (await maybeLeave() && mounted) await _load();
                    },
              icon: const Icon(Icons.refresh, size: 18),
            ),
            IconButton(
              tooltip: '新增模型平台',
              onPressed: _busy ? null : () => _select(null),
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
              : LayoutBuilder(
                  builder: (context, constraints) {
                    final narrow = constraints.maxWidth < 760;
                    if (narrow) {
                      return _showList ? _platformList() : _details(true);
                    }
                    return Row(
                      children: [
                        SizedBox(width: 228, child: _platformList()),
                        VerticalDivider(
                          width: 1,
                          color: Theme.of(context).dividerColor,
                        ),
                        Expanded(child: _details(false)),
                      ],
                    );
                  },
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
                  _saving
                      ? '正在保存…'
                      : _networkBusy
                      ? '正在连接…'
                      : _saveUncertain
                      ? '保存结果待核对，请重新加载'
                      : _dirty
                      ? '有未保存的修改'
                      : _choosingPreset
                      ? '选择服务类型'
                      : _disabled
                      ? '尚未配置'
                      : '已保存到本机',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
              const SizedBox(width: 8),
              FilledButton.icon(
                key: const ValueKey('save-platform'),
                onPressed: _busy || _choosingPreset || !_dirty || _saveUncertain
                    ? null
                    : _save,
                icon: const Icon(Icons.check, size: 16),
                label: const Text('保存配置'),
              ),
            ],
          ),
        ),
      ],
    ),
  );

  @override
  void dispose() {
    ++_loadRequest;
    ++_catalogRequest;
    for (final controller in [
      _name,
      _model,
      _base,
      _key,
      _timeout,
      _platformSearch,
      _modelSearch,
    ]) {
      controller.dispose();
    }
    super.dispose();
  }
}
