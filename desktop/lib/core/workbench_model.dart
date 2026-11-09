import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api.dart';

List<JsonMap> maps(dynamic value) => value is List
    ? value.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList()
    : <JsonMap>[];

/// 会话状态独立于页面生命周期，切换素材/设置后仍保留未发送文字和附件。
/// 新消息先在内存显示，正式记录返回后按旧消息编号去重，不伪造持久化成功。
class WorkbenchModel extends ChangeNotifier {
  WorkbenchModel(this.api);
  final WorkbenchApi api;
  List<JsonMap> conversations = [];
  List<JsonMap> platforms = [];
  List<JsonMap> models = [];
  JsonMap? conversation;
  JsonMap status = {};
  JsonMap capabilities = {};
  JsonMap progress = {};
  JsonMap? pendingMessage;
  String? pendingJobId;
  String error = '';
  String draft = '';
  List<JsonMap> attachments = [];
  String platformId = 'default';
  String modelId = '';
  String reasoning = 'default';
  String renderMode = 'smooth';
  bool includeSelection = false;
  bool sending = false;
  bool navigationBusy = false;
  bool submissionUnknown = false;
  int elapsedSeconds = 0;
  int _generation = 0;
  int _capabilityGeneration = 0;
  int _catalogGeneration = 0;
  Timer? _timer;
  DateTime? _startedAt;
  final Map<String, (String, List<JsonMap>)> _drafts = {};
  Set<String> _previousIds = {};
  Future<void> _preferenceQueue = Future<void>.value();
  String? _submittedConversationId;
  bool _disposed = false;
  bool _draftModelChosen = false;
  String get conversationId => conversation?['id']?.toString() ?? '';
  List<JsonMap> get messages => maps(conversation?['messages']);

  /// 展示实际会使用的平台和模型；“平台默认”只是选择方式，不能代替模型名称。
  JsonMap? get selectedPlatform =>
      platforms.where((item) => item['id'] == platformId).firstOrNull;
  String get effectiveModel => modelId.trim().isNotEmpty
      ? modelId.trim()
      : selectedPlatform?['model']?.toString() ?? '';

  /// 能力快照同时属于模型选择与连接配置。公开版本号覆盖密钥替换等不可见变化，
  /// 不读取或保存密钥本身；列表名称等展示内容不参与身份判断。
  List<Object?> get _capabilityIdentity {
    final platform = selectedPlatform;
    return [
      platformId,
      modelId,
      effectiveModel,
      platform?['provider'],
      platform?['baseUrl'],
      platform?['keyConfigured'],
      platform?['connectionConfigured'],
      platform?['configured'],
      platform?['revision'],
    ];
  }

  /// 配置缺失提前在输入区给出可操作的入口；后端仍是最终的请求校验边界。
  /// 空清单可能是初始化尚未完成，由 navigationBusy/原读取错误负责提示。
  String? get sendBlockReason {
    if (platforms.isEmpty) return null;
    final platform = selectedPlatform;
    if (platform == null) return '所选模型服务不存在，请重新选择。';
    if (platform['provider'] == 'none') return '请先配置一个模型服务。';
    if (platform['connectionConfigured'] != true &&
        platform['configured'] != true) {
      return '请先完成所选服务的连接配置。';
    }
    if (effectiveModel.isEmpty) return '请选择本次会话使用的模型。';
    if (attachments.isNotEmpty && capabilities['audioInput'] == 'unsupported') {
      return '当前模型不支持音频，请更换模型或移除附件。';
    }
    return null;
  }

  Future<void> initialize() async {
    navigationBusy = true;
    notifyListeners();
    try {
      await Future.wait([
        refreshConversations(),
        refreshPlatforms(),
        refreshStatus(),
      ]);
      await loadCatalog();
      await loadCapabilities();
    } catch (e) {
      error = e.toString();
      notifyListeners();
    } finally {
      navigationBusy = false;
      notifyListeners();
    }
  }

  Future<void> refreshConversations() async {
    conversations = maps((await api.getJson('/api/conversations'))['items']);
    notifyListeners();
  }

  Future<void> refreshPlatforms() async {
    final response = await api.getJson('/api/model-platforms');
    if (_disposed) return;
    final previousIdentity = _capabilityIdentity;
    platforms = maps(response['items']);
    if (conversation == null && !_draftModelChosen) {
      platformId = response['defaultPlatformId']?.toString() ?? 'default';
    }
    if (!listEquals(previousIdentity, _capabilityIdentity)) {
      // 平台默认模型或连接身份变化后，旧音频/推理说明立即失效。
      // 同时作废在途读取，防止它稍后又把已清空的旧能力写回来。
      ++_capabilityGeneration;
      capabilities = {};
    }
    notifyListeners();
  }

