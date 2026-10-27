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

Future<DesktopBackend> _backend(_Process process, _Api api) async {
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
  );
}

void main() {
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
