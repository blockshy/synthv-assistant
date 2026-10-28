import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/core/workbench_model.dart';

const conversationId = '11111111111111111111111111111111';
const taskId = '22222222222222222222222222222222';
const otherConversationId = '33333333333333333333333333333333';

/// 测试只模拟本机 API；不载入真实配置、不请求云端、不连接 SynthV。
class ModelApi extends WorkbenchApi {
  ModelApi() : super(port: 1, token: 'fixture-token');
  final calls = <String>[];
  final answer = Completer<dynamic>();
  bool loseSubmitResponse = false;
  JsonMap document = {
    'id': conversationId,
    'title': '测试会话',
    'messages': <JsonMap>[],
    'renderMode': 'smooth',
    'modelOptions': {
      'platformId': 'default',
      'model': '',
      'reasoningEffort': 'default',
    },
  };
  @override
  Future<dynamic> getJson(String path) async {
    calls.add('GET $path');
    if (path == '/api/conversations') {
      return {
        'items': [document],
      };
    }
    if (path == '/api/conversations/$conversationId') return document;
    if (path == '/api/desktop/status') {
      return {
        'busy': true,
        'runningJobIds': [taskId],
      };
    }
    if (path == '/api/model-platforms') {
      return {
        'items': [
          {'id': 'default', 'name': '默认平台'},
        ],
        'defaultPlatformId': 'default',
      };
    }
    if (path.endsWith('/models')) {
      return {
        'models': [
          {'id': 'fixture-model'},
        ],
      };
    }
    return {};
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    calls.add('POST $path');
    if (path == '/api/conversations') return document;
    if (path == '/api/model-capabilities') {
      return {
        'reasoning': {
          'options': [
            {'value': 'default', 'label': '模型默认'},
          ],
        },
      };
    }
    if (path.endsWith('/messages')) {
      if (loseSubmitResponse) throw const ApiException('连接中断', uncertain: true);
      return {'jobId': taskId};
    }
    return document;
  }

  @override
  Future<dynamic> waitForJob(String id, {void Function(JsonMap)? onProgress}) {
    calls.add('WAIT $id');
    onProgress?.call({'stage': '正在接收回复', 'reasoning': '这是实际接口返回的测试摘要'});
    return answer.future;
  }
}

/// 可控的迟到读回和偏好保存，用于重现实际网络等待顺序。
class RacingApi extends ModelApi {
  final Map<String, Completer<dynamic>> delayedReads = {};
  final List<(String, JsonMap)> writes = [];
  Completer<void>? blockFirstPreference;
  @override
  Future<dynamic> getJson(String path) {
    final delayed = delayedReads[path];
    return delayed?.future ?? super.getJson(path);
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    writes.add((path, Map<String, dynamic>.from(body)));
    if (path.endsWith('/model-options') && blockFirstPreference != null) {
      final pending = blockFirstPreference!;
      blockFirstPreference = null;
      await pending.future;
    }
    return super.postJson(path, body);
  }
}

