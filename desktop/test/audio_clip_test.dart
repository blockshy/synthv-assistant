import 'dart:async';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/widgets/audio_clip.dart';

/// 所有音频均为测试字节；播放器由内存对象替代，不访问声卡、本地文件或网络。
class _AudioApi extends WorkbenchApi {
  _AudioApi() : super(port: 1, token: 'isolated-test');
  Future<Uint8List>? pendingRead;
  int reads = 0;

  @override
  Future<Uint8List> readAudio(String path) async {
    reads++;
    return await (pendingRead ?? Future.value(Uint8List.fromList([1, 2, 3])));
  }
}

/// 可控制每条命令何时结束，专门验证超时以后旧命令的生命周期。
class _FakePlayer implements AudioClipPlayer {
  final positionController = StreamController<Duration>.broadcast();
  final stateController = StreamController<PlayerState>.broadcast();
  final eventController = StreamController<AudioEvent>.broadcast();
  Future<void>? initializing;
  Future<void>? preparing;
  Future<void>? starting;
  Future<void>? disposing;
  int initializationCount = 0;
  int sourceCount = 0;
  int resumeCount = 0;
  int pauseCount = 0;
  int disposeCount = 0;
  final seeks = <Duration>[];
  BytesSource? source;

  @override
  Stream<Duration> get positions => positionController.stream;
  @override
  Stream<PlayerState> get states => stateController.stream;
  @override
  Stream<AudioEvent> get events => eventController.stream;

  @override
  Future<void> initialize() async {
    initializationCount++;
    await initializing;
  }

  @override
  Future<void> setSource(BytesSource value) async {
    sourceCount++;
    source = value;
    await preparing;
    eventController.add(
      const AudioEvent(
        eventType: AudioEventType.duration,
        duration: Duration(seconds: 6),
      ),
    );
  }

  @override
  Future<void> resume() async {
    resumeCount++;
    await starting;
    stateController.add(PlayerState.playing);
  }

  @override
  Future<void> pause() async {
    pauseCount++;
    stateController.add(PlayerState.paused);
  }

  @override
  Future<void> seek(Duration position) async {
    seeks.add(position);
  }

  @override
  Future<void> dispose() async {
    disposeCount++;
    await disposing;
  }

  Future<void> closeControllers() async {
    await positionController.close();
    await stateController.close();
    await eventController.close();
  }
}

const _clip = {
  'id': '00000000000000000000000000000001',
  'url': '/uploads/00000000000000000000000000000001.wav',
  'durationSeconds': 6,
};

Future<void> _show(
  WidgetTester tester,
  _AudioApi api,
  AudioClipPlayer Function() factory, {
  JsonMap clip = _clip,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 400,
          child: AudioClip(api: api, clip: clip, playerFactory: factory),
        ),
      ),
    ),
  );
  await tester.pump();
}

Future<void> _click(WidgetTester tester) async {
  await tester.tap(find.byType(IconButton));
  await tester.pump();
}

/// StreamSubscription.cancel 的共享已完成 Future 可能来自测试时钟之外；
/// 让取消通知经过一次真实事件循环，再刷新假时钟中的后续清理，不等待声卡。
Future<void> _drainCleanup(WidgetTester tester) async {
  await tester.runAsync(() => Future<void>.delayed(Duration.zero));
  await tester.pump();
}

