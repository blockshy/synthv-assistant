"use strict";

/**
 * 工作台只在浏览器保存主题和栏宽偏好，不保存会话、工程数据或凭据。
 * 主题可提前执行；DOM 事件等文档准备后绑定，因此同时支持普通脚本和 defer。
 */
(() => {
  const THEME_KEY = "synthv.workbench.theme.v1";
  const WIDTH_KEY = "synthv.workbench.widths.v1";
  const THEMES = new Set(["system", "light", "dark"]);
  const SIDES = {
    left: { id: "sidebar-resizer", variable: "--sidebar-width", min: 180, max: 360, initial: 224 },
    right: { id: "inspector-resizer", variable: "--inspector-width", min: 280, max: 520, initial: 340 },
  };
  const root = document.documentElement;
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = readTheme();
  let repaintPending = false;

  /** 隐私模式和受限浏览器可能拒绝存储；失败不能影响当页功能。 */
  function readStorage(key) { try { return window.localStorage.getItem(key); } catch { return null; } }
  function saveStorage(key, value) { try { window.localStorage.setItem(key, value); } catch { /* 内存偏好仍然有效。 */ } }
  function readTheme() { const value = readStorage(THEME_KEY); return THEMES.has(value) ? value : "system"; }
  function defaults() { return { left: SIDES.left.initial, right: SIDES.right.initial }; }
  function readWidths() {
    try {
      const raw = readStorage(WIDTH_KEY);
      if (!raw || raw.length > 100) return defaults();
      const value = JSON.parse(raw);
      if (!value || Array.isArray(value) || typeof value !== "object") return defaults();
      if (Object.keys(value).length !== 2 || !Object.hasOwn(value, "left") || !Object.hasOwn(value, "right")) return defaults();
      // 禁止字符串数字、额外属性、NaN、浮点及越界值进入 CSS 样式。
      for (const side of ["left", "right"]) {
        if (!Number.isSafeInteger(value[side]) || value[side] < SIDES[side].min || value[side] > SIDES[side].max) return defaults();
      }
      return { left: value.left, right: value.right };
    } catch { return defaults(); }
  }
  function repaint() {
    if (repaintPending) return;
    repaintPending = true;
    window.requestAnimationFrame(() => {
      repaintPending = false;
      // 现有 Canvas 波形监听 resize；一帧仅通知一次，避免拖拽造成重复绘制。
      window.dispatchEvent(new Event("resize"));
    });
  }
  function applyTheme() {
    const theme = preference === "system" ? (systemDark.matches ? "dark" : "light") : preference;
    root.dataset.theme = theme;
    root.dataset.themePreference = preference;
    const select = document.getElementById("theme-preference");
    if (select) select.value = preference;
    window.dispatchEvent(new CustomEvent("synthv:themechange", { detail: { theme, preference } }));
    repaint();
  }
  const onSystemChange = () => { if (preference === "system") applyTheme(); };
  if (systemDark.addEventListener) systemDark.addEventListener("change", onSystemChange);
  else systemDark.addListener(onSystemChange);
  applyTheme();

  function initialize() {
    const shell = document.querySelector(".desktop-shell");
    if (!shell) return;
    const compactRight = window.matchMedia("(max-width: 1100px)");
    const compactLeft = window.matchMedia("(max-width: 760px)");
    let requested = readWidths();
    let actual = { ...requested };
    let dragging = null;
    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const rightVisible = () => !compactRight.matches && !document.body.classList.contains("inspector-hidden");
    const enabled = (side) => side === "left" ? !compactLeft.matches : rightVisible();
    const available = () => shell.clientWidth - 380 - (rightVisible() ? 10 : 5);

    /** 窗口缩小时约束实际栏宽，至少保留 380px 正文；不覆盖原始个人偏好。 */
    function applyWidths(priority = null) {
      let left = requested.left;
      let right = requested.right;
      if (!compactLeft.matches) {
        if (rightVisible()) {
          if (priority === "right") {
            right = clamp(right, SIDES.right.min, Math.min(SIDES.right.max, available() - left));
            left = clamp(left, SIDES.left.min, Math.min(SIDES.left.max, available() - right));
          } else {
            left = clamp(left, SIDES.left.min, Math.min(SIDES.left.max, available() - right));
            right = clamp(right, SIDES.right.min, Math.min(SIDES.right.max, available() - left));
          }
        } else left = clamp(left, SIDES.left.min, Math.min(SIDES.left.max, available()));
      }
      actual = { left: Math.round(left), right: Math.round(right) };
      for (const side of ["left", "right"]) {
        const config = SIDES[side];
        root.style.setProperty(config.variable, `${actual[side]}px`);
        const separator = document.getElementById(config.id);
        if (!separator) continue;
        separator.setAttribute("aria-valuemin", String(config.min));
        separator.setAttribute("aria-valuemax", String(maximum(side)));
        separator.setAttribute("aria-valuenow", String(actual[side]));
        separator.setAttribute("aria-valuetext", `${actual[side]} 像素`);
        separator.setAttribute("aria-disabled", String(!enabled(side)));
        separator.tabIndex = enabled(side) ? 0 : -1;
      }
    }
    function maximum(side) {
      const other = rightVisible() ? actual[side === "left" ? "right" : "left"] : 0;
      return Math.max(SIDES[side].min, Math.min(SIDES[side].max, Math.floor(available() - other)));
    }
    function setWidth(side, width, persist = true) {
      if (!enabled(side)) return;
      // 手动调整时以另一栏此刻可见的宽度为锚点，避免旧的大窗口偏好把拖动反向挤回。
      // 仅缩放窗口时仍由 applyWidths 保留原始偏好；这里代表一次明确的用户布局修改。
      const other = side === "left" ? "right" : "left";
      if (rightVisible()) requested[other] = actual[other];
      requested[side] = Math.round(clamp(width, SIDES[side].min, maximum(side)));
      applyWidths(side);
      if (persist) saveStorage(WIDTH_KEY, JSON.stringify(requested));
      repaint();
    }
    /** 取消、窗口失焦或指针丢失时恢复原偏好；成功结束时才写入存储。 */
    function finish(cancelled = false) {
      if (!dragging) return;
      const previous = dragging;
      dragging = null;
      if (cancelled) requested = previous.original;
      previous.element.removeAttribute("data-dragging");
      document.body.classList.remove("layout-resizing");
      if (previous.element.hasPointerCapture(previous.pointerId)) previous.element.releasePointerCapture(previous.pointerId);
      applyWidths();
      if (!cancelled) saveStorage(WIDTH_KEY, JSON.stringify(requested));
      repaint();
    }
    for (const side of ["left", "right"]) {
      const separator = document.getElementById(SIDES[side].id);
      if (!separator) continue;
      separator.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !event.isPrimary || !enabled(side)) return;
        event.preventDefault(); separator.focus();
        dragging = { side, element: separator, pointerId: event.pointerId, startX: event.clientX, startWidth: actual[side], original: { ...requested } };
        separator.setPointerCapture(event.pointerId);
        separator.dataset.dragging = "true";
        document.body.classList.add("layout-resizing");
      });
      separator.addEventListener("pointermove", (event) => {
        if (!dragging || dragging.element !== separator || dragging.pointerId !== event.pointerId) return;
        setWidth(side, dragging.startWidth + (event.clientX - dragging.startX) * (side === "left" ? 1 : -1), false);
      });
      separator.addEventListener("pointerup", (event) => { if (dragging?.pointerId === event.pointerId) finish(); });
      separator.addEventListener("pointercancel", (event) => { if (dragging?.pointerId === event.pointerId) finish(true); });
      separator.addEventListener("lostpointercapture", () => { if (dragging?.element === separator) finish(true); });
      separator.addEventListener("dblclick", () => setWidth(side, SIDES[side].initial));
      separator.addEventListener("keydown", (event) => {
        if (!enabled(side)) return;
        const step = event.shiftKey ? 32 : 8;
        let next;
        if (event.key === "Home") next = SIDES[side].min;
        else if (event.key === "End") next = maximum(side);
        else if (event.key === "ArrowLeft") next = actual[side] + (side === "left" ? -step : step);
        else if (event.key === "ArrowRight") next = actual[side] + (side === "left" ? step : -step);
        else return;
        event.preventDefault(); setWidth(side, next);
      });
    }
    document.getElementById("theme-preference")?.addEventListener("change", (event) => {
      preference = THEMES.has(event.target.value) ? event.target.value : "system";
      saveStorage(THEME_KEY, preference); applyTheme();
    });
    window.addEventListener("keydown", (event) => { if (event.key === "Escape" && dragging) { event.preventDefault(); finish(true); } });
    window.addEventListener("blur", () => finish(true));
    window.addEventListener("resize", () => {
      if (dragging && !enabled(dragging.side)) finish(true);
      applyWidths(dragging?.side);
    });
    // 跨页面同步个人偏好；正在拖拽时不接管对方页面的栏宽操作。
    window.addEventListener("storage", (event) => {
      if (event.key === THEME_KEY || event.key === null) { preference = readTheme(); applyTheme(); }
      if ((event.key === WIDTH_KEY || event.key === null) && !dragging) { requested = readWidths(); applyWidths(); repaint(); }
    });
    new MutationObserver(() => applyWidths(dragging?.side)).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    applyTheme(); applyWidths();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
})();
