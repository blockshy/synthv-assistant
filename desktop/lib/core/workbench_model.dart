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
  String get conversationId => conversation?['id']?.toString() ?? '';
  List<JsonMap> get messages => maps(conversation?['messages']);

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
    platforms = maps(response['items']);
    if (conversation == null) {
      platformId = response['defaultPlatformId']?.toString() ?? 'default';
    }
    notifyListeners();
  }

  Future<void> refreshStatus() async {
    status = Map<String, dynamic>.from(await api.getJson('/api/status') as Map);
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
    try {
      final result = await api.postJson('/api/model-capabilities', {
        'platformId': platformId,
        'model': modelId,
      });
      if (generation != _capabilityGeneration) return;
      capabilities = Map<String, dynamic>.from(result as Map);
      // 保留不兼容的旧选项，并在发送前交由后端拒绝；不悄悄降低用户推理强度。
      notifyListeners();
    } catch (e) {
      if (generation != _capabilityGeneration) return;
      error = e.toString();
      notifyListeners();
    }
  }

  Future<void> choosePlatform(String value) async {
    if (navigationBusy || sending || api.busy) return;
    platformId = value;
    modelId = '';
    models = [];
    notifyListeners();
    await Future.wait([savePreferences(), loadCatalog(), loadCapabilities()]);
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