void main() {
  test('状态读取失败立即撤掉在线与可写快照，通知界面并允许后续恢复', () async {
    final api = RacingApi();
    final model = WorkbenchModel(api);
    addTearDown(model.dispose);
    addTearDown(api.dispose);
    final displayed = <JsonMap>[];
    model.addListener(
      () => displayed.add(Map<String, dynamic>.from(model.status)),
    );

    api.delayedReads['/api/status'] = Completer<dynamic>()
      ..complete({
        'bridge': {'connected': true, 'session': 'fixture-session'},
        'writeEnabled': true,
      });
    await model.refreshStatus();
    expect(displayed.single['writeEnabled'], isTrue);

    // 先建立错误监听，再模拟本机连接中断，避免测试自身产生未处理异步异常。
    final unavailable = Completer<dynamic>();
    api.delayedReads['/api/status'] = unavailable;
    final failed = model.refreshStatus();
    const failure = ApiException('合成连接中断', uncertain: true);
    final checked = expectLater(failed, throwsA(same(failure)));
    unavailable.completeError(failure);
    await checked;
    expect(model.status, isEmpty);
    expect(displayed.length, 2);
    expect(displayed.last, isEmpty);

    api.delayedReads['/api/status'] = Completer<dynamic>()
      ..complete({
        'bridge': {'connected': true, 'session': 'recovered-session'},
        'writeEnabled': false,
      });
    await model.refreshStatus();
    expect(displayed.length, 3);
    expect(displayed.last['bridge']['session'], 'recovered-session');
    expect(displayed.last['writeEnabled'], isFalse);
    expect(api.writes, isEmpty);
  });

  test('切换会话期间禁止发送，迟到打开结果不会改变已开始请求的目标', () async {
    final api = RacingApi();
    final read = Completer<dynamic>();
    api.delayedReads['/api/conversations/$otherConversationId'] = read;
    final model = WorkbenchModel(api)..conversation = api.document;
    model.draft = '属于原会话的草稿';
    final opened = model.openConversation(otherConversationId);
    expect(model.navigationBusy, isTrue);
    await model.send();
    expect(
      api.writes.where((entry) => entry.$1.endsWith('/messages')),
      isEmpty,
    );
    read.complete({...api.document, 'id': otherConversationId});
    await opened;
    expect(model.conversationId, otherConversationId);
    expect(model.navigationBusy, isFalse);
    model.dispose();
    api.dispose();
  });

  test('旧会话重载迟到时不覆盖用户已打开的新会话', () async {
    final api = RacingApi();
    final stale = Completer<dynamic>();
    api.delayedReads['/api/conversations/$conversationId'] = stale;
    api.delayedReads['/api/conversations/$otherConversationId'] =
        Completer<dynamic>()
          ..complete({...api.document, 'id': otherConversationId});
    final model = WorkbenchModel(api)..conversation = api.document;
    final reloaded = model.reloadConversation();
    await model.openConversation(otherConversationId);
    stale.complete(api.document);
    await reloaded;
    expect(model.conversationId, otherConversationId);
    model.dispose();
    api.dispose();
  });

  test('偏好保存按操作顺序排队且模型与绘制模式来自同一份快照', () async {
    final api = RacingApi();
    final first = Completer<void>();
    api.blockFirstPreference = first;
    final model = WorkbenchModel(api)..conversation = api.document;
    model.platformId = 'first';
    model.renderMode = 'smooth';
    final savedFirst = model.savePreferences();
    await Future<void>.delayed(Duration.zero);
    model.platformId = 'second';
    model.renderMode = 'points';
    final savedSecond = model.savePreferences();
    expect(api.writes.length, 1);
    first.complete();
    await Future.wait([savedFirst, savedSecond]);
    expect(api.writes.map((entry) => entry.$2).toList(), [
      {'platformId': 'first', 'model': '', 'reasoningEffort': 'default'},
      {'renderMode': 'smooth'},
      {'platformId': 'second', 'model': '', 'reasoningEffort': 'default'},
      {'renderMode': 'points'},
    ]);
    model.dispose();
    api.dispose();
  });

  test('恢复任务返回其他会话时拒绝覆盖且继续锁定重复发送', () async {
    final api = ModelApi()..loseSubmitResponse = true;
    final model = WorkbenchModel(api)..draft = '本次请求';
    await model.send();
    api.answer.complete({...api.document, 'id': otherConversationId});
    await model.reconnectJob();
    expect(model.conversationId, conversationId);
    expect(model.submissionUnknown, isTrue);
    expect(model.error, contains('不一致'));
    model.dispose();
    api.dispose();
  });

  test('发送即显示本地消息，最终正式回复原子替换，不重复调用模型', () async {
    final api = ModelApi();
    final model = WorkbenchModel(api)..draft = '保留现有音高';
    final sent = model.send();
    expect(model.pendingMessage?['text'], '保留现有音高');
    expect(model.sending, isTrue);
    await Future<void>.delayed(Duration.zero);
    expect(model.pendingJobId, taskId);
    expect(model.progress['reasoning'], contains('测试摘要'));
    api.answer.complete({
      ...api.document,
      'messages': [
        {'id': 'user-1', 'role': 'user', 'text': '保留现有音高'},
        {'id': 'assistant-1', 'role': 'assistant', 'text': '先讨论目标。'},
      ],
    });
    await sent;
    expect(model.pendingMessage, isNull);
    expect(model.messages.length, 2);
    expect(api.calls.where((e) => e.endsWith('/messages')).length, 1);
    model.dispose();
    api.dispose();
  });

  test('切换和初始化仅读取模型缓存，显式刷新才提交目录请求', () async {
    final api = ModelApi();
    final model = WorkbenchModel(api);
    await model.initialize();
    expect(model.models.single['id'], 'fixture-model');
    expect(
      api.calls.where((e) => e.startsWith('POST') && e.endsWith('/models')),
      isEmpty,
    );
    await model.loadCatalog(refresh: true);
    expect(
      api.calls
          .where((e) => e.startsWith('POST') && e.endsWith('/models'))
          .length,
      1,
    );
    model.dispose();
    api.dispose();
  });

  test('提交响应丢失后核对并恢复同一任务，禁止自动重发', () async {
    final api = ModelApi()..loseSubmitResponse = true;
    final model = WorkbenchModel(api)..draft = '分析这句';
    await model.send();
    expect(model.submissionUnknown, isTrue);
    await model.send();
    expect(api.calls.where((e) => e.endsWith('/messages')).length, 1);
    api.answer.complete({
      ...api.document,
      'messages': [
        {'id': 'reply', 'role': 'assistant', 'text': '已完成分析'},
      ],
    });
    await model.reconnectJob();
    expect(api.calls, contains('WAIT $taskId'));
    expect(api.calls.where((e) => e.endsWith('/messages')).length, 1);
    expect(model.submissionUnknown, isFalse);
    expect(model.pendingJobId, isNull);
    model.dispose();
    api.dispose();
  });
}
