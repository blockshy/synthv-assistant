import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:screen_retriever/screen_retriever.dart';
import 'package:window_manager/window_manager.dart';

import 'core/backend.dart';
import 'theme.dart';
import 'workbench.dart';
import 'widgets/audio_clip.dart' show stopAndDisposeAllAudio;

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
    await windowManager.show();
    // Windows 插件 Show() 使用异步 SW_SHOW，Maximize() 投递 SC_MAXIMIZE；
    // 先显示再最大化，避免两个原生消息的顺序影响首次窗口的最终状态。
    await windowManager.maximize();
    await windowManager.focus();
  });
  runApp(const DesktopApp());
}

class DesktopApp extends StatefulWidget {
  const DesktopApp({
    super.key,
    this.backendFactory,
    this.closeWindow,
    this.preventClose,
  });
  @visibleForTesting
  final Future<DesktopBackend> Function()? backendFactory;
  @visibleForTesting
  final Future<void> Function()? closeWindow;
  @visibleForTesting
  final Future<void> Function(bool)? preventClose;
  @override
  State<DesktopApp> createState() => _DesktopAppState();
}

class _DesktopAppState extends State<DesktopApp> with WindowListener {
  DesktopBackend? backend;
  ThemeMode mode = ThemeMode.system;
  String? error;
  bool closing = false;
  bool _closeAttempt = false, _finishingWindow = false, _showWorkbench = true;
  bool _backendFailed = false, _starting = true;
  String? _closeError;
  String _closeStage = '正在确认后台是否可以退出…';
  int _closeSeconds = 0;
  Timer? _closeTimer;
  late Future<void> _startup;
  @override
  void initState() {
    super.initState();
    windowManager.addListener(this);
    _startup = _start();
  }

  Future<void> _start() async {
    _starting = true;
    try {
      final preferences = await SharedPreferences.getInstance().timeout(
        const Duration(seconds: 3),
      );
      final saved = preferences.getString('themeMode');
      if (mounted) {
        setState(
          () => mode =
              ThemeMode.values.where((e) => e.name == saved).firstOrNull ??
              ThemeMode.system,
        );
      }
      final core =
          await (widget.backendFactory?.call() ?? DesktopBackend.start());
      if (mounted) {
        core.addListener(_backendChanged);
        setState(() {
          backend = core;
          _backendFailed = false;
          error = null;
        });
        _backendChanged();
      } else {
        // 启动结果不能成为无人管理的子进程；窗口已经销毁时立即按协议退出。
        await core.close();
      }
    } catch (e) {
      if (mounted) setState(() => error = e.toString());
    } finally {
      if (mounted) setState(() => _starting = false);
    }
  }

  /// 自然退出也立即反馈，不等待下一次按钮操作或六十秒 HTTP 超时才发现后台消失。
  void _backendChanged() {
    if (!mounted || backend == null) return;
    final core = backend!;
    setState(() {
      if (closing) {
        _closeStage = core.closingMessage;
        // 退出等待超时以后可能收到明确的忙碌拒绝；恢复界面，不能继续锁住用户。
        if (!_closeAttempt && core.canResume) {
          closing = false;
          _closeError = null;
          error = '后台未接受退出，工作台已恢复；请完成当前任务后再关闭。';
        }
      } else if (core.hasExited && !_finishingWindow) {
        _backendFailed = true;
        error = '调教核心已停止（退出码 ${core.exitCode ?? '未知'}），可以重新连接。';
      }
    });
  }

  Future<void> _retryStartup() async {
    if (_starting || _closeAttempt) return;
    final old = backend;
    if (old != null && !old.hasExited) return;
    old?.removeListener(_backendChanged);
    if (old != null) await old.close();
    if (!mounted) return;
    setState(() {
      backend = null;
      error = null;
      _backendFailed = false;
      _showWorkbench = true;
      closing = false;
      _starting = true;
    });
    _startup = _start();
  }

  Future<void> _theme(ThemeMode value) async {
    setState(() => mode = value);
    final preferences = await SharedPreferences.getInstance();
    await preferences.setString('themeMode', value.name);
  }

