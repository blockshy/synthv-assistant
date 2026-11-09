import 'package:flutter/material.dart';

import '../theme.dart';

/// 工作台统一的单值选择控件。页面只提供选项与事件，不再单独覆盖框高、
/// 内边距和文字样式，避免 DropdownButton 默认使用标题字体及不同的密集高度。
///
/// value 是外部状态的唯一来源；内部表单按 value 重建，异步读回或切换平台后
/// 不会继续显示旧 initialValue。这个控件不执行保存，也不触发任何网络请求。
class WorkbenchDropdown<T> extends StatelessWidget {
  const WorkbenchDropdown({
    super.key,
    required this.value,
    required this.items,
    required this.onChanged,
    this.hint,
    this.selectedItemBuilder,
  });

  final T? value;
  final List<DropdownMenuItem<T>> items;
  final ValueChanged<T?>? onChanged;
  final Widget? hint;
  final List<Widget> Function(BuildContext)? selectedItemBuilder;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return DropdownButtonFormField<T>(
      key: ValueKey(value),
      initialValue: value,
      items: items,
      onChanged: onChanged,
      hint: hint,
      selectedItemBuilder: selectedItemBuilder,
      isExpanded: true,
      isDense: true,
      iconSize: WorkbenchTheme.iconSize,
      style: theme.textTheme.bodyMedium?.copyWith(
        fontWeight: FontWeight.w400,
        letterSpacing: 0,
      ),
      dropdownColor: theme.colorScheme.surfaceContainer,
      borderRadius: BorderRadius.circular(WorkbenchTheme.radius),
      menuMaxHeight: 360,
      decoration: const InputDecoration(
        // dense 模式按“内容 + padding”计算可见容器，tight 高度只约束外层，
        // 不会把 24px 的内部边框拉到 34px。保留与 TextField 一致的上下内边距，
        // 并固定标准密度，确保真实填色/边框区域填满控件，而不是留下透明空白。
        constraints: BoxConstraints.tightFor(
          height: WorkbenchTheme.controlHeight,
        ),
        isDense: true,
        visualDensity: VisualDensity.standard,
        contentPadding: EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      ),
    );
  }
}