  Future<void> refreshStatus() async {
    try {
      status = Map<String, dynamic>.from(
        await api.getJson('/api/status') as Map,
      );
    } catch (_) {
      // 连接失败意味着上一次心跳和写入权限已经无法确认。立即清空并通知，
      // 即使后台轮询调用者选择不显示错误，也不能继续把旧状态显示为在线可写。
      status = {};
      notifyListeners();
      rethrow;
    }
    notifyListeners();
  }

  void _storeDraft() =>
      _drafts[conversationId] = (draft, List<JsonMap>.from(attachments));

  Future<void> openConversation(String id) async {
    if (navigationBusy ||
        sending ||
        submissionUnknown ||
        pendingJobId != null ||
        api.busy) {
      return;
    }
    navigationBusy = true;
    _storeDraft();
    final generation = ++_generation;
    api.invalidatePreviews();
    notifyListeners();
    try {
      await _preferenceQueue;
      final result = Map<String, dynamic>.from(
        await api.getJson('/api/conversations/$id') as Map,
      );
      if (_disposed || generation != _generation) return;
      conversation = result;
      _draftModelChosen = false;
      final saved = _drafts[id];
      draft = saved?.$1 ?? '';
      attachments = List<JsonMap>.from(saved?.$2 ?? []);
      pendingMessage = null;
      error = '';
      _readOptions();
      notifyListeners();
      await Future.wait([loadCatalog(), loadCapabilities()]);
    } catch (e) {
      if (generation == _generation) error = e.toString();
    } finally {
      navigationBusy = false;
      notifyListeners();
    }
  }

  Future<void> newConversation() async {
    if (navigationBusy ||
        sending ||
        submissionUnknown ||
        pendingJobId != null ||
        api.busy) {
      return;
    }
    navigationBusy = true;
    final generation = ++_generation;
    _storeDraft();
    notifyListeners();
    try {
      await _preferenceQueue;
      final created = Map<String, dynamic>.from(
        await api.postJson('/api/conversations') as Map,
      );
      if (_disposed || generation != _generation) return;
      conversation = created;
      _draftModelChosen = false;
      api.invalidatePreviews();
      draft = '';
      attachments = [];
      pendingMessage = null;
      error = '';
      _readOptions();
      notifyListeners();
      await Future.wait([
        refreshConversations(),
        loadCatalog(),
        loadCapabilities(),
      ]);
    } catch (e) {
      if (generation == _generation) error = e.toString();
    } finally {
      navigationBusy = false;
      notifyListeners();
    }
  }

  void _readOptions() {
    final options = conversation?['modelOptions'] as Map? ?? {};
    platformId = options['platformId']?.toString() ?? 'default';
    modelId = options['model']?.toString() ?? '';
    reasoning = options['reasoningEffort']?.toString() ?? 'default';
    renderMode = conversation?['renderMode']?.toString() ?? 'smooth';
  }

  Future<void> reloadConversation() async {
    if (conversationId.isEmpty) return;
    final id = conversationId;
    final generation = _generation;
    final result = Map<String, dynamic>.from(
      await api.getJson('/api/conversations/$id') as Map,
    );
    // 预览回调的迟到读回不能把用户已经打开的另一会话覆盖回去。
    if (_disposed || generation != _generation || id != conversationId) return;
    conversation = result;
    notifyListeners();
    await refreshConversations();
  }

  Future<void> loadCatalog({bool refresh = false}) async {
    final generation = ++_catalogGeneration;
    final selected = platformId;
    try {
      final response = refresh
          ? await api.postJson('/api/model-platforms/$selected/models')
          : await api.getJson('/api/model-platforms/$selected/models');
      if (selected != platformId || generation != _catalogGeneration) return;
      models = maps(response['models']);
      if (refresh && response['status'] == 'error') {
        error = response['message']?.toString() ?? '模型目录获取失败。';
      }
      notifyListeners();
    } catch (e) {
      if (generation != _catalogGeneration) return;
      error = e.toString();
      notifyListeners();
    }
  }

