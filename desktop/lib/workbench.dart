import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/gestures.dart' show DragStartBehavior;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/api.dart';
import 'core/workbench_model.dart';
import 'main.dart' show DesktopTitleBar;
import 'pages/inspector.dart';
import 'pages/connection_page.dart';
import 'pages/library_page.dart';
import 'pages/recording_page.dart';
import 'pages/settings_page.dart';
import 'pages/trash_page.dart';
import 'widgets/audio_clip.dart';
import 'widgets/proposal_card.dart';

/// 三栏工作台只组织页面与会话；所有宿主写入都沿用后端票据流程。
class Workbench extends StatefulWidget {
  const Workbench({
    super.key,
    required this.api,
    required this.mode,
    required this.onThemeChanged,
    this.startupError,
  });
  final WorkbenchApi api;
  final ThemeMode mode;
  final ValueChanged<ThemeMode> onThemeChanged;
  final String? startupError;
  @override
  State<Workbench> createState() => _WorkbenchState();
}

class _WorkbenchState extends State<Workbench> {
  late final WorkbenchModel model;
  final _draft = TextEditingController();
  final _modelInput = TextEditingController();
  final _scroll = ScrollController();
  final _composerFocus = FocusNode();
  final _scaffoldKey = GlobalKey<ScaffoldState>();
  int page = 0;
  double leftWidth = 230, rightWidth = 302;
  bool leftVisible = true, rightVisible = true;
  String search = '';
  Timer? _statusTimer, _capabilityTimer, _saveTimer;
  bool _loadingStatus = false;
  int _libraryGeneration = 0;
  int _bridgeGeneration = 0;

  /// 显式切换连接后立即销毁旧检查器草稿，不等待下一次心跳轮询才失效。
  Future<void> _connectionChanged() async {
    if (mounted) setState(() => _bridgeGeneration++);
    await model.refreshStatus();
  }

  @override
  void initState() {
    super.initState();
    model = WorkbenchModel(widget.api)..addListener(_changed);
    widget.api.addListener(_changed);
    unawaited(model.initialize());
    unawaited(_restoreLayout());
    _statusTimer = Timer.periodic(const Duration(seconds: 8), (_) async {
      if (_loadingStatus || widget.api.busy) return;
      _loadingStatus = true;
      try {
        await model.refreshStatus();
      } catch (_) {
        /* 请求错误由操作入口说明，轮询不刷屏。 */
      } finally {
        _loadingStatus = false;
      }
    });
  }

