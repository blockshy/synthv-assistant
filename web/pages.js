"use strict";

/**
 * 中央主页面导航、独立录音和回收站。
 * 页面切换仅修改 hidden，不销毁聊天 DOM、输入草稿或正在运行的录音任务。
 * 真实采集和恢复均由本机服务完成，界面不模拟音频或成功结果。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.SynthVWorkbench;
  const ui = window.SynthVUI;
  const pages = new Set(["chat", "settings", "library", "record", "trash"]);
  const state = { current: "chat", recording: false, readingSelection: false, lastRecording: null, trashLoading: false, trashBusy: "", trashRequest: 0, trashItems: [], trashConfirm: "" };
  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }
  function feedback(id, message = "", error = false) { $(id).textContent = message; $(id).classList.toggle("error", error); }
  function number(value) { return Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : "—"; }

  /** 导航前发出可取消事件，设置正在保存时由设置模块阻止离开。 */
  function go(target) {
    if (!pages.has(target)) return false;
    if (target === state.current) return true;
    const from = state.current;
    const before = new CustomEvent("synthv:before-page-change", { cancelable: true, detail: { from, to: target } });
    if (!window.dispatchEvent(before)) return false;
    state.current = target;
    for (const name of pages) $("page-" + name).hidden = name !== target;
    document.querySelectorAll("[data-page-target]").forEach((button) => {
      const active = button.dataset.pageTarget === target;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    for (const [id, name] of [["open-audio-library", "library"], ["open-audio-settings", "settings"]]) {
      $(id).classList.toggle("active", name === target);
      if (name === target) $(id).setAttribute("aria-current", "page"); else $(id).removeAttribute("aria-current");
    }
    // 主页面导航也收起窄屏抽屉；可见聊天草稿仍保留在原来的输入框中。
    document.body.classList.remove("sidebar-open"); $("toggle-sidebar").setAttribute("aria-expanded", "false");
    window.dispatchEvent(new CustomEvent("synthv:page-change", { detail: { from, to: target } }));
    if (target === "trash") loadTrash();
    if (target === "record") syncRecording();
    const heading = $("page-" + target).querySelector("h1, h2");
    if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    return true;
  }
  window.SynthVPages = Object.freeze({ go, get current() { return state.current; } });
  for (const button of document.querySelectorAll("[data-page-target]")) button.addEventListener("click", () => go(button.dataset.pageTarget));
  $("skip-to-chat").addEventListener("click", (event) => {
    event.preventDefault();
    // 先经正常导航显示聊天页；保存设置等导航限制仍然有效，不能聚焦隐藏输入框。
    if (!go("chat")) return;
    document.body.classList.remove("sidebar-open"); $("toggle-sidebar").setAttribute("aria-expanded", "false");
    if (matchMedia("(max-width: 1100px)").matches) {
      document.body.classList.remove("inspector-open"); $("toggle-inspector").setAttribute("aria-expanded", "false");
    }
    const input = $("chat-input");
    // 发送期间输入框暂时禁用，改为聚焦真实任务状态，不改变发送或任务进度。
    if (input.disabled) { $("chat-job-status").tabIndex = -1; $("chat-job-status").focus({ preventScroll: true }); }
    else input.focus({ preventScroll: true });
  });

  function syncRecording() {
    const status = bridge.getStatus();
    const busy = state.recording || state.readingSelection || bridge.getManualBusy();
    $("page-record-submit").disabled = busy || !status?.bridge?.connected || !status?.capture?.available;
    $("page-record-read-selection").disabled = busy || !status?.bridge?.connected;
    $("page-record-read-selection").textContent = state.readingSelection ? "正在读取…" : "读取选区";
    // 缓存按钮只填表单，不访问 SynthV；没有有效范围时禁用，避免猜测录音区间。
    $("page-record-use-selection").disabled = busy || !bridge.getSelectionRange();
    for (const id of ["page-record-name", "page-record-start", "page-record-duration"]) $(id).disabled = state.recording || state.readingSelection;
    $("page-record-progress").hidden = !state.recording;
    $("page-record-form").setAttribute("aria-busy", String(state.recording || state.readingSelection));
    $("page-record-submit").textContent = state.recording ? "正在录制…" : "开始录制";
    $("page-record-attach").disabled = !state.lastRecording || state.recording;
  }

  /** 只展示实际录音元数据；请求播放时长和采集文件实际时长分开说明。 */
  function renderRecording(recording) {
    state.lastRecording = recording;
    $("page-record-empty").hidden = Boolean(recording);
    $("page-record-result").hidden = !recording;
    const audio = $("page-record-audio"); audio.pause();
    if (!recording) { audio.removeAttribute("src"); audio.load(); syncRecording(); return; }
    $("page-record-result-name").textContent = recording.label || "工程录音";
    const duration = Number.isFinite(recording.analysis?.duration) ? recording.analysis.duration : recording.durationSeconds;
    $("page-record-summary").textContent = `起点 ${number(recording.startSeconds)} 秒 · 请求播放 ${number(recording.durationSeconds)} 秒 · 实际音频 ${number(duration)} 秒`;
    const url = new URL(recording.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/audio/")) throw new Error("录音地址不是有效的本机音频地址。");
    audio.src = url.href; audio.load();
    const metrics = $("page-record-metrics"); metrics.replaceChildren();
    const analysis = recording.analysis || {};
    for (const [label, value, unit] of [["均方根电平", analysis.rmsDbfs, "dBFS"], ["峰值", analysis.peakDbfs, "dBFS"], ["采样率", analysis.sampleRate, "Hz"]]) {
      if (!Number.isFinite(value)) continue;
      const group = node("div"); group.append(node("dt", "", label), node("dd", "", `${number(value)} ${unit}`)); metrics.append(group);
    }
    $("page-record-warnings").textContent = Array.isArray(analysis.warnings) ? analysis.warnings.join(" ") : "";
    syncRecording();
  }

  $("page-record-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("page-record-submit").disabled || !$("page-record-form").reportValidity()) return;
    const payload = { label: $("page-record-name").value.trim(), startSeconds: Number($("page-record-start").value), durationSeconds: Number($("page-record-duration").value) };
    if (!payload.label) { feedback("page-record-feedback", "请填写录音名称。", true); return; }
    state.recording = true; bridge.setRecordingBusy(true); syncRecording();
    feedback("page-record-feedback", "已提交采集请求，等待 SynthV 的实际录音结果…");
    try {
      const job = await bridge.api("/api/record", payload);
      const result = await bridge.waitForJob(job.jobId);
      const recordings = await bridge.refreshRecordings();
      const id = result?.id || result?.recording?.id;
      const recording = recordings.find((item) => item.id === id);
      if (!recording) throw new Error("录音任务已返回，但暂未读到对应音频。请刷新素材库确认，未自动重录。");
      renderRecording({ ...recording, kind: "recording" });
      feedback("page-record-feedback", "录制完成并保存到素材库。可试听，或附加到当前会话；尚未发送给模型。");
      window.dispatchEvent(new CustomEvent("synthv:assets-changed", { detail: { kind: "recording", id } }));
    } catch (error) { feedback("page-record-feedback", bridge.errorMessage(error), true); }
    finally { state.recording = false; bridge.setRecordingBusy(false); syncRecording(); }
  });
  /** 新读取与使用缓存共用范围校验和时长限制，区别仅在是否访问宿主。 */
  function fillSelectionRange(range, fromCache = false) {
    if (!range) throw new Error("没有有效的已读取选区，请先读取最新选区，或手动填写录音范围。");
    const start = Math.round(range.start * 10) / 10;
    const duration = Math.max(1, Math.min(30, Math.round(range.duration * 10) / 10));
    $("page-record-start").value = start;
    $("page-record-duration").value = duration;
    const limit = range.duration > 30 ? "选区超过 30 秒，本次仅录制前 30 秒。" : range.duration < 1 ? "选区不足 1 秒，播放时长设为最少 1 秒。" : "";
    const source = fromCache ? "已使用选区缓存，本次未访问 SynthV" : "已读取当前选区";
    feedback("page-record-feedback", `${source}，填入起点 ${number(start)} 秒、播放 ${number(duration)} 秒。${limit}${fromCache ? "如宿主选区已变化，请读取最新选区。" : ""}`);
  }

  $("page-record-read-selection").addEventListener("click", async () => {
    if ($("page-record-read-selection").disabled) return;
    state.readingSelection = true;
    // 清空上次范围后才读取：无选区或网络失败时，必填校验阻止误录旧片段。
    $("page-record-start").value = "";
    $("page-record-duration").value = "";
    syncRecording();
    feedback("page-record-feedback", "正在读取 SynthV 当前选区…");
    try {
      await bridge.readSelection();
      const range = bridge.getSelectionRange();
      if (!range) throw new Error("未读取到有效音符选区，请在 SynthV 中选中音符后重试；也可手动填写录音范围。");
      fillSelectionRange(range);
    } catch (error) { feedback("page-record-feedback", bridge.errorMessage(error), true); }
    finally { state.readingSelection = false; syncRecording(); }
  });
  $("page-record-use-selection").addEventListener("click", () => {
    if ($("page-record-use-selection").disabled) return;
    try { fillSelectionRange(bridge.getSelectionRange(), true); }
    catch (error) { feedback("page-record-feedback", bridge.errorMessage(error), true); }
    syncRecording();
  });
  $("page-record-library").addEventListener("click", () => go("library"));
  $("page-record-attach").addEventListener("click", () => {
    if (!state.lastRecording) return;
    const item = { ...state.lastRecording, kind: "recording", durationSeconds: state.lastRecording.analysis?.duration ?? state.lastRecording.durationSeconds };
    if (!window.SynthVChat?.attachAsset(item)) feedback("page-record-feedback", "当前消息正在发送或已有两段附件。请先回到会话处理附件。", true);
  });

  /** 回收站的恢复与永久删除互斥执行；失败时保留项目与确认信息供检查。 */
  function syncTrashButtons() {
    const busy = state.trashLoading || Boolean(state.trashBusy);
    $("refresh-trash").disabled = busy;
    $("trash-list").querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  }

  async function loadTrash() {
    if (state.trashLoading) return;
    const request = ++state.trashRequest; state.trashLoading = true; syncTrashButtons();
    feedback("trash-feedback", "正在读取回收站…");
    try {
      const result = await bridge.api("/api/trash");
      if (request !== state.trashRequest) return;
      state.trashItems = Array.isArray(result.items) ? result.items : [];
      renderTrash();
      feedback("trash-feedback");
    } catch (error) { feedback("trash-feedback", bridge.errorMessage(error), true); }
    finally { state.trashLoading = false; syncTrashButtons(); }
  }

  function renderTrash() {
    const list = $("trash-list"); list.replaceChildren();
    if (!state.trashItems.length) list.append(node("p", "library-empty", "回收站是空的。移到回收站的会话与音频会显示在这里。"));
    for (const item of state.trashItems) {
      const key = `${item.kind}:${item.id}`, name = item.title || item.label || "未命名项目";
      const row = node("article", "trash-item"), content = node("div", "trash-item-info");
      content.append(node("h3", "", name));
      const type = { conversation: "会话", upload: "上传素材", recording: "工程录音" }[item.kind] || "项目";
      const date = new Date(item.deletedAt), when = Number.isNaN(date.getTime()) ? "" : " · " + date.toLocaleString("zh-CN");
      content.append(node("p", "field-help", `${item.starred ? "已星标 · " : ""}${type}${when}`));
      if (item.note) content.append(node("p", "asset-note", item.note));
      if (item.purgePending) content.append(node("p", "field-help warning", "上次永久删除未完成，资源已停用且不能恢复。请重试永久删除以完成清理。"));
      const actions = node("div", "asset-toolbar");
      // 服务端中断标记表示已进入永久删除流程，不能误导用户继续恢复。
      if (!item.permanent && !item.purgePending && item.restorable !== false) {
        // 工具栏图标携带完整目标名称，恢复事件与忙碌控制继续沿用原流程。
        const restore = ui.iconButton("restore", `恢复“${name}”`);
        restore.addEventListener("click", () => restoreItem(item, restore)); actions.append(restore);
      }
      const remove = ui.iconButton("trash", `${item.purgePending ? "重试永久删除" : "永久删除"}“${name}”`, { id: `trash-purge-${item.kind}-${item.id}` });
      remove.addEventListener("click", () => { state.trashConfirm = key; renderTrash(); $("trash-list").querySelector(".inline-confirm button")?.focus(); }); actions.append(remove);
      row.append(content, actions);
      if (state.trashConfirm === key) {
        const confirmation = node("div", "inline-confirm");
        const description = node("p", "", `永久删除“${name}”？${item.kind === "conversation" ? "会话正文与备注" : "原始音频与素材备注"}将被删除，无法恢复。`);
        description.id = `trash-purge-description-${item.kind}-${item.id}`; confirmation.append(description);
        const controls = node("div", "confirmation-actions"), yes = node("button", "button button-danger-quiet", "确认永久删除"), no = node("button", "button button-quiet", "取消");
        yes.type = "button"; no.type = "button";
        yes.setAttribute("aria-describedby", description.id);
        yes.addEventListener("click", () => purgeTrashItem(item));
        // 取消会重建列表，使用稳定标识定位新的入口，而不是聚焦已脱离文档的旧按钮。
        no.addEventListener("click", () => { state.trashConfirm = ""; renderTrash(); $(remove.id)?.focus(); });
        // 确认区独占整行，不嵌入标题列，避免右侧工具按钮挤窄说明和操作区。
        controls.append(yes, no); confirmation.append(controls); row.append(confirmation);
      }
      list.append(row);
    }
    syncTrashButtons();
  }

  /** 只在用户明确确认当前项目后永久删除，不把失败或超时当作成功。 */
  async function purgeTrashItem(item) {
    if (state.trashBusy || state.trashLoading) return;
    state.trashBusy = `${item.kind}:${item.id}`; syncTrashButtons();
    feedback("trash-feedback", `正在永久删除“${item.title || item.label || "未命名项目"}”…`);
    try {
      const result = await bridge.api(`/api/trash/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}/purge`, { confirm: true });
      if (!result.deleted || !result.permanent) throw new Error("服务未确认永久删除成功，请刷新回收站核实；未自动重试。");
      state.trashItems = state.trashItems.filter((entry) => `${entry.kind}:${entry.id}` !== state.trashBusy);
      state.trashConfirm = ""; renderTrash();
      if (item.kind === "conversation") await window.SynthVChat?.refreshConversations();
      else window.dispatchEvent(new CustomEvent("synthv:assets-changed", { detail: { kind: item.kind, id: item.id, deleted: true, permanent: true } }));
      feedback("trash-feedback", "已永久删除，无法恢复。");
    } catch (error) { feedback("trash-feedback", `${bridge.errorMessage(error)} 可刷新回收站检查结果；未自动重试。`, true); }
    finally { state.trashBusy = ""; syncTrashButtons(); }
  }

  async function restoreItem(item, button) {
    if (state.trashBusy || state.trashLoading || item.permanent || item.purgePending || item.restorable === false) return;
    state.trashBusy = `${item.kind}:${item.id}`; button.disabled = true; syncTrashButtons();
    try {
      const result = await bridge.api(`/api/trash/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}/restore`, {});
      if (!result.restored) throw new Error("服务未确认恢复成功，请刷新回收站核实。");
      await loadTrash();
      if (item.kind === "conversation") await window.SynthVChat?.refreshConversations();
      else {
        await bridge.refreshRecordings();
        window.dispatchEvent(new CustomEvent("synthv:assets-changed", { detail: { kind: item.kind, id: item.id, restored: true } }));
      }
      feedback("trash-feedback", "已恢复，可在会话列表或音频素材库中继续使用。");
    } catch (error) { feedback("trash-feedback", bridge.errorMessage(error), true); }
    finally { state.trashBusy = ""; syncTrashButtons(); }
  }
  $("refresh-trash").addEventListener("click", loadTrash);
  window.addEventListener("synthv:state", syncRecording);
  window.addEventListener("synthv:assets-changed", (event) => {
    if (event.detail?.deleted && event.detail.kind === "recording" && event.detail.id === state.lastRecording?.id) renderRecording(null);
  });
  syncRecording();
})();