void main() {
  late _AudioApi api;
  final players = <_FakePlayer>[];
  _FakePlayer makePlayer() {
    final player = _FakePlayer();
    players.add(player);
    return player;
  }

  setUp(() {
    api = _AudioApi();
    players.clear();
  });
  tearDown(() async {
    api.dispose();
    for (final player in players) {
      await player.closeControllers();
    }
  });

  testWidgets('窗口收尾有界等待原生释放，超时后重复等待不重复dispose', (tester) async {
    final player = makePlayer();
    final disposing = Completer<void>();
    player.disposing = disposing.future;
    await _show(tester, api, () => player);
    await _click(tester);
    await tester.pump();
    await tester.pumpWidget(const SizedBox.shrink());
    await _drainCleanup(tester);
    final first = stopAndDisposeAllAudio(
      timeout: const Duration(milliseconds: 50),
    );
    await tester.pump(const Duration(milliseconds: 60));
    expect(await first, isFalse);
    expect(player.disposeCount, 1);
    final second = stopAndDisposeAllAudio(
      timeout: const Duration(milliseconds: 50),
    );
    disposing.complete();
    await tester.pump();
    expect(await second, isTrue);
    expect(player.disposeCount, 1);
  });

  testWidgets('各加载阶段可见，播放完成后保留音频源供再次试听', (tester) async {
    final player = makePlayer();
    final initialize = Completer<void>();
    final read = Completer<Uint8List>();
    final prepare = Completer<void>();
    final start = Completer<void>();
    player.initializing = initialize.future;
    player.preparing = prepare.future;
    player.starting = start.future;
    api.pendingRead = read.future;
    var created = 0;
    await _show(tester, api, () {
      created++;
      return player;
    });
    expect(created, 0);
    expect(api.reads, 0);

    await _click(tester);
    expect(find.text('初始化播放器'), findsOneWidget);
    initialize.complete();
    await tester.pump();
    await _drainCleanup(tester);
    expect(find.text('读取音频'), findsOneWidget);
    read.complete(Uint8List.fromList([1, 2, 3]));
    await tester.pump();
    expect(find.text('准备播放'), findsOneWidget);
    prepare.complete();
    await tester.pump();
    expect(find.text('开始播放'), findsOneWidget);
    start.complete();
    await tester.pump();
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(find.byTooltip('暂停试听'), findsOneWidget);
    expect(player.source!.mimeType, 'audio/wav');
    expect(player.source!.bytes, [1, 2, 3]);

    player.stateController.add(PlayerState.completed);
    await tester.pump();
    await _click(tester);
    expect(player.seeks, [Duration.zero]);
    expect(player.resumeCount, 2);
    expect(player.sourceCount, 1);
    expect(api.reads, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('初始化超时恢复重试按钮，旧命令结束前不新建或释放播放器', (tester) async {
    final old = makePlayer();
    final replacement = makePlayer();
    final initialize = Completer<void>();
    old.initializing = initialize.future;
    var created = 0;
    await _show(tester, api, () => created++ == 0 ? old : replacement);
    await _click(tester);
    await tester.pump(const Duration(seconds: 11));
    expect(find.text('初始化播放器超时，请重试。'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(old.disposeCount, 0);

    await _click(tester);
    expect(find.text('清理上次播放'), findsOneWidget);
    await tester.pump(const Duration(seconds: 4));
    expect(find.text('上次播放仍在清理，请稍后重试。'), findsOneWidget);
    expect(created, 1);
    initialize.complete();
    await tester.pump();
    await _drainCleanup(tester);
    expect(old.disposeCount, 1);
    expect(old.sourceCount, 0);
    expect(old.resumeCount, 0);
    await _click(tester);
    expect(created, 2);
    expect(replacement.resumeCount, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('读取音频超时后晚到数据不会传入已失效播放器', (tester) async {
    final old = makePlayer();
    final replacement = makePlayer();
    final read = Completer<Uint8List>();
    api.pendingRead = read.future;
    var created = 0;
    await _show(tester, api, () => created++ == 0 ? old : replacement);
    await _click(tester);
    expect(find.text('读取音频'), findsOneWidget);
    await tester.pump(const Duration(seconds: 31));
    expect(find.text('读取音频超时，请重试。'), findsOneWidget);
    await _drainCleanup(tester);
    expect(old.disposeCount, 1);
    api.pendingRead = null;
    await _click(tester);
    read.complete(Uint8List.fromList([9, 9, 9]));
    await tester.pump();
    expect(old.sourceCount, 0);
    expect(old.resumeCount, 0);
    expect(replacement.source!.bytes, [1, 2, 3]);
    expect(replacement.resumeCount, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('准备播放超时不抢占旧解码器，完成清理后才能重试', (tester) async {
    final old = makePlayer();
    final replacement = makePlayer();
    final prepare = Completer<void>();
    old.preparing = prepare.future;
    var created = 0;
    await _show(tester, api, () => created++ == 0 ? old : replacement);
    await _click(tester);
    await tester.pump(const Duration(seconds: 21));
    expect(find.text('准备播放超时，请重试。'), findsOneWidget);
    expect(old.disposeCount, 0);
    await _click(tester);
    expect(created, 1);
    prepare.complete();
    await tester.pump();
    await _drainCleanup(tester);
    expect(old.disposeCount, 1);
    expect(old.resumeCount, 0);
    expect(replacement.resumeCount, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('开始播放超时同样恢复操作，迟到完成不更新新实例的界面', (tester) async {
    final old = makePlayer();
    final replacement = makePlayer();
    final start = Completer<void>();
    old.starting = start.future;
    var created = 0;
    await _show(tester, api, () => created++ == 0 ? old : replacement);
    await _click(tester);
    await tester.pump(const Duration(seconds: 9));
    expect(find.text('开始播放超时，请重试。'), findsOneWidget);
    expect(old.disposeCount, 0);
    start.complete();
    await tester.pump();
    await _drainCleanup(tester);
    expect(old.disposeCount, 1);
    expect(find.byTooltip('播放本地音频'), findsOneWidget);
    await _click(tester);
    expect(replacement.resumeCount, 1);
    expect(find.byTooltip('暂停试听'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('加载和播放中的原生异步错误均显示安全提示并重新创建实例', (tester) async {
    final old = makePlayer();
    final replacement = makePlayer();
    final prepare = Completer<void>();
    old.preparing = prepare.future;
    var created = 0;
    await _show(tester, api, () => created++ == 0 ? old : replacement);
    await _click(tester);
    old.eventController.addError(StateError('不允许回显的私有路径'));
    await tester.pump();
    expect(find.text('音频播放失败，请重试。'), findsOneWidget);
    expect(find.textContaining('私有路径'), findsNothing);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    prepare.completeError(StateError('原生错误'));
    await tester.pump();
    await _drainCleanup(tester);
    expect(old.disposeCount, 1);
    await _click(tester);
    replacement.eventController.addError(StateError('播放中错误'));
    await tester.pump();
    await _drainCleanup(tester);
    expect(find.text('音频播放失败，请重试。'), findsOneWidget);
    expect(replacement.disposeCount, 1);
    expect(tester.takeException(), isNull);
  });

  testWidgets('列表复用时旧下载完成也不会播放上一段音频', (tester) async {
    final old = makePlayer();
    final replacement = makePlayer();
    final read = Completer<Uint8List>();
    api.pendingRead = read.future;
    var created = 0;
    AudioClipPlayer factory() => created++ == 0 ? old : replacement;
    await _show(tester, api, factory);
    await _click(tester);
    await _show(tester, api, factory, clip: {..._clip, 'id': 'replacement'});
    await _drainCleanup(tester);
    api.pendingRead = null;
    await _click(tester);
    read.complete(Uint8List.fromList([9]));
    await tester.pump();
    expect(old.resumeCount, 0);
    expect(old.sourceCount, 0);
    expect(old.disposeCount, 1);
    expect(replacement.resumeCount, 1);
    expect(tester.takeException(), isNull);
  });
}
