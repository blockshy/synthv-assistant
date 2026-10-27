import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/pages/inspector.dart';
import 'package:synthv_desktop/widgets/curve_preview.dart';
import 'package:synthv_desktop/widgets/proposal_card.dart';

const _batch = '0123456789abcdef0123456789abcdef';

/// 固定快照不含本机工程、密钥或私人音频；测试绝不访问实际服务。
JsonMap _preview(String parameter, {int pitch = 60}) => {
  'previewId': 'preview-$parameter',
  'parameter': parameter,
  'label': parameter,
  'unit': parameter == 'pitchCurve' ? 'MIDI' : '',
  'startSeconds': 0,
  'endSeconds': 2,
  'notes': [
    {'startPosition': 0, 'endPosition': .4, 'pitch': pitch},
    {'startPosition': .5, 'endPosition': 1, 'pitch': pitch + 7},
  ],
  'curvePreview': [
    {'position': 0, 'before': 0, 'after': 0},
    {'position': .5, 'before': .1, 'after': .2},
    {'position': 1, 'before': 0, 'after': 0},
  ],
  'controlPoints': [
    {'position': 0, 'value': 0},
    {'position': 1, 'value': 0},
  ],
  'renderMode': 'points',
};

JsonMap _action(String id, {String status = 'proposed'}) => {
  'id': id,
  'parameter': id,
  'status': status,
  'delta': .1,
};

class _FakeApi extends WorkbenchApi {
  _FakeApi() : super(port: 0, token: 'test-only');
  final List<String> gets = [];
  final List<(String, JsonMap)> posts = [];
  bool partial = false, failApply = false;

  @override
  Future<dynamic> getJson(String path) async {
    gets.add(path);
    if (path == '/api/status') {
      return {
        'bridge': {'connected': true},
        'writeEnabled': true,
      };
    }
    if (path == '/api/selection') {
      return {
        'noteCount': 2,
        'startSeconds': 0,
        'endSeconds': 2,
        'capabilities': {'curves': true, 'nativePitch': true},
        'parameters': {
          'breathiness': {'label': '气声', 'maxDelta': .3},
          'pitchCurve': {'label': '原生音高曲线', 'available': true},
        },
        'notes': [
          {'onsetSeconds': 0, 'durationSeconds': 1, 'pitch': 60},
          {'onsetSeconds': 1, 'durationSeconds': 1, 'pitch': 67},
        ],
      };
    }
    throw StateError('未允许的测试请求：$path');
  }

  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    posts.add((path, Map<String, dynamic>.from(body)));
    if (path == '/api/assistant/batches/preview') {
      final ids = (body['actionIds'] as List).cast<String>();
      return {
        'batchId': _batch,
        'actions': [
          for (final id in ids)
            if (partial && id == 'tension')
              _action(id)
            else
              {
                ..._action(id, status: 'previewed'),
                'previewBatchId': _batch,
                'preview': _preview(id),
              },
        ],
        'errors': partial
            ? [
                {'actionId': 'tension', 'message': '张力未通过校验'},
              ]
            : [],
      };
    }
    if (path == '/api/assistant/batches/apply') {
      if (failApply) throw const ApiException('模拟连接中断', uncertain: true);
      return {
        'batchId': _batch,
        'actions': [_action('breathiness', status: 'applied')],
      };
    }
    if (path == '/api/preview') {
      return _preview(body['parameter'] as String);
    }
    throw StateError('未允许的测试请求：$path');
  }
}

Widget _host(Widget child, {double width = 360, bool dark = false}) =>
    MaterialApp(
      theme: ThemeData(brightness: dark ? Brightness.dark : Brightness.light),
      home: Scaffold(
        body: Align(
          alignment: Alignment.topLeft,
          child: SizedBox(
            width: width,
            child: SingleChildScrollView(child: child),
          ),
        ),
      ),
    );

Widget _proposal(_FakeApi api, {bool pair = false}) => ProposalCard(
  api: api,
  conversationId: 'test-conversation',
  onChanged: () {},
  message: {
    'id': 'test-message',
    'actions': [_action('breathiness'), if (pair) _action('tension')],
  },
);

Future<void> _tap(WidgetTester tester, String label) async {
  await tester.pumpAndSettle();
  await tester.ensureVisible(find.text(label));
  await tester.pumpAndSettle();
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

void main() {
  test('组合图拒绝不同音符或时间的历史快照', () {
    expect(compatiblePreviews([_preview('a'), _preview('b')]), isTrue);
    expect(
      compatiblePreviews([_preview('a'), _preview('b', pitch: 64)]),
      isFalse,
    );
    expect(
      compatiblePreviews([
        _preview('a'),
        {..._preview('b'), 'endSeconds': 3},
      ]),
      isFalse,
    );
    expect(
      compatiblePreviews([
        _preview('a'),
        {'notes': []},
      ]),
      isFalse,
    );
  });

  for (final dark in [false, true]) {
    testWidgets('窄侧栏多参数图例可换行 ${dark ? '深色' : '浅色'}', (tester) async {
      await tester.pumpWidget(
        _host(
          CurvePreview(
            previews: [
              for (var i = 0; i < 5; i++)
                {
                  ..._preview('参数$i'),
                  'label': '很长的声库专属参数名称 Airy Chest Cute $i',
                },
            ],
          ),
          width: 280,
          dark: dark,
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.byType(FilterChip), findsNWidgets(5));
    });
  }

  testWidgets('只有当次预览票据可确认，其他预览使旧票据失效', (tester) async {
    final api = _FakeApi();
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
    await _tap(tester, '生成预览');
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNotNull,
    );
    api.invalidatePreviews();
    await tester.pump();
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
    expect(api.posts.where((call) => call.$1.endsWith('/apply')), isEmpty);
  });

  testWidgets('部分参数失败时只确认成功批次并保留失败明细', (tester) async {
    final api = _FakeApi()..partial = true;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api, pair: true), width: 700));
    await _tap(tester, '生成预览');
    expect(find.text('张力未通过校验'), findsOneWidget);
    await _tap(tester, '确认应用');
    expect(api.posts.last.$1, '/api/assistant/batches/apply');
    expect(api.posts.last.$2, {'batchId': _batch});
    expect(find.text('breathiness · 已应用'), findsOneWidget);
    expect(find.text('tension · 待预览'), findsOneWidget);
    expect(find.text('张力未通过校验'), findsOneWidget);
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
  });

  testWidgets('应用连接中断立即锁定结果未知的方案且不重复提交', (tester) async {
    final api = _FakeApi()..failApply = true;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    await _tap(tester, '生成预览');
    await _tap(tester, '确认应用');
    expect(find.text('breathiness · 结果未知'), findsOneWidget);
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
    expect(
      tester.widget<OutlinedButton>(find.byType(OutlinedButton)).onPressed,
      isNull,
    );
    expect(api.posts.where((call) => call.$1.endsWith('/apply')).length, 1);
  });

  testWidgets('检查器仅显式读取选区并在窄侧栏校验非法增量', (tester) async {
    final api = _FakeApi();
    addTearDown(api.dispose);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(width: 280, child: Inspector(api: api)),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(api.gets, ['/api/status']);
    await _tap(tester, '读取当前选区');
    final delta = find.byType(TextField).first;
    await tester.enterText(delta, '5');
    await _tap(tester, '生成宿主预览');
    expect(api.posts, isEmpty);
    expect(find.text('请输入当前范围内的非零增量。'), findsOneWidget);
    expect(tester.takeException(), isNull);
    // 销毁检查器时取消周期状态轮询，测试不留下计时器。
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
