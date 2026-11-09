import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/pages/inspector.dart';
import 'package:synthv_desktop/theme.dart';
import 'package:synthv_desktop/widgets/parameter_draft_editor.dart';

/// 所有工程、音符与声线均为虚构数据；这些回归不会连接宿主或调用模型。
JsonMap _selection({bool native = true}) => {
  'projectFile': r'D:\fixture\song.svp',
  'hostVersion': '2.3.0',
  'groupName': '示例主歌',
  'groupPitchOffset': 2,
  'noteCount': 2,
  'startSeconds': 10.0,
  'endSeconds': 12.0,
  'notes': [
    {
      'index': 1,
      'pitch': 60,
      'lyrics': '你',
      'onsetSeconds': 10.0,
      'durationSeconds': 1.0,
    },
    {
      'index': 2,
      'pitch': 64,
      'lyrics': '好',
      'onsetSeconds': 11.0,
      'durationSeconds': 1.0,
    },
  ],
  'capabilities': {'curves': true, 'nativePitch': native},
  'parameters': {
    'breathiness': {
      'label': '气声',
      'kind': 'automation',
      'maxDelta': .3,
      'range': [-1, 1],
      'currentValues': {'minimum': -.1, 'maximum': .2},
    },
    'pitchCurve': {
      'label': '原生音高',
      'kind': 'pitch',
      'available': native,
      if (!native) 'unavailableReason': '既有原生曲线跨越选区边界。',
    },
    'vocalMode_Airy': {
      'label': '声线 · Airy',
      'modeName': 'Airy',
      'kind': 'vocalMode',
      'source': 'host',
      'maxDelta': 30,
      'unit': '百分点',
      'range': [0, 150],
      'voiceValues': {'pitch': 80, 'timbre': 50, 'pronunciation': 40},
    },
    'vocalMode_Cute': {
      'label': '声线 · Cute',
      'modeName': 'Cute',
      'kind': 'vocalMode',
      'source': 'user',
      'maxDelta': 30,
      'unit': '百分点',
      'range': [0, 150],
    },
  },
  'computedPitch': {
    'status': 'ready',
    'unit': 'MIDI',
    'samples': [
      [0.0, 62.1],
      [.4, null],
      [.6, 66.2],
      [1.0, 66.0],
    ],
  },
  'capabilityWarnings': ['宿主目录未承诺包含声库所有模式。'],
};

class _InspectorApi extends WorkbenchApi {
  _InspectorApi({this.native = true}) : super(port: 1, token: 'fixture');
  final bool native;
  bool write = false;
  final reads = <String>[];
  final posts = <({String path, JsonMap body})>[];
  @override
  Future<dynamic> getJson(String path) async {
    reads.add(path);
    if (path == '/api/status') {
      return {
        'bridge': {
          'connected': true,
          'session': 'fixture',
          'hostVersion': '2.3.0',
        },
        'writeEnabled': write,
      };
    }
    if (path == '/api/selection') return _selection(native: native);
    if (path == '/api/project') {
      return {
        'projectFile': r'D:\fixture\song.svp',
        'hostVersion': '2.3.0',
        'trackCount': 1,
        'tracks': [
          {'index': 1, 'name': '主唱', 'groups': []},
        ],
      };
    }
    throw StateError('未声明读取 $path');
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    posts.add((path: path, body: Map<String, dynamic>.from(body)));
    if (path == '/api/write-mode') {
      write = body['enabled'] == true;
      return {};
    }
    if (path == '/api/preview') {
      return {
        'previewId': 'fixture-preview',
        'parameter': body['parameter'],
        'startSeconds': 10,
        'endSeconds': 12,
        'beforePointCount': 2,
        'pointCount': 4,
        'curvePreview': [
          {'position': 0, 'before': 0, 'after': .05},
          {'position': 1, 'before': 0, 'after': .05},
        ],
      };
    }
    if (path == '/api/apply' || path == '/api/restore') {
      return {'verified': true};
    }
    throw StateError('未声明写入 $path');
  }
}

