import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/core/workbench_model.dart';

/// 此文件使用 ordinary test，不初始化 Widget binding，避免 Flutter 测试层
/// 把真实 HttpClient 替换成固定 400 响应。所有业务资料只写入新建临时目录。
Directory _sourceRoot() {
  var directory = Directory.current.absolute;
  for (var depth = 0; depth < 5; depth++) {
    if (File('${directory.path}/synthv_assistant/__main__.py').existsSync()) {
      return directory;
    }
    directory = directory.parent;
  }
  throw StateError('未找到源码目录；请在 desktop 目录执行真实传输测试。');
}

/// 构造 0.1 秒单声道 16 位 PCM 静音，绝不读取或录制用户音频。
Uint8List _syntheticWav() {
  const samples = 1600, rate = 16000, size = samples * 2;
  final bytes = Uint8List(44 + size);
  final data = ByteData.sublistView(bytes);
  bytes.setRange(0, 4, ascii.encode('RIFF'));
  data.setUint32(4, 36 + size, Endian.little);
  bytes.setRange(8, 16, ascii.encode('WAVEfmt '));
  data.setUint32(16, 16, Endian.little);
  data.setUint16(20, 1, Endian.little);
  data.setUint16(22, 1, Endian.little);
  data.setUint32(24, rate, Endian.little);
  data.setUint32(28, rate * 2, Endian.little);
  data.setUint16(32, 2, Endian.little);
  data.setUint16(34, 16, Endian.little);
  bytes.setRange(36, 40, ascii.encode('data'));
  data.setUint32(40, size, Endian.little);
  return bytes;
}

