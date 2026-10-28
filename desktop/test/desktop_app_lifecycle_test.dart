import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:window_manager/window_manager.dart';
import 'package:synthv_desktop/core/api.dart';
import 'package:synthv_desktop/core/backend.dart';
import 'package:synthv_desktop/main.dart';
import 'package:synthv_desktop/workbench.dart';

import 'workbench_model_test.dart' show ModelApi;

/// 窗口与后台均替换为内存对象；不调用 WindowManager 原生关闭、不访问宿主。
class _Input extends Fake implements IOSink {
  @override
  Future<void> close() async {}
}

class _Process extends Fake implements Process {
  final done = Completer<int>();
  @override
  Future<int> get exitCode => done.future;
  @override
  IOSink get stdin => _Input();
}

class _Api extends ModelApi {
  _Api(this.process);
  final _Process process;
  bool refuse = false;
  int shutdowns = 0;
  Completer<void>? held;
  @override
  Future<dynamic> postJson(String path, [JsonMap body = const {}]) async {
    if (path == '/api/desktop/shutdown') {
      shutdowns++;
      if (refuse) throw const ApiException('录音仍在进行，请完成后退出。');
      await held?.future;
      if (!process.done.isCompleted) process.done.complete(0);
      return {'closing': true};
    }
    return super.postJson(path, body);
  }
}

class _Fixture {
  _Fixture(this.directory, this.process, this.api, this.backend);
  final Directory directory;
  final _Process process;
  final _Api api;
  final DesktopBackend backend;
  Future<void> finish() async {
    if (!process.done.isCompleted) process.done.complete(0);
    await backend.close();
    await directory.delete(recursive: true);
  }
}

Future<_Fixture> _fixture() async {
  final directory = await Directory.systemTemp.createTemp(
    'synthv-window-life-',
  );
  final lock = await File('${directory.path}/instance.lock')
      .open(mode: FileMode.append);
  final process = _Process();
  final api = _Api(process);
  return _Fixture(
    directory,
    process,
    api,
    DesktopBackend.forTesting(
      process,
      api,
      lock,
      shutdownRequestTimeout: const Duration(milliseconds: 20),
    ),
  );
}

/// 文件句柄关闭使用真实事件循环，界面进度与帧仍由 WidgetTester 确定性推进。
Future<void> _settle(WidgetTester tester) async {
  for (var step = 0; step < 5; step++) {
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 5)),
    );
    await tester.pump(const Duration(milliseconds: 10));
  }
}

void main() {
  Future<void> show(
    WidgetTester tester,
    _Fixture fixture,
    List<String> events,
  ) async {
    SharedPreferences.setMockInitialValues({});
    await tester.binding.setSurfaceSize(const Size(1100, 720));
    await tester.pumpWidget(
      DesktopApp(
        backendFactory: () async => fixture.backend,
        preventClose: (value) async {
          events.add('prevent:$value');
        },
        closeWindow: () async {
          events.add('close');
        },
      ),
    );
    await _settle(tester);
  }

  Future<void> finish(WidgetTester tester, _Fixture fixture) async {
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.runAsync(fixture.finish);
    await tester.binding.setSurfaceSize(null);
  }

  testWidgets('后台确认退出后卸载页面并走正常close顺序', (tester) async {
    final fixture = (await tester.runAsync(_fixture))!;
    final events = <String>[];
    await show(tester, fixture, events);
    (tester.state(find.byType(DesktopApp)) as WindowListener).onWindowClose();
    await _settle(tester);
    expect(fixture.backend.isClosed, isTrue);
    expect(find.byType(Workbench), findsNothing);
    expect(events, ['prevent:false', 'close']);
    expect(tester.takeException(), isNull);
    await finish(tester, fixture);
  });

  testWidgets('忙碌拒绝退出后恢复工作台交互，不关闭窗口', (tester) async {
    final fixture = (await tester.runAsync(_fixture))!;
    fixture.api.refuse = true;
    final events = <String>[];
    await show(tester, fixture, events);
    (tester.state(find.byType(DesktopApp)) as WindowListener).onWindowClose();
    await _settle(tester);
    expect(find.byType(Workbench), findsOneWidget);
    expect(find.textContaining('录音仍在进行'), findsOneWidget);
    expect(fixture.api.closing, isFalse);
    expect(events, isEmpty);
    await finish(tester, fixture);
  });

  testWidgets('关闭请求未知时展示继续等待且不会重复提交', (tester) async {
    final fixture = (await tester.runAsync(_fixture))!;
    final held = Completer<void>();
    fixture.api.held = held;
    final events = <String>[];
    await show(tester, fixture, events);
    (tester.state(find.byType(DesktopApp)) as WindowListener).onWindowClose();
    await _settle(tester);
    expect(find.text('继续等待退出'), findsOneWidget);
    expect(events, isEmpty);
    expect(fixture.api.shutdowns, 1);
    held.complete();
    await _settle(tester);
    await tester.tap(find.text('继续等待退出'));
    await _settle(tester);
    expect(fixture.api.shutdowns, 1);
    expect(events, ['prevent:false', 'close']);
    await finish(tester, fixture);
  });

  testWidgets('后台自然停止后直接显示重新连接，不留下假工作台', (tester) async {
    final fixture = (await tester.runAsync(_fixture))!;
    final events = <String>[];
    await show(tester, fixture, events);
    fixture.process.done.complete(17);
    await _settle(tester);
    expect(find.text('后台连接已结束'), findsOneWidget);
    expect(find.text('重新连接'), findsOneWidget);
    expect(fixture.api.closing, isTrue);
    expect(events, isEmpty);
    await finish(tester, fixture);
  });
}
