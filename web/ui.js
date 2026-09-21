"use strict";

/**
 * 工作台共享表现层：所有图标均由本地、固定的 SVG 路径生成，不加载字体、CDN 或外部脚本。
 * 页面只选择图标名称与中文操作名称；尺寸、描边、状态与焦点统一由样式表控制。
 * 不把模型回复、素材名称等不可信内容解析成 HTML 或 SVG。
 */
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICONS = Object.freeze({
    star: ["M12 3 14.8 8.7 21 9.6 16.5 14 17.6 20.2 12 17.3 6.4 20.2 7.5 14 3 9.6 9.2 8.7Z"],
    note: ["M14 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-9", "m10 14 1-4 7-7 3 3-7 7-4 1Z", "m16 5 3 3"],
    trash: ["M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"],
    restore: ["M4 5v5h5M4.4 10A8 8 0 1 1 5 17"],
    refresh: ["M20 4v5h-5M4 20v-5h5M20 9a8 8 0 0 0-14-4M4 15a8 8 0 0 0 14 4"],
    close: ["m6 6 12 12M18 6 6 18"],
    plus: ["M12 5v14M5 12h14"],
    "arrow-left": ["M20 12H4m6-6-6 6 6 6"],
    "arrow-up": ["M12 20V4m-6 6 6-6 6 6"],
    menu: ["M4 6h16M4 12h16M4 18h16"],
    panel: ["M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1ZM15 3v18"],
    music: ["M9 17V5l11-2v12M9 8l11-2", "M9 17c0 2-2 3-4 3s-3-1-3-2 2-3 4-3 3 1 3 2ZM20 15c0 2-2 3-4 3s-3-1-3-2 2-3 4-3 3 1 3 2Z"],
    record: ["M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"],
    settings: ["m9 3-1 3-3 1-2 3 2 2v3l2 3 3 1 2 2 3-2 3-1 2-3v-3l2-2-2-3-3-1-1-3H9Z", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"],
    message: ["M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2Z"],
    wave: ["M2 12h3l3-7 5 14 3-7h6"],
    upload: ["M12 16V3m-5 5 5-5 5 5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"],
    selection: ["M8 3H3v5M16 3h5v5M21 16v5h-5M8 21H3v-5M8 12h8M12 8v8"],
  });

  /** 只允许已登记的图标；SVG 隐藏于辅助技术，由所属按钮提供可读的操作名称。 */
  function icon(name) {
    if (!Object.hasOwn(ICONS, name)) throw new TypeError(`未登记的工作台图标：${name}`);
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "ui-icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.dataset.icon = name;
    for (const pathData of ICONS[name]) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", pathData); svg.append(path);
    }
    return svg;
  }

  /**
   * 更新图标按钮时保留事件、ID、禁用状态与 aria-pressed/expanded。
   * 相同图标不重复创建 DOM，防止状态轮询打断焦点或原生悬停提示。
   */
  function setIconButton(button, name, label) {
    if (button.tagName !== "BUTTON" || typeof label !== "string" || !label.trim()) {
      throw new TypeError("图标按钮必须提供按钮元素和明确的操作名称。");
    }
    if (!Object.hasOwn(ICONS, name)) throw new TypeError(`未登记的工作台图标：${name}`);
    button.type = "button";
    button.classList.add("icon-button");
    if (button.dataset.uiIcon !== name || !button.querySelector("svg.ui-icon")) button.replaceChildren(icon(name));
    button.dataset.uiIcon = name;
    button.title = label;
    button.setAttribute("aria-label", label);
    return button;
  }

  /** 动态列表复用与静态页面同一按钮结构；状态属性必须使用布尔值，避免字符串歧义。 */
  function iconButton(name, label, options = {}) {
    const button = document.createElement("button");
    if (options.id) button.id = options.id;
    if (options.className) button.className = options.className;
    if (typeof options.pressed === "boolean") button.setAttribute("aria-pressed", String(options.pressed));
    return setIconButton(button, name, label);
  }

  /** 装饰图标与纯图标按钮均声明 data-ui-icon；初始化不触发任何业务动作。 */
  function hydrate(root = document) {
    for (const element of root.querySelectorAll("[data-ui-icon]")) {
      if (element.tagName === "BUTTON") {
        setIconButton(element, element.dataset.uiIcon, element.title || element.getAttribute("aria-label"));
      } else {
        element.setAttribute("aria-hidden", "true");
        element.replaceChildren(icon(element.dataset.uiIcon));
      }
    }
  }

  window.SynthVUI = Object.freeze({ icon, iconButton, setIconButton, hydrate });
  hydrate();
})();
