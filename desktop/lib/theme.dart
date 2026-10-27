import 'package:flutter/material.dart';

/// 全局尺寸与灰阶主题是桌面视觉规范的唯一来源，页面不另造控件高度。
abstract final class WorkbenchTheme {
  static const double controlHeight = 38;
  static const double iconSize = 18;
  static const double radius = 10;
  static ThemeData build(Brightness brightness) {
    final dark = brightness == Brightness.dark;
    final surface = dark ? const Color(0xff202020) : const Color(0xfffcfcfc);
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
      borderRadius: BorderRadius.circular(8),
    );
    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      scaffoldBackgroundColor: surface,
      fontFamily: 'Microsoft YaHei UI',
      visualDensity: VisualDensity.compact,
      dividerColor: border,
      textTheme: Typography.material2021().black.apply(
        bodyColor: foreground,
        displayColor: foreground,
        fontFamily: 'Microsoft YaHei UI',
      ),
      inputDecorationTheme: InputDecorationTheme(
        isDense: true,
        filled: true,
        fillColor: scheme.surfaceContainerLow,
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 12,
          vertical: 10,
        ),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: BorderSide(color: border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: BorderSide(color: border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
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
          minimumSize: const Size(34, 34),
          maximumSize: const Size(34, 34),
          iconSize: iconSize,
          padding: const EdgeInsets.all(6),
        ),
      ),
      cardTheme: CardThemeData(
        elevation: 0,
        color: scheme.surfaceContainerLow,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: border),
        ),
      ),
      tooltipTheme: const TooltipThemeData(
        waitDuration: Duration(milliseconds: 450),
      ),
      scrollbarTheme: ScrollbarThemeData(
        radius: const Radius.circular(6),
        thickness: WidgetStateProperty.all(5),
      ),
      dropdownMenuTheme: DropdownMenuThemeData(
        inputDecorationTheme: InputDecorationTheme(
          isDense: true,
          contentPadding: const EdgeInsets.symmetric(
            horizontal: 10,
            vertical: 8,
          ),
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
        ),
      ),
    );
  }
}
