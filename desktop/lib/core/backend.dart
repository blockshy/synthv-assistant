import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'api.dart';

/// 负责启动受控后台及其生命周期；不依赖固定端口，不接管旧网页服务。
class DesktopBackend {
  DesktopBackend._(
    this.process,
    this.api,
    this.dataDirectory,
    this._instanceLock, {
    this.shutdownTimeout = const Duration(seconds: 15),
  }) {
    // 只注册一次退出观察，即使退出响应在连接断开时丢失，也能确认进程真实状态。
    _exitFuture = process.exitCode.then((_) {
      _exited = true;
    });
  }
  final Process process;
  final WorkbenchApi api;
  final String dataDirectory;
  final RandomAccessFile _instanceLock;
  final Duration shutdownTimeout;
  late final Future<void> _exitFuture;
  bool _closed = false;
  bool _exited = false;
  bool _shutdownRequested = false;
  Future<void>? _closingFuture;

  /// 隔离测试注入假进程和本机 API，不启动真实后台、SynthV 或云模型。
  @visibleForTesting
  factory DesktopBackend.forTesting(
    Process process,
    WorkbenchApi api,
    RandomAccessFile lock, {
    Duration shutdownTimeout = const Duration(seconds: 15),
  }) => DesktopBackend._(
    process,
    api,
    '',
    lock,
    shutdownTimeout: shutdownTimeout,
  );

  static String _join(String first, String second) =>
      '$first${Platform.pathSeparator}$second';

  static Future<DesktopBackend> start() async {
    final executableDirectory = File(Platform.resolvedExecutable).parent;
    final packaged = File(
      _join(executableDirectory.path, r'backend\synthv-backend.exe'),
    );
    Directory? source;
    // 开发时从当前目录或运行程序所在目录向上查找项目标记；不扫描用户磁盘。
    for (final seed in [Directory.current, executableDirectory]) {
      var cursor = seed;
      for (var i = 0; i < 10; i++) {
        if (File(_join(cursor.path, 'synthv_assistant/__main__.py'))
                .existsSync() &&
            File(_join(cursor.path, 'pyproject.toml')).existsSync()) {
          source = cursor;
          break;
        }
        if (cursor.parent.path == cursor.path) break;
        cursor = cursor.parent;
      }
      if (source != null) break;
    }
    final override = Platform.environment['SYNTHV_ASSISTANT_DATA'];
    final local = Platform.environment['LOCALAPPDATA'];
    // 持久数据不能静默落到临时目录，否则系统清理会丢失会话和加密配置。
    if (packaged.existsSync() &&
        override == null &&
        (local == null || local.isEmpty)) {
      throw const ApiException('无法确定本机数据目录，请设置 SYNTHV_ASSISTANT_DATA 后启动。');
    }
    final data =
        override ??
        (packaged.existsSync()
            ? _join(local!, r'SynthV Assistant\data')
            : source == null
            ? null
            : _join(source.path, 'data'));
    if (data == null) throw const ApiException('未找到调教核心。请使用完整桌面发行包，或从项目目录启动。');
    // 子进程工作目录可能不同；启动时统一解析，锁、桥接与配置必须指向同一目录。
    final resolvedData = Directory(data).absolute.path;
    await Directory(resolvedData).create(recursive: true);
    final lock = await File(_join(resolvedData, 'desktop-instance.lock'))
        .open(mode: FileMode.append);
    try {
      await lock.lock(FileLock.exclusive, 0, 1);
    } on FileSystemException {
      await lock.close();
      throw const ApiException('该数据目录的工作台已在运行，请切换至已有窗口。');
    }
    Process? process;
    try {
      final python = source == null
          ? null
          : File(_join(source.path, r'.venv\Scripts\python.exe'));
      if (!packaged.existsSync() && (python == null || !python.existsSync())) {
        throw const ApiException('未找到项目 Python 环境，请先按桌面运行说明准备依赖。');
      }
      process = await Process.start(
        packaged.existsSync() ? packaged.path : python!.path,
        packaged.existsSync()
            ? ['desktop']
            : ['-m', 'synthv_assistant', 'desktop'],
        workingDirectory: source?.path ?? executableDirectory.path,
        environment: {
          'SYNTHV_ASSISTANT_DATA': resolvedData,
          'PYTHONIOENCODING': 'utf-8',
        },
        runInShell: false,
      );
      // 标准错误只消费不回显，避免把内部异常和本机路径落入用户日志。
      unawaited(process.stderr.drain<void>());
      final handshake = Completer<JsonMap>();
      process.stdout
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .listen(
            (line) {
              if (handshake.isCompleted) return;
              try {
                final value = jsonDecode(line);
                if (value is Map) {
                  handshake.complete(Map<String, dynamic>.from(value));
                }
              } catch (_) {
                /* 非协议行不能成为启动地址。 */
              }
            },
            onDone: () {
              if (!handshake.isCompleted) {
                handshake.completeError(
                  const ApiException('调教核心未能启动，请检查安装文件和数据目录权限。'),
                );
              }
            },
          );
      final value = await handshake.future.timeout(const Duration(seconds: 25));
      if (value['protocol'] != 1 ||
          value['port'] is! int ||
          (value['port'] as int) < 1 ||
          (value['port'] as int) > 65535 ||
          value['token'] is! String ||
          (value['token'] as String).length < 20) {
        throw const ApiException('调教核心版本不兼容，未建立连接。');
      }
      final api = WorkbenchApi(
        port: value['port'] as int,
        token: value['token'] as String,
      );
      await api.getJson('/api/desktop/status');
      return DesktopBackend._(process, api, resolvedData, lock);
    } catch (_) {
      // EOF 由后台实现排空退出；不能强杀未知状态的宿主写入。
      await process?.stdin.close();
      await lock.close();
      rethrow;
    }
  }

  /// 后台忙碌时拒绝关闭窗口；关闭成功后等进程退出再释放单实例锁。
  Future<void> close() {
    return _closingFuture ??= _closeOnce().whenComplete(
      () => _closingFuture = null,
    );
  }

  Future<void> _closeOnce() async {
    if (_closed) return;
    if (!_exited && !_shutdownRequested) {
      // 后台自然退出时不必等网络超时；明确 busy 失败仍原样保留窗口和任务。
      await Future.any<void>([
        api.postJson('/api/desktop/shutdown').then((_) {
          _shutdownRequested = true;
        }),
        _exitFuture,
      ]);
    }
    if (!_exited) {
      await process.stdin.close();
      await _exitFuture.timeout(
        shutdownTimeout,
        onTimeout: () => throw const ApiException('调教核心正在完成退出，请稍后再次关闭窗口。'),
      );
    }
    // 只有进程确实退出后才释放实例锁；超时后再次关闭只继续等待，不重发关闭请求。
    api.dispose();
    await _instanceLock.close();
    _closed = true;
  }
}
