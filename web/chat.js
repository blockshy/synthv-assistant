"use strict";

/**
 * 对话工作台：负责会话、消息附件和逐项参数建议。
 * 所有会话正文由本机服务持久化；浏览器仅记忆最后打开的会话编号。
 * 不在浏览器存储正文或凭据，不自动上传音频给供应商，也不模拟模型回复。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.SynthVWorkbench;
  const ui = window.SynthVUI;
  if (!bridge) return;
  const state = {
    conversations: [], conversation: null, loading: false, sending: false,
    conversationRequest: 0, conversationListRequest: 0, actionBusy: "", activePreview: "", actionErrors: new Map(),
    attachments: [], assets: [], libraryDraft: new Set(), libraryLoading: false,
    uploading: false, libraryOpener: null, libraryRequest: 0,
    metadataBusy: false, assetBusy: "", assetEditor: null, assetDelete: "", assetDeleteMode: "trash",
    status: bridge.getStatus(), manualBusy: bridge.getManualBusy(), settingsSaving: false,
    modelState: window.SynthVModels?.getState(),
    jobProgress: { startedAt: 0, elapsedSeconds: 0, stage: "", text: "", reasoning: "", reasoningAvailable: false, receivedCharacters: 0 },
    jobTimer: null,
  };
  const parameterNames = { breathiness: "气声", tension: "张力", loudness: "响度", gender: "性别", pitchDelta: "音高偏移", toneShift: "音色偏移", vibratoEnv: "颤音包络", pitchCurve: "原生音高曲线" };
  const parameterUnits = { breathiness: "参数值", tension: "参数值", loudness: "dB", gender: "参数值", pitchDelta: "音分", toneShift: "音分", vibratoEnv: "参数值", pitchCurve: "MIDI 半音" };
  const actionLabels = { proposed: "建议 · 尚未预览", previewed: "已生成预览", applied: "已应用", unknown: "结果待核实" };
  const MAX_FILE_BYTES = 12_000_000;
  const welcome = $("chat-welcome");

  /** 创建纯文本节点；模型输出、文件名和工程信息始终作为数据处理。 */
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  /** 装饰图形统一来自本地图标表；保留包装类，并避免辅助技术重复朗读图形。 */
  function decorativeIcon(className, name) {
    const wrapper = element("span", className);
    wrapper.setAttribute("aria-hidden", "true"); wrapper.append(ui.icon(name));
    return wrapper;
  }
  function feedback(id, message = "", error = false) {
    $(id).textContent = message;
    $(id).classList.toggle("error", error);
  }
  function number(value, digits = 2) { return Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—"; }
  function signed(value) { return Number.isFinite(value) ? `${value > 0 ? "+" : ""}${number(value)}` : "—"; }
  function assetKey(asset) { return `${asset.kind}:${asset.id}`; }
  function assetName(asset) { return asset.label || asset.name || (asset.kind === "recording" ? "工程录音" : "音频素材"); }
  function dateLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function details(title, value) {
    const node = element("details", "message-details");
    node.append(element("summary", "", title), element("pre", "", bridge.printable(value)));
    return node;
  }

  /** 文件播放地址仅允许本机服务的音频路径；外部 URL 不会作为媒体请求加载。 */
  function localAudioUrl(asset) {
    if (!asset?.url || asset.available === false || asset.deleted || asset.permanent) return null;
    try {
      const url = new URL(asset.url, location.href);
      if (url.origin !== location.origin || !/^\/(uploads|audio|recordings)\//.test(url.pathname)) return null;
      return url.href;
    } catch { return null; }
  }

  function rememberConversation(id) {
    try { localStorage.setItem("lastConversationId", id); }
    catch { /* 隐私模式可能禁止本地存储；服务端会话仍然可从列表恢复。 */ }
  }
  function rememberedConversation() {
    try { return localStorage.getItem("lastConversationId"); } catch { return null; }
  }

  /** 状态只影响可用动作，不重绘正文，避免轮询打断阅读或正在播放的附件。 */
  function syncControls() {
    const interactionBusy = state.loading || state.sending || state.metadataBusy || Boolean(state.actionBusy);
    const busy = interactionBusy || Boolean(state.modelState?.busy);
    const configured = Boolean(state.modelState?.configured);
    const modelValid = state.modelState?.valid !== false;
    const audioUnsupported = state.attachments.length > 0 && state.modelState?.audioInput === "unsupported";
    window.SynthVModels?.setInteractionBusy(interactionBusy || state.settingsSaving);
    const connected = Boolean(state.status?.bridge?.connected);
    $("new-conversation").disabled = busy;
    $("refresh-conversations").disabled = state.loading || state.metadataBusy;
    $("chat-input").disabled = state.sending;
    $("include-selection").disabled = state.sending;
    $("attach-audio").disabled = state.sending;
    $("send-message").disabled = busy || state.settingsSaving || !configured || !modelValid || audioUnsupported || !$("chat-input").value.trim() || ($("include-selection").checked && (!connected || state.manualBusy));
    $("send-message").querySelector("span").textContent = state.sending ? "等待中" : "发送";
    $("chat-model-label").textContent = configured ? `${state.modelState.platformName} · ${state.modelState.modelLabel}` : "所选平台未配置";
    $("chat-model-label").title = configured ? "当前会话的模型选择。已配置不代表已验证供应商连接。" : "在设置中配置平台，再从消息输入区选择。";
    $("chat-form").setAttribute("aria-busy", state.sending ? "true" : "false");
    for (const id of ["star-conversation", "edit-conversation", "delete-conversation"]) $(id).disabled = busy || !state.conversation;
    // 图标、悬停说明与辅助名称同步更新；收藏状态仍由真实会话数据决定。
    ui.setIconButton($("star-conversation"), "star", state.conversation?.starred ? "取消当前会话星标" : "星标当前会话");
    $("star-conversation").setAttribute("aria-pressed", String(Boolean(state.conversation?.starred)));
    $("star-conversation").classList.toggle("is-starred", Boolean(state.conversation?.starred));
    for (const id of ["save-conversation-metadata", "cancel-conversation-metadata", "confirm-delete-conversation", "cancel-delete-conversation"]) $(id).disabled = state.metadataBusy;
    $("conversation-delete-mode").disabled = state.metadataBusy;
    $("conversation-title-input").disabled = state.metadataBusy; $("conversation-note-input").disabled = state.metadataBusy;
    $("chat-job-status").hidden = !state.sending;
    // 发送范围以实际附件列表为准；选择录音 A/B 不会暗中加入消息。
    const audioScope = state.attachments.length ? `发送所选 ${state.attachments.length} 段音频` : "本次不发送音频";
    const selectionScope = $("include-selection").checked ? "附带当前选区" : "不附带选区";
    const scopeProblem = audioUnsupported ? "当前模型不支持音频，请移除附件或更换模型。" : $("include-selection").checked && !connected ? "选区未连接，可取消勾选后咨询。" : "";
    // 正常发送范围仍可由输入框 aria-describedby 读取，仅需要处理的问题占用视觉空间。
    $("send-scope").textContent = scopeProblem || `${audioScope} · ${selectionScope}`;
    $("send-scope").hidden = false;
    $("send-scope").classList.toggle("visually-hidden", !scopeProblem);
    $("send-scope").classList.toggle("scope-warning", Boolean(scopeProblem));
    $("chat-subtitle").textContent = !configured ? "配置并选择本会话的平台；文字咨询无需附加音频。" : "先讨论与预览，再由你确认每一次工程修改。";
    document.querySelectorAll(".conversation-item").forEach((button) => { button.disabled = state.sending || state.metadataBusy || state.modelState?.busy || Boolean(state.actionBusy); });
    document.querySelectorAll("[data-action-preview]").forEach((button) => {
      button.disabled = busy || state.manualBusy || !connected;
    });
    document.querySelectorAll("[data-action-apply]").forEach((button) => {
      button.disabled = busy || state.manualBusy || !connected || !state.status?.writeEnabled || button.dataset.actionApply !== state.activePreview;
    });
    document.querySelectorAll(".attachment-remove").forEach((button) => { button.disabled = state.sending; });
    document.querySelectorAll("[data-chat-restore]").forEach((button) => {
      button.disabled = busy || state.manualBusy || !connected || !state.status?.writeEnabled;
    });
  }

  function renderConversations() {
    const list = $("conversation-list"); list.replaceChildren();
    if (!state.conversations.length) list.append(element("p", "sidebar-empty", "还没有会话。\n从一个调教想法开始。"));
    for (const conversation of [...state.conversations].sort((a, b) => Number(Boolean(b.starred)) - Number(Boolean(a.starred)))) {
      const button = element("button", "conversation-item"); button.type = "button";
      if (conversation.id === state.conversation?.id) { button.classList.add("active"); button.setAttribute("aria-current", "page"); }
      const indicator = element("span", "conversation-icon"); indicator.setAttribute("aria-hidden", "true");
      indicator.append(ui.icon(conversation.starred ? "star" : "message"));
      button.append(indicator, element("span", "conversation-title", conversation.title || "未命名会话"));
      button.setAttribute("aria-label", `${conversation.starred ? "已星标，" : ""}${conversation.title || "未命名会话"}`);
      button.title = `${conversation.title || "未命名会话"}${conversation.note ? " · " + conversation.note : ""}${conversation.updatedAt ? " · " + dateLabel(conversation.updatedAt) : ""}`;
      button.addEventListener("click", () => openConversation(conversation.id));
      list.append(button);
    }
    syncControls();
  }

  async function refreshConversations() {
    const request = ++state.conversationListRequest;
    try {
      const data = await bridge.api("/api/conversations");
      if (request !== state.conversationListRequest) return false;
      state.conversations = Array.isArray(data.items) ? data.items : [];
      renderConversations(); feedback("conversation-feedback");
      return true;
    } catch (error) { if (request === state.conversationListRequest) feedback("conversation-feedback", bridge.errorMessage(error), true); return false; }
  }

  function setConversation(conversation, scroll = true, preserveModelDraft = false) {
    if (!conversation?.id || !Array.isArray(conversation.messages)) throw new Error("本地服务未返回完整会话，请刷新列表后重新打开。");
    state.conversation = conversation;
    window.SynthVModels?.setConversation(conversation, { preserveDraft: preserveModelDraft });
    rememberConversation(conversation.id);
    $("chat-title").textContent = conversation.title || "新的调教会话";
    renderHistory(scroll); renderConversations();
  }

  async function openConversation(id, { navigate = true } = {}) {
    if (state.sending || state.actionBusy || state.metadataBusy) return;
    if (navigate && !window.SynthVPages.go("chat")) return;
    $("conversation-editor").hidden = true; $("conversation-delete-confirm").hidden = true;
    $("edit-conversation").setAttribute("aria-expanded", "false");
    const request = ++state.conversationRequest;
    state.loading = true; state.activePreview = ""; syncControls();
    feedback("chat-feedback", "正在读取会话…");
    try {
      const result = await bridge.api(`/api/conversations/${encodeURIComponent(id)}`);
      if (request !== state.conversationRequest) return;
      setConversation(result); feedback("chat-feedback");
      document.body.classList.remove("sidebar-open"); $("toggle-sidebar").setAttribute("aria-expanded", "false");
    } catch (error) { if (request === state.conversationRequest) feedback("chat-feedback", bridge.errorMessage(error), true); }
    finally { if (request === state.conversationRequest) { state.loading = false; syncControls(); } }
  }

  async function newConversation() {
    if (state.sending || state.loading || state.actionBusy || state.metadataBusy) return;
    if (!window.SynthVPages.go("chat")) return;
    $("conversation-editor").hidden = true; $("conversation-delete-confirm").hidden = true;
    $("edit-conversation").setAttribute("aria-expanded", "false");
    state.loading = true; state.activePreview = ""; syncControls();
    try {
      const result = await bridge.api("/api/conversations", {});
      state.attachments = []; $("chat-input").value = "";
      setConversation(result); renderAttachments(); await refreshConversations();
      feedback("chat-feedback"); $("chat-input").focus();
    } catch (error) { feedback("chat-feedback", bridge.errorMessage(error), true); }
    finally { state.loading = false; syncControls(); }
  }

  /** 会话名称、备注和星标只更新本机资料，不提交模型，也不会成为消息正文。 */
  async function updateConversationMetadata(payload) {
    if (!state.conversation || state.metadataBusy || state.sending || state.actionBusy) return;
    const id = state.conversation.id;
    state.metadataBusy = true; syncControls();
    try {
      const result = await bridge.api(`/api/conversations/${encodeURIComponent(id)}/metadata`, payload);
      state.conversations = state.conversations.map((item) => item.id === id ? { ...item, title: result.title, note: result.note, starred: result.starred } : item);
      if (state.conversation?.id === id) setConversation(result, false);
      if (payload.title !== undefined || payload.note !== undefined) {
        $("conversation-editor").hidden = true; $("edit-conversation").setAttribute("aria-expanded", "false");
      }
      await refreshConversations(); feedback("chat-feedback", "会话信息已保存在本机。");
    } catch (error) { feedback("chat-feedback", bridge.errorMessage(error), true); }
    finally { state.metadataBusy = false; syncControls(); }
  }

  /** 确认区明确显示当前目标与删除方式，只有再次点击确认才执行写请求。 */
  function describeConversationDelete() {
    const permanent = $("conversation-delete-mode").value === "permanent";
    const name = state.conversation?.title || "未命名会话";
    $("conversation-delete-description").textContent = permanent
      ? `永久删除“${name}”？会话正文与备注将被删除，无法恢复。关联音频素材需单独管理。`
      : `将“${name}”移到回收站？会话记录会保留，可从回收站恢复。`;
    $("confirm-delete-conversation").textContent = permanent ? "确认永久删除" : "确认移到回收站";
  }

  async function deleteConversation() {
    if (!state.conversation || state.metadataBusy || state.sending || state.actionBusy) return;
    const id = state.conversation.id;
    const permanent = $("conversation-delete-mode").value === "permanent";
    state.metadataBusy = true; syncControls();
    try {
      const result = await bridge.api(`/api/conversations/${encodeURIComponent(id)}/${permanent ? "purge" : "delete"}`, permanent ? { confirm: true } : {});
      if (!result.deleted || (permanent && !result.permanent)) throw new Error("服务未确认删除成功，请刷新会话列表或回收站核实；未自动重试。");
      state.conversations = state.conversations.filter((item) => item.id !== id);
      state.conversation = null; state.activePreview = "";
      window.SynthVModels?.setConversation(null, { preserveDraft: true });
      $("conversation-editor").hidden = true; $("conversation-delete-confirm").hidden = true;
      $("chat-title").textContent = "新的调教会话";
      // 删除会话不清空尚未发送的草稿，下一次发送可创建新会话继续使用。
      try { localStorage.removeItem("lastConversationId"); } catch { /* 存储受限不影响删除结果。 */ }
      renderHistory(); await refreshConversations();
      feedback("chat-feedback", permanent ? "会话已永久删除，无法恢复。当前输入草稿保留。" : "会话已移到回收站，可从左侧回收站恢复。当前输入草稿保留。");
    } catch (error) { feedback("chat-feedback", bridge.errorMessage(error), true); }
    finally { state.metadataBusy = false; syncControls(); }
  }

  function renderMessageAttachment(asset) {
    const card = element("div", "message-audio");
    card.append(decorativeIcon("message-audio-icon", "music"));
    const content = element("div", "message-audio-content");
    content.append(element("strong", "", assetName(asset)));
    if (Number.isFinite(asset.durationSeconds)) content.append(element("span", "", `${number(asset.durationSeconds)} 秒`));
    if (asset.available === false || asset.deleted || asset.permanent) content.append(element("span", "asset-unavailable", asset.permanent ? "音频已永久删除，无法恢复" : asset.deleted ? "音频已移到回收站，恢复后可继续试听" : "音频暂不可用，请检查素材库"));
    const url = localAudioUrl(asset);
    if (url) {
      const audio = element("audio"); audio.controls = true; audio.preload = "none"; audio.src = url;
      audio.setAttribute("aria-label", `播放 ${assetName(asset)}`);
      content.append(audio);
    }
    card.append(content); return card;
  }

  /** 预览信息直接呈现宿主返回的范围，不把模型推测值当作已读取的工程事实。 */
  function renderAction(action) {
    const card = element("article", "assistant-action"); card.dataset.actionId = action.id;
    const heading = element("div", "action-heading");
    const preview = action.preview;
    const isVocalMode = action.kind === "vocalMode" || String(action.parameter || "").startsWith("vocalMode_");
    // 优先使用提案时保存的公开名称，避免声库切换后把旧建议解释成新声库参数。
    const modeName = action.modeName || (isVocalMode ? String(action.parameter).slice("vocalMode_".length) : "");
    const label = action.label || preview?.label || parameterNames[action.parameter] || modeName || action.parameter || "参数建议";
    const unit = action.unit || preview?.unit || parameterUnits[action.parameter] || (isVocalMode ? "百分点" : "参数值");
    const curve = Array.isArray(action.curve) ? action.curve : null;
    const amount = curve ? `曲线 · ${curve.length} 个点` : `${signed(action.delta)} ${unit}`;
    heading.append(element("strong", "", `${label} ${amount}`));
    const status = element("span", "action-status", actionLabels[action.status] || "未知状态");
    status.dataset.state = action.status; heading.append(status); card.append(heading);
    if (action.reason) card.append(element("p", "action-reason", action.reason));
    if (curve || action.renderMode || isVocalMode) {
      const semantics = action.parameter === "pitchCurve" ? `绝对 MIDI 半音值，原生音高曲线` : `相对增量 · ${unit}`;
      card.append(element("p", "action-curve-summary", `${modeName ? `声线：${modeName} · ` : ""}${semantics} · ${window.SynthVCurves.representation(preview?.representation, action.renderMode || preview?.renderMode || "smooth")}`));
    }
    if (preview && typeof preview === "object") {
      const scope = element("dl", "action-scope");
      const fields = [["时间范围", `${number(preview.startSeconds)}–${number(preview.endSeconds)} 秒`], ["选中音符", Number.isFinite(preview.noteCount) ? `${preview.noteCount} 个` : "未返回"], ["控制点", Number.isFinite(preview.pointCount) ? `${preview.pointCount} 个` : "未返回"]];
      for (const [label, value] of fields) { const group = element("div"); group.append(element("dt", "", label), element("dd", "", value)); scope.append(group); }
      card.append(scope);
      if (Number.isFinite(preview.beforePointCount) || Number.isFinite(preview.pointReduction)) card.append(element("p", "action-curve-summary", `${Number.isFinite(preview.beforePointCount) ? `原有 ${preview.beforePointCount} 点` : "原有点数未提供"}${Number.isFinite(preview.pointReduction) ? ` · 精简减少 ${preview.pointReduction} 点` : ""} · ${window.SynthVCurves.representation(preview.representation, preview.renderMode)}`));
      const chart = window.SynthVCurves.createPreview(preview); if (chart) card.append(chart);
      card.append(details("完整预览明细", preview));
    }
    if (action.result) card.append(details("查看真实执行结果", action.result));
    if (action.status === "proposed" || action.status === "previewed") {
      const controls = element("div", "action-controls");
      const previewButton = element("button", "button button-secondary", action.status === "previewed" ? "重新预览当前选区" : "预览当前选区");
      previewButton.type = "button"; previewButton.dataset.actionPreview = action.id;
      previewButton.addEventListener("click", () => executeAction(action.id, "preview"));
      controls.append(previewButton);
      if (action.status === "previewed") {
        const apply = element("button", "button button-primary", "确认应用"); apply.type = "button"; apply.dataset.actionApply = action.id;
        apply.addEventListener("click", () => executeAction(action.id, "apply")); controls.append(apply);
      }
      card.append(controls);
      const explanation = action.status === "proposed" ? "先读取当前选区生成预览；这一步不会修改工程。" : state.activePreview !== action.id ? "此预览目前不可应用。请重新预览，核对当前选区。" : !state.status?.writeEnabled ? "预览尚未写入工程。开启选区写入后，再确认应用。" : "请核对数值和范围，点击“确认应用”才会修改工程。";
      card.append(element("p", "action-help", explanation));
    } else if (action.status === "unknown") {
      card.append(element("p", "action-help warning", "执行结果尚不能确认。请检查 SynthV 当前工程与服务记录，勿重复提交同一修改。"));
    }
    if (state.actionErrors.has(action.id)) card.append(element("p", "inline-feedback error", state.actionErrors.get(action.id)));
    return card;
  }

  /** 等待阶段来自本机任务进度；计时独立更新，在减少动态效果模式下仍可读。 */
  function progressStage(stage) {
    const names = { queued: "请求已排队", preparing: "正在准备请求", selection: "正在读取选区", capturing_selection: "正在读取选区",
      connecting: "正在连接模型服务", requesting: "已请求模型，等待响应", waiting: "等待模型响应", streaming: "正在接收模型响应",
      reasoning: "正在接收模型提供的思考摘要", thinking: "正在等待模型处理", generating: "正在生成回复", validating: "正在校验调教方案",
      parsing: "正在解析调教方案", saving: "正在保存会话", done: "回复处理完成", error: "请求未完成" };
    return names[stage] || (stage ? `处理阶段：${stage}` : "等待本机任务进度");
  }

  function renderJobProgress() {
    if (!state.sending) return;
    const progress = state.jobProgress;
    const elapsed = Math.max(progress.elapsedSeconds || 0, Math.floor((Date.now() - progress.startedAt) / 1000));
    $("chat-job-text").textContent = progressStage(progress.stage);
    $("chat-job-time").textContent = `已等待 ${elapsed} 秒${progress.receivedCharacters > 0 ? ` · 已接收 ${progress.receivedCharacters.toLocaleString("zh-CN")} 字符` : ""}`;
    const history = $("chat-history");
    const nearBottom = history.scrollHeight - history.scrollTop - history.clientHeight < 110;
    let live = $("chat-live-message");
    if (!live) {
      live = element("article", "chat-message message-assistant live-message"); live.id = "chat-live-message";
      const heading = element("header", "message-meta");
      heading.append(decorativeIcon("message-avatar", "wave"), element("strong", "", "调教助手"), element("span", "message-context", "正在等待真实响应"));
      const body = element("div", "message-text"); body.id = "chat-live-text";
      const reasoning = element("details", "message-details live-reasoning"); reasoning.open = true;
      reasoning.append(element("summary", "", "思考摘要 · 仅显示模型提供的内容"));
      const note = element("p", "field-help"); note.id = "chat-live-reasoning-note";
      const pre = element("pre"); pre.id = "chat-live-reasoning";
      reasoning.append(note, pre); live.append(heading, body, reasoning); history.append(live);
    }
    // 后端目前在计划完整前令 text 为空。额外拒绝看起来像原始 JSON 的增量，
    // 避免把结构化工具协议当作聊天正文；最终完整回复仍由会话消息展示。
    const visibleText = typeof progress.text === "string" && !/^[\s]*[\[{]/.test(progress.text) ? progress.text : "";
    const body = $("chat-live-text");
    const display = visibleText || `${progressStage(progress.stage)}。完成校验后会显示正式回复与参数建议。`;
    if (body.textContent !== display) body.textContent = display;
    const hasReasoning = typeof progress.reasoning === "string" && Boolean(progress.reasoning.trim());
    $("chat-live-reasoning-note").textContent = hasReasoning ? "以下为模型 API 实际返回的可展示摘要，不代表工作台已执行任何工程修改。" : progress.reasoningAvailable ? "服务支持返回摘要，目前尚未收到可展示内容。" : "目前未收到可展示的思考摘要；部分模型或平台不提供此内容。工作台不会模拟思考。";
    const reasoning = $("chat-live-reasoning"); reasoning.hidden = !hasReasoning;
    if (reasoning.textContent !== progress.reasoning) reasoning.textContent = progress.reasoning || "";
    if (nearBottom) history.scrollTop = history.scrollHeight;
  }

  function startJobProgress() {
    clearInterval(state.jobTimer);
    // 新会话首条请求开始即移除欢迎区，避免等待回复时同时显示大块欢迎内容。
    welcome.remove();
    state.jobProgress = { startedAt: Date.now(), elapsedSeconds: 0, stage: "preparing", text: "", reasoning: "", reasoningAvailable: false, receivedCharacters: 0 };
    renderJobProgress();
    state.jobTimer = setInterval(renderJobProgress, 1000);
  }
  function receiveJobProgress(job) {
    const progress = job?.progress;
    if (!progress || typeof progress !== "object") return;
    if (typeof progress.stage === "string") state.jobProgress.stage = progress.stage;
    if (Number.isFinite(progress.elapsedSeconds)) state.jobProgress.elapsedSeconds = progress.elapsedSeconds;
    if (typeof progress.reasoning === "string") state.jobProgress.reasoning = progress.reasoning;
    if (typeof progress.text === "string") state.jobProgress.text = progress.text;
    if (typeof progress.reasoningAvailable === "boolean") state.jobProgress.reasoningAvailable = progress.reasoningAvailable;
    if (Number.isFinite(progress.receivedCharacters) && progress.receivedCharacters >= 0) state.jobProgress.receivedCharacters = Math.floor(progress.receivedCharacters);
    renderJobProgress();
  }

  function stopJobProgress() {
    clearInterval(state.jobTimer); state.jobTimer = null;
    $("chat-live-message")?.remove();
  }

  function renderHistory(scroll = false) {
    const history = $("chat-history"); const previousScroll = history.scrollTop;
    history.replaceChildren();
    const messages = state.conversation?.messages || [];
    if (!messages.length && !state.sending) history.append(welcome);
    for (const message of messages) {
      const role = ["user", "assistant", "error"].includes(message.role) ? message.role : "assistant";
      const article = element("article", `chat-message message-${role}`);
      const meta = element("header", "message-meta");
      meta.append(role === "assistant" ? decorativeIcon("message-avatar", "wave") : element("span", "message-avatar", role === "user" ? "你" : "!"));
      meta.append(element("strong", "", role === "user" ? "你" : role === "error" ? "服务消息" : "调教助手"));
      if (message.model && role !== "user") meta.append(element("span", "message-model", message.model));
      if (message.createdAt) meta.append(element("time", "", dateLabel(message.createdAt)));
      article.append(meta);
      if (message.text) article.append(element("div", "message-text", message.text));
      if (typeof message.reasoningSummary === "string" && message.reasoningSummary.trim()) {
        const summary = element("details", "message-details reasoning-summary");
        summary.append(element("summary", "", "思考摘要 · 模型实际返回"), element("pre", "", message.reasoningSummary)); article.append(summary);
      } else if (role === "assistant" || role === "error") article.append(element("p", "message-context", "本条回复未提供可展示的思考摘要。"));
      if (role === "user") {
        const count = Array.isArray(message.attachments) ? message.attachments.length : 0;
        article.append(element("p", "message-context", `${count ? `已发送 ${count} 段所选音频` : "未发送音频"}${message.selection ? " · 含选区上下文" : " · 无选区上下文"}`));
      } else if (message.inputMode) {
        article.append(element("p", "message-context", message.inputMode === "audio" ? "本次请求包含音频附件；回复内容由模型返回。" : "文字与工程信息回复；本次未传入音频。"));
      }
      if (Array.isArray(message.attachments)) for (const asset of message.attachments) article.append(renderMessageAttachment(asset));
      if (message.selection) article.append(details("发送时的选区摘要", message.selection));
      const actions = Array.isArray(message.actions) ? message.actions : [];
      if (actions.length) {
        const group = element("section", "message-actions");
        group.setAttribute("aria-label", "待确认的参数建议");
        group.append(element("p", "action-group-note", "逐项预览与确认 · 仅最新预览有效 · 恢复仅针对最近一次修改"));
        for (const action of actions) group.append(renderAction(action));
        if (actions.some((action) => action.status === "applied")) {
          const restore = element("button", "button button-quiet chat-restore", "恢复最近一次修改");
          restore.type = "button"; restore.dataset.chatRestore = "true";
          restore.addEventListener("click", restoreLatest); group.append(restore);
        }
        article.append(group);
      }
      history.append(article);
    }
    if (state.sending) renderJobProgress();
    syncControls();
    history.scrollTop = scroll ? history.scrollHeight : previousScroll;
  }

  function findAction(id) {
    for (const message of state.conversation?.messages || []) {
      const action = message.actions?.find((item) => item.id === id);
      if (action) return action;
    }
    return null;
  }

  /**
   * 每条建议都经过服务端单独预览与单独应用，没有“全部应用”路径。
   * 发出应用请求前即撤销本地确认资格；网络结果不明时禁止同一请求重放。
   */
  async function executeAction(id, operation) {
    if (state.actionBusy || state.sending || state.manualBusy) return;
    const action = findAction(id);
    if (!action || !["proposed", "previewed"].includes(action.status)) return;
    if (operation === "apply" && (state.activePreview !== id || !state.status?.writeEnabled)) return;
    state.actionBusy = id; state.activePreview = ""; state.actionErrors.delete(id);
    bridge.clearManualPreview(); bridge.setAssistantBusy(true);
    renderHistory(); feedback("chat-feedback", operation === "preview" ? "正在读取当前选区并生成预览，不会修改工程…" : "正在应用你已确认的单项修改…");
    try {
      const response = await bridge.api(`/api/assistant/actions/${encodeURIComponent(id)}/${operation}`, {});
      const updated = response.action || response;
      if (updated.id !== id || !updated.status) throw new Error("服务未返回可核实的建议状态，请读取会话和工程确认结果。");
      Object.assign(action, updated);
      if (operation === "preview" && updated.status === "previewed" && updated.preview?.previewId) state.activePreview = id;
      feedback("chat-feedback", operation === "preview" ? state.activePreview ? "预览已生成，尚未修改工程。核对建议卡中的数值与范围后再确认。" : "服务已返回预览状态，请查看建议卡。" : updated.status === "applied" ? "这项修改已应用。可在右侧录制 B，或恢复最近一次修改。" : "服务已返回执行状态，请查看建议卡中的真实结果。");
    } catch (error) {
      if (operation === "apply") action.status = "unknown";
      state.actionErrors.set(id, bridge.errorMessage(error));
      feedback("chat-feedback", bridge.errorMessage(error), true);
    } finally {
      state.actionBusy = ""; bridge.setAssistantBusy(false); renderHistory();
    }
  }

  async function restoreLatest() {
    if (state.actionBusy || state.manualBusy || state.sending || !state.status?.writeEnabled) return;
    state.actionBusy = "restore"; state.activePreview = "";
    bridge.clearManualPreview(); bridge.setAssistantBusy(true); renderHistory();
    feedback("chat-feedback", "正在恢复最近一次修改…");
    try {
      const result = await bridge.api("/api/restore", {});
      feedback("chat-feedback", typeof result.summary === "string" ? result.summary : "恢复请求已完成，请查看返回结果。");
      const box = element("div", "chat-operation-result");
      box.append(element("strong", "", "恢复结果"), details("查看服务实际返回", result)); $("chat-history").append(box);
      $("chat-history").scrollTop = $("chat-history").scrollHeight;
    } catch (error) { feedback("chat-feedback", bridge.errorMessage(error), true); }
    finally { state.actionBusy = ""; bridge.setAssistantBusy(false); syncControls(); }
  }

  function renderAttachments() {
    const container = $("composer-attachments"); container.replaceChildren();
    for (const asset of state.attachments) {
      const chip = element("span", "attachment-chip");
      chip.append(decorativeIcon("", "music"), element("span", "attachment-name", assetName(asset)));
      const remove = ui.iconButton("close", `移除音频附件 ${assetName(asset)}`, { className: "attachment-remove" });
      remove.addEventListener("click", () => { state.attachments = state.attachments.filter((item) => assetKey(item) !== assetKey(asset)); renderAttachments(); });
      chip.append(remove); container.append(chip);
    }
    syncControls();
  }

  async function sendMessage(event) {
    event.preventDefault();
    if ($("send-message").disabled) return;
    const text = $("chat-input").value.trim();
    if (!text) return;
    if (text.length > 4000) { feedback("chat-feedback", "每条消息最多 4000 个字符，请缩短后再发送。", true); return; }
    const payload = { text, includeSelection: $("include-selection").checked, attachments: state.attachments.map(({ kind, id }) => ({ kind, id })),
      modelOptions: window.SynthVModels.snapshot() };
    state.sending = true; state.activePreview = ""; syncControls();
    feedback("chat-feedback");
    startJobProgress();
    try {
      if (!state.conversation) setConversation(await bridge.api("/api/conversations", {}), true, true);
      const id = state.conversation.id;
      await window.SynthVModels.ensureSaved();
      const job = await bridge.api(`/api/conversations/${encodeURIComponent(id)}/messages`, payload);
      state.jobProgress.stage = "queued"; renderJobProgress();
      try { setConversation(await bridge.api(`/api/conversations/${encodeURIComponent(id)}`)); }
      catch { /* 中途读取失败不重发消息，继续等待已创建的后台任务。 */ }
      const result = await bridge.waitForJob(job.jobId, receiveJobProgress);
      setConversation(result?.conversation || result);
      // 入队不代表消息已通过选区校验或已持久化。仅在最终完整会话返回后清空，
      // 队列、轮询和选区校验失败都保留原草稿与附件，且不会自动重新发送。
      $("chat-input").value = ""; state.attachments = []; renderAttachments();
      await refreshConversations();
    } catch (error) {
      feedback("chat-feedback", bridge.errorMessage(error), true);
      // 失败消息若已被服务记录，应显示真实记录，而不是合成一条助手回复。
      if (state.conversation?.id) {
        try { setConversation(await bridge.api(`/api/conversations/${encodeURIComponent(state.conversation.id)}`)); }
        catch { /* 原始失败信息继续保留。 */ }
      }
    } finally { state.sending = false; stopJobProgress(); syncControls(); syncLibrary(); if (window.SynthVPages.current === "chat") $("chat-input").focus(); }
  }

  function syncLibrary() {
    const busy = state.libraryLoading || state.uploading || Boolean(state.assetBusy);
    $("choose-audio-file").disabled = busy;
    $("reload-audio-library").disabled = busy;
    $("use-audio-attachments").disabled = busy || state.sending;
    $("library-selection-count").textContent = `已选 ${state.libraryDraft.size} / 2 段`;
    $("audio-library-items").setAttribute("aria-busy", busy ? "true" : "false");
    document.querySelectorAll(".library-item-choice input").forEach((input) => { input.disabled = busy || (!input.checked && state.libraryDraft.size >= 2); });
    document.querySelectorAll(".asset-toolbar button, .asset-meta-editor button, .asset-meta-editor input, .asset-meta-editor textarea, .library-item .inline-confirm button, .library-item .inline-confirm select").forEach((control) => { control.disabled = busy || state.sending; });
  }

  function normalizeAsset(item, kind = item.kind) {
    const actualDuration = kind === "recording" && Number.isFinite(item.analysis?.duration) && item.analysis.duration > 0
      ? item.analysis.duration : item.durationSeconds;
    return { ...item, kind, durationSeconds: actualDuration };
  }

  function renderLibrary() {
    const container = $("audio-library-items"); container.replaceChildren();
    $("library-count").textContent = String(state.assets.length);
    if (!state.assets.length) container.append(element("p", "library-empty", "还没有音频素材。上传参考音频，或在录音页面采集 SynthV。"));
    for (const asset of [...state.assets].sort((a, b) => Number(Boolean(b.starred)) - Number(Boolean(a.starred)))) {
      const key = assetKey(asset);
      const row = element("article", "library-item");
      const label = element("label", "library-item-choice");
      const input = element("input"); input.type = "checkbox"; input.checked = state.libraryDraft.has(key);
      input.addEventListener("change", () => {
        if (input.checked && state.libraryDraft.size < 2) state.libraryDraft.add(key);
        else { state.libraryDraft.delete(key); input.checked = false; }
        syncLibrary();
      });
      const content = element("span", "library-item-info");
      content.append(element("strong", "", assetName(asset)), element("small", "", `${asset.kind === "upload" ? "上传素材" : "工程录音"} · ${number(asset.durationSeconds)} 秒${asset.createdAt ? " · " + dateLabel(asset.createdAt) : ""}`));
      if (asset.name && asset.name !== asset.label) content.append(element("small", "", `原文件：${asset.name}`));
      label.append(input, decorativeIcon("library-audio-icon", "music"), content); row.append(label);
      if (asset.note) row.append(element("p", "asset-note", asset.note));
      const url = localAudioUrl(asset);
      if (url) { const audio = element("audio"); audio.controls = true; audio.preload = "none"; audio.src = url; audio.setAttribute("aria-label", `试听 ${assetName(asset)}`); row.append(audio); }
      if (Array.isArray(asset.analysis?.warnings) && asset.analysis.warnings.length) row.append(element("p", "field-help warning", asset.analysis.warnings.join(" ")));
      const toolbar = element("div", "asset-toolbar");
      // 管理操作统一使用图标组件；完整对象名称保留在 title 和 aria-label 中。
      const star = ui.iconButton("star", `${asset.starred ? "取消星标" : "星标"}“${assetName(asset)}”`, { id: `asset-star-${asset.kind}-${asset.id}`, pressed: Boolean(asset.starred), className: `star-button${asset.starred ? " is-starred" : ""}` });
      star.addEventListener("click", () => updateAsset(asset, { starred: !asset.starred }, star.id));
      const edit = ui.iconButton("note", `编辑“${assetName(asset)}”的名称与备注`, { id: `asset-edit-${asset.kind}-${asset.id}` });
      // 列表重绘会替换原按钮，按素材稳定标识将焦点移到新建的名称输入框。
      edit.addEventListener("click", () => { state.assetEditor = { key, label: assetName(asset), note: asset.note || "" }; state.assetDelete = ""; renderLibrary(); $(`asset-name-${asset.kind}-${asset.id}`)?.focus(); });
      const remove = ui.iconButton("trash", `删除“${assetName(asset)}”`, { id: `asset-delete-${asset.kind}-${asset.id}` });
      remove.addEventListener("click", () => { state.assetDelete = key; state.assetDeleteMode = "trash"; state.assetEditor = null; renderLibrary(); $("audio-library-items").querySelector(".inline-confirm select")?.focus(); });
      toolbar.append(star, edit, remove); row.append(toolbar);
      if (state.assetEditor?.key === key) {
        const form = element("form", "metadata-form asset-meta-editor");
        const nameLabel = element("label", "", "素材名称"); const name = element("input"); name.type = "text"; name.maxLength = 100; name.required = true; name.value = state.assetEditor.label;
        name.id = `asset-name-${asset.kind}-${asset.id}`; nameLabel.htmlFor = name.id;
        name.addEventListener("input", () => { if (state.assetEditor?.key === key) state.assetEditor.label = name.value; }); nameLabel.append(name);
        const noteLabel = element("label", "", "备注 · 仅本机保存"); const note = element("textarea"); note.rows = 3; note.maxLength = 2000; note.value = state.assetEditor.note;
        note.addEventListener("input", () => { if (state.assetEditor?.key === key) state.assetEditor.note = note.value; }); noteLabel.append(note);
        const actions = element("div", "metadata-actions"); const save = element("button", "button button-secondary", "保存信息"); save.type = "submit";
        const cancel = element("button", "button button-quiet", "取消"); cancel.type = "button";
        cancel.addEventListener("click", () => { state.assetEditor = null; renderLibrary(); $(edit.id)?.focus(); });
        actions.append(save, cancel); form.append(nameLabel, noteLabel, actions);
        form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) updateAsset(asset, { label: name.value.trim(), note: note.value }, edit.id); }); row.append(form);
      }
      if (state.assetDelete === key) {
        // 删除控件共用网格布局；显式关联标签，便于键盘与辅助技术定位同一选项。
        const confirm = element("div", "inline-confirm delete-confirm");
        const modeLabel = element("label", "", "删除方式"); const mode = element("select");
        mode.id = `asset-delete-mode-${asset.id}`; modeLabel.htmlFor = mode.id;
        mode.add(new Option("移到回收站", "trash")); mode.add(new Option("永久删除", "permanent"));
        mode.value = state.assetDeleteMode; modeLabel.append(mode);
        const description = element("p"); description.id = `asset-delete-description-${asset.kind}-${asset.id}`; description.setAttribute("aria-live", "polite");
        const actions = element("div", "confirmation-actions"); const yes = element("button", "button button-danger-quiet"); yes.type = "button";
        // 确认按钮与选择框均关联完整目标说明，键盘进入时可读到不可恢复提示。
        mode.setAttribute("aria-describedby", description.id); yes.setAttribute("aria-describedby", description.id);
        // 切换删除方式只更新确认文案，不触发删除，也不会重新创建试听播放器。
        const describe = () => {
          const permanent = mode.value === "permanent";
          description.textContent = permanent ? `永久删除“${assetName(asset)}”？原始音频和素材备注将被删除，无法恢复。历史消息将不再提供试听。` : `将“${assetName(asset)}”移到回收站？音频可恢复；本次消息附件会移除它。`;
          yes.textContent = permanent ? "确认永久删除" : "确认移到回收站";
        };
        mode.addEventListener("change", () => { state.assetDeleteMode = mode.value; describe(); }); describe();
        yes.addEventListener("click", () => deleteAsset(asset, mode.value === "permanent"));
        const no = element("button", "button button-quiet", "取消"); no.type = "button"; no.addEventListener("click", () => { state.assetDelete = ""; renderLibrary(); $(remove.id)?.focus(); });
        actions.append(yes, no); confirm.append(modeLabel, description, actions); row.append(confirm);
      }
      container.append(row);
    }
    syncLibrary();
  }

  /** 更改素材资料不上传音频给模型；服务返回后再更新卡片与当前附件名称。 */
  async function updateAsset(asset, payload, focusTargetId = "") {
    if (state.assetBusy || state.libraryLoading || state.sending) return;
    // 只有显式用户操作传入焦点目标；后台刷新不恢复焦点，也不打断正在阅读的控件。
    const originalFocus = document.activeElement;
    let rebuilt = false;
    let focusMoved = false;
    const trackFocus = (event) => { if (event.target !== originalFocus && event.target !== document.body) focusMoved = true; };
    if (focusTargetId) document.addEventListener("focusin", trackFocus);
    state.assetBusy = assetKey(asset); syncLibrary();
    try {
      const result = await bridge.api(`/api/assets/${encodeURIComponent(asset.kind)}/${encodeURIComponent(asset.id)}/metadata`, payload);
      const updated = normalizeAsset({ ...asset, ...result }, asset.kind);
      state.assets = state.assets.map((item) => assetKey(item) === assetKey(asset) ? updated : item);
      state.attachments = state.attachments.map((item) => assetKey(item) === assetKey(asset) ? updated : item);
      if (payload.label !== undefined || payload.note !== undefined) state.assetEditor = null;
      renderAttachments(); renderLibrary(); rebuilt = true;
      if (asset.kind === "recording") await bridge.refreshRecordings();
      feedback("library-feedback", "素材信息已保存在本机，未发送给模型。");
    } catch (error) { feedback("library-feedback", bridge.errorMessage(error), true); }
    finally {
      if (focusTargetId) document.removeEventListener("focusin", trackFocus);
      state.assetBusy = ""; syncLibrary();
      // 列表按钮须先解除禁用才可聚焦；若用户已经离页或选择其他控件，不抢回焦点。
      const active = document.activeElement;
      const stayedAtOperation = !active || active === document.body || active === originalFocus;
      if (focusTargetId && !focusMoved && window.SynthVPages.current === "library" && stayedAtOperation) {
        const target = rebuilt ? $(focusTargetId) : originalFocus?.isConnected ? originalFocus : $(focusTargetId);
        if (target && !target.disabled) target.focus();
      }
    }
  }

  /** 已确认删除后同步当前草稿和历史附件；永久删除与可恢复删除使用不同提示。 */
  function removeLocalAsset(asset, permanent = false) {
    const key = assetKey(asset);
    // 使删除前发出的列表读取失效，防止迟到响应把已删除素材重新放回可用列表。
    state.libraryRequest++; state.libraryLoading = false;
    state.assets = state.assets.filter((item) => assetKey(item) !== key);
    state.attachments = state.attachments.filter((item) => assetKey(item) !== key);
    state.libraryDraft.delete(key);
    for (const message of state.conversation?.messages || []) for (const item of message.attachments || []) {
      if (assetKey(item) === key) { item.available = false; item.deleted = true; item.permanent = permanent; }
    }
    renderAttachments(); renderLibrary(); renderHistory(false);
  }

  async function deleteAsset(asset, permanent = false) {
    if (state.assetBusy || state.libraryLoading || state.sending) return;
    const key = assetKey(asset); state.assetBusy = key; syncLibrary();
    try {
      const result = await bridge.api(`/api/assets/${encodeURIComponent(asset.kind)}/${encodeURIComponent(asset.id)}/${permanent ? "purge" : "delete"}`, permanent ? { confirm: true } : {});
      if (!result.deleted || (permanent && !result.permanent)) throw new Error("服务未确认删除成功，请刷新素材库或回收站核实；未自动重试。");
      state.assetDelete = ""; state.assetEditor = null;
      removeLocalAsset(asset, permanent);
      // 先停用所有相关播放器；后续列表刷新失败也不能继续使用已删除的缓存音频。
      window.dispatchEvent(new CustomEvent("synthv:assets-changed", { detail: { kind: asset.kind, id: asset.id, deleted: true, permanent } }));
      let refreshWarning = "";
      if (asset.kind === "recording") {
        try { await bridge.refreshRecordings(); }
        catch { refreshWarning = "录音列表刷新失败，请稍后手动刷新；删除请求不会重发。"; }
      }
      feedback("library-feedback", `${permanent ? "音频已永久删除，无法恢复。" : "音频已移到回收站，可从左侧回收站恢复。"}${refreshWarning}`, Boolean(refreshWarning));
    } catch (error) { feedback("library-feedback", bridge.errorMessage(error), true); }
    finally { state.assetBusy = ""; syncLibrary(); }
  }

  async function loadLibrary() {
    if (state.libraryLoading || state.assetBusy) return;
    const request = ++state.libraryRequest; state.libraryLoading = true; syncLibrary();
    feedback("library-feedback", "正在读取本机音频素材…");
    try {
      const results = await Promise.allSettled([bridge.api("/api/uploads"), bridge.api("/api/recordings")]);
      if (request !== state.libraryRequest) return;
      const assets = [], errors = [];
      for (let index = 0; index < results.length; index++) {
        const result = results[index];
        if (result.status === "fulfilled") {
          for (const item of result.value.items || []) {
            assets.push(normalizeAsset(item, index === 0 ? "upload" : "recording"));
          }
        }
        else errors.push(bridge.errorMessage(result.reason));
      }
      state.assets = assets;
      state.libraryDraft = new Set([...state.libraryDraft].filter((key) => assets.some((item) => assetKey(item) === key)));
      renderLibrary(); feedback("library-feedback", errors.join(" "), Boolean(errors.length));
    } finally { if (request === state.libraryRequest) { state.libraryLoading = false; syncLibrary(); } }
  }

  function openLibrary(opener) {
    state.libraryOpener = opener;
    if (window.SynthVPages.current === "library") return;
    window.SynthVPages.go("library");
  }
  function closeLibrary() { window.SynthVPages.go("chat"); }

  /** 原始文件只提交到同源本地上传接口；不会在上传步骤调用任何模型。 */
  async function uploadAudio(file) {
    if (!file || state.uploading) return;
    if (!/\.(wav|mp3)$/i.test(file.name)) { feedback("library-feedback", "请选择 WAV 或 MP3 音频文件。", true); return; }
    if (!file.size || file.size > MAX_FILE_BYTES) { feedback("library-feedback", "文件不能为空，且单个文件不能超过 12 MB。", true); return; }
    state.uploading = true; syncLibrary(); feedback("library-feedback", "正在上传到本机并分析音频；不会发送给模型…");
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 120000);
    try {
      const bootstrap = await bridge.api("/api/bootstrap");
      if (!bootstrap.token) throw new Error("本地服务会话尚未准备好，请刷新后重试。");
      const response = await fetch("/api/uploads", { method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "X-SV-Token": bootstrap.token, "X-File-Name": encodeURIComponent(file.name), "Content-Type": "application/octet-stream", Accept: "application/json" }, body: file });
      let data;
      try { data = await response.json(); } catch { throw new Error("上传服务未返回有效结果，请刷新素材列表确认是否已保存。"); }
      if (!response.ok || data.ok === false) throw new Error(bridge.errorMessage(data));
      await loadLibrary();
      feedback("library-feedback", `“${assetName(data)}”已保存到本机。勾选素材并加入消息后，点击发送才会交给模型。`);
    } catch (error) { feedback("library-feedback", error.name === "AbortError" ? "上传等待超时，请刷新素材列表确认结果；未自动重试。" : bridge.errorMessage(error), true); }
    finally { clearTimeout(timeout); state.uploading = false; $("audio-file-input").value = ""; syncLibrary(); }
  }

  /** 工具面板使用标准 tab 键盘操作，隐藏面板不会参与焦点导航。 */
  function selectInspectorTab(name, focus = false) {
    for (const tab of ["project", "listen"]) {
      const selected = tab === name;
      $("tab-" + tab).setAttribute("aria-selected", String(selected));
      $("tab-" + tab).tabIndex = selected ? 0 : -1;
      $("pane-" + tab).hidden = !selected;
    }
    if (focus) $("tab-" + name).focus();
    // 波形容器从隐藏恢复时重绘，保持真实波形尺寸正确。
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }

  $("chat-form").addEventListener("submit", sendMessage);
  $("chat-input").addEventListener("input", syncControls);
  $("chat-input").addEventListener("keydown", (event) => {
    // 中文输入法正在上屏时 Enter 由输入法处理，不能误发半句消息。
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); if (!$("send-message").disabled) $("chat-form").requestSubmit(); }
  });
  $("include-selection").addEventListener("change", syncControls);
  $("new-conversation").addEventListener("click", newConversation);
  $("refresh-conversations").addEventListener("click", refreshConversations);
  $("star-conversation").addEventListener("click", () => updateConversationMetadata({ starred: !state.conversation?.starred }));
  $("edit-conversation").addEventListener("click", () => {
    if (!state.conversation || state.metadataBusy) return;
    const opening = $("conversation-editor").hidden;
    $("conversation-editor").hidden = !opening; $("conversation-delete-confirm").hidden = true;
    $("edit-conversation").setAttribute("aria-expanded", String(opening));
    if (opening) {
      $("conversation-title-input").value = state.conversation.title || "新的调教会话";
      $("conversation-note-input").value = state.conversation.note || "";
      $("conversation-title-input").focus();
    }
  });
  $("cancel-conversation-metadata").addEventListener("click", () => { $("conversation-editor").hidden = true; $("edit-conversation").setAttribute("aria-expanded", "false"); $("edit-conversation").focus(); });
  $("conversation-metadata-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if ($("conversation-metadata-form").reportValidity()) updateConversationMetadata({ title: $("conversation-title-input").value.trim(), note: $("conversation-note-input").value });
  });
  $("delete-conversation").addEventListener("click", () => {
    if (!state.conversation || state.metadataBusy) return;
    $("conversation-editor").hidden = true; $("edit-conversation").setAttribute("aria-expanded", "false");
    $("conversation-delete-mode").value = "trash"; describeConversationDelete();
    $("conversation-delete-confirm").hidden = false; $("conversation-delete-mode").focus();
  });
  $("conversation-delete-mode").addEventListener("change", describeConversationDelete);
  // 页面内确认区不使用模态对话框，因此显式关联说明并在取消后归还入口焦点。
  for (const id of ["conversation-delete-mode", "confirm-delete-conversation"]) $(id).setAttribute("aria-describedby", "conversation-delete-description");
  $("cancel-delete-conversation").addEventListener("click", () => { $("conversation-delete-confirm").hidden = true; $("delete-conversation").focus(); });
  $("confirm-delete-conversation").addEventListener("click", deleteConversation);
  for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => {
    $("chat-input").value = button.dataset.prompt; $("chat-input").focus(); syncControls();
    if (button.dataset.prompt.includes("两段音频")) openLibrary($("attach-audio"));
  });
  for (const id of ["open-audio-library", "attach-audio"]) $(id).addEventListener("click", (event) => openLibrary(event.currentTarget));
  for (const id of ["close-audio-library", "cancel-audio-library"]) $(id).addEventListener("click", closeLibrary);
  window.addEventListener("synthv:page-change", (event) => {
    if (event.detail.to === "library") {
      state.libraryDraft = new Set(state.attachments.map(assetKey));
      renderLibrary(); loadLibrary();
    }
    if (event.detail.from === "library") {
      // 离开素材主页面停止试听，未点“使用所选素材”的勾选不会自动成为附件。
      $("audio-library-dialog").querySelectorAll("audio").forEach((audio) => audio.pause());
      $("audio-file-input").value = "";
    }
  });
  $("reload-audio-library").addEventListener("click", loadLibrary);
  $("choose-audio-file").addEventListener("click", () => $("audio-file-input").click());
  $("audio-file-input").addEventListener("change", (event) => uploadAudio(event.target.files[0]));
  $("use-audio-attachments").addEventListener("click", () => {
    state.attachments = state.assets.filter((asset) => state.libraryDraft.has(assetKey(asset))).slice(0, 2);
    renderAttachments(); closeLibrary(); $("chat-input").focus();
  });
  // 任意一个本地播放器开始播放时暂停其他播放器，避免并行播放混淆 A/B 判断。
  document.addEventListener("play", (event) => {
    if (event.target instanceof HTMLAudioElement) document.querySelectorAll("audio").forEach((audio) => { if (audio !== event.target) audio.pause(); });
  }, true);
  for (const name of ["project", "listen"]) {
    $("tab-" + name).addEventListener("click", () => selectInspectorTab(name));
    $("tab-" + name).addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); selectInspectorTab(event.key === "Home" ? "project" : event.key === "End" ? "listen" : name === "project" ? "listen" : "project", true); }
    });
  }
  $("toggle-sidebar").addEventListener("click", () => { const open = document.body.classList.toggle("sidebar-open"); $("toggle-sidebar").setAttribute("aria-expanded", String(open)); });
  $("toggle-inspector").addEventListener("click", () => {
    const compact = matchMedia("(max-width: 1100px)").matches;
    const open = compact ? document.body.classList.toggle("inspector-open") : !document.body.classList.toggle("inspector-hidden");
    $("toggle-inspector").setAttribute("aria-expanded", String(open)); window.dispatchEvent(new Event("resize"));
  });
  // 桌面常驻面板切换为窄屏抽屉时，CSS 可见性会变化；同步读屏状态而不主动打开抽屉。
  matchMedia("(max-width: 1100px)").addEventListener("change", (event) => {
    const open = event.matches ? document.body.classList.contains("inspector-open") : !document.body.classList.contains("inspector-hidden");
    $("toggle-inspector").setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      const focusedInspector = $("manual-inspector").contains(document.activeElement);
      const focusedSidebar = document.querySelector(".app-sidebar").contains(document.activeElement);
      document.body.classList.remove("sidebar-open", "inspector-open");
      $("toggle-sidebar").setAttribute("aria-expanded", "false");
      if (matchMedia("(max-width: 1100px)").matches) $("toggle-inspector").setAttribute("aria-expanded", "false");
      // 抽屉隐藏后将焦点交还入口，避免键盘焦点留在不可见控件中。
      if (focusedInspector && matchMedia("(max-width: 1100px)").matches) $("toggle-inspector").focus();
      else if (focusedSidebar && matchMedia("(max-width: 760px)").matches) $("toggle-sidebar").focus();
    }
  });
  window.addEventListener("synthv:state", (event) => {
    const hadWrite = Boolean(state.status?.writeEnabled);
    const hadPreview = Boolean(state.activePreview);
    // 保存公开状态的独立快照，避免另一模块原地更新 writeEnabled 后无法识别变化。
    state.status = event.detail.status ? structuredClone(event.detail.status) : null;
    state.manualBusy = event.detail.manualBusy; state.settingsSaving = event.detail.settingsSaving;
    if (!state.status?.bridge?.connected) state.activePreview = "";
    if ((hadWrite !== Boolean(state.status?.writeEnabled) || (hadPreview && !state.activePreview)) && state.conversation?.messages?.length) renderHistory();
    else syncControls();
  });
  window.addEventListener("synthv:model-options", (event) => {
    state.modelState = event.detail;
    syncControls();
  });
  window.addEventListener("synthv:model-options-saved", (event) => {
    // 选项保存不重绘聊天正文，不覆盖用户正在编辑的文字或附件。
    if (event.detail?.id === state.conversation?.id) state.conversation.modelOptions = event.detail.modelOptions;
  });
  window.addEventListener("synthv:preview-invalidated", () => { if (state.activePreview) { state.activePreview = ""; renderHistory(); } });
  window.addEventListener("synthv:restored", (event) => { state.activePreview = ""; renderHistory(); feedback("chat-feedback", event.detail.summary || "手动工具已返回恢复结果，请查看右侧操作信息。"); });
  window.addEventListener("synthv:recordings", () => { if (window.SynthVPages.current === "library" && !state.uploading) loadLibrary(); });

  window.addEventListener("synthv:assets-changed", async (event) => {
    // 回收站永久删除也须移除草稿附件、停用历史试听，不能等待重新打开会话。
    if (event.detail?.deleted && !state.assetBusy) removeLocalAsset(event.detail, Boolean(event.detail.permanent));
    if (!state.uploading && !state.assetBusy) loadLibrary();
    // 恢复音频后重新读取持久消息中的 available 标记，恢复真实播放入口。
    const id = state.conversation?.id;
    if (event.detail?.restored && id && !state.sending) {
      try {
        const result = await bridge.api(`/api/conversations/${encodeURIComponent(id)}`);
        if (state.conversation?.id === id && !state.sending) setConversation(result, false);
      } catch (error) { feedback("chat-feedback", bridge.errorMessage(error), true); }
    }
  });

  window.SynthVChat = Object.freeze({
    refreshConversations,
    attachAsset: (asset) => {
      if (state.sending || asset.available === false || asset.deleted || asset.permanent) return false;
      const exists = state.attachments.some((item) => assetKey(item) === assetKey(asset));
      if (!exists && state.attachments.length >= 2) return false;
      if (!window.SynthVPages.go("chat")) return false;
      if (!exists) state.attachments.push(normalizeAsset(asset));
      renderAttachments(); $("chat-input").focus();
      feedback("chat-feedback", "音频已加入当前消息附件，尚未发送给模型。");
      return true;
    },
  });

  async function initialize() {
    $("toggle-inspector").setAttribute("aria-expanded", String(!matchMedia("(max-width: 1100px)").matches));
    syncControls();
    try {
      if (await refreshConversations()) {
        const lastId = rememberedConversation();
        const conversation = state.conversations.find((item) => item.id === lastId) || state.conversations[0];
        if (conversation) await openConversation(conversation.id, { navigate: false });
      }
    } finally { window.SynthVModels.finishInitialization(); }
    // 素材列表读取不提交音频，也不触发录制，便于左侧显示真实数量。
    loadLibrary();
  }
  initialize();
})();
