import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'api.dart';

/// 负责启动受控后台及其生命周期；不依赖固定端口，不接管旧网页服务。
class DesktopBackend extends ChangeNotifier {
  DesktopBackend._(
    this.process,
    this.api,
    this.dataDirectory,
    this._instanceLock, {
    this.shutdownTimeout = const Duration(seconds: 12),
    this.shutdownRequestTimeout = const Duration(seconds: 4),
    Future<void> Function(String)? diagnosticWriter,
  }) {
    _log = _LifecycleLog(dataDirectory, writer: diagnosticWriter);
    // 只注册一次退出观察，即使退出响应在连接断开时丢失，也能确认进程真实状态。
    _exitFuture = process.exitCode.then((code) {
      _exited = true;
      _exitCode = code;
      api.setClosing(true);
      unawaited(_log.write('backend-exited', code: code));
      notifyListeners();
    });
  }
  final Process process;
  final WorkbenchApi api;
  final String dataDirectory;
  final RandomAccessFile _instanceLock;
  final Duration shutdownTimeout;
  final Duration shutdownRequestTimeout;
  late final _LifecycleLog _log;
  late final Future<void> _exitFuture;
  bool _closed = false;
  bool _exited = false;
  bool _shutdownRequested = false;
  Future<void>? _closingFuture;
  Future<void>? _shutdownRequest;
  Future<void>? _inputClose;
  bool _requestPending = false;
  bool _uncertain = false;
  int? _exitCode;
  String _stage = 'ready';
  bool get hasExited => _exited;
  bool get isClosed => _closed;
  int? get exitCode => _exitCode;
  bool get canResume =>
      !_exited && !_shutdownRequested && !_requestPending && !_uncertain;
  String? get diagnosticPath => _log.path;
  String get closingMessage => _stage == 'request'
      ? '正在确认后台是否可以退出…'
      : _exited
      ? '后台已退出，正在释放窗口资源…'
      : '正在等待后台安全退出…';

  Future<void> recordDiagnostic(String event) => _log.write(event);

