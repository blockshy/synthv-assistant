import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:screen_retriever/screen_retriever.dart';
import 'package:window_manager/window_manager.dart';

import 'core/backend.dart';
import 'theme.dart';
import 'workbench.dart';

/// 原生桌面入口；先显示启动状态，再启动受控后台，避免空白窗口等待。
Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await windowManager.ensureInitialized();
  await windowManager.setPreventClose(true);
  // 按系统逻辑工作区限制初始尺寸，避免高 DPI 或较矮屏幕把标题栏放到屏幕外。
  // 桌面 API 不可用时采用能容纳双行输入工具栏的保守窗口尺寸。
  var initialSize = const Size(1100, 700);
  try {
    final display = await screenRetriever.getPrimaryDisplay();
    final available = display.visibleSize ?? display.size;
    initialSize = Size(
      math.min(1440, available.width),
      math.min(900, available.height),
    );
  } catch (_) {
    // 显示器暂不可读不阻止启动，用户仍可通过标准标题栏调整窗口。
  }
  final options = WindowOptions(
    size: initialSize,
    minimumSize: Size(
      math.min(900, initialSize.width),
      math.min(620, initialSize.height),
    ),
    center: true,
    title: 'SynthV Assistant',
    titleBarStyle: TitleBarStyle.hidden,
    backgroundColor: const Color(0xff202020),
  );
  await windowManager.waitUntilReadyToShow(options, () async {
    // 多显示器的 DPI 与可用区可能不同，首次按当前屏幕最大化保证完整标题栏可见。
    // 还原时仍采用上方有界尺寸，用户可以继续调整窗口与两侧面板。
    await windowManager.maximize();
    await windowManager.show();
    await windowManager.focus();
  });
  runApp(const DesktopApp());
}

class DesktopApp extends StatefulWidget {
  const DesktopApp({super.key});
  @override
  State<DesktopApp> createState() => _DesktopAppState();
}

class _DesktopAppState extends State<DesktopApp> with WindowListener {
  DesktopBackend? backend;
  ThemeMode mode = ThemeMode.system;
  String? error;
  bool closing = false;
  late final Future<void> _startup;
  @override
  void initState() {
    super.initState();
    windowManager.addListener(this);
    _startup = _start();
  }

  Future<void> _start() async {
    try {
      final preferences = await SharedPreferences.getInstance();
      final saved = preferences.getString('themeMode');
      if (mounted) {
        setState(
          () => mode =
              ThemeMode.values.where((e) => e.name == saved).firstOrNull ??
              ThemeMode.system,
        );
      }
      final core = await DesktopBackend.start();
      if (mounted) {
        setState(() => backend = core);
      } else {
        // 启动结果不能成为无人管理的子进程；窗口已经销毁时立即按协议退出。
        await core.close();
      }
    } catch (e) {
      if (mounted) setState(() => error = e.toString());
    }
  }

  Future<void> _theme(ThemeMode value) async {
    setState(() => mode = value);
    final preferences = await SharedPreferences.getInstance();
    await preferences.setString('themeMode', value.name);
  }

  @override
  void onWindowClose() async {
    if (closing) return;
    closing = true;
    try {
      // 初始化阶段关闭同样等待启动句柄，避免窗口消失后才启动后台。
      await _startup;
      await backend?.close();
      await windowManager.destroy();
    } catch (e) {
      // 后台拒绝忙碌退出时保留原窗口，录音、模型及宿主写入不会被强杀。
      if (mounted) setState(() => error = '暂时无法退出：$e');
    } finally {
      closing = false;
    }
  }

  @override
  void dispose() {
    windowManager.removeListener(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'SynthV Assistant',
    debugShowCheckedModeBanner: false,
    theme: WorkbenchTheme.build(Brightness.light),
    darkTheme: WorkbenchTheme.build(Brightness.dark),
    themeMode: mode,
    home: backend == null
        ? Scaffold(
            body: Column(
              children: [
                const DesktopTitleBar(),
                Expanded(
                  child: Center(
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 550),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Icon(Icons.graphic_eq_rounded, size: 40),
                          const SizedBox(height: 24),
                          Text(
                            error == null ? '正在连接调教核心' : '工作台暂时无法启动',
                            style: const TextStyle(
                              fontSize: 22,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const SizedBox(height: 16),
                          if (error == null)
                            const SizedBox(
                              width: 22,
                              height: 22,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            ),
                          if (error != null)
                            Padding(
                              padding: const EdgeInsets.all(24),
                              child: SelectableText(error!),
                            ),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          )
        : Workbench(
            api: backend!.api,
            mode: mode,
            onThemeChanged: _theme,
            startupError: error,
          ),
  );
}

/// 自绘标题栏保留 Windows 拖动、最大化、最小化和关闭的常规行为。
class DesktopTitleBar extends StatelessWidget {
  const DesktopTitleBar({super.key, this.leading, this.trailing});
  final Widget? leading;
  final Widget? trailing;
  @override
  Widget build(BuildContext context) => Container(
    height: 46,
    decoration: BoxDecoration(
      border: Border(bottom: BorderSide(color: Theme.of(context).dividerColor)),
    ),
    child: Row(
      children: [
        ?leading,
        Expanded(
          child: DragToMoveArea(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: Row(
                children: [
                  const Icon(Icons.graphic_eq_rounded, size: 19),
                  const SizedBox(width: 10),
                  Text(
                    'SynthV Assistant',
                    style: Theme.of(context).textTheme.labelLarge,
                  ),
                  const SizedBox(width: 12),
                  Text(
                    '调教工作台',
                    style: TextStyle(
                      fontSize: 11,
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
        ?trailing,
        IconButton(
          tooltip: '最小化',
          onPressed: windowManager.minimize,
          icon: const Icon(Icons.remove_rounded),
        ),
        IconButton(
          tooltip: '最大化 / 还原',
          onPressed: () async {
            if (await windowManager.isMaximized()) {
              await windowManager.unmaximize();
            } else {
              await windowManager.maximize();
            }
          },
          icon: const Icon(Icons.crop_square_rounded, size: 16),
        ),
        IconButton(
          tooltip: '关闭工作台',
          onPressed: windowManager.close,
          icon: const Icon(Icons.close_rounded),
        ),
        const SizedBox(width: 6),
      ],
    ),
  );
}
