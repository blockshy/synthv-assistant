import 'dart:ui' show PointerDeviceKind;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:synthv_desktop/theme.dart';
import 'package:synthv_desktop/workbench.dart';

import 'workbench_model_test.dart' show ModelApi;

void main() {
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
        expect(find.text('一起把这句唱得更好。'), findsOneWidget);
        final composer = find.widgetWithText(TextField, '描述希望怎样演唱，或讨论这段调教…');
        await tester.enterText(composer, '未发送的测试草稿');
        await tester.pump();
        if (size.width >= 940) {
          await tester.tap(find.text('音频素材').first);
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