  Future<void> loadCapabilities() async {
    final generation = ++_capabilityGeneration;
    final selectedPlatformId = platformId;
    final selectedModelId = modelId;
    final identity = _capabilityIdentity;
    // 新读取尚未完成或失败时保持未知状态，不能用上一模型的 unsupported 禁发。
    capabilities = {};
    notifyListeners();
    bool isCurrent() =>
        !_disposed &&
        generation == _capabilityGeneration &&
        selectedPlatformId == platformId &&
        selectedModelId == modelId &&
        listEquals(identity, _capabilityIdentity);
    try {
      final result = await api.postJson('/api/model-capabilities', {
        'platformId': selectedPlatformId,
        'model': selectedModelId,
      });
      if (!isCurrent()) return;
      capabilities = Map<String, dynamic>.from(result as Map);
      // 单纯刷新能力不更改已存选项；只有用户主动切换模型时才按新能力调整，
      // 并由 chooseModel 返回复位结果供界面提示。
      notifyListeners();
    } catch (e) {
      if (!isCurrent()) return;
      error = e.toString();
      notifyListeners();
    }
  }

  Future<void> choosePlatform(String value) async {
    await chooseModel(value, '');
  }

  /// 一次选择原子更新平台与模型，避免先换平台后误发到其默认模型。
  /// 新模型不支持旧推理档位时才复位，并将结果交给界面明确告知用户。
  Future<bool> chooseModel(String platform, String selectedModel) async {
    if (navigationBusy ||
        sending ||
        api.busy ||
        submissionUnknown ||
        pendingJobId != null) {
      return false;
    }
    navigationBusy = true;
    platformId = platform;
    modelId = selectedModel.trim();
    _draftModelChosen = true;
    models = [];
    capabilities = {};
    error = '';
    notifyListeners();
    var resetEffort = false;
    try {
      await Future.wait([loadCatalog(), loadCapabilities()]);
      final available = maps(capabilities['reasoning']?['options']);
      if (available.isNotEmpty &&
          !available.any((option) => option['value'] == reasoning)) {
        reasoning = 'default';
        resetEffort = true;
      }
    } finally {
      navigationBusy = false;
      notifyListeners();
    }
    await savePreferences();
    return resetEffort;
  }

  /// 显式修改会话选项后保存；不在初始化读取或目录缓存命中时写回配置。
  Future<void> savePreferences() {
    if (conversationId.isEmpty || api.busy || navigationBusy || sending) {
      return Future<void>.value();
    }
    final id = conversationId;
    final generation = _generation;
    final options = <String, dynamic>{
      'platformId': platformId,
      'model': modelId,
      'reasoningEffort': reasoning,
    };
    final mode = renderMode;
    // 按用户操作顺序串行保存，并捕获会话与全部字段，避免后完成的旧请求覆盖新选择。
    _preferenceQueue = _preferenceQueue.then((_) async {
      if (_disposed) return;
      try {
        await api.postJson('/api/conversations/$id/model-options', options);
        await api.postJson('/api/conversations/$id/render-mode', {
          'renderMode': mode,
        });
      } catch (e) {
        if (generation == _generation && id == conversationId) {
          error = e.toString();
          notifyListeners();
        }
      }
    });
    return _preferenceQueue;
  }

  void attach(List<JsonMap> clips) {
    final combined = <String, JsonMap>{
      for (final clip in [...attachments, ...clips])
        '${clip['kind']}:${clip['id']}': clip,
    };
    if (combined.length > 2) {
      error = '每条消息最多附带两段音频，请先移除已有附件。';
    } else {
      attachments = combined.values.toList();
    }
    notifyListeners();
  }

