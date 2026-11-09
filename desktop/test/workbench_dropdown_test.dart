import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:synthv_desktop/theme.dart';
import 'package:synthv_desktop/widgets/workbench_dropdown.dart';

void main() {
  for (final brightness in Brightness.values) {
    for (final density in [VisualDensity.standard, VisualDensity.compact]) {
      for (final enabled in [true, false]) {
        testWidgets(
          '选择框真实绘制区域在 $brightness / $density / enabled=$enabled 下保持34px',
          (tester) async {
            await tester.pumpWidget(
              MaterialApp(
                theme: WorkbenchTheme.build(brightness)
                    .copyWith(visualDensity: density),
                home: Scaffold(
                  body: Align(
                    alignment: Alignment.topLeft,
                    child: SizedBox(
                      width: 240,
                      child: WorkbenchDropdown<String>(
                        value: 'selected',
                        items: const [
                          DropdownMenuItem(
                            value: 'selected',
                            child: Text('选择值'),
                          ),
                        ],
                        onChanged: enabled ? (_) {} : null,
                      ),
                    ),
                  ),
                ),
              ),
            );
            await tester.pumpAndSettle();
            // 通过文本后代定位实际 InputDecorator 容器；这个矩形就是背景填充与
            // OutlineInputBorder 的绘制区域，不能用外层表单或装饰器尺寸代替。
            final selected = find.text('选择值');
            final container = InputDecorator.containerOf(
              tester.element(selected),
            )!;
            expect(container.size.height, WorkbenchTheme.controlHeight);
            final top = container.localToGlobal(Offset.zero).dy;
            expect(
              tester.getCenter(selected).dy,
              closeTo(top + WorkbenchTheme.controlHeight / 2, 1),
            );
            final decoration = tester
                .widget<InputDecorator>(find.byType(InputDecorator))
                .decoration;
            expect(decoration.visualDensity, VisualDensity.standard);
            expect(
              decoration.contentPadding,
              const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
            );
            expect(tester.takeException(), isNull);
          },
        );
      }
    }
  }
}