  @override
  void onWindowClose() async {
    if (_closeAttempt || _finishingWindow) return;
    setState(() {
      closing = true;
      _closeAttempt = true;
      _closeError = null;
      _closeSeconds = 0;
      _closeStage = _starting ? '正在等待启动流程安全收尾…' : '正在确认后台是否可以退出…';
    });
    backend?.api.setClosing(true);
    _closeTimer?.cancel();
    _closeTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() => _closeSeconds++);
    });
    try {
      // 初始化阶段关闭同样等待启动句柄，避免窗口消失后才启动后台。
      await _startup.timeout(
        const Duration(seconds: 35),
        onTimeout: () => throw TimeoutException('启动仍在收尾，请稍后继续等待退出。'),
      );
      await backend?.close();
      // 后台确认停止后先卸载业务页面，使定时器、播放器订阅和编辑器 Focus 正常释放。
      // window_manager.destroy() 直接 PostQuitMessage，曾触发本机 Flutter 引擎退出崩溃；
      // 必须经解除拦截后的 WM_CLOSE，保留 Windows 正常窗口销毁次序。
      if (!mounted) return;
      setState(() {
        _showWorkbench = false;
        _closeStage = '正在释放音频与窗口资源…';
      });
      await WidgetsBinding.instance.endOfFrame;
      final audioReleased = await stopAndDisposeAllAudio();
      await backend?.recordDiagnostic(
        audioReleased ? 'audio-released' : 'audio-release-incomplete',
      );
      await (widget.preventClose?.call(false) ??
          windowManager.setPreventClose(false));
      _finishingWindow = true;
      await backend?.recordDiagnostic('window-close-normal');
      await (widget.closeWindow?.call() ?? windowManager.close());
    } catch (e) {
      // 后台拒绝忙碌退出时保留原窗口，录音、模型及宿主写入不会被强杀。
      _finishingWindow = false;
      final canResume = backend?.canResume == true;
      if (canResume) backend?.api.setClosing(false);
      if (mounted) {
        setState(() {
          _closeError = e is TimeoutException ? e.message : e.toString();
          error = '暂时无法退出：$_closeError';
          // 明确忙碌拒绝可恢复交互；结果未知继续展示等待/重试，不擅自重启后台。
          if (canResume) closing = false;
        });
      }
    } finally {
      _closeTimer?.cancel();
      if (mounted) setState(() => _closeAttempt = false);
    }
  }

  @override
  void dispose() {
    windowManager.removeListener(this);
    _closeTimer?.cancel();
    backend?.removeListener(_backendChanged);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'SynthV Assistant',
    debugShowCheckedModeBanner: false,
    theme: WorkbenchTheme.build(Brightness.light),
    darkTheme: WorkbenchTheme.build(Brightness.dark),
    themeMode: mode,
    home: Stack(
      children: [
        backend == null || !_showWorkbench
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
                                closing
                                    ? '正在关闭工作台'
                                    : error == null
                                    ? '正在连接调教核心'
                                    : '工作台暂时无法启动',
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
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                  ),
                                ),
                              if (error != null)
                                Padding(
                                  padding: const EdgeInsets.all(24),
                                  child: SelectableText(error!),
                                ),
                              if (error != null && !_starting && !closing)
                                FilledButton.icon(
                                  onPressed: _retryStartup,
                                  icon: const Icon(Icons.refresh, size: 16),
                                  label: const Text('重新连接'),
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
        if (closing || _backendFailed) ...[
          const Positioned.fill(
            child: ModalBarrier(dismissible: false, color: Color(0x99000000)),
          ),
          Positioned.fill(
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 460),
                child: Builder(
                  builder: (context) => Material(
                    color: Theme.of(context).colorScheme.surface,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(4),
                      side: BorderSide(
                        color: Theme.of(context).colorScheme.outlineVariant,
                      ),
                    ),
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Text(
                            closing ? '正在关闭工作台' : '后台连接已结束',
                            style: Theme.of(context).textTheme.titleMedium,
                          ),
                          const SizedBox(height: 14),
                          if (closing && _closeAttempt) ...[
                            const LinearProgressIndicator(minHeight: 2),
                            const SizedBox(height: 14),
                            Text('$_closeStage  $_closeSeconds 秒'),
                          ],
                          if (_closeError != null && closing)
                            SelectableText(_closeError!),
                          if (!closing && error != null) SelectableText(error!),
                          const SizedBox(height: 12),
                          Text(
                            '诊断仅记录本机生命周期状态，不包含密钥、工程或音频内容。',
                            style: Theme.of(context).textTheme.bodySmall,
                          ),
                          const SizedBox(height: 18),
                          Wrap(
                            spacing: 8,
                            runSpacing: 8,
                            children: [
                              if (closing &&
                                  !_closeAttempt &&
                                  !_finishingWindow)
                                FilledButton.icon(
                                  onPressed: onWindowClose,
                                  icon: const Icon(Icons.refresh, size: 16),
                                  label: const Text('继续等待退出'),
                                ),
                              if (!closing) ...[
                                FilledButton.icon(
                                  onPressed: _retryStartup,
                                  icon: const Icon(Icons.refresh, size: 16),
                                  label: const Text('重新连接'),
                                ),
                                OutlinedButton(
                                  onPressed: onWindowClose,
                                  child: const Text('关闭工作台'),
                                ),
                              ],
                            ],
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ],
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
    height: 38,
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
                    style: Theme.of(context).textTheme.labelLarge
                        ?.copyWith(fontSize: 12),
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