Future<void> _show(
  WidgetTester tester,
  Widget child, {
  double height = 760,
  double width = 360,
  Brightness brightness = Brightness.dark,
}) async {
  tester.view.physicalSize = Size(width, height);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    MaterialApp(
      theme: WorkbenchTheme.build(brightness),
      home: Scaffold(body: child),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _category(WidgetTester tester, String category) async {
  final picker = find.byKey(const ValueKey('inspector-choice:参数分类'));
  await tester.ensureVisible(picker);
  await tester.pumpAndSettle();
  await tester.tap(picker);
  await tester.pumpAndSettle();
  await tester.tap(find.text(category).last);
  await tester.pumpAndSettle();
}

void main() {
  test('草稿音符使用实际秒范围并且只叠加一次组移调', () {
    final notes = draftNotes(_selection());
    expect(notes.map((n) => n.pitch), [62, 66]);
    expect(notes.map((n) => n.start), [0, .5]);
    expect(notes.map((n) => n.end), [.5, 1]);
  });

  test('采样简化保留端点与转折，不抽掉明显音高变化', () {
    expect(
      simplifyDraft([
        [0, 60],
        [.25, 60.5],
        [.5, 61],
        [1, 62],
      ], .01),
      [
        [0, 60],
        [1, 62],
      ],
    );
    expect(
      simplifyDraft([
        [0, 60],
        [.5, 64],
        [1, 60],
      ], .01),
      [
        [0, 60],
        [.5, 64],
        [1, 60],
      ],
    );
  });

  testWidgets('工程和选区分别显式读取，缺失能力不可凭空出现', (tester) async {
    final api = _InspectorApi();
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api));
    expect(api.reads, ['/api/status']);
    await tester.tap(find.text('读取工程'));
    await tester.pumpAndSettle();
    expect(find.text('song.svp'), findsOneWidget);
    expect(find.text('2.3.0'), findsOneWidget);
    expect(api.reads, isNot(contains('/api/selection')));
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    expect(find.text('示例主歌'), findsOneWidget);
    expect(find.text('2 个'), findsOneWidget);
    expect(api.posts, isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets('声线目录按实际项目列出，并以自动化增量预览而不写入组级三维设置', (tester) async {
    final api = _InspectorApi();
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api), height: 1000);
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await _category(tester, '声库声线');
    expect(find.text('Airy'), findsOneWidget);
    expect(find.text('Cute'), findsOneWidget);
    expect(find.text('组声线设置（只读，不是自动化曲线）'), findsOneWidget);
    expect(find.text('80%'), findsOneWidget);
    await tester.tap(find.text('生成预览'));
    await tester.pumpAndSettle();
    final call = api.posts.single;
    expect(call.path, '/api/preview');
    expect(call.body['parameter'], 'vocalMode_Airy');
    expect(call.body['delta'], 5);
    expect(call.body.containsKey('voiceValues'), isFalse);
    expect(api.posts.where((p) => p.path == '/api/apply'), isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets('原生草稿来自乐谱，计算音高缺帧仅作参考且不会转为可写曲线', (tester) async {
    final api = _InspectorApi();
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api), height: 1200);
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await _category(tester, '原生音高');
    final editor = tester.widget<ParameterDraftEditor>(
      find.byType(ParameterDraftEditor),
    );
    expect(editor.points.first, [0, 62]);
    expect(editor.points.last, [1, 66]);
    expect(editor.selection['computedPitch']['samples'][1][1], isNull);
    expect(find.text('载入计算音高'), findsNothing);
    await tester.tap(find.text('生成预览'));
    await tester.pumpAndSettle();
    expect(api.posts.single.body['parameter'], 'pitchCurve');
    expect(api.posts.single.body['renderMode'], 'smooth');
    expect(api.posts.single.body['curve'], editor.points);
    expect(tester.takeException(), isNull);
  });

  testWidgets('原生能力不可用时给出原因且不能生成对应预览', (tester) async {
    final api = _InspectorApi(native: false);
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api));
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await _category(tester, '原生音高');
    expect(find.text('既有原生曲线跨越选区边界。'), findsOneWidget);
    expect(
      tester
          .widget<OutlinedButton>(find.widgetWithText(OutlinedButton, '生成预览'))
          .onPressed,
      isNull,
    );
    expect(api.posts, isEmpty);
  });

  testWidgets('编辑使旧确认失效，正式应用只提交已生成的预览编号', (tester) async {
    final api = _InspectorApi()..write = true;
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api), height: 1000);
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('生成预览'));
    await tester.pumpAndSettle();
    final apply = find.widgetWithText(FilledButton, '确认应用');
    expect(tester.widget<FilledButton>(apply).onPressed, isNotNull);
    final delta = find.byKey(const ValueKey('inspector-delta'));
    await tester.ensureVisible(delta);
    await tester.enterText(delta, '0.1');
    await tester.pumpAndSettle();
    expect(tester.widget<FilledButton>(apply).onPressed, isNull);
    expect(api.posts.where((p) => p.path == '/api/apply'), isEmpty);
    await tester.tap(find.text('生成预览'));
    await tester.pumpAndSettle();
    await tester.tap(apply);
    await tester.pumpAndSettle();
    // 工具栏入口只打开具体范围确认，已有写入权限也不跳过这一层。
    expect(api.posts.where((p) => p.path == '/api/apply'), isEmpty);
    expect(find.textContaining('2 个音符 · 10–12 秒'), findsWidgets);
    await tester.tap(find.text('确定应用'));
    await tester.pumpAndSettle();
    final submitted = api.posts.where((p) => p.path == '/api/apply').single;
    expect(submitted.body, {'previewId': 'fixture-preview'});
    expect(tester.widget<FilledButton>(apply).onPressed, isNull);
  });

  testWidgets('草稿撤销不恢复宿主也不写入参数', (tester) async {
    final api = _InspectorApi();
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api), height: 1200);
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await _category(tester, '原生音高');
    final original = tester
        .widget<ParameterDraftEditor>(find.byType(ParameterDraftEditor))
        .points;
    tester
        .widget<ParameterDraftEditor>(find.byType(ParameterDraftEditor))
        .onChanged([
          [0, 62.2],
          [.5, 64],
          [1, 66.1],
        ]);
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('撤销草稿'));
    await tester.tap(find.text('撤销草稿'));
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<ParameterDraftEditor>(find.byType(ParameterDraftEditor))
          .points,
      original,
    );
    expect(api.posts, isEmpty);
  });

  testWidgets('只读预览可就地确认权限，取消不写入，允许后只应用已确认票据', (tester) async {
    final api = _InspectorApi();
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api), height: 900);
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('生成预览'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('确认应用'));
    await tester.pumpAndSettle();
    expect(find.text('允许修改当前选区'), findsOneWidget);
    final finalConfirm = find.widgetWithText(FilledButton, '确定应用');
    expect(tester.widget<FilledButton>(finalConfirm).onPressed, isNull);
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();
    expect(api.posts.map((p) => p.path), ['/api/preview']);
    await tester.tap(find.text('确认应用'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('允许修改当前选区'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('确定应用'));
    await tester.pumpAndSettle();
    expect(api.posts.map((p) => p.path), [
      '/api/preview',
      '/api/write-mode',
      '/api/apply',
    ]);
    expect(api.posts.last.body, {'previewId': 'fixture-preview'});
    expect(find.text('已允许修改 · 关闭'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('确认期间票据失效时不因对话框同意而开启权限或应用旧范围', (tester) async {
    final api = _InspectorApi();
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api), height: 900);
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('生成预览'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('确认应用'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('允许修改当前选区'));
    await tester.pumpAndSettle();
    api.invalidatePreviews();
    await tester.tap(find.text('确定应用'));
    await tester.pumpAndSettle();
    expect(api.posts.map((p) => p.path), ['/api/preview']);
    expect(find.textContaining('预览已变化'), findsOneWidget);
  });

  testWidgets('宽屏参数页将属性与大画布分栏且底栏始终可见', (tester) async {
    final api = _InspectorApi();
    addTearDown(api.dispose);
    await _show(tester, Inspector(api: api), width: 1200, height: 720);
    await tester.tap(find.text('读取当前选区'));
    await tester.pumpAndSettle();
    await _category(tester, '原生音高');
    final canvas = tester.getRect(
      find.byKey(const ValueKey('parameter-draft-canvas')),
    );
    final category = tester.getRect(
      find.byKey(const ValueKey('inspector-choice:参数分类')),
    );
    expect(canvas.left, greaterThan(category.right));
    expect(canvas.width, greaterThan(700));
    expect(canvas.height, 340);
    expect(tester.getRect(find.text('生成预览')).bottom, lessThan(720));
    expect(tester.takeException(), isNull);
  });

  for (final brightness in Brightness.values) {
    testWidgets('低高度检查器 $brightness 固定确认栏可见且参数分组不溢出', (tester) async {
      final api = _InspectorApi();
      addTearDown(api.dispose);
      await _show(
        tester,
        Inspector(api: api),
        height: 400,
        brightness: brightness,
      );
      await tester.tap(find.text('读取当前选区'));
      await tester.pumpAndSettle();
      expect(tester.getRect(find.text('确认应用')).bottom, lessThan(400));
      expect(tester.getRect(find.text('生成预览')).bottom, lessThan(400));
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('控制点可直接新增拖动与删除，首尾与严格时间顺序仍有效', (tester) async {
    var points = <List<double>>[
      [0, 62],
      [1, 66],
    ];
    await _show(
      tester,
      StatefulBuilder(
        builder: (context, change) => ParameterDraftEditor(
          points: points,
          bounds: const [60, 68],
          selection: _selection(),
          parameter: 'pitchCurve',
          enabled: true,
          onChanged: (value) => change(() => points = value),
        ),
      ),
    );
    await tester.tap(find.byTooltip('编辑控制点'));
    await tester.pumpAndSettle();
    final rect = tester.getRect(
      find.byKey(const ValueKey('parameter-draft-canvas')),
    );
    final middle = rect.topLeft + Offset(38 + (rect.width - 48) / 2, 104.5);
    await tester.tapAt(middle);
    await tester.pumpAndSettle();
    expect(points.length, 3);
    expect(points[1][0], closeTo(.5, .001));
    await tester.dragFrom(middle, const Offset(20, -24));
    await tester.pumpAndSettle();
    expect(points[1][1], greaterThan(64));
    expect(points.first[0], 0);
    expect(points.last[0], 1);
    await tester.tap(find.byTooltip('删除选中的内部节点'));
    await tester.pumpAndSettle();
    expect(points.length, 2);
    expect(tester.takeException(), isNull);
  });

  testWidgets('手绘只替换本笔覆盖范围，范围外草稿与端点保持不变', (tester) async {
    var points = <List<double>>[
      [0, 62],
      [.3, 62],
      [.7, 66],
      [1, 66],
    ];
    await _show(
      tester,
      StatefulBuilder(
        builder: (context, change) => ParameterDraftEditor(
          points: points,
          bounds: const [60, 68],
          selection: _selection(),
          parameter: 'pitchCurve',
          enabled: true,
          onChanged: (value) => change(() => points = value),
        ),
      ),
    );
    final rect = tester.getRect(
      find.byKey(const ValueKey('parameter-draft-canvas')),
    );
    await tester.dragFrom(
      rect.topLeft + Offset(38 + (rect.width - 48) * .42, 60),
      Offset((rect.width - 48) * .14, 20),
    );
    await tester.pumpAndSettle();
    expect(
      points,
      containsAll([
        [0, 62],
        [.3, 62],
        [.7, 66],
        [1, 66],
      ]),
    );
    expect(points.length, lessThanOrEqualTo(64));
    expect(points.length, greaterThan(4));
    expect(tester.takeException(), isNull);
  });
}
