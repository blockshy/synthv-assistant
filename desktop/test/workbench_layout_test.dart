import 'dart:ui' show PointerDeviceKind;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/theme.dart';
import 'package:synthv_desktop/workbench.dart';

import 'workbench_model_test.dart' show ModelApi;

/// 给完整工作台提供可编辑的合成平台；不读取真实保存配置或发送供应商请求。
class _NavigationApi extends ModelApi {
  @override
  Future<dynamic> getJson(String path) async {
    if (path == '/api/model-platforms') {
      calls.add('GET $path');
      return {
        'revision': 'registry-fixture',
        'defaultPlatformId': 'default',
        'items': <JsonMap>[
          {
            'id': 'default',
            'name': '默认配置',
            'provider': 'openai',
            'model': 'fixture-model',
            'baseUrl': 'https://example.invalid/v1',
            'timeoutSeconds': 60,
            'configured': true,
            'connectionConfigured': true,
            'keyConfigured': true,
            'revision': 'platform-fixture',
          },
        ],
      };
    }
    return super.getJson(path);
  }
}

void main() {
  testWidgets('设置草稿保护覆盖页面导航，取消保留编辑，放弃后才离开', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.binding.setSurfaceSize(const Size(1440, 900));
    final api = _NavigationApi();
    await tester.pumpWidget(
      MaterialApp(
        theme: WorkbenchTheme.build(Brightness.dark),
        home: Workbench(
          api: api,
          mode: ThemeMode.system,
          onThemeChanged: (_) {},
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('模型服务').first);
    await tester.pumpAndSettle();
    final base = find.descendant(
      of: find.byKey(const ValueKey('field:API Base URL')),
      matching: find.byType(TextField),
    );
    await tester.ensureVisible(base);
    await tester.enterText(base, 'https://changed.invalid/v1');
    await tester.tap(find.text('回收站').first);
    await tester.pumpAndSettle();
    expect(find.text('保存平台修改？'), findsOneWidget);
    await tester.tap(find.text('继续编辑'));
    await tester.pumpAndSettle();
    expect(
      tester.widget<TextField>(base).controller!.text,
      'https://changed.invalid/v1',
    );
    await tester.tap(find.text('回收站').first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('放弃修改'));
    await tester.pumpAndSettle();
    expect(base, findsNothing);
    expect(
      api.calls.where((call) => call == 'POST /api/model-platforms'),
      isEmpty,
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    api.dispose();
    await tester.binding.setSurfaceSize(null);
  });

  testWidgets('鼠标单次位移可拖动左右分隔条并保存宽度', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.binding.setSurfaceSize(const Size(1536, 816));
    final api = ModelApi();
    await tester.pumpWidget(
      MaterialApp(
        theme: WorkbenchTheme.build(Brightness.dark),
        home: Workbench(
          api: api,
          mode: ThemeMode.system,
          onThemeChanged: (_) {},
        ),
      ),
    );
    await tester.pumpAndSettle();
    final left = find.byTooltip('调整导航宽度 · 双击复位');
    final right = find.byTooltip('调整检查器宽度 · 双击复位');
    final originalLeft = tester.getRect(left);
    final originalRight = tester.getRect(right);
    expect(originalLeft.left, 230);

    // 桌面自动化、低采样率鼠标或短拖动可能只产生一次 move，不能丢弃越过
    // 手势阈值的首段位移，否则用户明确拖动后宽度仍然保持不变。
    final leftGesture = await tester.startGesture(
      Offset(originalLeft.center.dx, 451),
      kind: PointerDeviceKind.mouse,
    );
    await leftGesture.moveBy(const Offset(42, 0));
    await leftGesture.up();
    await tester.pumpAndSettle();
    expect(tester.getRect(left).left, closeTo(272, .01));

    final rightGesture = await tester.startGesture(
      Offset(tester.getRect(right).center.dx, 451),
      kind: PointerDeviceKind.mouse,
    );
    await rightGesture.moveBy(const Offset(-36, 0));
    await rightGesture.up();
    await tester.pumpAndSettle();
    expect(tester.getRect(right).left, closeTo(originalRight.left - 36, .01));
    await tester.pump(const Duration(milliseconds: 400));
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getDouble('leftWidth'), 272);
    expect(prefs.getDouble('rightWidth'), 338);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
    api.dispose();
    await tester.binding.setSurfaceSize(null);
  });

  for (final size in [
    const Size(1440, 900),
    const Size(1150, 720),
    const Size(900, 620),
  ]) {
    for (final brightness in Brightness.values) {
      testWidgets('工作台 $size $brightness 控件布局无溢出且导航保留草稿', (tester) async {
        SharedPreferences.setMockInitialValues({});
        await tester.binding.setSurfaceSize(size);
        final api = ModelApi();
        await tester.pumpWidget(
          MaterialApp(
            theme: WorkbenchTheme.build(brightness),
            home: Workbench(
              api: api,
              mode: ThemeMode.system,
              onThemeChanged: (_) {},
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull);
        expect(find.text('调教会话'), findsOneWidget);
        // 音频功能退役后，主导航、附件按钮和欢迎入口均不再提供可触发操作。
        expect(find.text('音频素材'), findsNothing);
        expect(find.text('录音与听评'), findsNothing);
        expect(find.byTooltip('选择音频附件'), findsNothing);
        expect(find.text('听一听 A / B'), findsNothing);
        final composer = find.widgetWithText(TextField, '描述希望怎样演唱，或讨论这段调教…');
        await tester.enterText(composer, '未发送的测试草稿');
        await tester.pump();
        if (size.width >= 940) {
          await tester.tap(find.text('回收站').first);
          await tester.pumpAndSettle();
          expect(tester.takeException(), isNull);
          await tester.tap(find.text('当前会话').first);
          await tester.pumpAndSettle();
          expect(find.text('未发送的测试草稿'), findsOneWidget);
          expect(tester.takeException(), isNull);
        }
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
        api.dispose();
        await tester.binding.setSurfaceSize(null);
      });
    }
  }
}
