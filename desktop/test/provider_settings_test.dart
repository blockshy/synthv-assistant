import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/pages/page_chrome.dart';
import 'package:synthv_desktop/pages/settings_page.dart';
import 'package:synthv_desktop/theme.dart';

const _namedId = '11111111111111111111111111111111';
const _createdId = '22222222222222222222222222222222';

/// 仅模拟本机 API 与公开目录；测试不接触真实凭据、模型供应商或宿主。
class _ProviderApi extends WorkbenchApi {
  _ProviderApi() : super(port: 1, token: 'isolated-fixture');
  final reads = <String>[];
  final posts = <({String path, JsonMap body})>[];
  String defaultId = 'default', registryRevision = 'registry-1';
  bool failSave = false;
  String failureMessage = '测试保存失败';
  Completer<JsonMap>? connectionReply;
  final items = <JsonMap>[
    {
      'id': 'default',
      'name': '默认平台',
      'provider': 'openai',
      'baseUrl': 'https://example.invalid/v1',
      'model': 'voice-model',
      'timeoutSeconds': 60,
      'keyConfigured': true,
      'configured': true,
      'connectionConfigured': true,
      'revision': 'default-revision',
    },
    {
      'id': _namedId,
      'name': '专用平台',
      'provider': 'qwen',
      'baseUrl': 'https://example.invalid/v1',
      'model': 'second-model',
      'timeoutSeconds': 60,
      'keyConfigured': true,
      'configured': true,
      'connectionConfigured': true,
      'revision': 'registry-1',
    },
  ];

  JsonMap get catalog => {
    'models': [
      {'id': 'voice-model', 'label': '语音模型'},
      {'id': 'text-model', 'label': '文本模型'},
    ],
    'source': 'cache',
    'cacheHit': true,
    'stale': false,
  };

  @override
  Future<dynamic> getJson(String path) async {
    reads.add(path);
    if (path == '/api/model-platforms') {
      return {
        'items': items.map((item) => Map<String, dynamic>.from(item)).toList(),
        'defaultPlatformId': defaultId,
        'revision': registryRevision,
      };
    }
    if (path.endsWith('/models')) return catalog;
    throw StateError('测试未声明的读取：$path');
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    posts.add((path: path, body: Map<String, dynamic>.from(body)));
    if (path == '/api/model-platforms') {
      if (failSave) throw ApiException(failureMessage);
      final id = body['id'] ?? _createdId;
      final previous =
          items.where((item) => item['id'] == id).firstOrNull ?? {};
      final disabled = body['provider'] == 'none';
      registryRevision = 'registry-${posts.length + 1}';
      final item = <String, dynamic>{
        ...previous,
        ...body,
        'id': id,
        'revision': id == 'default'
            ? 'default-${posts.length}'
            : registryRevision,
        'connectionConfigured': !disabled,
        'configured': !disabled && body['model'] != '',
        'keyConfigured': !disabled,
      }..remove('apiKey');
      if (disabled) {
        item.addAll({'model': '', 'baseUrl': '', 'configured': false});
      }
      items.removeWhere((value) => value['id'] == id);
      items.add(item);
      return item;
    }
    if (path.endsWith('/test-connection')) {
      return connectionReply?.future ??
          {
            ...catalog,
            'status': 'ok',
            'revision': body['revision'],
            'configurationChanged': false,
            'elapsedMs': 123,
            'message': '目录访问成功，不代表模型推理已经验证。',
          };
    }
    if (path.endsWith('/models')) return {...catalog, 'message': '目录已更新'};
    if (path.endsWith('/default-selection')) {
      defaultId = body['id'].toString();
      return {};
    }
    throw StateError('测试未声明的写入：$path');
  }
}

Finder _field(String name) => find.descendant(
  of: find.byKey(ValueKey('field:$name')),
  matching: find.byType(TextField),
  matchRoot: true,
);

