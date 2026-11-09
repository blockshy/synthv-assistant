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
  'label': {'breathiness': '气声', 'tension': '张力'}[parameter] ?? parameter,
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
  if (status == 'applied') 'previewBatchId': _batch,
  if (status == 'applied') 'result': {'verified': true},
};

class _FakeApi extends WorkbenchApi {
  _FakeApi() : super(port: 0, token: 'test-only');
  final List<String> gets = [];
  final List<(String, JsonMap)> posts = [];
  bool partial = false,
      failApply = false,
      writeEnabled = true,
      failRestore = false,
      uncertainRestore = false;

  @override
  Future<dynamic> getJson(String path) async {
    gets.add(path);
    if (path == '/api/status') {
      return {
        'bridge': {'connected': true},
        'writeEnabled': writeEnabled,
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
    if (path == '/api/write-mode') {
      writeEnabled = body['enabled'] == true;
      return {'writeEnabled': writeEnabled};
    }
    if (path == '/api/assistant/batches/restore') {
      if (uncertainRestore) {
        throw const ApiException('撤销连接中断，结果尚未确认。', uncertain: true);
      }
      if (failRestore) {
        return {
          'batchId': _batch,
          'verified': false,
          'rejected': true,
          'message': '已有其他修改，未撤销此方案。',
          'actions': [_action('breathiness', status: 'applied')],
        };
      }
      return {
        'batchId': _batch,
        'verified': true,
        'actions': [_action('breathiness')],
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

  testWidgets('主动作从预览切换到应用，其他预览使确认资格失效', (tester) async {
    final api = _FakeApi();
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    expect(find.text('预览方案'), findsOneWidget);
    expect(find.text('应用方案'), findsNothing);
    await _tap(tester, '预览方案');
    expect(find.text('应用方案'), findsOneWidget);
    api.invalidatePreviews();
    await tester.pump();
    expect(find.text('应用方案'), findsNothing);
    expect(find.text('预览方案'), findsOneWidget);
    expect(api.posts.where((call) => call.$1.endsWith('/apply')), isEmpty);
  });

  testWidgets('部分失败先展示原因，只有显式重新预览通过项后才可应用', (tester) async {
    final api = _FakeApi()..partial = true;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api, pair: true), width: 700));
    await _tap(tester, '预览方案');
    expect(find.textContaining('张力未通过校验'), findsOneWidget);
    expect(find.text('应用方案'), findsNothing);
    expect(api.posts.where((call) => call.$1.endsWith('/apply')), isEmpty);
    // 用户明确选择子方案；第二次完整预览必须只请求气声，不能沿用部分批次票据。
    api.partial = false;
    await _tap(tester, '仅预览通过的 1 项');
    expect(api.posts.last.$2, {
      'actionIds': ['breathiness'],
    });
    expect(find.text('本次已排除：张力'), findsOneWidget);
    await _tap(tester, '应用方案');
    expect(find.byType(AlertDialog), findsOneWidget);
    expect(api.posts.where((call) => call.$1.endsWith('/apply')), isEmpty);
    await _tap(tester, '确认应用');
    expect(api.posts.last.$1, '/api/assistant/batches/apply');
    expect(api.posts.last.$2, {'batchId': _batch});
    expect(find.text('撤销本次应用'), findsOneWidget);
    expect(find.text('本次已排除：张力'), findsOneWidget);
  });

  testWidgets('只读时在同一确认框明确授权，取消不切换权限或提交应用', (tester) async {
    final api = _FakeApi()..writeEnabled = false;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    await _tap(tester, '预览方案');
    await _tap(tester, '应用方案');
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, '确认应用'))
          .onPressed,
      isNull,
    );
    await _tap(tester, '取消');
    expect(
      api.posts.where(
        (call) => call.$1 == '/api/write-mode' || call.$1.endsWith('/apply'),
      ),
      isEmpty,
    );
    await _tap(tester, '应用方案');
    await _tap(tester, '允许修改选区');
    await _tap(tester, '确认应用');
    expect(api.posts.map((call) => call.$1).toList(), [
      '/api/assistant/batches/preview',
      '/api/write-mode',
      '/api/assistant/batches/apply',
    ]);
  });

  testWidgets('确认对话框打开期间预览失效，不开启权限或应用', (tester) async {
    final api = _FakeApi()..writeEnabled = false;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    await _tap(tester, '预览方案');
    await _tap(tester, '应用方案');
    await _tap(tester, '允许修改选区');
    api.invalidatePreviews();
    await _tap(tester, '确认应用');
    expect(
      api.posts.where(
        (call) => call.$1 == '/api/write-mode' || call.$1.endsWith('/apply'),
      ),
      isEmpty,
    );
    expect(find.textContaining('确认期间预览已变化'), findsOneWidget);
  });

  testWidgets('应用连接中断立即锁定结果未知的方案且不重复提交', (tester) async {
    final api = _FakeApi()..failApply = true;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    await _tap(tester, '预览方案');
    await _tap(tester, '应用方案');
    await _tap(tester, '确认应用');
    expect(find.text('需要核对结果'), findsOneWidget);
    expect(find.text('应用方案'), findsNothing);
    expect(find.text('预览方案'), findsNothing);
    expect(find.widgetWithText(OutlinedButton, '刷新方案状态'), findsOneWidget);
    expect(api.posts.where((call) => call.$1.endsWith('/apply')).length, 1);
  });

  testWidgets('撤销只提交本方案批次，成功后可重新预览', (tester) async {
    final api = _FakeApi();
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    await _tap(tester, '预览方案');
    await _tap(tester, '应用方案');
    await _tap(tester, '确认应用');
    await _tap(tester, '撤销本次应用');
    expect(api.posts.where((call) => call.$1.endsWith('/restore')), isEmpty);
    await _tap(tester, '确认撤销');
    expect(api.posts.last.$1, '/api/assistant/batches/restore');
    expect(api.posts.last.$2, {'batchId': _batch});
    expect(find.text('预览方案'), findsOneWidget);
    expect(find.text('已撤销。可重新预览这份方案。'), findsOneWidget);
    expect(api.posts.where((call) => call.$1 == '/api/restore'), isEmpty);
  });

  testWidgets('撤销被明确拒绝时保留已应用状态，不虚构成功', (tester) async {
    final api = _FakeApi()..failRestore = true;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    await _tap(tester, '预览方案');
    await _tap(tester, '应用方案');
    await _tap(tester, '确认应用');
    await _tap(tester, '撤销本次应用');
    await _tap(tester, '确认撤销');
    expect(find.text('撤销本次应用'), findsOneWidget);
    expect(find.text('已有其他修改，未撤销此方案。'), findsOneWidget);
    expect(find.text('已撤销。可重新预览这份方案。'), findsNothing);
  });

  testWidgets('撤销传输中断只允许刷新，不会重复提交恢复请求', (tester) async {
    final api = _FakeApi()..uncertainRestore = true;
    addTearDown(api.dispose);
    await tester.pumpWidget(_host(_proposal(api), width: 700));
    await _tap(tester, '预览方案');
    await _tap(tester, '应用方案');
    await _tap(tester, '确认应用');
    await _tap(tester, '撤销本次应用');
    await _tap(tester, '确认撤销');
    expect(find.text('需要核对结果'), findsOneWidget);
    expect(find.text('撤销本次应用'), findsNothing);
    expect(find.text('预览方案'), findsNothing);
    expect(api.posts.where((call) => call.$1.endsWith('/restore')).length, 1);
  });

  testWidgets('音高修改语义在预览前可见，原生替换确认再次说明范围', (tester) async {
    final api = _FakeApi();
    addTearDown(api.dispose);
    await tester.pumpWidget(
      _host(
        ProposalCard(
          api: api,
          conversationId: 'test-conversation',
          onChanged: () {},
          message: {
            'id': 'pitch-message',
            'actions': [_action('pitchCurve')],
          },
        ),
        width: 700,
      ),
    );
    expect(find.text('原生音高（替换）'), findsOneWidget);
    await _tap(tester, '预览方案');
    await _tap(tester, '应用方案');
    expect(find.text('原生音高将替换该范围的现有曲线，包括原有演唱细节。'), findsOneWidget);
    await _tap(tester, '取消');
    await tester.pumpWidget(
      _host(
        ProposalCard(
          api: api,
          conversationId: 'test-conversation',
          onChanged: () {},
          message: {
            'id': 'offset-message',
            'actions': [_action('pitchDelta')],
          },
        ),
        width: 700,
      ),
    );
    expect(find.text('音高偏差（叠加）'), findsOneWidget);
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
    // 使用参数输入的语义键，不依赖检查器增加工程信息后输入框的排列位置。
    final delta = find.byKey(const ValueKey('inspector-delta'));
    await tester.ensureVisible(delta);
    await tester.enterText(delta, '5');
    await _tap(tester, '生成预览');
    expect(api.posts, isEmpty);
    expect(find.text('请输入当前范围内的非零增量。'), findsOneWidget);
    expect(tester.takeException(), isNull);
    // 销毁检查器时取消周期状态轮询，测试不留下计时器。
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