  /// 隔离测试注入假进程和本机 API，不启动真实后台、SynthV 或云模型。
  @visibleForTesting
  factory DesktopBackend.forTesting(
    Process process,
    WorkbenchApi api,
    RandomAccessFile lock, {
    Duration shutdownTimeout = const Duration(seconds: 12),
    Duration shutdownRequestTimeout = const Duration(seconds: 4),
    Future<void> Function(String)? diagnosticWriter,
  }) => DesktopBackend._(
    process,
    api,
    '',
    lock,
    shutdownTimeout: shutdownTimeout,
    shutdownRequestTimeout: shutdownRequestTimeout,
    diagnosticWriter: diagnosticWriter,
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
    final diagnostics = _LifecycleLog(resolvedData);
    await diagnostics.write('startup-requested');
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
      // 只记录是否收到诊断输出，绝不记录正文；Python traceback 可能含私人路径。
      var reportedStderr = false;
      process.stderr.listen(
        (_) {
          if (!reportedStderr) {
            reportedStderr = true;
            unawaited(diagnostics.write('backend-stderr-received'));
          }
        },
        onError: (Object _) {
          unawaited(diagnostics.write('backend-stderr-stream-error'));
        },
      );
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
            onError: (Object _) {
              if (!handshake.isCompleted) {
                handshake.completeError(const ApiException('调教核心启动管道异常，请重试。'));
              }
              unawaited(diagnostics.write('backend-stdout-stream-error'));
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
      await api
          .getJson('/api/desktop/status')
          .timeout(
            const Duration(seconds: 5),
            onTimeout: () => throw const ApiException('调教核心未及时响应启动检查，请重试。'),
          );
      await diagnostics.write('startup-ready');
      return DesktopBackend._(process, api, resolvedData, lock);
    } catch (error) {
      await diagnostics.write(
        error is TimeoutException ? 'startup-timeout' : 'startup-failed',
      );
      // EOF 由后台实现排空退出；不能强杀未知状态的宿主写入。
      // 关闭拥有者管道通知后台安全排空；清理等待也有上限，不能拖住启动错误页。
      try {
        await process?.stdin.close().timeout(const Duration(seconds: 2));
      } catch (_) {
        await diagnostics.write('startup-pipe-close-failed');
      }
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
    api.setClosing(true);
    try {
      if (!_exited && !_shutdownRequested) {
        _stage = 'request';
        notifyListeners();
        // 保留原始请求 Future。四秒只结束本次 UI 等待，不代表请求被取消；
        // 用户再次等待时复用同一个在途请求，避免堆积关闭指令。
        _shutdownRequest ??= () async {
          _requestPending = true;
          await _log.write('shutdown-requested');
          try {
            await api.postJson('/api/desktop/shutdown');
            _shutdownRequested = true;
            _uncertain = false;
            _stage = 'exit';
            await _log.write('shutdown-accepted');
          } catch (error) {
            _uncertain = error is ApiException && error.uncertain;
            await _log.write(
              _uncertain ? 'shutdown-result-uncertain' : 'shutdown-refused',
            );
            rethrow;
          } finally {
            _requestPending = false;
            _shutdownRequest = null;
            if (canResume) api.setClosing(false);
            notifyListeners();
          }
        }();
        // 后台自然退出时不必等网络超时；明确 busy 失败仍原样保留窗口和任务。
        await Future.any<void>([_shutdownRequest!, _exitFuture]).timeout(
          shutdownRequestTimeout,
          onTimeout: () {
            _uncertain = true;
            unawaited(_log.write('shutdown-response-timeout'));
            throw const ApiException(
              '后台尚未确认退出，请继续等待或稍后重试；在途任务仍受保护。',
              uncertain: true,
            );
          },
        );
      }
      if (!_exited) {
        _stage = 'exit';
        notifyListeners();
        _inputClose ??= process.stdin.close();
        await Future.any<void>([_inputClose!, _exitFuture]).timeout(
          const Duration(seconds: 2),
          onTimeout: () =>
              throw const ApiException('后台退出管道尚未关闭，请继续等待。', uncertain: true),
        );
        await _exitFuture.timeout(
          shutdownTimeout,
          onTimeout: () => throw const ApiException(
            '后台正在完成退出，请继续等待；没有强制终止录音或工程操作。',
            uncertain: true,
          ),
        );
      }
      // 只有进程确实退出后才释放实例锁；超时后再次关闭只继续等待，不重发关闭请求。
      api.dispose();
      await _instanceLock.close();
      _closed = true;
      await _log.write('backend-resources-released');
      notifyListeners();
    } catch (_) {
      if (canResume) api.setClosing(false);
      rethrow;
    }
  }
}

/// 生命周期日志只允许固定事件代码和退出码；有界轮转，不记录令牌、请求正文、
/// 音频、工程路径、异常全文或供应商地址。测试的空资料目录不会生成日志。
class _LifecycleLog {
  _LifecycleLog(String directory, {this.writer})
    : path = directory.isEmpty
          ? null
          : '$directory${Platform.pathSeparator}desktop-lifecycle.log';
  final String? path;
  final Future<void> Function(String)? writer;
  final _queue = <_LogEntry>[];
  bool _draining = false;
  Future<void> write(String event, {int? code}) {
    if ((path == null && writer == null) ||
        !RegExp(r'^[a-z-]{1,64}$').hasMatch(event)) {
      return Future<void>.value();
    }
    // 单个文件 I/O 无法由 Future.timeout 取消。保留至多 16 条待写事件，顺序
    // 排空；磁盘长期无响应时丢弃后续诊断，不能把无界 Future 链留在退出路径。
    if (_queue.length >= 16) return Future<void>.value();
    final entry = _LogEntry(
      '${DateTime.now().toUtc().toIso8601String()} $event${code == null ? '' : ' code=$code'}\n',
    );
    _queue.add(entry);
    if (!_draining) {
      _draining = true;
      unawaited(_drain());
    }
    return entry.done.future.timeout(
      const Duration(milliseconds: 500),
      onTimeout: () {},
    );
  }

  Future<void> _drain() async {
    while (_queue.isNotEmpty) {
      final entry = _queue.removeAt(0);
      try {
        if (writer != null) {
          await writer!(entry.line);
          continue;
        }
        final file = File(path!);
        if (await file.exists() && await file.length() > 128 * 1024) {
          final previous = File('${path!}.previous');
          if (await previous.exists()) await previous.delete();
          await file.rename(previous.path);
        }
        await file.writeAsString(entry.line, mode: FileMode.append);
      } catch (_) {
        // 诊断是尽力写入；失败既不泄露底层路径，也不阻止窗口与后台安全退出。
      } finally {
        if (!entry.done.isCompleted) entry.done.complete();
      }
    }
    _draining = false;
  }
}

class _LogEntry {
  _LogEntry(this.line);
  final String line;
  final done = Completer<void>();
}