  Future<void> send() async {
    if (navigationBusy ||
        sending ||
        submissionUnknown ||
        pendingJobId != null ||
        api.busy ||
        draft.trim().isEmpty) {
      return;
    }
    if (sendBlockReason case final String reason) {
      error = reason;
      notifyListeners();
      return;
    }
    final text = draft.trim();
    final clips = List<JsonMap>.from(attachments);
    if (text.length > 4000) {
      error = '每条消息最多 4,000 字符。';
      notifyListeners();
      return;
    }
    final options = {
      'platformId': platformId,
      'model': modelId.trim(),
      'reasoningEffort': reasoning,
    };
    final mode = renderMode;
    final selection = includeSelection;
    _previousIds = messages.map((e) => e['id'].toString()).toSet();
    pendingMessage = {
      'role': 'user',
      'text': text,
      'attachments': clips,
      'delivery': '正在提交',
    };
    draft = '';
    attachments = [];
    error = '';
    _beginProgress();
    api.invalidatePreviews();
    notifyListeners();
    try {
      await _preferenceQueue;
      await api.exclusive(() async {
        conversation ??= Map<String, dynamic>.from(
          await api.postJson('/api/conversations') as Map,
        );
        // 一次请求从选项保存到提交都固定同一会话，不跨 await 重新读取可变状态。
        final targetId = conversationId;
        _submittedConversationId = targetId;
        await api.postJson(
          '/api/conversations/$targetId/model-options',
          options,
        );
        await api.postJson('/api/conversations/$targetId/render-mode', {
          'renderMode': mode,
        });
        final result = await api.postJson(
          '/api/conversations/$targetId/messages',
          {
            'text': text,
            'includeSelection': selection,
            'attachments': clips
                .map((e) => {'kind': e['kind'], 'id': e['id']})
                .toList(),
            'modelOptions': options,
            'renderMode': mode,
          },
        );
        pendingJobId = result['jobId'] as String;
        submissionUnknown = false;
        pendingMessage?['delivery'] = '已提交';
        notifyListeners();
        await _receiveJob();
      });
    } catch (e) {
      await _failed(e);
      if (pendingJobId == null && draft.isEmpty) {
        draft = text;
        attachments = clips;
      }
    } finally {
      _endProgress();
      notifyListeners();
    }
  }

  void _beginProgress() {
    sending = true;
    _startedAt = DateTime.now();
    progress = {'stage': '正在准备请求'};
    elapsedSeconds = 0;
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) {
      elapsedSeconds = DateTime.now().difference(_startedAt!).inSeconds;
      notifyListeners();
    });
  }

  void _endProgress() {
    sending = false;
    _timer?.cancel();
  }

  Future<void> _receiveJob() async {
    final result = await api.waitForJob(
      pendingJobId!,
      onProgress: (value) {
        progress = value;
        notifyListeners();
      },
    );
    if (result is! Map ||
        result['id'] != _submittedConversationId ||
        result['messages'] is! List) {
      pendingJobId = null;
      throw const ApiException('后台任务结果与当前会话不一致，请再次核对任务状态。', uncertain: true);
    }
    pendingJobId = null;
    conversation = Map<String, dynamic>.from(result);
    pendingMessage = null;
    await refreshConversations();
  }

  Future<void> _failed(Object e) async {
    error = e.toString();
    // POST 响应断开时不知道模型是否已开始；锁住再次发送，通过后台任务编号恢复。
    submissionUnknown =
        e is ApiException && e.uncertain && pendingJobId == null;
    if (e is! ApiException || !e.uncertain) pendingJobId = null;
    pendingMessage?['delivery'] = pendingJobId == null ? '请求失败' : '状态待确认';
    if (conversationId.isNotEmpty) {
      try {
        await reloadConversation();
        // 只用本次新增的正式用户消息替换即时消息，不能误匹配历史同文消息。
        final pending = pendingMessage;
        if (pending != null &&
            messages.any(
              (m) =>
                  m['role'] == 'user' &&
                  !_previousIds.contains(m['id']) &&
                  m['text'] == pending['text'],
            )) {
          pendingMessage = null;
        }
      } catch (_) {
        /* 原错误保留；读回失败不能触发再次发送。 */
      }
    }
  }

  Future<void> reconnectJob() async {
    if ((pendingJobId == null && !submissionUnknown) || api.busy) return;
    _beginProgress();
    error = '';
    try {
      await api.exclusive(() async {
        if (pendingJobId == null) {
          final status = await api.getJson('/api/desktop/status');
          final jobs = status['runningJobIds'] as List? ?? [];
          if (jobs.length == 1) {
            pendingJobId = jobs.single as String;
          } else if (status['busy'] == false) {
            await reloadConversation();
            pendingMessage = null;
            submissionUnknown = false;
            return;
          } else {
            throw const ApiException('后台仍在处理，请稍后再次核对任务状态。', uncertain: true);
          }
        }
        submissionUnknown = false;
        await _receiveJob();
      });
    } catch (e) {
      await _failed(e);
    } finally {
      _endProgress();
      notifyListeners();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    ++_generation;
    ++_capabilityGeneration;
    ++_catalogGeneration;
    _timer?.cancel();
    super.dispose();
  }

  /// 关闭窗口后的异步读回可以结束，但不得再通知已经销毁的界面。
  @override
  void notifyListeners() {
    if (!_disposed) super.notifyListeners();
  }
}
