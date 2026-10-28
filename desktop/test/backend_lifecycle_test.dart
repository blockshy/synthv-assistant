import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/core/backend.dart';

/// 只模拟进程完成事件与输入管道，不创建操作系统子进程。
class _Process extends Fake implements Process {
  final completed = Completer<int>();
  final input = _Input();
  @override
  Future<int> get exitCode => completed.future;
  @override
  IOSink get stdin => input;
}

class _Input extends Fake implements IOSink {
  int closes = 0;
  @override
  Future<void> close() async {
    closes++;
  }
}

class _Api extends WorkbenchApi {
  _Api() : super(port: 0, token: 'isolated-test');
  int shutdowns = 0, disposals = 0;
  Future<void> Function()? shutdown;
  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    expect(path, '/api/desktop/shutdown');
    shutdowns++;
    await shutdown?.call();
    return {};
  }

  @override
  void dispose() {
    disposals++;
    super.dispose();
  }
}

Future<DesktopBackend> _backend(
  _Process process,
  _Api api, {
  Future<void> Function(String)? diagnosticWriter,
}) async {
  // 仅在临时目录建立实例锁，退出后测试框架负责清理。
  final folder = await Directory.systemTemp.createTemp('synthv-desktop-life-');
  final lock = await File('${folder.path}/instance.lock')
      .open(mode: FileMode.append);
  await lock.lock(FileLock.exclusive, 0, 1);
  addTearDown(() async {
    try {
      await lock.close();
    } catch (_) {
      /* 正常关闭流程已释放该句柄。 */
    }
    await folder.delete(recursive: true);
  });
  return DesktopBackend.forTesting(
    process,
    api,
    lock,
    shutdownTimeout: const Duration(milliseconds: 20),
    shutdownRequestTimeout: const Duration(milliseconds: 20),
    diagnosticWriter: diagnosticWriter,
  );
}

void main() {
  test('诊断磁盘等待最多500ms且堵塞时日志队列有界并保留顺序', () async {
    final process = _Process();
    final api = _Api();
    final blocked = Completer<void>();
    final written = <String>[];
    final backend = await _backend(
      process,
      api,
      diagnosticWriter: (line) async {
        written.add(line);
        await blocked.future;
      },
    );
    final elapsed = Stopwatch()..start();
    await backend.recordDiagnostic('first');
    expect(elapsed.elapsedMilliseconds, lessThan(1500));
    expect(written.length, 1);
    await Future.wait(
      List.generate(100, (_) => backend.recordDiagnostic('second')),
    );
    expect(written.length, 1);
    blocked.complete();
    await Future<void>.delayed(Duration.zero);
    expect(written.length, lessThanOrEqualTo(17));
    expect(written.first, contains('first'));
    expect(written.skip(1).every((line) => line.contains('second')), isTrue);
    process.completed.complete(0);
    await backend.close();
  });
  test('关闭响应卡住时有界返回且重试复用同一个请求', () async {
    final process = _Process();
    final api = _Api();
    final response = Completer<void>();
    api.shutdown = () => response.future;
    final backend = await _backend(process, api);
    final elapsed = Stopwatch()..start();
    await expectLater(backend.close(), throwsA(isA<ApiException>()));
    expect(elapsed.elapsedMilliseconds, lessThan(500));
    expect(api.closing, isTrue);
    await expectLater(backend.close(), throwsA(isA<ApiException>()));
    expect(api.shutdowns, 1);
    expect(process.input.closes, 0);
    response.completeError(const ApiException('任务仍在进行'));
    await Future<void>.delayed(Duration.zero);
    expect(backend.canResume, isTrue);
    expect(api.closing, isFalse);
    process.completed.complete(0);
    await backend.close();
  });

  test('后台自然退出主动通知界面并暂停新操作', () async {
    final process = _Process();
    final api = _Api();
    final backend = await _backend(process, api);
    var notifications = 0;
    backend.addListener(() => notifications++);
    process.completed.complete(17);
    await Future<void>.delayed(Duration.zero);
    expect(backend.hasExited, isTrue);
    expect(backend.exitCode, 17);
    expect(api.closing, isTrue);
    expect(notifications, greaterThan(0));
    await backend.close();
    expect(api.shutdowns, 0);
  });
  test('退出超时后再次关闭只等待已有退出，不重复请求已停用服务', () async {
    final process = _Process();
    final api = _Api();
    final backend = await _backend(process, api);
    await expectLater(backend.close(), throwsA(isA<ApiException>()));
    expect(api.shutdowns, 1);
    expect(api.disposals, 0);
    process.completed.complete(0);
    await backend.close();
    await backend.close();
    expect(api.shutdowns, 1);
    expect(api.disposals, 1);
  });

  test('后台明确忙碌时不关闭输入、不释放锁、不终止任务', () async {
    final process = _Process();
    final api = _Api()
      ..shutdown = () async {
        throw const ApiException('仍在录音');
      };
    final backend = await _backend(process, api);
    await expectLater(backend.close(), throwsA(isA<ApiException>()));
    expect(process.input.closes, 0);
    expect(api.disposals, 0);
    process.completed.complete(0);
    await Future<void>.delayed(Duration.zero);
    await backend.close();
    expect(api.shutdowns, 1);
    expect(api.disposals, 1);
  });

  test('后台已退出且关闭响应丢失时仍能释放资源', () async {
    final process = _Process();
    final api = _Api()
      ..shutdown = () async {
        process.completed.complete(0);
        await Future<void>.delayed(Duration.zero);
        throw const ApiException('连接已断开', uncertain: true);
      };
    final backend = await _backend(process, api);
    await backend.close();
    expect(api.disposals, 1);
    expect(api.shutdowns, 1);
    await Future<void>.delayed(Duration.zero);
  });
}
