import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';

typedef JsonMap = Map<String, dynamic>;

/// 提供可直接显示的错误；不把认证头、请求正文或底层路径加入诊断。
class ApiException implements Exception {
  const ApiException(this.message, {this.uncertain = false});
  final String message;
  final bool uncertain;
  @override
  String toString() => message;
}

/// 桌面端唯一的服务入口：凭据只在内存中，禁止向回环地址之外发送请求。
/// 实际权限、参数限幅和确认票据仍由现有 Python 核心判定。
class WorkbenchApi extends ChangeNotifier {
  WorkbenchApi({required this.port, required this.token});
  final int port;
  final String token;
  final HttpClient _client = HttpClient()
    // 本机 IPC 必须直连；Dart 默认读取环境代理，不能把令牌或作品送给代理。
    ..connectionTimeout = const Duration(seconds: 10)
    ..findProxy = (_) => 'DIRECT';
  bool _busy = false;
  bool _closing = false;
  bool _disposed = false;
  bool get busy => _busy || _closing;
  bool get closing => _closing;
  int previewEpoch = 0;

  /// 退出协调期间暂停页面轮询和新业务请求；忙碌退出被拒绝后可以明确恢复。
  /// 生命周期路由仍可查询，已经发出的 AI/录音/宿主请求不会被取消或重发。
  void setClosing(bool value) {
    if (_disposed || value == _closing) return;
    _closing = value;
    notifyListeners();
  }

  /// 所有页面共享互斥状态，避免会话、录音和工程工具同时提交耗时任务。
  Future<T> exclusive<T>(Future<T> Function() action) async {
    if (busy) throw const ApiException('当前任务尚未完成或正在退出，请稍后操作。');
    _busy = true;
    notifyListeners();
    try {
      return await action();
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  /// 切换会话、生成新预览或应用之前，使所有旧确认按钮失去资格。
  void invalidatePreviews() {
    previewEpoch++;
    notifyListeners();
  }

  Future<Uint8List> _request(
    String method,
    String path, {
    Object? body,
    Uint8List? bytes,
    String? filename,
  }) async {
    if (_disposed) throw const ApiException('调教核心已停止，请重新连接。');
    // 在途模型任务仍需轮询并读回正式会话，否则一次忙碌退出被拒绝也会丢失 jobId。
    // 仅放行纯本机记录读取；GET /api/selection、/api/project 仍可能触发宿主 IPC，
    // 因而不能把“GET”整体视为安全只读并全部放开。
    final closingRead =
        method == 'GET' &&
        (RegExp(r'^/api/jobs/[0-9a-f]{32}$').hasMatch(path) ||
            path == '/api/conversations' ||
            RegExp(r'^/api/conversations/[0-9a-f]{32}$').hasMatch(path));
    if (_closing && !closingRead && !path.startsWith('/api/desktop/')) {
      throw const ApiException('工作台正在准备退出，已暂停新的操作。');
    }
    if (!path.startsWith('/') ||
        path.startsWith('//') ||
        path.contains('://')) {
      throw const ApiException('本机请求地址无效。');
    }
    try {
      final request = await _client.openUrl(
        method,
        Uri(scheme: 'http', host: '127.0.0.1', port: port, path: path),
      );
      request.followRedirects = false;
      request.headers.set('X-SV-Token', token);
      if (bytes != null) {
        request.headers.contentType = ContentType.binary;
        request.headers.set('X-File-Name', Uri.encodeComponent(filename!));
        request.contentLength = bytes.length;
        request.add(bytes);
      } else if (method == 'POST') {
        final encoded = utf8.encode(jsonEncode(body ?? <String, dynamic>{}));
        request.headers.contentType = ContentType.json;
        request.contentLength = encoded.length;
        request.add(encoded);
      }
      final response = await request.close().timeout(
        const Duration(seconds: 60),
      );
      final result = BytesBuilder(copy: false);
      await for (final chunk in response.timeout(const Duration(seconds: 60))) {
        result.add(chunk);
        if (result.length > 128 * 1024 * 1024) {
          throw const ApiException('本机响应过大，操作状态尚未确认。', uncertain: true);
        }
      }
      final data = result.takeBytes();
      if (response.statusCode < 200 || response.statusCode >= 300) {
        String? message;
        try {
          final decoded = jsonDecode(utf8.decode(data));
          if (decoded is Map && decoded['error'] is String) {
            message = decoded['error'] as String;
          }
        } catch (_) {
          /* 非 JSON 失败响应不回显原文。 */
        }
        throw ApiException(message ?? '本机操作未完成（${response.statusCode}）。');
      }
      return data;
    } on ApiException {
      rethrow;
    } on TimeoutException {
      throw const ApiException('等待本机服务超时；操作状态尚未确认，请先刷新状态。', uncertain: true);
    } on SocketException {
      throw const ApiException('无法连接本机服务，请检查后台运行状态。', uncertain: true);
    } on HttpException {
      throw const ApiException('本机连接中断；没有自动重复提交操作。', uncertain: true);
    }
  }

  /// 无法解析响应时不能假定写入失败；保留任务恢复和宿主状态核对路径。
  Future<dynamic> _json(String method, String path, [JsonMap? body]) async {
    final bytes = await _request(method, path, body: body);
    try {
      return jsonDecode(utf8.decode(bytes));
    } on FormatException {
      throw const ApiException('本机响应不完整，操作状态尚未确认。', uncertain: true);
    }
  }

  Future<dynamic> getJson(String path) => _json('GET', path);
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) =>
      _json('POST', path, body);

  Future<dynamic> uploadFile(String path) async {
    final file = File(path);
    if (await file.length() > 12000000) {
      throw const ApiException('音频文件不能超过 12 MB。');
    }
    final filename = path.split(RegExp(r'[/\\]')).last;
    return jsonDecode(
      utf8.decode(
        await _request(
          'POST',
          '/api/uploads',
          bytes: await file.readAsBytes(),
          filename: filename,
        ),
      ),
    );
  }

  /// 只获取受控音频路由，播放器不接受任意远程 URL 或本机文件路径。
  Future<Uint8List> readAudio(String path) {
    if (!RegExp(r'^/(audio|uploads)/[0-9a-f]{32}\.wav$').hasMatch(path)) {
      throw const ApiException('该音频已不可用或播放地址无效。');
    }
    return _request('GET', path);
  }

  /// 只轮询已经提交的任务，不重发模型请求；供应商空闲超时由后端统一处理。
  Future<dynamic> waitForJob(
    String jobId, {
    void Function(JsonMap)? onProgress,
  }) async {
    if (!RegExp(r'^[0-9a-f]{32}$').hasMatch(jobId)) {
      throw const ApiException('任务编号无效。');
    }
    while (true) {
      final job = Map<String, dynamic>.from(
        await getJson('/api/jobs/$jobId') as Map,
      );
      if (job['progress'] is Map) {
        onProgress?.call(Map<String, dynamic>.from(job['progress'] as Map));
      }
      if (job['state'] == 'done') return job['result'];
      if (job['state'] == 'error') {
        throw ApiException(job['error']?.toString() ?? '任务未完成。');
      }
      if (job['state'] != 'running') {
        throw const ApiException('无法确认后台任务状态。', uncertain: true);
      }
      await Future<void>.delayed(const Duration(milliseconds: 450));
    }
  }

  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _client.close(force: true);
    super.dispose();
  }

  /// 后台自然退出时，已经在途的 Future 仍可能完成 finally，不再向销毁的页面通知。
  @override
  void notifyListeners() {
    if (!_disposed) super.notifyListeners();
  }
}