  Future<void> _restoreLayout() async {
    final prefs = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      leftWidth = (prefs.getDouble('leftWidth') ?? 230).clamp(190, 340);
      rightWidth = (prefs.getDouble('rightWidth') ?? 302).clamp(260, 430);
    });
  }

  void _saveLayout() {
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 350), () async {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setDouble('leftWidth', leftWidth);
      await prefs.setDouble('rightWidth', rightWidth);
    });
  }

  void _changed() {
    if (!mounted) return;
    final follow = !_scroll.hasClients || _scroll.position.extentAfter < 110;
    if (_draft.text != model.draft) {
      _draft.value = TextEditingValue(
        text: model.draft,
        selection: TextSelection.collapsed(offset: model.draft.length),
      );
    }
    if (_modelInput.text != model.modelId) {
      _modelInput.value = TextEditingValue(
        text: model.modelId,
        selection: TextSelection.collapsed(offset: model.modelId.length),
      );
    }
    setState(() {});
    if (follow) _toBottom();
  }

  void _toBottom() => WidgetsBinding.instance.addPostFrameCallback((_) {
    if (mounted && _scroll.hasClients) {
      _scroll.jumpTo(_scroll.position.maxScrollExtent);
    }
  });

  void _notice(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), behavior: SnackBarBehavior.floating),
    );
  }

  void _navigate(int target) {
    if (target == page) return;
    setState(() {
      page = target;
      if (target == 1) _libraryGeneration++;
    });
  }

  void _openConnection() {
    _scaffoldKey.currentState?.closeEndDrawer();
    _navigate(5);
  }

  void _attach(List<JsonMap> clips) {
    model.attach(clips);
    setState(() => page = 0);
    _composerFocus.requestFocus();
  }

  Future<void> _manage(String action) async {
    if (model.conversation == null ||
        widget.api.busy ||
        model.navigationBusy ||
        model.submissionUnknown ||
        model.pendingJobId != null) {
      return;
    }
    final id = model.conversationId;
    try {
      if (action == 'star') {
        await widget.api.postJson('/api/conversations/$id/metadata', {
          'starred': model.conversation!['starred'] != true,
        });
      } else if (action == 'note') {
        final name = TextEditingController(
          text: model.conversation!['title']?.toString() ?? '',
        );
        final note = TextEditingController(
          text: model.conversation!['note']?.toString() ?? '',
        );
        final saved = await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('会话名称与备注'),
            content: SizedBox(
              width: 420,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: name,
                    maxLength: 80,
                    decoration: const InputDecoration(labelText: '名称'),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: note,
                    minLines: 3,
                    maxLines: 5,
                    maxLength: 1000,
                    decoration: const InputDecoration(labelText: '备注'),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('取消'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('保存'),
              ),
            ],
          ),
        );
        if (saved == true) {
          await widget.api.postJson('/api/conversations/$id/metadata', {
            'title': name.text,
            'note': note.text,
          });
        }
        // 弹窗退出动画仍可能使用 controller，延后一帧再回收。
        Future<void>.delayed(const Duration(milliseconds: 350), () {
          name.dispose();
          note.dispose();
        });
      } else if (action == 'delete') {
        final choice = await showDialog<String>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('删除这段会话？'),
            content: const Text(
              '移入回收站后可以恢复。永久删除会清除会话记录和备注，不删除音频素材或 SynthV 工程。',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('取消'),
              ),
              OutlinedButton(
                onPressed: () => Navigator.pop(context, 'purge'),
                child: const Text('永久删除'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, 'delete'),
                child: const Text('移入回收站'),
              ),
            ],
          ),
        );
        if (choice == null) return;
        await widget.api.postJson(
          '/api/conversations/$id/$choice',
          choice == 'purge' ? {'confirm': true} : {},
        );
        widget.api.invalidatePreviews();
        model.conversation = null;
        model.draft = '';
        model.attachments = [];
        model.pendingMessage = null;
        await model.refreshConversations();
        return;
      }
      await model.reloadConversation();
    } catch (e) {
      _notice(e.toString());
    }
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _capabilityTimer?.cancel();
    _saveTimer?.cancel();
    widget.api.removeListener(_changed);
    model.removeListener(_changed);
    model.dispose();
    _draft.dispose();
    _modelInput.dispose();
    _scroll.dispose();
    _composerFocus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, bounds) {
      final wide = bounds.maxWidth >= 1160;
      final left = leftVisible && bounds.maxWidth >= 940;
      final right = rightVisible && wide;
      final rightContext = page == 0 || page == 2;
      final actualLeft = math.min(leftWidth, bounds.maxWidth * .23);
      final actualRight = math.min(rightWidth, bounds.maxWidth * .28);
      return Scaffold(
        key: _scaffoldKey,
        drawer: Drawer(width: 260, child: _sidebar()),
        endDrawer: SizedBox(
          width: 340,
          child: Drawer(
            child: Inspector(
              key: ValueKey(_bridgeGeneration),
              api: widget.api,
              onConnection: _openConnection,
            ),
          ),
        ),
        body: Column(
          children: [
            DesktopTitleBar(
              leading: Padding(
                padding: const EdgeInsets.only(left: 8),
                child: IconButton(
                  tooltip: left ? '收起导航' : '打开导航',
                  icon: const Icon(Icons.view_sidebar_outlined),
                  onPressed: () {
                    if (bounds.maxWidth < 940) {
                      _scaffoldKey.currentState?.openDrawer();
                    } else {
                      setState(() => leftVisible = !leftVisible);
                    }
                  },
                ),
              ),
              trailing: Row(
                children: [
                  PopupMenuButton<ThemeMode>(
                    tooltip: '界面主题',
                    icon: Icon(
                      widget.mode == ThemeMode.system
                          ? Icons.brightness_auto_outlined
                          : widget.mode == ThemeMode.dark
                          ? Icons.dark_mode_outlined
                          : Icons.light_mode_outlined,
                      size: 18,
                    ),
                    initialValue: widget.mode,
                    onSelected: widget.onThemeChanged,
                    itemBuilder: (_) => const [
                      PopupMenuItem(
                        value: ThemeMode.system,
                        child: Text('跟随系统'),
                      ),
                      PopupMenuItem(value: ThemeMode.light, child: Text('浅色')),
                      PopupMenuItem(value: ThemeMode.dark, child: Text('深色')),
                    ],
                  ),
                  if (rightContext)
                    IconButton(
                      tooltip: right ? '收起工程检查器' : '打开工程检查器',
                      onPressed: () {
                        if (wide) {
                          setState(() => rightVisible = !rightVisible);
                        } else {
                          _scaffoldKey.currentState?.openEndDrawer();
                        }
                      },
                      icon: const Icon(Icons.tune_rounded),
                    ),
                  const SizedBox(width: 12),
                ],
              ),
            ),
            if (widget.startupError != null)
              MaterialBanner(
                content: Text(widget.startupError!),
                actions: const [SizedBox.shrink()],
              ),
            Expanded(
              child: Row(
                children: [
                  if (left) SizedBox(width: actualLeft, child: _sidebar()),
                  if (left)
                    _ResizeHandle(
                      label: '调整导航宽度',
                      onDelta: (delta) {
                        setState(
                          () => leftWidth = (leftWidth + delta).clamp(190, 340),
                        );
                        _saveLayout();
                      },
                      onReset: () {
                        setState(() => leftWidth = 230);
                        _saveLayout();
                      },
                    ),
                  Expanded(child: ClipRect(child: _page())),
                  // 属性面板仅在工程相关页面占用宽度；隐藏时保留已读选区与草稿。
                  if (right)
                    Visibility(
                      visible: rightContext,
                      maintainState: true,
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          _ResizeHandle(
                            label: '调整检查器宽度',
                            onDelta: (delta) {
                              setState(
                                () => rightWidth = (rightWidth - delta).clamp(
                                  260,
                                  430,
                                ),
                              );
                              _saveLayout();
                            },
                            onReset: () {
                              setState(() => rightWidth = 302);
                              _saveLayout();
                            },
                          ),
                          SizedBox(
                            width: actualRight,
                            child: Inspector(
                              key: ValueKey(_bridgeGeneration),
                              api: widget.api,
                              onConnection: _openConnection,
                            ),
                          ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
            _statusBar(),
          ],
        ),
      );
    },
  );

  Widget _sidebar() {
    final colors = Theme.of(context).colorScheme;
    final entries = model.conversations
        .where(
          (e) => '${e['title']} ${e['note'] ?? ''}'.toLowerCase().contains(
            search.toLowerCase(),
          ),
        )
        .toList();
    return ColoredBox(
      color: colors.surfaceContainerLowest,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.all(10),
            child: OutlinedButton.icon(
              onPressed:
                  widget.api.busy ||
                      model.navigationBusy ||
                      model.submissionUnknown ||
                      model.pendingJobId != null
                  ? null
                  : () async {
                      await model.newConversation();
                      if (mounted) setState(() => page = 0);
                    },
              icon: const Icon(Icons.add_rounded, size: 18),
              label: const Text('新调教会话'),
            ),
          ),
          // 返回当前草稿只切换页面；不能通过新建会话丢弃尚未发送的内容。
          _nav(0, Icons.chat_bubble_outline_rounded, '当前会话'),
          _nav(1, Icons.audio_file_outlined, '音频素材'),
          _nav(2, Icons.mic_none_rounded, '录音与听评'),
          _nav(5, Icons.cable_outlined, '连接管理'),
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 8, 4),
            child: Row(
              children: [
                Text(
                  '会话',
                  style: TextStyle(
                    fontSize: 12,
                    color: colors.onSurfaceVariant,
                  ),
                ),
                const Spacer(),
                IconButton(
                  tooltip: '刷新会话',
                  onPressed: widget.api.busy
                      ? null
                      : () async {
                          try {
                            await model.refreshConversations();
                          } catch (e) {
                            _notice(e.toString());
                          }
                        },
                  icon: const Icon(Icons.refresh_rounded, size: 16),
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: SizedBox(
              height: 34,
              child: TextField(
                onChanged: (value) => setState(() => search = value),
                decoration: const InputDecoration(
                  hintText: '搜索会话',
                  prefixIcon: Icon(Icons.search_rounded, size: 16),
                  contentPadding: EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 6,
                  ),
                ),
                style: const TextStyle(fontSize: 12),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Expanded(
            child: entries.isEmpty
                ? Padding(
                    padding: const EdgeInsets.all(20),
                    child: Text(
                      search.isEmpty ? '暂无会话' : '没有匹配的会话',
                      style: TextStyle(
                        fontSize: 12,
                        color: colors.onSurfaceVariant,
                      ),
                    ),
                  )
                : ListView.builder(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    itemCount: entries.length,
                    itemBuilder: (_, index) {
                      final item = entries[index];
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 3),
                        child: Material(
                          color: page == 0 && model.conversationId == item['id']
                              ? colors.surfaceContainerHighest
                              : Colors.transparent,
                          borderRadius: BorderRadius.circular(3),
                          child: ListTile(
                            dense: true,
                            minTileHeight: 34,
                            contentPadding: const EdgeInsets.symmetric(
                              horizontal: 10,
                            ),
                            title: Tooltip(
                              message: '${item['title']}',
                              child: Text(
                                '${item['title']}',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(fontSize: 13),
                              ),
                            ),
                            leading: Icon(
                              item['starred'] == true
                                  ? Icons.star_outline_rounded
                                  : Icons.chat_bubble_outline_rounded,
                              size: 16,
                            ),
                            horizontalTitleGap: 8,
                            onTap:
                                widget.api.busy ||
                                    model.navigationBusy ||
                                    model.submissionUnknown ||
                                    model.pendingJobId != null
                                ? null
                                : () async {
                                    await model.openConversation(
                                      item['id'] as String,
                                    );
                                    if (mounted) setState(() => page = 0);
                                  },
                          ),
                        ),
                      );
                    },
                  ),
          ),
          const Divider(height: 1),
          const SizedBox(height: 8),
          _nav(3, Icons.delete_outline_rounded, '回收站'),
          _nav(4, Icons.settings_outlined, '设置'),
          const SizedBox(height: 8),
        ],
      ),
    );
  }

  /// 连接与全局工作状态集中到底栏，避免侧栏、会话头和输入区重复提示。
  /// 状态文字也是诊断入口；未连接时用户不必猜测该去哪个设置页面。
  Widget _statusBar() {
    final colors = Theme.of(context).colorScheme;
    final connected = model.status['bridge']?['connected'] == true;
    // HTTP 中断后的任务结果未知不等于空闲，底栏与会话恢复入口保持同一语义。
    final needsCheck = model.submissionUnknown || model.pendingJobId != null;
    return Container(
      height: 28,
      padding: const EdgeInsets.symmetric(horizontal: 10),
      decoration: BoxDecoration(
        color: colors.surfaceContainerLow,
        border: Border(top: BorderSide(color: colors.outlineVariant)),
      ),
      child: DefaultTextStyle(
        style: TextStyle(fontSize: 11, color: colors.onSurfaceVariant),
        child: Row(
          children: [
            Tooltip(
              message: '打开连接管理与诊断',
              child: InkWell(
                onTap: () => _navigate(5),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(connected ? Icons.link : Icons.link_off, size: 14),
                    const SizedBox(width: 6),
                    Text(connected ? 'SynthV 已连接' : 'SynthV 未连接'),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 16),
            Text(model.status['writeEnabled'] == true ? '选区可写 · 应用需确认' : '只读'),
            const Spacer(),
            if (widget.api.busy) ...[
              const SizedBox(
                width: 10,
                height: 10,
                child: CircularProgressIndicator(strokeWidth: 1.5),
              ),
              const SizedBox(width: 7),
            ],
            Text(
              widget.api.busy
                  ? '任务执行中'
                  : needsCheck
                  ? '任务状态待核对'
                  : '就绪',
            ),
          ],
        ),
      ),
    );
  }

  Widget _nav(int target, IconData icon, String label) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
    child: Material(
      color: page == target
          ? Theme.of(context).colorScheme.surfaceContainerHighest
          : Colors.transparent,
      borderRadius: BorderRadius.circular(3),
      child: ListTile(
        dense: true,
        minTileHeight: 34,
        contentPadding: const EdgeInsets.symmetric(horizontal: 12),
        leading: Icon(icon, size: 18),
        horizontalTitleGap: 10,
        title: Text(label, style: const TextStyle(fontSize: 13)),
        onTap: () {
          _navigate(target);
          _scaffoldKey.currentState?.closeDrawer();
        },
      ),
    ),
  );

  Widget _page() => switch (page) {
    5 => ConnectionPage(api: widget.api, onChanged: _connectionChanged),
    1 => LibraryPage(
      key: ValueKey(_libraryGeneration),
      api: widget.api,
      onAttach: _attach,
    ),
    2 => RecordingPage(api: widget.api, onAttach: _attach),
    3 => TrashPage(
      api: widget.api,
      onChanged: () => unawaited(model.refreshConversations()),
    ),
    4 => SettingsPage(
      api: widget.api,
      onChanged: () async {
        await model.refreshPlatforms();
        await model.loadCatalog();
        await model.loadCapabilities();
      },
    ),
    _ => _chat(),
  };

  Widget _chat() {
    final colors = Theme.of(context).colorScheme;
    final allMessages = [
      ...model.messages,
      if (model.pendingMessage != null) model.pendingMessage!,
    ];
    return Column(
      children: [
        Container(
          height: 48,
          padding: const EdgeInsets.symmetric(horizontal: 16),
          decoration: BoxDecoration(
            border: Border(bottom: BorderSide(color: colors.outlineVariant)),
          ),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  model.conversation?['title']?.toString() ?? '新调教会话',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontWeight: FontWeight.w600,
                    fontSize: 14,
                  ),
                ),
              ),
              IconButton(
                tooltip: model.conversation?['starred'] == true
                    ? '取消收藏'
                    : '收藏会话',
                onPressed: model.conversation == null || widget.api.busy
                    ? null
                    : () => _manage('star'),
                icon: Icon(
                  model.conversation?['starred'] == true
                      ? Icons.star_rounded
                      : Icons.star_outline_rounded,
                ),
              ),
              IconButton(
                tooltip: '名称与备注',
                onPressed: model.conversation == null || widget.api.busy
                    ? null
                    : () => _manage('note'),
                icon: const Icon(Icons.edit_note_rounded),
              ),
              IconButton(
                tooltip: '删除会话',
                onPressed: model.conversation == null || widget.api.busy
                    ? null
                    : () => _manage('delete'),
                icon: const Icon(Icons.delete_outline_rounded),
              ),
            ],
          ),
        ),
        Expanded(
          child: allMessages.isEmpty && !model.sending
              ? _welcome()
              : SelectionArea(
                  child: ListView(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
                    children: [
                      for (final message in allMessages) _message(message),
                      if (model.sending) _progress(),
                    ],
                  ),
                ),
        ),
        if (model.error.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Icon(Icons.info_outline_rounded, size: 16),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    model.error,
                    style: const TextStyle(fontSize: 12),
                  ),
                ),
                if (model.pendingJobId != null || model.submissionUnknown)
                  TextButton(
                    onPressed: model.sending ? null : model.reconnectJob,
                    child: const Text('重新连接任务'),
                  ),
                if (model.pendingJobId == null && !model.submissionUnknown)
                  IconButton(
                    tooltip: '收起提示',
                    onPressed: () => setState(() => model.error = ''),
                    icon: const Icon(Icons.close_rounded, size: 16),
                  ),
              ],
            ),
          ),
        _composer(),
      ],
    );
  }

  /// 空会话只提供可执行的起点，避免大幅欢迎区与工程面板争夺注意力。
  Widget _welcome() => Center(
    child: SingleChildScrollView(
      padding: const EdgeInsets.all(28),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 520),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              '调教会话',
              style: TextStyle(fontSize: 20, fontWeight: FontWeight.w500),
            ),
            const SizedBox(height: 12),
            Text(
              '输入调教目标。参数方案先预览，确认后才写入工程。',
              style: TextStyle(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
                fontSize: 13,
              ),
            ),
            const SizedBox(height: 20),
            LayoutBuilder(
              builder: (context, size) => Wrap(
                spacing: 10,
                runSpacing: 10,
                children: [
                  _starter(
                    Icons.music_note_outlined,
                    '看看当前这句',
                    '根据我附带的选区，先分析哪些地方值得调整，并说明依据。',
                    size.maxWidth,
                  ),
                  _starter(
                    Icons.auto_awesome_outlined,
                    '让声音更自然',
                    '保留现有音高与演唱细节，结合我附带的选区提出少量局部修饰，先说明目标。',
                    size.maxWidth,
                  ),
                  _starter(
                    Icons.headphones_outlined,
                    '听一听 A / B',
                    '请先判断两段音频是否可比，再结合我的目标比较同一位置的差异，不预设 B 更好。',
                    size.maxWidth,
                    audio: true,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    ),
  );

  Widget _starter(
    IconData icon,
    String title,
    String prompt,
    double width, {
    bool audio = false,
  }) => SizedBox(
    width: width,
    child: TextButton(
      onPressed: () {
        setState(() {
          model.draft = prompt;
          _draft.text = prompt;
          if (audio) {
            page = 1;
            _libraryGeneration++;
          }
        });
        _composerFocus.requestFocus();
      },
      child: Row(
        children: [
          Icon(icon, size: 17),
          const SizedBox(width: 10),
          Expanded(child: Text(title)),
          const Icon(Icons.chevron_right, size: 16),
        ],
      ),
    ),
  );

  Widget _message(JsonMap message) {
    final user = message['role'] == 'user';
    final error = message['role'] == 'error';
    final colors = Theme.of(context).colorScheme;
    return Align(
      alignment: Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 1040),
        child: Padding(
          padding: const EdgeInsets.only(bottom: 20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.only(bottom: 4),
                child: Row(
                  children: [
                    Icon(
                      user
                          ? Icons.person_outline
                          : error
                          ? Icons.error_outline_rounded
                          : Icons.graphic_eq_rounded,
                      size: 18,
                    ),
                    const SizedBox(width: 8),
                    Text(
                      user
                          ? '用户'
                          : error
                          ? '请求未完成'
                          : '调教助手',
                      style: const TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        message['model']?.toString() ?? '',
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: 11,
                          color: colors.onSurfaceVariant,
                        ),
                      ),
                    ),
                    IconButton(
                      tooltip: user ? '复制消息' : '复制回复',
                      onPressed: () => Clipboard.setData(
                        ClipboardData(text: message['text']?.toString() ?? ''),
                      ),
                      icon: const Icon(Icons.copy_outlined, size: 15),
                    ),
                  ],
                ),
              ),
              Container(
                padding: user
                    ? const EdgeInsets.symmetric(horizontal: 12, vertical: 10)
                    : EdgeInsets.zero,
                decoration: user
                    ? BoxDecoration(
                        color: colors.surfaceContainerLow,
                        border: Border(
                          left: BorderSide(
                            color: colors.outlineVariant,
                            width: 2,
                          ),
                        ),
                      )
                    : null,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (user)
                      SelectableText(
                        message['text']?.toString() ?? '',
                        style: const TextStyle(fontSize: 13, height: 1.6),
                      )
                    else
                      MarkdownBody(
                        data: message['text']?.toString() ?? '',
                        selectable: false,
                        styleSheet:
                            MarkdownStyleSheet.fromTheme(Theme.of(context))
                                .copyWith(
                                  p: TextStyle(
                                    color: colors.onSurface,
                                    fontSize: 13,
                                    height: 1.6,
                                  ),
                                ),
                        imageBuilder: (uri, title, alt) => Text(
                          alt ?? '图片链接',
                          style: TextStyle(color: colors.onSurfaceVariant),
                        ),
                        onTapLink: (_, href, _) {
                          if (href != null) {
                            Clipboard.setData(ClipboardData(text: href));
                            _notice('链接已复制');
                          }
                        },
                      ),
                    for (final clip in maps(message['attachments']))
                      Padding(
                        padding: const EdgeInsets.only(top: 8),
                        child: ConstrainedBox(
                          constraints: const BoxConstraints(maxWidth: 480),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Tooltip(
                                message:
                                    '${clip['label'] ?? clip['name'] ?? '音频附件'}',
                                child: Text(
                                  '${clip['label'] ?? clip['name'] ?? '音频附件'}',
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    fontSize: 12,
                                    color: colors.onSurfaceVariant,
                                  ),
                                ),
                              ),
                              AudioClip(
                                key: ValueKey(clip['id']),
                                api: widget.api,
                                clip: clip,
                              ),
                            ],
                          ),
                        ),
                      ),
                  ],
                ),
              ),
              if (message['delivery'] != null)
                Padding(
                  padding: const EdgeInsets.only(top: 5),
                  child: Text(
                    message['delivery'] as String,
                    style: TextStyle(
                      fontSize: 11,
                      color: colors.onSurfaceVariant,
                    ),
                  ),
                ),
              if ((message['reasoningSummary']?.toString() ?? '').isNotEmpty)
                Theme(
                  data: Theme.of(context)
                      .copyWith(dividerColor: Colors.transparent),
                  child: ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    dense: true,
                    title: const Text('思考摘要', style: TextStyle(fontSize: 12)),
                    children: [
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: SelectableText(
                            message['reasoningSummary'] as String,
                            style: TextStyle(
                              fontSize: 12,
                              height: 1.6,
                              color: colors.onSurfaceVariant,
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              if (maps(message['actions']).isNotEmpty)
                ProposalCard(
                  key: ValueKey(message['id']),
                  api: widget.api,
                  message: message,
                  conversationId: model.conversationId,
                  onChanged: () async {
                    await model.reloadConversation();
                    await model.refreshStatus();
                  },
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _progress() => Padding(
    padding: const EdgeInsets.only(bottom: 12),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                '${model.progress['stage'] ?? '正在处理'}',
                style: const TextStyle(fontSize: 13),
              ),
            ),
            Text(
              '${model.elapsedSeconds} 秒',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ),
        if ((model.progress['reasoning']?.toString() ?? '').isNotEmpty)
          ExpansionTile(
            key: ValueKey('live-${model.conversationId}'),
            initiallyExpanded: true,
            tilePadding: EdgeInsets.zero,
            title: const Text('思考摘要', style: TextStyle(fontSize: 12)),
            children: [
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  model.progress['reasoning'] as String,
                  style: TextStyle(
                    fontSize: 12,
                    height: 1.6,
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                ),
              ),
            ],
          ),
      ],
    ),
  );

  Widget _composer() {
    final disabled =
        widget.api.busy ||
        model.navigationBusy ||
        model.sending ||
        model.submissionUnknown ||
        model.pendingJobId != null;
    final colors = Theme.of(context).colorScheme;
    return Padding(
      padding: EdgeInsets.zero,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: double.infinity),
        child: Container(
          decoration: BoxDecoration(
            color: colors.surfaceContainerLow,
            border: Border(top: BorderSide(color: colors.outlineVariant)),
          ),
          padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _modelControls(disabled),
              const SizedBox(height: 8),
              CallbackShortcuts(
                bindings: {
                  const SingleActivator(
                    LogicalKeyboardKey.enter,
                    control: true,
                  ): () {
                    if (!disabled) {
                      unawaited(model.send());
                      _toBottom();
                    }
                  },
                },
                child: TextField(
                  controller: _draft,
                  focusNode: _composerFocus,
                  minLines: 2,
                  maxLines: 4,
                  enabled: !disabled,
                  onChanged: (value) => setState(() => model.draft = value),
                  style: const TextStyle(fontSize: 13, height: 1.5),
                  decoration: const InputDecoration(
                    hintText: '描述希望怎样演唱，或讨论这段调教…',
                    border: InputBorder.none,
                    enabledBorder: InputBorder.none,
                    focusedBorder: InputBorder.none,
                    filled: false,
                    contentPadding: EdgeInsets.symmetric(
                      horizontal: 4,
                      vertical: 8,
                    ),
                  ),
                ),
              ),
              for (final clip in model.attachments)
                Row(
                  children: [
                    Expanded(
                      child: AudioClip(api: widget.api, clip: clip),
                    ),
                    IconButton(
                      tooltip: '移除本次附件',
                      onPressed: disabled
                          ? null
                          : () =>
                                setState(() => model.attachments.remove(clip)),
                      icon: const Icon(Icons.close_rounded, size: 16),
                    ),
                  ],
                ),
              const SizedBox(height: 6),
              Row(
                children: [
                  IconButton(
                    tooltip: '选择音频附件',
                    onPressed: disabled ? null : () => _navigate(1),
                    icon: const Icon(Icons.attach_file_rounded),
                  ),
                  const SizedBox(width: 4),
                  SizedBox(
                    width: 24,
                    height: 24,
                    child: Checkbox(
                      value: model.includeSelection,
                      onChanged: disabled
                          ? null
                          : (value) =>
                                setState(() => model.includeSelection = value!),
                    ),
                  ),
                  const SizedBox(width: 6),
                  const Text('附带选区', style: TextStyle(fontSize: 12)),
                  const Spacer(),
                  Tooltip(
                    message: 'Ctrl + Enter 发送；Enter 换行',
                    child: FilledButton.icon(
                      onPressed: disabled || model.draft.trim().isEmpty
                          ? null
                          : () {
                              unawaited(model.send());
                              _toBottom();
                            },
                      icon: Icon(
                        model.sending
                            ? Icons.more_horiz_rounded
                            : Icons.arrow_upward_rounded,
                        size: 18,
                      ),
                      label: Text(model.sending ? '处理中' : '发送'),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _modelControls(bool disabled) => LayoutBuilder(
    builder: (context, constraints) {
      final four = constraints.maxWidth >= 660;
      final width = four
          ? (constraints.maxWidth - 24) / 4
          : (constraints.maxWidth - 8) / 2;
      final currentPlatformExists = model.platforms.any(
        (e) => e['id'] == model.platformId,
      );
      final effort = maps(model.capabilities['reasoning']?['options']);
      if (!effort.any((e) => e['value'] == model.reasoning)) {
        effort.add({
          'value': model.reasoning,
          'label': '${model.reasoning}（需检查）',
        });
      }
      Widget field(String label, Widget child) => SizedBox(
        width: width,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.only(left: 2, bottom: 5),
              child: Text(
                label,
                style: TextStyle(
                  fontSize: 10,
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
            ),
            SizedBox(height: 34, child: child),
          ],
        ),
      );
      Widget dropdown(
        String value,
        List<DropdownMenuItem<String>> items,
        ValueChanged<String?>? onChanged,
      ) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10),
        decoration: BoxDecoration(
          border: Border.all(
            color: Theme.of(context).colorScheme.outlineVariant,
          ),
          borderRadius: BorderRadius.circular(4),
        ),
        child: DropdownButtonHideUnderline(
          child: DropdownButton<String>(
            value: value,
            items: items,
            onChanged: onChanged,
            isExpanded: true,
            isDense: true,
            style: Theme.of(context).textTheme.bodySmall,
            iconSize: 16,
          ),
        ),
      );
      return Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          field(
            '平台',
            dropdown(
              model.platformId,
              [
                if (!currentPlatformExists)
                  DropdownMenuItem(
                    value: model.platformId,
                    child: const Text('默认平台'),
                  ),
                for (final platform in model.platforms)
                  DropdownMenuItem(
                    value: platform['id'] as String,
                    child: Text(
                      platform['name']?.toString() ?? '平台',
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
              ],
              disabled
                  ? null
                  : (value) => unawaited(model.choosePlatform(value!)),
            ),
          ),
          field(
            '模型',
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _modelInput,
                    enabled: !disabled,
                    style: const TextStyle(fontSize: 12),
                    decoration: InputDecoration(
                      hintText: '平台默认',
                      contentPadding: const EdgeInsets.symmetric(
                        horizontal: 9,
                        vertical: 8,
                      ),
                      suffixIconConstraints: const BoxConstraints(
                        maxWidth: 28,
                        minWidth: 28,
                      ),
                      suffixIcon: model.models.isEmpty
                          ? null
                          : PopupMenuButton<String>(
                              tooltip: '已缓存的模型',
                              padding: EdgeInsets.zero,
                              icon: const Icon(
                                Icons.expand_more_rounded,
                                size: 16,
                              ),
                              onSelected: disabled
                                  ? null
                                  : (value) {
                                      setState(() {
                                        model.modelId = value;
                                        _modelInput.text = value;
                                      });
                                      unawaited(model.loadCapabilities());
                                      unawaited(model.savePreferences());
                                    },
                              itemBuilder: (_) => model.models
                                  .map(
                                    (e) => PopupMenuItem(
                                      value: e['id'] as String,
                                      child: Text(
                                        e['id'] as String,
                                        overflow: TextOverflow.ellipsis,
                                      ),
                                    ),
                                  )
                                  .toList(),
                            ),
                    ),
                    onChanged: (value) {
                      model.modelId = value;
                      _capabilityTimer?.cancel();
                      _capabilityTimer = Timer(
                        const Duration(milliseconds: 350),
                        () async {
                          await model.loadCapabilities();
                          await model.savePreferences();
                        },
                      );
                    },
                  ),
                ),
                IconButton(
                  tooltip: '从平台刷新模型目录',
                  onPressed: disabled
                      ? null
                      : () => unawaited(model.loadCatalog(refresh: true)),
                  icon: const Icon(Icons.refresh_rounded, size: 16),
                ),
              ],
            ),
          ),
          field(
            '推理强度',
            dropdown(
              model.reasoning,
              effort
                  .map(
                    (e) => DropdownMenuItem(
                      value: e['value'] as String,
                      child: Text(
                        e['label'] as String,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  )
                  .toList(),
              disabled
                  ? null
                  : (value) {
                      setState(() => model.reasoning = value!);
                      unawaited(model.savePreferences());
                    },
            ),
          ),
          field(
            '绘制方式',
            dropdown(
              model.renderMode,
              const [
                DropdownMenuItem(value: 'smooth', child: Text('绘制模式')),
                DropdownMenuItem(value: 'points', child: Text('控制点模式')),
              ],
              disabled
                  ? null
                  : (value) {
                      setState(() => model.renderMode = value!);
                      unawaited(model.savePreferences());
                    },
            ),
          ),
        ],
      );
    },
  );
}