Future<void> _show(
  WidgetTester tester,
  _ProviderApi api, {
  double width = 1000,
  double height = 650,
  Brightness brightness = Brightness.dark,
  GlobalKey<SettingsPageState>? pageKey,
  String? initialPlatformId,
}) async {
  tester.view.physicalSize = Size(width, height);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      theme: WorkbenchTheme.build(brightness),
      home: Scaffold(
        body: SettingsPage(
          key: pageKey,
          api: api,
          initialPlatformId: initialPlatformId,
          onChanged: () {},
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('离开设置主页面再返回后，已展开高级设置和输入框均可正常恢复', (tester) async {
    final api = _ProviderApi();
    addTearDown(api.dispose);
    tester.view.physicalSize = const Size(900, 500);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    late StateSetter navigate;
    var settingsVisible = true;
    await tester.pumpWidget(
      MaterialApp(
        theme: WorkbenchTheme.build(Brightness.dark),
        home: StatefulBuilder(
          builder: (context, setState) {
            navigate = setState;
            return Scaffold(
              body: settingsVisible
                  ? SettingsPage(api: api, onChanged: () {})
                  : const Center(child: Text('其他主页面')),
            );
          },
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('高级设置'));
    await tester.tap(find.text('高级设置'));
    await tester.pumpAndSettle();
    await tester.drag(
      find.byKey(const PageStorageKey('provider-config-scroll')),
      const Offset(0, -160),
    );
    await tester.pumpAndSettle();
    // 与工作台主页面导航一致：移除 SettingsPage，但保留同一路由的 PageStorage。
    navigate(() => settingsVisible = false);
    await tester.pumpAndSettle();
    navigate(() => settingsVisible = true);
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.byType(ErrorWidget), findsNothing);
    expect(_field('等待超时（秒）'), findsOneWidget);
    expect(api.posts, isEmpty);
  });

  testWidgets('滚动连接配置后切换标签再返回，滚动位置不覆盖高级设置展开状态', (tester) async {
    final api = _ProviderApi();
    addTearDown(api.dispose);
    await _show(tester, api, width: 900, height: 420);
    // 真实滚动会向 PageStorage 写入 double；展开控件只能读取自己的 bool。
    // 仅切页但不滚动的布局测试无法发现两种状态共享存储键的问题。
    await tester.drag(
      find.byKey(const PageStorageKey('provider-config-scroll')),
      const Offset(0, -180),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('settings-tab:1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('settings-tab:0')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.byType(ErrorWidget), findsNothing);
    expect(find.text('高级设置'), findsOneWidget);
    await tester.ensureVisible(find.text('高级设置'));
    await tester.tap(find.text('高级设置'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('settings-tab:1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('settings-tab:0')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(_field('等待超时（秒）'), findsOneWidget);
  });

  for (final width in [360.0, 900.0]) {
    for (final brightness in Brightness.values) {
      testWidgets('模型服务在 $width / $brightness 下两页签与新增流程保持底栏可见', (tester) async {
        final api = _ProviderApi();
        addTearDown(api.dispose);
        await _show(
          tester,
          api,
          width: width,
          height: 480,
          brightness: brightness,
        );
        final footer = tester.getRect(find.byType(PageFooter));
        expect(footer.bottom, lessThanOrEqualTo(480));
        expect(tester.takeException(), isNull);
        await tester.tap(find.byKey(const ValueKey('settings-tab:1')));
        await tester.pumpAndSettle();
        expect(tester.getRect(find.byType(PageFooter)), footer);
        expect(tester.takeException(), isNull);
        await tester.tap(find.byTooltip('新增模型平台'));
        await tester.pumpAndSettle();
        expect(find.text('添加模型平台'), findsOneWidget);
        expect(tester.getRect(find.byType(PageFooter)), footer);
        expect(tester.takeException(), isNull);
        expect(api.posts, isEmpty);
      });
    }
  }

  testWidgets('新建先保存空模型连接，再明确获取目录选择默认模型', (tester) async {
    final api = _ProviderApi();
    addTearDown(api.dispose);
    await _show(tester, api);
    await tester.tap(find.byTooltip('新增模型平台'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('provider-preset:0')));
    await tester.pumpAndSettle();
    await tester.enterText(_field('API 密钥'), 'fixture-key-only');
    await tester.tap(find.byKey(const ValueKey('save-platform')));
    await tester.pumpAndSettle();
    expect(api.posts.single.path, '/api/model-platforms');
    expect(api.posts.single.body['model'], '');
    expect(find.byKey(const ValueKey('field:默认模型 ID')), findsOneWidget);
    expect(find.byTooltip('设为新会话默认'), findsOneWidget);
    expect(
      tester
          .widget<IconButton>(
            find.byWidgetPredicate(
              (widget) => widget is IconButton && widget.tooltip == '设为新会话默认',
            ),
          )
          .onPressed,
      isNull,
    );
    await tester.tap(find.text('获取模型'));
    await tester.pumpAndSettle();
    expect(api.posts.last.path, '/api/model-platforms/$_createdId/models');
    await tester.tap(find.byKey(const ValueKey('model-item:voice-model')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('save-platform')));
    await tester.pumpAndSettle();
    expect(api.posts.last.body['id'], _createdId);
    expect(api.posts.last.body['model'], 'voice-model');
    expect(api.posts.last.body['apiKey'], '');
    expect(tester.takeException(), isNull);
  });

  testWidgets('离开前取消保留草稿，保存失败不离开且清除密钥', (tester) async {
    final api = _ProviderApi()..failSave = true;
    addTearDown(api.dispose);
    final key = GlobalKey<SettingsPageState>();
    await _show(tester, api, pageKey: key);
    await tester.enterText(_field('API 密钥'), 'fixture-not-persisted');
    final cancelled = key.currentState!.maybeLeave();
    await tester.pumpAndSettle();
    await tester.tap(find.text('继续编辑'));
    await tester.pumpAndSettle();
    expect(await cancelled, isFalse);
    expect(
      tester.widget<TextField>(_field('API 密钥')).controller!.text,
      'fixture-not-persisted',
    );
    final saved = key.currentState!.maybeLeave();
    await tester.pumpAndSettle();
    await tester.tap(find.text('保存并继续'));
    await tester.pumpAndSettle();
    expect(await saved, isFalse);
    expect(
      tester.widget<TextField>(_field('API 密钥')).controller!.text,
      isEmpty,
    );
    expect(find.text('测试保存失败'), findsOneWidget);
    expect(api.posts, hasLength(1));
  });

  testWidgets('按会话指定平台定位，选择默认与切换编辑状态独立', (tester) async {
    final api = _ProviderApi();
    addTearDown(api.dispose);
    await _show(tester, api, initialPlatformId: _namedId);
    expect(tester.widget<TextField>(_field('平台名称')).controller!.text, '专用平台');
    expect(api.defaultId, 'default');
    await tester.tap(find.byTooltip('设为新会话默认'));
    await tester.pumpAndSettle();
    expect(api.posts.single.body, {'id': _namedId, 'revision': 'registry-1'});
    expect(find.text('新会话默认'), findsOneWidget);
    expect(api.defaultId, _namedId);
  });

  testWidgets('连接测试仅发送保存版本，配置已变更的迟到目录被丢弃', (tester) async {
    final api = _ProviderApi()..connectionReply = Completer<JsonMap>();
    addTearDown(api.dispose);
    await _show(tester, api);
    await tester.tap(find.text('测试连接'));
    await tester.pump();
    expect(
      api.posts.single.path,
      '/api/model-platforms/default/test-connection',
    );
    expect(api.posts.single.body, {'revision': 'default-revision'});
    api.connectionReply!.complete({
      'status': 'ok',
      'revision': 'default-revision',
      'configurationChanged': true,
      'models': [
        {'id': 'stale-model', 'label': '不能显示'},
      ],
    });
    await tester.pumpAndSettle();
    expect(find.text('测试期间配置发生变化，请重新加载后再测试。'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('settings-tab:1')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('model-item:stale-model')), findsNothing);
    expect(
      find.byKey(const ValueKey('model-item:voice-model')),
      findsOneWidget,
    );
  });

  testWidgets('低高度模型目录出现长错误后仍可滚动编辑并访问固定保存栏', (tester) async {
    final api = _ProviderApi()
      ..failSave = true
      ..failureMessage = '配置已被其他页面更新，请重新加载后检查平台信息。' * 10;
    addTearDown(api.dispose);
    await _show(tester, api, width: 360, height: 480);
    await tester.tap(find.byKey(const ValueKey('settings-tab:1')));
    await tester.pumpAndSettle();
    await tester.enterText(_field('默认模型 ID'), 'draft-model');
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('save-platform')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(
      tester.getRect(find.byType(PageFooter)).bottom,
      lessThanOrEqualTo(480),
    );
    expect(
      tester.widget<TextField>(_field('默认模型 ID')).controller!.text,
      'draft-model',
    );
  });

  testWidgets('更换地址后旧密钥不能沿用，未填写新密钥时不提交', (tester) async {
    final api = _ProviderApi();
    addTearDown(api.dispose);
    await _show(tester, api);
    await tester.enterText(
      _field('API Base URL'),
      'https://new.example.invalid/v1',
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('save-platform')));
    await tester.pumpAndSettle();
    expect(api.posts, isEmpty);
    expect(find.text('请填写 API 密钥；切换服务类型或地址后必须重新填写。'), findsOneWidget);
    await tester.enterText(_field('API 密钥'), 'fixture-replacement');
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('save-platform')));
    await tester.pumpAndSettle();
    expect(api.posts.single.body['apiKey'], 'fixture-replacement');
    expect(
      tester.widget<TextField>(_field('API 密钥')).controller!.text,
      isEmpty,
    );
  });
}
