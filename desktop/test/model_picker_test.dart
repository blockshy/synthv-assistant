import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/theme.dart';
import 'package:synthv_desktop/widgets/model_picker.dart';

/// 模拟接口仅返回内存目录，不接触真实供应商、用户密钥或会话存储。
/// 分开记录 GET / POST / exclusive，核验浏览、搜索、刷新三种动作的边界。
class _CatalogApi extends WorkbenchApi {
  _CatalogApi() : super(port: 1, token: 'fixture');

  final gets = <String>[];
  final posts = <String>[];
  final pending = <String, Completer<dynamic>>{};
  final catalogs = <String, JsonMap>{};
  int exclusiveCalls = 0;
  Completer<dynamic>? pendingRefresh;
  JsonMap refreshResult = {
    'source': 'api',
    'cachedAt': '2026-10-27T12:00:00Z',
    'models': [
      {'id': 'fresh-model', 'label': '新目录模型'},
    ],
  };

  @override
  Future<dynamic> getJson(String path) async {
    gets.add(path);
    if (pending[path] case final completer?) return completer.future;
    return catalogs[path] ??
        {
          'source': 'cache',
          'cachedAt': '2026-10-27T12:00:00Z',
          'models': [
            {'id': 'alpha-model', 'label': 'Alpha'},
            {'id': 'beta-model', 'label': 'Beta'},
          ],
        };
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    posts.add(path);
    return pendingRefresh?.future ?? refreshResult;
  }

  @override
  Future<T> exclusive<T>(Future<T> Function() action) {
    exclusiveCalls++;
    return super.exclusive(action);
  }
}

const _platforms = <JsonMap>[
  {
    'id': 'first',
    'name': '主服务',
    'provider': 'openai',
    'keyConfigured': true,
    'connectionConfigured': true,
    'configured': true,
    'model': 'default-model',
  },
  {
    'id': 'second',
    'name': '备用服务',
    'provider': 'qwen',
    'keyConfigured': true,
    'connectionConfigured': true,
    'configured': true,
    'model': 'other-default',
  },
];

/// 通过真实 showDialog 获取提交值；取消返回 null，不借助页面全局状态模拟提交。
Future<void> _openPicker(
  WidgetTester tester,
  _CatalogApi api,
  List<ModelSelectionResult?> results, {
  List<JsonMap> platforms = _platforms,
  String modelId = '',
  bool settle = true,
  Brightness brightness = Brightness.dark,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      theme: WorkbenchTheme.build(brightness),
      home: Builder(
        builder: (context) => Scaffold(
          body: TextButton(
            child: const Text('打开选择器'),
            onPressed: () async {
              results.add(
                await showDialog<ModelSelectionResult>(
                  context: context,
                  builder: (context) => ModelPickerDialog(
                    api: api,
                    platforms: platforms,
                    platformId: 'first',
                    modelId: modelId,
                  ),
                ),
              );
            },
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('打开选择器'));
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    // 在途目录有旋转指示器，不能等待全局动画稳定，否则测试本身会超时。
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 250));
  }
}

Future<void> _switchToSecond(WidgetTester tester, {bool settle = true}) async {
  await tester.tap(find.byType(DropdownButtonFormField<String>));
  await tester.pump(const Duration(milliseconds: 250));
  await tester.tap(find.text('备用服务').last);
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 250));
  }
}