/// 分隔条同时支持拖动、方向键与双击复位；拖动只影响布局而不重建业务状态。
class _ResizeHandle extends StatelessWidget {
  const _ResizeHandle({
    required this.label,
    required this.onDelta,
    required this.onReset,
  });
  final String label;
  final ValueChanged<double> onDelta;
  final VoidCallback onReset;
  @override
  Widget build(BuildContext context) => Semantics(
    label: label,
    child: Focus(
      onKeyEvent: (_, event) {
        if (event is KeyDownEvent) {
          if (event.logicalKey == LogicalKeyboardKey.arrowLeft) {
            onDelta(-12);
            return KeyEventResult.handled;
          }
          if (event.logicalKey == LogicalKeyboardKey.arrowRight) {
            onDelta(12);
            return KeyEventResult.handled;
          }
          if (event.logicalKey == LogicalKeyboardKey.home) {
            onReset();
            return KeyEventResult.handled;
          }
        }
        return KeyEventResult.ignored;
      },
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeColumn,
        child: GestureDetector(
          behavior: HitTestBehavior.opaque,
          // 从按下位置累计位移，保留首次越过拖动阈值的那一段；桌面短拖动
          // 或自动化可能只有一次 move，默认 start 行为会将该段全部吞掉。
          dragStartBehavior: DragStartBehavior.down,
          onHorizontalDragUpdate: (details) => onDelta(details.delta.dx),
          onDoubleTap: onReset,
          child: Tooltip(
            message: '$label · 双击复位',
            child: SizedBox(
              width: 5,
              child: Center(
                child: Container(
                  width: 1,
                  color: Theme.of(context).dividerColor,
                ),
              ),
            ),
          ),
        ),
      ),
    ),
  );
}
