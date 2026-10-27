import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/pages/library_page.dart';
import 'package:synthv_desktop/pages/recording_page.dart';
import 'package:synthv_desktop/pages/settings_page.dart';
import 'package:synthv_desktop/pages/trash_page.dart';
import 'package:synthv_desktop/theme.dart';

/// 页面验证只使用合成资料；绝不启动桥接、录音、原生播放器或云端模型。
class _PageApi extends WorkbenchApi {
  _PageApi() : super(port: 1, token: 'isolated-test');
  final reads = <String>[];
  final posts = <({String path, JsonMap body})>[];
  int audioReads = 0;
  bool selectionFails = false;
  bool saveFails = false;
  List<JsonMap> uploads = List.generate(
    20,
    (index) => {
      'id': index.toString().padLeft(32, '0'),
      'label': '合成素材 $index',
      'createdAt': index.toString().padLeft(2, '0'),
      'durationSeconds': 8,
      'url': '/uploads/${index.toString().padLeft(32, '0')}.wav',
      'available': true,
      'projectFile': '不应被附件携带的测试字段',
    },
  );

  @override
  Future<dynamic> getJson(String path) async {
    reads.add(path);
    if (path == '/api/uploads') return {'items': uploads};
    if (path == '/api/recordings') return {'items': <JsonMap>[]};
    if (path == '/api/status') {
      return {
        'review': {'configured': false},
      };
    }
    if (path == '/api/selection') {
      if (selectionFails) throw const ApiException('测试选区已变化');
      return {'startSeconds': 2.5, 'endSeconds': 42.5, 'noteCount': 3};
    }
    if (path == '/api/model-platforms') {
      return {
        'revision': 'test-registry',
        'defaultPlatformId': 'default',
        'items': [
          {
            'id': 'default',
            'name': '默认配置',
            'provider': 'openai',
            'model': 'test-model',
            'baseUrl': 'https://example.invalid/v1',
            'timeoutSeconds': 60,
            'configured': true,
            'keyConfigured': true,
            'revision': 'test-platform',
          },
        ],
      };
    }
    if (path.endsWith('/models')) return {'models': <JsonMap>[]};
    if (path == '/api/trash') {
      return {
        'items': [
          {'kind': 'upload', 'id': 'test-trash', 'label': '待清理素材'},
        ],
      };
    }
    throw StateError('测试未声明的本机读取：$path');
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    posts.add((path: path, body: Map<String, dynamic>.from(body)));
    if (saveFails && path == '/api/model-platforms') {
      throw const ApiException('测试保存失败');
    }
    return {'id': 'default', 'ok': true};
  }

  @override
  Future<Uint8List> readAudio(String path) async {
    audioReads++;
    throw StateError('页面载入不得自动读取音频');
  }
}

Finder _field(String label) => find.byWidgetPredicate(
  (widget) => widget is TextField && widget.decoration?.labelText == label,
);

/// 使用真实工作台主题和较窄窗口，验证主操作始终留在可视区域。
Future<void> _show(
  WidgetTester tester,
  Widget page, {
  double height = 600,
}) async {
  tester.view.physicalSize = Size(500, height);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      theme: WorkbenchTheme.build(Brightness.light),
      home: Scaffold(body: page),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('素材固定栏可见，附件顺序遵循选择且不会带出私有索引', (tester) async {
    final api = _PageApi();
    addTearDown(api.dispose);
    List<JsonMap>? attached;
    await _show(
      tester,
      LibraryPage(api: api, onAttach: (items) => attached = items),
    );
    expect(api.posts, isEmpty);
    expect(api.audioReads, 0);
    final attach = find.widgetWithText(FilledButton, '附到会话');
    expect(tester.getRect(attach).bottom, lessThanOrEqualTo(600));
    await tester.tap(find.byType(Checkbox).at(1));
    await tester.pump();
    await tester.tap(find.byType(Checkbox).first);
    await tester.pump();
    await tester.tap(attach);
    expect(attached?.map((item) => item['label']).toList(), [
      '合成素材 18',
      '合成素材 19',
    ]);
    expect(attached!.every((item) => !item.containsKey('projectFile')), isTrue);
    expect(tester.takeException(), isNull);
  });

  testWidgets('素材永久删除只在固定确认栏明确确认后提交', (tester) async {
    final api = _PageApi();
    addTearDown(api.dispose);
    await _show(tester, LibraryPage(api: api, onAttach: (_) {}));
    await tester.tap(find.byTooltip('删除音频').first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('永久删除'));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
    await tester.tap(find.text('确认永久删除'));
    await tester.pumpAndSettle();
    expect(
      api.posts.single.path,
      '/api/assets/upload/00000000000000000000000000000019/purge',
    );
    expect(api.posts.single.body, {'confirm': true});
    expect(tester.takeException(), isNull);
  });

  testWidgets('平台页只读缓存，保存失败清除密钥并保留模型草稿', (tester) async {
    final api = _PageApi()..saveFails = true;
    addTearDown(api.dispose);
    await _show(tester, SettingsPage(api: api, onChanged: () {}));
    expect(api.posts, isEmpty);
    expect(api.reads, contains('/api/model-platforms/default/models'));
    await tester.ensureVisible(_field('默认模型 ID'));
    await tester.enterText(_field('默认模型 ID'), 'edited-test-model');
    await tester.ensureVisible(_field('API 密钥'));
    await tester.enterText(_field('API 密钥'), 'synthetic-key-never-sent');
    await tester.tap(find.text('保存并生效'));
    await tester.pumpAndSettle();
    expect(api.posts.single.body['model'], 'edited-test-model');
    expect(
      tester.widget<TextField>(_field('API 密钥')).controller!.text,
      isEmpty,
    );
    expect(
      tester.widget<TextField>(_field('默认模型 ID')).controller!.text,
      'edited-test-model',
    );
    expect(find.text('测试保存失败'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('录音独立读取选区并限长，读取失败清除旧范围且不提交录制', (tester) async {
    final api = _PageApi();
    addTearDown(api.dispose);
    await _show(tester, RecordingPage(api: api, onAttach: (_) {}));
    await tester.ensureVisible(find.text('读取选区'));
    await tester.tap(find.text('读取选区'));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(_field('起点')).controller!.text, '2.500');
    expect(tester.widget<TextField>(_field('时长')).controller!.text, '30.000');
    expect(api.posts, isEmpty);
    api.selectionFails = true;
    await tester.tap(find.text('读取选区'));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(_field('起点')).controller!.text, isEmpty);
    expect(tester.widget<TextField>(_field('时长')).controller!.text, isEmpty);
    await tester.tap(find.text('开始录制'));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
    expect(api.audioReads, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets('回收站确认取消不会删除，确认删除携带后端确认字段', (tester) async {
    final api = _PageApi();
    addTearDown(api.dispose);
    var changes = 0;
    await _show(
      tester,
      TrashPage(api: api, onChanged: () => changes++),
      height: 450,
    );
    await tester.tap(find.byTooltip('永久删除'));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
    await tester.tap(find.byTooltip('永久删除'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('确认永久删除'));
    await tester.pumpAndSettle();
    expect(api.posts.single.path, '/api/trash/upload/test-trash/purge');
    expect(api.posts.single.body, {'confirm': true});
    expect(changes, 1);
    expect(tester.takeException(), isNull);
  });
}
