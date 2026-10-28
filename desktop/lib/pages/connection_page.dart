import 'package:flutter/material.dart';

import '../core/api.dart';
import 'page_chrome.dart';

/// 桥接修复是独立工作页：展示真实目录与心跳，所有修改均由按钮显式发起。
/// 客户端只提交后端给出的版本及固定目标枚举，不允许输入任意文件路径。
class ConnectionPage extends StatefulWidget {
  const ConnectionPage({super.key, required this.api, required this.onChanged});
  final WorkbenchApi api;
  final Future<void> Function() onChanged;
  @override
  State<ConnectionPage> createState() => _ConnectionPageState();
}

class _ConnectionPageState extends State<ConnectionPage> {
  JsonMap _diagnostics = {};
  bool _loading = false;
  String? _feedback;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  /// 只读诊断不会读取选区，也不会要求用户先允许工程写入。
  Future<void> _refresh() async {
    if (_loading) return;
    setState(() => _loading = true);
    try {
      final value = Map<String, dynamic>.from(
        await widget.api.getJson('/api/bridge/diagnostics') as Map,
      );
      if (mounted) setState(() => _diagnostics = value);
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _change(String target) async {
    try {
      await widget.api.exclusive(() async {
        widget.api.invalidatePreviews();
        final result = Map<String, dynamic>.from(
          await widget.api.postJson('/api/bridge/connect', {
            'target': target,
            'revision': _diagnostics['revision'],
          }) as Map,
        );
        if (mounted) {
          setState(() {
            _diagnostics = result;
            _feedback = result['message']?.toString() ?? '连接目标已更新。';
          });
        }
        await widget.onChanged();
      });
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) await _refresh();
    }
  }

  /// 安装仅替换固定位置的桥接脚本，明确说明备份与宿主重新启动要求。
  /// 不能把“安装成功”显示成“工程已连接”，最终仍以新心跳为准。
  Future<void> _install() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('安装匹配当前目录的桥接脚本？'),
        content: const Text(
          '现有脚本将先备份。安装后，请在 SynthV 中结束旧桥接，再重新扫描并运行 SynthV Assistant。此操作不会修改工程。',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('备份并安装'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await widget.api.exclusive(() async {
        widget.api.invalidatePreviews();
        final result = Map<String, dynamic>.from(
          await widget.api.postJson('/api/bridge/install', {
            'confirm': true,
            'revision': _diagnostics['revision'],
          }) as Map,
        );
        if (mounted) {
          setState(() {
            _diagnostics = result;
            _feedback = '脚本已安装。请在 SynthV 中结束旧桥接、重新扫描并运行脚本，再刷新诊断。';
          });
        }
        await widget.onChanged();
      });
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) await _refresh();
    }
  }

  Widget _value(String label, Object? value) => PageProperty(
    label: label,
    child: Padding(
      padding: const EdgeInsets.only(top: 6),
      child: SelectableText(value?.toString() ?? '未发现'),
    ),
  );

  String _heartbeat(dynamic raw) {
    if (raw is! Map) return '未收到心跳';
    if (raw['connected'] != true) return '未收到有效心跳';
    final age = raw['heartbeatAge'];
    return age is num ? '在线 · ${age.toStringAsFixed(1)} 秒前更新' : '在线';
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final disabled =
          _loading || widget.api.busy || _diagnostics['busy'] == true;
      final installed = _diagnostics['installedScript'] as Map? ?? {};
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          PageToolbar(
            title: '连接管理',
            actions: [
              IconButton(
                tooltip: '刷新连接诊断',
                // 诊断中的 busy 是上次快照；刷新必须能重新读取任务结束后的状态，
                // 否则进入页面时后台繁忙会导致本页永久无法恢复。
                onPressed: _loading ? null : _refresh,
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
          if (_loading) const LinearProgressIndicator(minHeight: 2),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.all(20),
              children: [
                Text(
                  _diagnostics['message']?.toString() ?? '正在检查桥接目录与脚本状态…',
                  style: Theme.of(context).textTheme.titleSmall,
                ),
                const SizedBox(height: 16),
                PageSection(
                  title: '当前连接',
                  children: [
                    _value(
                      '桥接状态',
                      _heartbeat(_diagnostics['currentHeartbeat']),
                    ),
                    _value('连接目录', _diagnostics['currentIpcDirectory']),
                    _value('资料目录', _diagnostics['dataDirectory']),
                  ],
                ),
                PageSection(
                  title: 'SynthV 已安装脚本',
                  children: [
                    _value(
                      '脚本文件',
                      installed['exists'] == true ? installed['path'] : '未安装',
                    ),
                    _value('脚本绑定目录', installed['ipcDirectory']),
                    _value(
                      '脚本心跳',
                      _heartbeat(_diagnostics['installedHeartbeat']),
                    ),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        FilledButton.icon(
                          onPressed:
                              disabled ||
                                  _diagnostics['canConnectInstalled'] != true
                              ? null
                              : () => _change('installed'),
                          icon: const Icon(Icons.link, size: 16),
                          label: const Text('连接正在运行的脚本'),
                        ),
                        OutlinedButton(
                          onPressed:
                              disabled || _diagnostics['canUseCurrent'] != true
                              ? null
                              : () => _change('current'),
                          child: const Text('使用当前资料目录'),
                        ),
                      ],
                    ),
                  ],
                ),
                PageSection(
                  title: '脚本维护',
                  children: [
                    const Text('脚本缺失或需要重新绑定目录时，可安装匹配版本。已有有效心跳时优先直接连接。'),
                    const SizedBox(height: 10),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: OutlinedButton.icon(
                        onPressed:
                            disabled || _diagnostics['canInstall'] != true
                            ? null
                            : _install,
                        icon: const Icon(Icons.build_outlined, size: 16),
                        label: const Text('安装匹配脚本'),
                      ),
                    ),
                  ],
                ),
                const PageNotes(
                  children: [
                    Text('连接目标与资料目录分别保存。连接旧脚本不会移动会话、音频或密钥。'),
                    SizedBox(height: 8),
                    Text('修改工程仍需开启“允许修改选区”，并确认具体预览。桥接连接或脚本安装本身不修改工程。'),
                  ],
                ),
              ],
            ),
          ),
          if (_feedback != null)
            PageNotice(
              message: _feedback!,
              onDismiss: () => setState(() => _feedback = null),
            ),
          const PageFooter(child: Text('连接诊断不读取或修改工程。')),
        ],
      );
    },
  );
}
