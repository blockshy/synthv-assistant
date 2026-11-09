import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/widgets/curve_preview.dart';

/// 使用合成的宿主最终值验证图例语义，防止坐标留白被误报为实际修改幅度。
void main() {
  testWidgets('音高偏差展示最终原单位范围，并默认显示调整前曲线', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: Scaffold(
          body: CurvePreview(
            previews: [
              {
                'parameter': 'pitchDelta',
                'label': '旧名称',
                'unit': '音分',
                'startSeconds': 0,
                'endSeconds': 1,
                'curvePreview': [
                  {'position': 0, 'before': 12, 'after': 12},
                  {'position': .5, 'before': 12, 'after': 20},
                  {'position': 1, 'before': 12, 'after': 12},
                ],
              },
            ],
          ),
        ),
      ),
    );
    expect(find.text('音高偏差（叠加） · 12–20 音分'), findsOneWidget);
    expect(tester.widget<Checkbox>(find.byType(Checkbox)).value, isTrue);
    expect(find.textContaining('不是最终演唱音高'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
