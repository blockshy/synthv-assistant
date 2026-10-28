import 'package:flutter/material.dart';

/// 功能页共用的紧凑标题栏。主要内容留给工作区，不重复显示宣传式副标题。
class PageToolbar extends StatelessWidget {
  const PageToolbar({super.key, required this.title, this.actions = const []});
  final String title;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) => Container(
    constraints: const BoxConstraints(minHeight: 48),
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 7),
    decoration: BoxDecoration(
      border: Border(bottom: BorderSide(color: Theme.of(context).dividerColor)),
    ),
    child: Row(
      children: [
        Expanded(
          child: Text(title, style: Theme.of(context).textTheme.titleSmall),
        ),
        ...actions,
      ],
    ),
  );
}

/// 属性名称固定在控件之外，避免浮动标签使输入框高度和文字基线发生跳动。
/// 窄工作区改为上下排列，仍保留完整属性名与校验错误的可用宽度。
class PageProperty extends StatelessWidget {
  const PageProperty({
    super.key,
    required this.label,
    required this.child,
    this.labelWidth = 108,
  });
  final String label;
  final Widget child;
  final double labelWidth;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 10),
    child: LayoutBuilder(
      builder: (context, constraints) {
        final caption = Text(
          label,
          style: Theme.of(context).textTheme.bodySmall
              ?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant),
        );
        if (constraints.maxWidth < 390) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [caption, const SizedBox(height: 5), child],
          );
        }
        return Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: labelWidth,
              child: Padding(
                padding: const EdgeInsets.only(top: 8, right: 12),
                child: caption,
              ),
            ),
            Expanded(child: child),
          ],
        );
      },
    ),
  );
}

/// 分组只使用小标题和单条分隔线，不在已有主页面里再嵌套大卡片。
class PageSection extends StatelessWidget {
  const PageSection({
    super.key,
    required this.title,
    required this.children,
    this.trailing,
  });
  final String title;
  final List<Widget> children;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 14),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Container(
          constraints: const BoxConstraints(minHeight: 32),
          margin: const EdgeInsets.only(bottom: 10),
          decoration: BoxDecoration(
            border: Border(
              bottom: BorderSide(color: Theme.of(context).dividerColor),
            ),
          ),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  title,
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              ?trailing,
            ],
          ),
        ),
        ...children,
      ],
    ),
  );
}

/// 固定底栏放在页面 Column 的 Expanded 滚动区之外，低分辨率仍可确认操作。
class PageFooter extends StatelessWidget {
  const PageFooter({super.key, required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 9),
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surfaceContainerLow,
      border: Border(top: BorderSide(color: Theme.of(context).dividerColor)),
    ),
    child: child,
  );
}

/// 长说明按需展开；云端发送、不可逆删除等决策提示应直接留在对应操作旁。
class PageNotes extends StatelessWidget {
  const PageNotes({super.key, required this.children});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => ExpansionTile(
    tilePadding: EdgeInsets.zero,
    childrenPadding: const EdgeInsets.only(bottom: 12),
    dense: true,
    visualDensity: VisualDensity.compact,
    shape: const Border(),
    collapsedShape: const Border(),
    title: Text('说明', style: Theme.of(context).textTheme.bodySmall),
    children: [
      DefaultTextStyle(
        style: Theme.of(context).textTheme.bodySmall!,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: children,
        ),
      ),
    ],
  );
}

/// 操作结果独立于表单，可换行并可主动关闭，避免长错误挤出底部按钮。
class PageNotice extends StatelessWidget {
  const PageNotice({super.key, required this.message, this.onDismiss});
  final String message;
  final VoidCallback? onDismiss;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 7),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 96),
            child: SingleChildScrollView(
              child: Text(
                message,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
          ),
        ),
        if (onDismiss != null)
          IconButton(
            tooltip: '关闭提示',
            onPressed: onDismiss,
            icon: const Icon(Icons.close, size: 16),
          ),
      ],
    ),
  );
}
