"use strict";

/**
 * 会话级模型选择：平台目录、手动模型 ID、能力提示和推理强度。
 * 平台与能力请求只访问本机；只有用户点击“刷新模型列表”才调用供应商目录。
 * 此模块不读取密钥，也不把选择存入浏览器存储；目录缓存与会话选项由本机后端持久化。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.SynthVWorkbench;
  const DEFAULTS = { platformId: "default", model: "", reasoningEffort: "default" };
  const state = {
    platforms: [], options: { ...DEFAULTS }, conversationId: null, defaultPlatformId: "default",
    listLoading: false, listRequest: 0, capabilities: null, capabilityLoading: false,
    capabilityRequest: 0, capabilityTimer: null, catalogLoading: false, catalogs: new Map(),
    dirty: false, saving: false, savePromise: null, interactionBusy: false, initializing: true,
    platformGenerations: new Map(), feedback: "", feedbackError: false, feedbackTimer: null,
    catalogRequests: new Map(), catalogMetadata: new Map(), refreshingPlatforms: new Set(),
  };
  function options(value = {}) {
    return { platformId: typeof value.platformId === "string" && value.platformId ? value.platformId : "default",
      model: typeof value.model === "string" ? value.model.trim() : "",
      reasoningEffort: typeof value.reasoningEffort === "string" && value.reasoningEffort ? value.reasoningEffort : "default" };
  }
  function selectedPlatform() { return state.platforms.find((platform) => platform.id === state.options.platformId); }
  function reasoningOptions() {
    const values = [{ value: "default", label: "默认" }];
    for (const item of state.capabilities?.reasoning?.options || []) {
      if (typeof item.value === "string" && item.value !== "default" && !values.some((entry) => entry.value === item.value)) {
        const label = `${item.label || item.value}${state.capabilities?.reasoning?.supported === null ? " · 平台需支持" : ""}`;
        values.push({ value: item.value, label });
      }
    }
    return values;
  }
  function reasoningValid() { return state.options.reasoningEffort === "default" || reasoningOptions().some((item) => item.value === state.options.reasoningEffort); }
  function publicState() {
    const platform = selectedPlatform();
    return { configured: Boolean(platform?.configured), busy: state.initializing || state.listLoading || state.saving || state.capabilityLoading,
      valid: reasoningValid(), platformName: platform?.name || "平台不可用", modelLabel: state.options.model || platform?.model || "未设置模型",
      audioInput: state.capabilities?.audioInput || "unknown", options: { ...state.options } };
  }

  /** 更新控件不重新赋值模型输入框，避免轮询、切页和能力响应覆盖正在编辑的草稿。 */
  function render() {
    const platform = selectedPlatform();
    const busy = state.initializing || state.interactionBusy || state.saving || state.listLoading;
    $("chat-platform").disabled = busy;
    $("chat-model").disabled = busy;
    $("chat-model").maxLength = platform?.provider === "gemini" ? 128 : 200;
    $("chat-model").placeholder = platform?.model ? `平台默认：${platform.model}` : "留空使用平台默认模型";
    $("chat-reasoning").disabled = busy || state.capabilityLoading;
    $("refresh-chat-models").disabled = busy || state.catalogLoading || !platform?.configured;
    // 刷新状态只更新可读名称与忙碌属性，保持共享 SVG，不以省略号替换整个按钮。
    window.SynthVUI.setIconButton($("refresh-chat-models"), "refresh", state.catalogLoading ? "正在刷新模型列表" : "从当前平台刷新模型列表（会访问供应商）");
    $("refresh-chat-models").setAttribute("aria-busy", String(state.catalogLoading));
    $("chat-model-feedback").textContent = state.feedback;
    $("chat-model-feedback").hidden = !state.feedback;
    $("chat-model-feedback").classList.toggle("error", state.feedbackError);
    const note = state.capabilities?.reasoning?.note;
    const audio = state.capabilities?.audioInput;
    const audioNote = audio === "supported" ? "规则表标记支持音频输入。" : audio === "unsupported" ? "此模型不支持音频输入，请仅发送文字。" : "音频能力未知，附加音频前请确认平台支持。";
    $("chat-model-capability").textContent = state.capabilityLoading ? "正在读取本机能力规则…" : `${typeof note === "string" ? note : "默认推理不额外传参。"} ${audioNote} 模型 ID 可手动填写。`;
    // 技术能力说明移到悬停及辅助描述中；仅在实际阻止发送时由会话模块给出简短提示。
    const catalog = state.catalogMetadata.get(state.options.platformId);
    const cacheNote = catalog?.cachedAt ? `已缓存模型目录${catalog.stale ? "（超过 7 天，可主动刷新）" : ""}。` : platform?.provider === "qwen" ? "提供官方模型预设，尚未验证账号权限；可主动刷新目录。" : "首次使用可点击刷新获取模型列表。";
    $("chat-model").title = `可选择或填写模型 ID。${cacheNote}${audioNote}`;
    $("chat-model").setAttribute("aria-describedby", "chat-model-capability");
    $("chat-reasoning-label").title = state.capabilities?.reasoning?.supported === null ? "兼容平台能力未知；非默认强度需要平台支持，失败不会自动降级。" : "默认选项不额外指定推理参数。";
  }
  function publish() { render(); window.dispatchEvent(new CustomEvent("synthv:model-options", { detail: publicState() })); }
  function feedback(message = "", error = false) {
    clearTimeout(state.feedbackTimer);
    state.feedback = message; state.feedbackError = error; render();
    // 用户主动刷新的成功反馈短暂展示；错误持续保留，避免隐藏需要处理的问题。
    if (message && !error && !state.catalogLoading) state.feedbackTimer = setTimeout(() => {
      state.feedback = ""; render();
    }, 4000);
  }

  function renderPlatforms() {
    const select = $("chat-platform"); select.replaceChildren();
    for (const platform of state.platforms) select.add(new Option(`${platform.name || platform.id}${platform.configured ? "" : " · 未配置"}`, platform.id));
    if (!selectedPlatform()) select.add(new Option(`${state.options.platformId} · 暂不可用`, state.options.platformId));
    select.value = state.options.platformId;
  }
  function renderCatalog() {
    const list = $("chat-model-options"); list.replaceChildren();
    const models = state.catalogs.get(state.options.platformId) || [];
    // Qwen 初次配置即可选择常用模型；预设与 API 实际目录区分，不能冒充账号授权结果。
    const presets = selectedPlatform()?.provider === "qwen" && !models.length
      ? ["qwen3.8-flash", "qwen3.8-max", "qwen3.8-omni-flash"].map((id) => ({ id, label: "官方预设 · 未验证账号权限" })) : [];
    for (const model of models.length ? models : presets) list.append(new Option(model.label || model.id, model.id));
  }
  function renderReasoning() {
    const select = $("chat-reasoning"); select.replaceChildren();
    for (const item of reasoningOptions()) select.add(new Option(item.label, item.value));
    // 保留不再兼容的旧选择，明确要求用户调整；不默默降低用户已选的推理强度。
    if (!reasoningValid()) {
      const stale = new Option(`${state.options.reasoningEffort} · 当前模型不支持，请调整`, state.options.reasoningEffort);
      stale.disabled = true; select.add(stale);
    }
    select.value = state.options.reasoningEffort;
  }

  async function loadPlatforms() {
    const request = ++state.listRequest; state.listLoading = true; publish();
    try {
      const result = await bridge.api("/api/model-platforms");
      if (request !== state.listRequest) return;
      // 后端公开响应已脱敏；前端仍仅保存显示与能力判断需要的字段。
      state.platforms = (Array.isArray(result.items) ? result.items : []).map((item) => ({ id: item.id, name: item.name, model: item.model, provider: item.provider, configured: Boolean(item.configured) }));
      // 停用或删除的平台不继续展示内存中的旧目录；重新启用后由本机身份校验决定复用。
      for (const id of state.catalogs.keys()) {
        if (!state.platforms.some((item) => item.id === id && item.configured)) {
          state.catalogs.delete(id); state.catalogMetadata.delete(id);
        }
      }
      state.defaultPlatformId = typeof result.defaultPlatformId === "string" ? result.defaultPlatformId : "default";
      // 尚未建立会话且用户没有主动修改选择时，使用设置中的新会话默认平台。
      // 已有会话和明确编辑过的草稿保持原选择，不因全局偏好改变而切换供应商。
      if (!state.conversationId && !state.dirty) {
        state.options = { ...DEFAULTS, platformId: state.defaultPlatformId };
        $("chat-model").value = "";
      }
      renderPlatforms(); renderCatalog();
      if (state.feedbackError) feedback();
      await Promise.all([loadCapabilities(), loadCachedCatalog()]);
    } catch (error) { if (request === state.listRequest) feedback(bridge.errorMessage(error), true); }
    finally { if (request === state.listRequest) { state.listLoading = false; publish(); } }
  }

  /**
   * 切换会话、平台或重新打开页面时仅读取本机持久化缓存，不触发供应商请求。
   * 每个平台单独维护响应序号；主动刷新或配置变更会使旧读取失效，避免迟到的
   * 空缓存响应覆盖刚取得的目录。模型输入草稿不受目录加载和响应顺序影响。
   */
  async function loadCachedCatalog() {
    const platformId = state.options.platformId;
    if (!selectedPlatform()?.configured || state.refreshingPlatforms.has(platformId)) return;
    const generation = state.platformGenerations.get(platformId) || 0;
    const request = (state.catalogRequests.get(platformId) || 0) + 1;
    state.catalogRequests.set(platformId, request);
    try {
      const result = await bridge.api(`/api/model-platforms/${encodeURIComponent(platformId)}/models`);
      if ((state.platformGenerations.get(platformId) || 0) !== generation || state.catalogRequests.get(platformId) !== request) return;
      const models = (Array.isArray(result.models) ? result.models : []).filter((item) => typeof item.id === "string" && item.id);
      state.catalogs.set(platformId, models);
      state.catalogMetadata.set(platformId, { cachedAt: result.cachedAt, stale: Boolean(result.stale) });
      if (platformId === state.options.platformId) { renderCatalog(); render(); }
    } catch (error) {
      if ((state.platformGenerations.get(platformId) || 0) !== generation || state.catalogRequests.get(platformId) !== request) return;
      // 本地读取失败时不偷偷改为外网刷新；清理旧目录，保留手动输入和明确错误。
      state.catalogs.delete(platformId); state.catalogMetadata.delete(platformId);
      if (platformId === state.options.platformId) {
        renderCatalog(); feedback(`暂不能读取模型缓存：${bridge.errorMessage(error)} 仍可手动填写模型 ID。`, true);
      }
    }
  }

  /** 能力查询走本机规则，既不访问模型供应商，也不更改选中的平台或模型。 */
  async function loadCapabilities() {
    clearTimeout(state.capabilityTimer);
    const request = ++state.capabilityRequest;
    const snapshot = { platformId: state.options.platformId, model: state.options.model };
    state.capabilityLoading = true; state.capabilities = null; publish();
    try {
      const result = await bridge.api("/api/model-capabilities", snapshot);
      if (request !== state.capabilityRequest) return;
      state.capabilities = result;
    } catch (error) {
      if (request === state.capabilityRequest) feedback(`暂不能读取能力规则：${bridge.errorMessage(error)} 默认推理不额外指定参数。`, true);
    } finally {
      if (request === state.capabilityRequest) { state.capabilityLoading = false; renderReasoning(); publish(); }
    }
  }

  /** 只在用户更改后保存会话选项。失败保留草稿，不影响平台全局默认配置。 */
  async function persist() {
    if (state.savePromise) return state.savePromise;
    if (!state.dirty || !state.conversationId) return null;
    const id = state.conversationId; const snapshot = { ...state.options };
    state.saving = true; publish();
    state.savePromise = (async () => {
      try {
        const result = await bridge.api(`/api/conversations/${encodeURIComponent(id)}/model-options`, snapshot);
        if (state.conversationId === id && JSON.stringify(state.options) === JSON.stringify(snapshot)) state.dirty = false;
        window.dispatchEvent(new CustomEvent("synthv:model-options-saved", { detail: result }));
        feedback();
        return result;
      } catch (error) { feedback(`选择尚未保存：${bridge.errorMessage(error)}`, true); throw error; }
      finally { state.saving = false; state.savePromise = null; publish(); }
    })();
    return state.savePromise;
  }

  async function refreshCatalog() {
    if (state.catalogLoading || !selectedPlatform()?.configured) return;
    const platformId = state.options.platformId;
    const generation = state.platformGenerations.get(platformId) || 0;
    state.catalogRequests.set(platformId, (state.catalogRequests.get(platformId) || 0) + 1);
    state.refreshingPlatforms.add(platformId);
    state.catalogLoading = true; render(); feedback("正在读取模型列表…");
    try {
      const result = await bridge.api(`/api/model-platforms/${encodeURIComponent(platformId)}/models`, {});
      // 同一平台在请求期间可能已更换协议或地址，旧目录不能写入新配置的缓存。
      if ((state.platformGenerations.get(platformId) || 0) !== generation) {
        if (platformId === state.options.platformId) feedback("平台配置已更新，旧模型目录已忽略；可再次刷新。");
        return;
      }
      const models = (Array.isArray(result.models) ? result.models : []).filter((item) => typeof item.id === "string" && item.id);
      state.catalogs.set(platformId, models);
      state.catalogMetadata.set(platformId, { cachedAt: result.cachedAt, stale: false });
      if (platformId === state.options.platformId) {
        renderCatalog(); state.catalogLoading = false;
        feedback(result.cachePersisted === false ? `已读取 ${models.length} 个模型，但本地缓存保存失败；当前列表仍可使用。` : `已读取并缓存 ${models.length} 个模型。`, result.cachePersisted === false);
      }
    } catch (error) {
      if (platformId === state.options.platformId && (state.platformGenerations.get(platformId) || 0) === generation) feedback(`模型列表读取失败：${bridge.errorMessage(error)} 仍可手动填写模型 ID。`, true);
    }
    finally {
      state.refreshingPlatforms.delete(platformId); state.catalogLoading = false; publish();
      // 设置更新时可能已跳过正在刷新的平台；旧请求结束后只补读新身份的本地缓存。
      if ((state.platformGenerations.get(platformId) || 0) !== generation && platformId === state.options.platformId) loadCachedCatalog();
    }
  }

  $("chat-platform").addEventListener("change", () => {
    state.options = { platformId: $("chat-platform").value, model: "", reasoningEffort: "default" };
    state.dirty = true; $("chat-model").value = ""; renderCatalog(); renderReasoning();
    feedback();
    loadCapabilities(); loadCachedCatalog(); persist().catch(() => {});
  });
  $("chat-model").addEventListener("input", () => {
    state.options.model = $("chat-model").value.trim(); state.dirty = true;
    // 使旧模型的能力响应立即失效；短暂防抖只合并本机读取，不发送供应商请求。
    state.capabilityRequest++; state.capabilities = null; state.capabilityLoading = true;
    clearTimeout(state.capabilityTimer); state.capabilityTimer = setTimeout(loadCapabilities, 350); publish();
  });
  $("chat-model").addEventListener("change", () => { loadCapabilities(); persist().catch(() => {}); });
  $("chat-reasoning").addEventListener("change", () => {
    state.options.reasoningEffort = $("chat-reasoning").value; state.dirty = true;
    publish(); persist().catch(() => {});
  });
  $("refresh-chat-models").addEventListener("click", refreshCatalog);
  window.addEventListener("synthv:platforms-changed", (event) => {
    if (event.detail?.platform?.id) {
      const id = event.detail.platform.id;
      state.platformGenerations.set(id, (state.platformGenerations.get(id) || 0) + 1);
      state.catalogs.delete(id);
      state.catalogMetadata.delete(id);
      if (id === state.options.platformId) renderCatalog();
    }
    loadPlatforms();
  });
  window.addEventListener("synthv:page-change", (event) => { if (event.detail.from === "settings" && event.detail.to === "chat") loadPlatforms(); });

  window.SynthVModels = Object.freeze({
    getState: publicState,
    snapshot: () => ({ ...state.options }),
    ensureSaved: persist,
    // 初始会话恢复结束前保持模型控件不可编辑，避免晚到恢复结果覆盖用户选择。
    finishInitialization: () => { state.initializing = false; publish(); },
    setInteractionBusy: (value) => { if (state.interactionBusy !== Boolean(value)) { state.interactionBusy = Boolean(value); render(); } },
    setConversation: (conversation, { preserveDraft = false } = {}) => {
      const id = conversation?.id || null;
      if (id === state.conversationId) return;
      state.conversationId = id;
      // 会话切换后清除旧平台的目录/保存提示，避免把上一会话结果误认为当前状态。
      state.feedback = ""; state.feedbackError = false;
      if (!preserveDraft) {
        state.options = conversation ? options(conversation.modelOptions) : { ...DEFAULTS, platformId: state.defaultPlatformId }; state.dirty = false;
        $("chat-model").value = state.options.model;
      } else state.dirty = true;
      renderPlatforms(); renderCatalog(); renderReasoning(); loadCapabilities(); loadCachedCatalog(); publish();
    },
  });
  loadPlatforms();
})();
