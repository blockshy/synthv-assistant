import 'package:flutter/material.dart';

/// 工业工作台统一规范：低圆角、紧凑工具栏、中性层级与可辨识焦点。
/// 页面只负责信息分组；字体、按钮与输入控件不能各自派生另一套尺寸。
abstract final class WorkbenchTheme {
  static const double controlHeight = 34;
  static const double iconSize = 17;
  static const double radius = 4;
  static ThemeData build(Brightness brightness) {
    final dark = brightness == Brightness.dark;
    final surface = dark ? const Color(0xff202020) : const Color(0xfffafafa);
    final foreground = dark ? const Color(0xffededed) : const Color(0xff202020);
    final border = dark ? const Color(0xff3a3a3a) : const Color(0xffdedede);
    final scheme =
        ColorScheme.fromSeed(
          seedColor: const Color(0xff737373),
          brightness: brightness,
        ).copyWith(
          primary: foreground,
          onPrimary: surface,
          secondary: foreground,
          onSecondary: surface,
          primaryContainer: dark
              ? const Color(0xff343434)
              : const Color(0xffe9e9e9),
          onPrimaryContainer: foreground,
          secondaryContainer: dark
              ? const Color(0xff343434)
              : const Color(0xffe9e9e9),
          onSecondaryContainer: foreground,
          surface: surface,
          onSurface: foreground,
          // 菜单与弹层同样使用灰阶，防止 Material 派生表面带入蓝绿色调。
          surfaceTint: Colors.transparent,
          surfaceContainer: dark
              ? const Color(0xff262626)
              : const Color(0xfff3f3f3),
          surfaceContainerHigh: dark
              ? const Color(0xff2b2b2b)
              : const Color(0xffefefef),
          surfaceBright: dark
              ? const Color(0xff383838)
              : const Color(0xfffcfcfc),
          surfaceDim: dark ? const Color(0xff202020) : const Color(0xffdedede),
          inverseSurface: dark
              ? const Color(0xffeeeeee)
              : const Color(0xff303030),
          onInverseSurface: dark
              ? const Color(0xff202020)
              : const Color(0xffeeeeee),
          outline: dark ? const Color(0xff888888) : const Color(0xff777777),
          surfaceContainerLowest: dark
              ? const Color(0xff181818)
              : const Color(0xfff3f3f3),
          surfaceContainerLow: dark
              ? const Color(0xff252525)
              : const Color(0xfff6f6f6),
          surfaceContainerHighest: dark
              ? const Color(0xff303030)
              : const Color(0xffeeeeee),
          onSurfaceVariant: dark
              ? const Color(0xffaaa9a9)
              : const Color(0xff696969),
          outlineVariant: border,
          error: foreground,
          onError: surface,
        );
    final rounded = RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(radius),
    );
    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      scaffoldBackgroundColor: surface,
      fontFamily: 'Microsoft YaHei UI',
      // 桌面尺寸直接由控件规范决定，避免 compact 再减小 8px 造成同排错位。
      visualDensity: VisualDensity.standard,
      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
      dividerColor: border,
      textTheme: Typography.material2021().black
          .apply(
            bodyColor: foreground,
            displayColor: foreground,
            fontFamily: 'Microsoft YaHei UI',
          )
          .copyWith(
            bodyLarge: TextStyle(fontSize: 13, height: 1.45, color: foreground),
            bodyMedium: TextStyle(
              fontSize: 13,
              height: 1.45,
              color: foreground,
            ),
            bodySmall: TextStyle(
              fontSize: 11,
              height: 1.4,
              color: scheme.onSurfaceVariant,
            ),
            titleLarge: TextStyle(
              fontSize: 17,
              fontWeight: FontWeight.w600,
              color: foreground,
            ),
            titleMedium: TextStyle(
              fontSize: 14,
              fontWeight: FontWeight.w600,
              color: foreground,
            ),
            titleSmall: TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.w600,
              color: foreground,
            ),
            labelLarge: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w500,
              color: foreground,
            ),
          ),
      inputDecorationTheme: InputDecorationTheme(
        constraints: const BoxConstraints(minHeight: controlHeight),
        isDense: true,
        filled: true,
        fillColor: surface,
        floatingLabelBehavior: FloatingLabelBehavior.never,
        contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(radius),
          borderSide: BorderSide(color: border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(radius),
          borderSide: BorderSide(color: border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(radius),
          borderSide: BorderSide(color: foreground),
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size(64, controlHeight),
          shape: rounded,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          minimumSize: const Size(64, controlHeight),
          shape: rounded,
          side: BorderSide(color: border),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          minimumSize: const Size(48, controlHeight),
          shape: rounded,
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(
          minimumSize: const Size(32, 32),
          maximumSize: const Size(32, 32),
          iconSize: iconSize,
          padding: const EdgeInsets.all(6),
        ),
      ),
      cardTheme: CardThemeData(
        elevation: 0,
        color: scheme.surfaceContainerLow,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(radius),
          side: BorderSide(color: border),
        ),
      ),
      tooltipTheme: const TooltipThemeData(
        waitDuration: Duration(milliseconds: 450),
      ),
      // 弹层和选项统一沿用矩形工具面板，不再混用大圆角 Material 卡片。
      popupMenuTheme: PopupMenuThemeData(
        shape: rounded,
        elevation: 4,
        textStyle: TextStyle(fontSize: 12, color: foreground),
      ),
      dialogTheme: DialogThemeData(
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
      ),
      chipTheme: ChipThemeData(
        shape: rounded,
        side: BorderSide(color: border),
        labelStyle: TextStyle(fontSize: 12, color: foreground),
      ),
      listTileTheme: const ListTileThemeData(
        dense: true,
        minTileHeight: 34,
        horizontalTitleGap: 8,
      ),
      expansionTileTheme: const ExpansionTileThemeData(
        tilePadding: EdgeInsets.symmetric(horizontal: 4),
        childrenPadding: EdgeInsets.only(bottom: 8),
      ),
      scrollbarTheme: ScrollbarThemeData(
        radius: const Radius.circular(2),
        thickness: WidgetStateProperty.all(5),
      ),
      dropdownMenuTheme: DropdownMenuThemeData(
        inputDecorationTheme: InputDecorationTheme(
          isDense: true,
          contentPadding: const EdgeInsets.symmetric(
            horizontal: 10,
            vertical: 8,
          ),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(radius),
          ),
        ),
      ),
    );
  }
}
