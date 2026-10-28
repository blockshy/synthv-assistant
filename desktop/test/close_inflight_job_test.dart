import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/core/backend.dart';
import 'package:synthv_desktop/core/workbench_model.dart';

const _conversation = '11111111111111111111111111111111';
const _job = '22222222222222222222222222222222';

class _Input extends Fake implements IOSink {
  @override
  Future<void> close() async {}
}

class _Process extends Fake implements Process {
  final completed = Completer<int>();
  @override
  Future<int> get exitCode => completed.future;
  @override
  IOSink get stdin => _Input();
}

/// 使用真实 WorkbenchApi HTTP 边界，避免 mock 覆盖 getJson 后绕过退出白名单。
/// 服务只返回内存任务，不调用云模型、音频设备或 SynthV。
void main() {
  test('关闭被忙碌拒绝期间原AI任务继续轮询并完成，宿主GET仍被阻止', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final api = WorkbenchApi(port: server.port, token: 'isolated-fixture');
    final process = _Process();
    final directory = await Directory.systemTemp.createTemp(
      'synthv-close-job-',
    );
    final lock = await File('${directory.path}/instance.lock')
        .open(mode: FileMode.append);
    final backend = DesktopBackend.forTesting(process, api, lock);
    final model = WorkbenchModel(api);
    final shutdown = Completer<void>();
    final observedClosingPoll = Completer<void>();
    var done = false, messagePosts = 0, selectionRequests = 0;
    final document = <String, dynamic>{
      'id': _conversation,
      'messages': <JsonMap>[],
    };
    model.conversation = document;
    final subscription = server.listen((request) async {
      await request.drain<void>();
      dynamic value = document;
      switch (request.uri.path) {
        case '/api/desktop/shutdown':
          await shutdown.future;
          request.response.statusCode = 409;
          value = {'error': 'AI 任务仍在进行'};
        case '/api/conversations/$_conversation/messages':
          messagePosts++;
          value = {'jobId': _job};
        case '/api/jobs/$_job':
          if (api.closing && !observedClosingPoll.isCompleted) {
            observedClosingPoll.complete();
          }
          value = done
              ? {
                  'state': 'done',
                  'result': {
                    ...document,
                    'messages': [
                      {'id': 'answer', 'role': 'assistant', 'text': '测试完成'},
                    ],
                  },
                }
              : {
                  'state': 'running',
                  'progress': {'stage': '测试中'},
                };
        case '/api/conversations':
          value = {
            'items': [document],
          };
        case '/api/selection':
          selectionRequests++;
          value = {};
      }
      request.response.headers.contentType = ContentType.json;
      request.response.write(jsonEncode(value));
      await request.response.close();
    });
    Future<void>? sending;
    try {
      model.draft = '仅用于隔离回归';
      sending = model.send();
      for (
        var attempt = 0;
        attempt < 100 && model.pendingJobId == null;
        attempt++
      ) {
        await Future<void>.delayed(const Duration(milliseconds: 10));
      }
      expect(model.pendingJobId, _job);
      final refused = expectLater(
        backend.close(),
        throwsA(isA<ApiException>()),
      );
      await observedClosingPoll.future.timeout(const Duration(seconds: 2));
      expect(model.pendingJobId, _job);
      expect(model.sending, isTrue);
      await expectLater(
        api.getJson('/api/selection'),
        throwsA(isA<ApiException>()),
      );
      expect(selectionRequests, 0);
      shutdown.complete();
      await refused;
      expect(api.closing, isFalse);
      expect(model.pendingJobId, _job);
      done = true;
      await sending.timeout(const Duration(seconds: 2));
      expect(model.pendingJobId, isNull);
      expect(model.messages.single['text'], '测试完成');
      expect(messagePosts, 1);
      expect(model.error, isEmpty);
    } finally {
      done = true;
      if (!shutdown.isCompleted) shutdown.complete();
      if (sending != null) {
        try {
          await sending.timeout(const Duration(seconds: 2));
        } catch (_) {}
      }
      process.completed.complete(0);
      await backend.close();
      model.dispose();
      await subscription.cancel();
      await server.close(force: true);
      await directory.delete(recursive: true);
    }
  });
}
