import 'package:flutter/material.dart';

/// 可安全嵌入滚动页的统一展开区。
///
/// Flutter 的展开状态和 Scrollable 位置都通过 PageStorage 保存。展开控件
/// 若复用祖先滚动键，会把 double 当作 bool；直接添加展开键后，内部 TextField
/// 或 SelectableText 的滚动又可能把 bool 当作 double。此处同时为展开状态
/// 提供独立键，并为展开内容建立独立存储边界，隔离这两种恢复流程。
class WorkbenchExpansionTile extends StatefulWidget {
  const WorkbenchExpansionTile({
    super.key,
    required this.storageId,
    required this.title,
    this.children = const [],
    this.subtitle,
    this.trailing,
    this.tilePadding = EdgeInsets.zero,
    this.childrenPadding = EdgeInsets.zero,
    this.initiallyExpanded = false,
    this.maintainState = false,
    this.dense = true,
    this.visualDensity,
    this.onExpansionChanged,
    this.shape = const Border(),
    this.collapsedShape = const Border(),
  });

  /// 同一页面中的各个展开区必须不同；动态条目宜使用消息或提案 ID。
  final String storageId;
  final Widget title;
  final List<Widget> children;
  final Widget? subtitle;
  final Widget? trailing;
  final EdgeInsetsGeometry tilePadding;
  final EdgeInsetsGeometry childrenPadding;
  final bool initiallyExpanded;
  final bool maintainState;
  final bool dense;
  final VisualDensity? visualDensity;
  final ValueChanged<bool>? onExpansionChanged;
  final ShapeBorder shape;
  final ShapeBorder collapsedShape;

  @override
  State<WorkbenchExpansionTile> createState() => _WorkbenchExpansionTileState();
}

class _WorkbenchExpansionTileState extends State<WorkbenchExpansionTile> {
  PageStorageBucket _contentStorage = PageStorageBucket();

  @override
  void didUpdateWidget(covariant WorkbenchExpansionTile oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.storageId != widget.storageId) {
      // 组件位置不变但目标条目改变时，子控件不能恢复上一个条目的滚动位置。
      _contentStorage = PageStorageBucket();
    }
  }

  @override
  Widget build(BuildContext context) => ExpansionTile(
    key: PageStorageKey('workbench-expansion:${widget.storageId}'),
    title: widget.title,
    subtitle: widget.subtitle,
    trailing: widget.trailing,
    tilePadding: widget.tilePadding,
    childrenPadding: widget.childrenPadding,
    initiallyExpanded: widget.initiallyExpanded,
    maintainState: widget.maintainState,
    dense: widget.dense,
    visualDensity: widget.visualDensity,
    onExpansionChanged: widget.onExpansionChanged,
    shape: widget.shape,
    collapsedShape: widget.collapsedShape,
    children: [
      // 嵌套 PageStorage 使子级收集存储键时在此停止，不能再看到展开区的 bool 键。
      PageStorage(
        bucket: _contentStorage,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: widget.children,
        ),
      ),
    ],
  );
}