void main() {
  for (final brightness in Brightness.values) {
    testWidgets('模型服务下拉与搜索框在 $brightness 下边框等高且正文不变粗', (tester) async {
      final api = _CatalogApi();
      addTearDown(api.dispose);
      await _openPicker(tester, api, [], brightness: brightness);
      // InputDecorator 本身的布局高度仍可能包含未填色的空白；通过其公开的
      // containerOf 读取真正绘制边框与填充的区域，避免只测外框而漏掉 24px 矮框。
      final dropdown = find.descendant(
        of: find.byKey(const ValueKey('model-picker-platform-first')),
        matching: find.byType(InputDecorator),
      );
      final search = find.descendant(
        of: find.byKey(const ValueKey('model-picker-search')),
        matching: find.byType(InputDecorator),
      );
      final dropdownText = find.descendant(
        of: dropdown,
        matching: find.text('主服务'),
      );
      final searchText = find.descendant(
        of: search,
        matching: find.byType(EditableText),
      );
      final dropdownContainer = InputDecorator.containerOf(
        tester.element(dropdownText),
      )!;
      final searchContainer = InputDecorator.containerOf(
        tester.element(searchText),
      )!;
      final dropdownRect =
          dropdownContainer.localToGlobal(Offset.zero) & dropdownContainer.size;
      final searchRect =
          searchContainer.localToGlobal(Offset.zero) & searchContainer.size;
      expect(dropdownRect.height, WorkbenchTheme.controlHeight);
      expect(dropdownRect.height, searchRect.height);
      expect(dropdownRect.top, searchRect.top);
      expect(
        tester.getCenter(dropdownText).dy,
        closeTo(tester.getCenter(searchText).dy, 1),
      );
      final field = tester.widget<DropdownButtonFormField<String>>(
        find.byType(DropdownButtonFormField<String>),
      );
      // DropdownButtonFormField 没有公开 style；检验最终渲染树中的下拉按钮。
      final button = tester.widget<DropdownButton<String>>(
        find.descendant(
          of: find.byWidget(field),
          matching: find.byType(DropdownButton<String>),
        ),
      );
      expect(button.style?.fontSize, 13);
      expect(button.style?.fontWeight, FontWeight.w400);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('切换服务与编辑草稿后取消，不返回配置且不执行任何写入', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final results = <ModelSelectionResult?>[];
    await _openPicker(tester, api, results, modelId: 'current-model');
    await _switchToSecond(tester);
    await tester.enterText(
      find.byKey(const ValueKey('model-picker-manual')),
      'draft-model',
    );
    expect(results, isEmpty);
    await tester.tap(find.byTooltip('取消选择'));
    await tester.pumpAndSettle();
    expect(results, [null]);
    expect(api.posts, isEmpty);
    expect(api.exclusiveCalls, 0);
    expect(api.gets, [
      '/api/model-platforms/first/models',
      '/api/model-platforms/second/models',
    ]);
    expect(_platforms.first['model'], 'default-model');
  });

  testWidgets('打开显示缓存时间，搜索只过滤本地目录，点击模型才提交', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final results = <ModelSelectionResult?>[];
    await _openPicker(tester, api, results);
    expect(find.textContaining('本地缓存 · 2026-10-27'), findsOneWidget);
    expect(find.text('alpha-model'), findsOneWidget);
    await tester.enterText(
      find.byKey(const ValueKey('model-picker-search')),
      'BETA',
    );
    await tester.pump();
    expect(find.text('alpha-model'), findsNothing);
    expect(find.text('beta-model'), findsOneWidget);
    expect(results, isEmpty);
    expect(api.gets.length, 1);
    expect(api.posts, isEmpty);
    await tester.tap(
      find.byKey(const ValueKey('model-picker-model-beta-model')),
    );
    await tester.pumpAndSettle();
    expect(results.single!.platformId, 'first');
    expect(results.single!.modelId, 'beta-model');
    expect(results.single!.manage, isFalse);
  });

  testWidgets('默认选项显示真实模型 ID，并用空 ID 保持跟随平台默认值', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final results = <ModelSelectionResult?>[];
    await _openPicker(tester, api, results);
    final defaultRow = find.byKey(const ValueKey('model-picker-default'));
    expect(
      find.descendant(of: defaultRow, matching: find.text('default-model')),
      findsOneWidget,
    );
    await tester.tap(defaultRow);
    await tester.pumpAndSettle();
    expect(results.single!.modelId, isEmpty);
    expect(results.single!.platformId, 'first');
    expect(api.posts, isEmpty);
  });

  testWidgets('目录缺少目标时可提交手动 ID，去掉两端空白且不发模型请求', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final results = <ModelSelectionResult?>[];
    await _openPicker(tester, api, results);
    await tester.enterText(
      find.byKey(const ValueKey('model-picker-manual')),
      '  custom-model-v2  ',
    );
    await tester.pump();
    expect(results, isEmpty);
    await tester.tap(find.widgetWithText(FilledButton, '使用此模型'));
    await tester.pumpAndSettle();
    expect(results.single!.modelId, 'custom-model-v2');
    expect(results.single!.platformId, 'first');
    expect(api.posts, isEmpty);
  });

  testWidgets('平台切换后忽略旧平台的迟到目录，选中结果仍属于当前平台', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final old = Completer<dynamic>();
    api.pending['/api/model-platforms/first/models'] = old;
    api.catalogs['/api/model-platforms/second/models'] = {
      'platformId': 'second',
      'source': 'cache',
      'models': [
        {'id': 'second-model', 'label': '第二平台模型'},
      ],
    };
    final results = <ModelSelectionResult?>[];
    await _openPicker(tester, api, results, settle: false);
    await _switchToSecond(tester);
    expect(find.text('second-model'), findsOneWidget);
    old.complete({
      'platformId': 'first',
      'source': 'cache',
      'models': [
        {'id': 'late-old-model', 'label': '旧平台迟到内容'},
      ],
    });
    await tester.pumpAndSettle();
    expect(find.text('late-old-model'), findsNothing);
    expect(find.text('second-model'), findsOneWidget);
    await tester.tap(
      find.byKey(const ValueKey('model-picker-model-second-model')),
    );
    await tester.pumpAndSettle();
    expect(results.single!.platformId, 'second');
    expect(results.single!.modelId, 'second-model');
    expect(api.posts, isEmpty);
  });

  testWidgets('只有用户显式刷新才通过 exclusive POST 模型目录', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final results = <ModelSelectionResult?>[];
    await _openPicker(tester, api, results);
    expect(api.posts, isEmpty);
    await tester.tap(find.byTooltip('刷新模型目录（访问服务）'));
    await tester.pumpAndSettle();
    expect(api.posts, ['/api/model-platforms/first/models']);
    expect(api.exclusiveCalls, 1);
    expect(find.text('fresh-model'), findsOneWidget);
    expect(find.textContaining('已刷新目录'), findsOneWidget);
    expect(results, isEmpty);
    await tester.enterText(
      find.byKey(const ValueKey('model-picker-search')),
      'fresh',
    );
    await tester.pump();
    expect(api.posts.length, 1);
    expect(api.gets.length, 1);
  });

  testWidgets('已有缓存时刷新在途禁止关闭并提交，刷新结束后保留草稿供明确选择', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final refresh = Completer<dynamic>();
    api.pendingRefresh = refresh;
    final results = <ModelSelectionResult?>[];
    await _openPicker(tester, api, results);
    await tester.enterText(
      find.byKey(const ValueKey('model-picker-manual')),
      'kept-manual-model',
    );
    await tester.pump();
    await tester.tap(find.byTooltip('刷新模型目录（访问服务）'));
    await tester.pump();
    expect(api.busy, isTrue);

    // 旧缓存和默认项仍可浏览，但不可让弹层先 pop、随后被会话 busy guard 丢弃。
    final cached = find.byKey(const ValueKey('model-picker-model-alpha-model'));
    final defaultRow = find.byKey(const ValueKey('model-picker-default'));
    expect(tester.widget<InkWell>(cached).onTap, isNull);
    expect(tester.widget<InkWell>(defaultRow).onTap, isNull);
    await tester.tap(cached);
    await tester.tap(defaultRow);
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, '使用此模型'))
          .onPressed,
      isNull,
    );
    // 键盘提交必须走同一个保护条件，不能绕过按钮禁用状态。
    await tester.showKeyboard(
      find.byKey(const ValueKey('model-picker-manual')),
    );
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pump();
    expect(results, isEmpty);
    expect(find.byType(ModelPickerDialog), findsOneWidget);

    refresh.complete(api.refreshResult);
    await tester.pumpAndSettle();
    expect(api.busy, isFalse);
    expect(find.text('kept-manual-model'), findsOneWidget);
    expect(tester.widget<InkWell>(defaultRow).onTap, isNotNull);
    await tester.tap(find.widgetWithText(FilledButton, '使用此模型'));
    await tester.pumpAndSettle();
    expect(results.single!.platformId, 'first');
    expect(results.single!.modelId, 'kept-manual-model');
    expect(api.posts, ['/api/model-platforms/first/models']);
  });

  testWidgets('没有默认模型但连接完整的服务仍能读取目录并使用手动 ID', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final results = <ModelSelectionResult?>[];
    await _openPicker(
      tester,
      api,
      results,
      platforms: [
        {..._platforms.first, 'configured': false, 'model': ''},
      ],
    );
    expect(api.gets.length, 1);
    expect(find.byKey(const ValueKey('model-picker-default')), findsNothing);
    expect(find.text('alpha-model'), findsOneWidget);
    await tester.enterText(
      find.byKey(const ValueKey('model-picker-manual')),
      'chosen-model',
    );
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, '使用此模型'));
    await tester.pumpAndSettle();
    expect(results.single!.modelId, 'chosen-model');
  });

  testWidgets('未配置密钥的平台禁止选择并引导管理，管理返回导航意图', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    final results = <ModelSelectionResult?>[];
    await _openPicker(
      tester,
      api,
      results,
      platforms: [
        {
          ..._platforms.first,
          'connectionConfigured': false,
          'keyConfigured': false,
        },
      ],
    );
    expect(api.gets, isEmpty);
    expect(find.byKey(const ValueKey('model-picker-default')), findsNothing);
    expect(
      tester
          .widget<TextField>(find.byKey(const ValueKey('model-picker-manual')))
          .enabled,
      isFalse,
    );
    await tester.tap(find.text('前往模型服务'));
    await tester.pumpAndSettle();
    expect(results.single!.manage, isTrue);
    expect(results.single!.platformId, 'first');
    expect(results.single!.modelId, isEmpty);
    expect(api.posts, isEmpty);
  });

  testWidgets('旧服务快照依据协议与密钥兼容读取，窄窗口内容不溢出', (tester) async {
    final api = _CatalogApi();
    addTearDown(api.dispose);
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(420, 440);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final results = <ModelSelectionResult?>[];
    await _openPicker(
      tester,
      api,
      results,
      platforms: const [
        {
          'id': 'first',
          'name': '一个可以完整保存在提示中的很长服务名称',
          'provider': 'qwen',
          'keyConfigured': true,
          'model': 'default-model',
        },
      ],
    );
    expect(api.gets.length, 1);
    expect(tester.takeException(), isNull);
    await tester.enterText(
      find.byKey(const ValueKey('model-picker-manual')),
      'narrow-model',
    );
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, '使用此模型'));
    await tester.pumpAndSettle();
    expect(results.single!.modelId, 'narrow-model');
    expect(tester.takeException(), isNull);
  });
}