Future<void> _exerciseTransport({required bool ownerEof}) async {
  final root = _sourceRoot();
  final python = File('${root.path}/.venv/Scripts/python.exe');
  expect(python.existsSync(), isTrue, reason: '真实烟测需要项目 .venv Python');
  final temporary = await Directory.systemTemp.createTemp('synthv-transport-');
  final data = Directory('${temporary.path}/isolated-data')..createSync();
  Process? process;
  WorkbenchApi? api;
  StreamSubscription<String>? output;
  Future<String>? stderr;
  var exited = false;
  try {
    process = await Process.start(
      python.path,
      ['-m', 'synthv_assistant', 'desktop'],
      workingDirectory: root.path,
      environment: {
        'SYNTHV_ASSISTANT_DATA': data.path,
        'SYNTHV_AUDIO_PROVIDER': 'none',
        'SYNTHV_AUDIO_MODEL': '',
        'SYNTHV_AUDIO_BASE_URL': '',
        'PYTHONIOENCODING': 'utf-8',
      },
      runInShell: false,
    );
    // 一直消费管道，握手完成后也不能让后台因 stdout/stderr 缓冲区填满而停住。
    stderr = process.stderr.transform(utf8.decoder).join();
    final handshake = Completer<JsonMap>();
    output = process.stdout
        .transform(utf8.decoder)
        .transform(const LineSplitter())
        .listen(
          (line) {
            if (!handshake.isCompleted) {
              handshake.complete(
                Map<String, dynamic>.from(jsonDecode(line) as Map),
              );
            }
          },
          onError: (Object error) {
            if (!handshake.isCompleted) handshake.completeError(error);
          },
          onDone: () {
            if (!handshake.isCompleted) {
              handshake.completeError(StateError('后台在握手之前退出'));
            }
          },
        );
    final info = await handshake.future.timeout(const Duration(seconds: 15));
    expect(info['protocol'], 1);
    // Windows 临时目录可能使用 ADMINI~1 短名；比较目录身份而非路径字符串。
    expect(
      await FileSystemEntity.identical(
        info['dataDirectory'] as String,
        data.path,
      ),
      isTrue,
    );
    api = WorkbenchApi(
      port: info['port'] as int,
      token: info['token'] as String,
    );
    final client = api;
    expect(
      (await client
          .getJson('/api/desktop/status')
          .timeout(
            const Duration(seconds: 8),
            onTimeout: () => throw TimeoutException('阶段：首次 desktop/status'),
          ))['busy'],
      isFalse,
    );
    final model = WorkbenchModel(client);
    try {
      await model.initialize().timeout(
        const Duration(seconds: 8),
        onTimeout: () =>
            throw TimeoutException('阶段：真实 WorkbenchModel.initialize'),
      );
      expect(model.error, isEmpty);
      expect(model.navigationBusy, isFalse);
    } finally {
      model.dispose();
    }

    // 多轮并发与持续复用同一个 Dart HttpClient，覆盖主工作台首次加载及后续轮询。
    for (var round = 0; round < 5; round++) {
      final responses =
          await Future.wait([
            client.getJson('/api/status'),
            client.getJson('/api/conversations'),
            client.getJson('/api/model-platforms'),
            client.postJson('/api/model-capabilities', {
              'platformId': 'default',
              'model': '',
            }),
          ]).timeout(
            const Duration(seconds: 8),
            onTimeout: () => throw TimeoutException('阶段：并发请求第 $round 轮'),
          );
      expect(responses[0]['bridge']['connected'], isFalse);
      expect(responses[1]['items'], isEmpty);
      expect(responses[2]['items'], isA<List>());
      expect(responses[3], isA<Map>());
    }

    final wav = _syntheticWav();
    final file = await File('${temporary.path}/合成测试音频.wav').writeAsBytes(wav);
    final uploaded = await client
        .uploadFile(file.path)
        .timeout(
          const Duration(seconds: 8),
          onTimeout: () => throw TimeoutException('阶段：上传合成音频（含首次 NumPy 加载）'),
        );
    expect(uploaded['name'], '合成测试音频.wav');
    expect(uploaded['durationSeconds'], closeTo(.1, .0001));
    final readback = await client
        .readAudio(uploaded['url'] as String)
        .timeout(
          const Duration(seconds: 8),
          onTimeout: () => throw TimeoutException('阶段：读取合成音频'),
        );
    expect(readback, wav);
    final uploads = await client.getJson('/api/uploads');
    expect((uploads['items'] as List).length, 1);

    // 两条退出路径均保持真实管道契约：显式退出或拥有者 EOF，不发送工程修改请求。
    if (!ownerEof) {
      final stopped = await client
          .postJson('/api/desktop/shutdown')
          .timeout(
            const Duration(seconds: 8),
            onTimeout: () => throw TimeoutException('阶段：关闭后台'),
          );
      expect(stopped['closing'], isTrue);
    }
    await process.stdin.close();
    expect(await process.exitCode.timeout(const Duration(seconds: 10)), 0);
    exited = true;
    expect(await stderr, isEmpty);
  } finally {
    api?.dispose();
    if (process != null && !exited) {
      try {
        await process.stdin.close();
      } catch (_) {
        /* 管道可能已关闭。 */
      }
      try {
        await process.exitCode.timeout(const Duration(seconds: 10));
      } on TimeoutException {
        // 此处只涉及本测试创建的空目录后台；没有模型任务或宿主写入可被中断。
        process.kill();
        await process.exitCode.timeout(const Duration(seconds: 5));
      }
    }
    await output?.cancel();
    if (stderr != null) await stderr;
    await temporary.delete(recursive: true);
  }
}

void main() {
  for (final eof in [false, true]) {
    test(
      '真实 Dart/Python 本机传输、并发、音频及 ${eof ? 'EOF' : 'shutdown'} 退出不经过代理',
      () async {
        final badProxy = HttpClient.findProxyFromEnvironment(
          Uri.parse('http://127.0.0.1:12345/api/status'),
          environment: {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''},
        );
        expect(badProxy, startsWith('PROXY'));
        var proxyLookups = 0;
        await HttpOverrides.runZoned(
          () async {
            await _exerciseTransport(ownerEof: eof);
          },
          findProxyFromEnvironment: (uri, environment) {
            proxyLookups++;
            return badProxy;
          },
        );
        expect(proxyLookups, 0, reason: '本机认证令牌和音频必须直连回环服务，不允许交给环境代理');
      },
      timeout: const Timeout(Duration(minutes: 2)),
    );
  }
}
