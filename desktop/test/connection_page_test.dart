import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/pages/connection_page.dart';
import 'package:synthv_desktop/pages/inspector.dart';
import 'package:synthv_desktop/theme.dart';

/// 只模拟目录错配、脚本安装及会话切换，不读取本机真实脚本或写入用户工程。
class _BridgeApi extends WorkbenchApi {
  _BridgeApi() : super(port: 1, token: 'fixture');
  final posts = <({String path, JsonMap body})>[];
  String session = 'old-session';
  final JsonMap diagnostics = {
    'revision': 'known-revision',
    'message': '已安装脚本与当前连接目录不同',
    'dataDirectory': r'D:\fixture\desktop',
    'currentIpcDirectory': r'D:\fixture\desktop\ipc',
    'installedScript': {
      'exists': true,
      'recognized': true,
      'path': r'D:\fixture\scripts\SynthVAssistant.lua',
      'ipcDirectory': r'D:\fixture\legacy\ipc',
    },
    'currentHeartbeat': {'connected': false},
    'installedHeartbeat': {'connected': true, 'heartbeatAge': 0.5},
    'canConnectInstalled': true,
    'canUseCurrent': false,
    'canInstall': true,
  };
  @override
  Future<dynamic> getJson(String path) async {
    if (path == '/api/bridge/diagnostics') return diagnostics;
    if (path == '/api/status') {
      return {
        'bridge': {
          'connected': true,
          'session': session,
          'ipcDirectory': 'fixture/$session',
        },
        'writeEnabled': false,
      };
    }
    if (path == '/api/selection') {
      return {
        'noteCount': 1,
        'startSeconds': 0,
        'endSeconds': 1,
        'parameters': {
          'breathiness': {'label': '气声', 'maxDelta': 0.3},
        },
      };
    }
    throw StateError('未声明的读取');
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    posts.add((path: path, body: Map<String, dynamic>.from(body)));
    return diagnostics;
  }
}

void main() {
  testWidgets('繁忙诊断快照不阻止刷新，任务结束后可恢复连接操作', (tester) async {
    final api = _BridgeApi();
    api.diagnostics['busy'] = true;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ConnectionPage(api: api, onChanged: () async {}),
        ),
      ),
    );
    await tester.pumpAndSettle();
    final connect = find.widgetWithText(FilledButton, '连接正在运行的脚本');
    expect(tester.widget<FilledButton>(connect).onPressed, isNull);
    api.diagnostics['busy'] = false;
    await tester.tap(find.byTooltip('刷新连接诊断'));
    await tester.pumpAndSettle();
    expect(tester.widget<FilledButton>(connect).onPressed, isNotNull);
    expect(api.posts, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    api.dispose();
  });

  testWidgets('目录错配修复只发送固定目标与已读取版本，不读取工程', (tester) async {
    final api = _BridgeApi();
    var refreshed = 0;
    await tester.pumpWidget(
      MaterialApp(
        theme: WorkbenchTheme.build(Brightness.dark),
        home: Scaffold(
          body: ConnectionPage(
            api: api,
            onChanged: () async {
              refreshed++;
            },
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('已安装脚本与当前连接目录不同'), findsOneWidget);
    expect(api.posts, isEmpty);
    await tester.ensureVisible(find.text('连接正在运行的脚本'));
    await tester.tap(find.text('连接正在运行的脚本'));
    await tester.pumpAndSettle();
    expect(api.posts.single.path, '/api/bridge/connect');
    expect(api.posts.single.body, {
      'target': 'installed',
      'revision': 'known-revision',
    });
    expect(refreshed, 1);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    api.dispose();
  });

  testWidgets('安装脚本先展示明确确认，取消不发送安装请求', (tester) async {
    final api = _BridgeApi();
    await tester.pumpWidget(
      MaterialApp(
        theme: WorkbenchTheme.build(Brightness.light),
        home: Scaffold(
          body: ConnectionPage(api: api, onChanged: () async {}),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('安装匹配脚本'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('安装匹配脚本'));
    await tester.pumpAndSettle();
    expect(find.text('备份并安装'), findsOneWidget);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    api.dispose();
  });

  testWidgets('桥接目标切换后检查器清空旧选区与参数工具', (tester) async {
    final api = _BridgeApi();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(width: 302, child: Inspector(api: api)),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    expect(find.text('生成宿主预览'), findsOneWidget);
    final previousEpoch = api.previewEpoch;
    api.session = 'another-session';
    await tester.pump(const Duration(seconds: 4));
    await tester.pumpAndSettle();
    expect(find.text('生成宿主预览'), findsNothing);
    expect(api.previewEpoch, greaterThan(previousEpoch));
    expect(api.posts, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    api.dispose();
  });
}
