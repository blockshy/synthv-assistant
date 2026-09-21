"use strict";

/**
 * SynthV 本地工作台前端。
 * 所有项目变更均交给本地服务校验；浏览器只负责明确呈现操作范围与真实结果。
 * 不生成示例录音、模拟波形或虚构连接状态，服务不可用时保持可解释的空状态。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    status: null, project: null, selection: null, recordings: [],
    selected: { a: "", b: "" }, preview: null, busy: new Set(),
    waveformCache: new Map(), waveformRequest: { a: 0, b: 0 },
    lastApplied: false, lastStatusRequest: 0, recordingsRevision: 0,
    settingsEpoch: 0,
    // 此对象仅保存公开设置与界面状态，绝不保存 apiKey 或密码输入值。
    audioSettings: { snapshot: null, request: 0, loading: false, saving: false, conflict: false, opener: null,
      platforms: [], listRevision: null, defaultPlatformId: "default", selectedId: "default", isNew: false },
  };

  const audioDefaults = {
    none: { model: "", baseUrl: "" },
    openai: { model: "gpt-audio-1.5", baseUrl: "https://api.openai.com/v1" },
    gemini: { model: "gemini-3.8-flash", baseUrl: "https://generativelanguage.googleapis.com/v1beta" },
  };

  // 目录、手绘草稿及局部校验由共享模块负责，API 请求与工程写入状态仍在本模块中。
  let parameterEditor = null;

  /** 从结构化服务错误中提取可读信息，避免向界面输出 [object Object]。 */
  function errorMessage(value) {
    if (typeof value === "string") return value;
    if (value instanceof Error) return value.message;
    return value?.message || value?.error?.message || (typeof value?.error === "string" ? value.error : "操作未完成，请检查本地服务状态。");
  }

  /**
   * 统一同源 API 请求。每次写操作都先获取会话令牌，并使用请求头传递。
   * 写操作失败后不自动重试，避免在服务端已执行但响应丢失时重复修改工程。
   */
  async function api(path, body) {
    const headers = { Accept: "application/json" };
    if (body !== undefined || path === "/api/audio-settings" || path === "/api/uploads" || path === "/api/recordings" || path === "/api/trash" || path.startsWith("/api/model-platforms") || path.startsWith("/api/conversations") || path.startsWith("/api/jobs/")) {
      const bootstrap = await api("/api/bootstrap");
      if (!bootstrap.token) throw new Error("本地服务会话尚未准备好，请刷新页面后重试。");
      headers["X-SV-Token"] = bootstrap.token;
      if (body !== undefined) headers["Content-Type"] = "application/json";
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", cache: "no-store", signal: controller.signal });
      const content = await response.text();
      let data;
      try { data = content ? JSON.parse(content) : {}; }
      catch { throw new Error("本地服务返回了无法读取的结果，请检查服务是否正常运行。"); }
      if (!response.ok || data.ok === false) {
        const error = new Error(errorMessage(data));
        // 保留状态码供设置页识别并发版本冲突，不自动覆盖或重放写请求。
        error.httpStatus = response.status;
        error.code = data.code || data.error?.code;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求等待超时。操作可能仍在执行，请读取当前状态后再决定是否重试。");
      if (error instanceof TypeError) throw new Error("无法连接本地服务，请确认工作台服务仍在运行。");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function feedback(id, message = "", isError = false) {
    const element = $(id);
    element.textContent = message;
    element.classList.toggle("error", isError);
  }

  function notice(message = "", isError = false) {
    feedback("global-notice", message, isError);
    $("global-notice").hidden = !message;
  }

  /** 只用 textContent 呈现返回内容；详细结果隐藏凭证字段，防止误显示敏感配置。 */
  function printable(value) {
    return JSON.stringify(value, (key, item) => /token|api.?key|secret|authorization|fingerprint/i.test(key) ? "[已隐藏]" : item, 2);
  }

  function resultData(value) { return value?.result ?? value?.data ?? value ?? {}; }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function numberText(value, digits = 2) { return finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—"; }
  function getRecording(slot) { return state.recordings.find((recording) => recording.id === state.selected[slot]); }
  function bothRecordings() { return Boolean(getRecording("a") && getRecording("b") && state.selected.a !== state.selected.b); }
  /** 声线名保持原大小写，仅去除首尾空格；字节上限与本地服务一致，避免多字节名称误通过。 */
  function vocalModeNameError(name) {
    if (/[\u0000-\u001f\u007f]/.test(name)) return "模式名称不能包含控制字符。";
    return new TextEncoder().encode(name).length > 80 ? "模式名称过长：UTF-8 编码后不能超过 80 字节。" : "";
  }

  /** 所有动作共用禁用条件，定时刷新状态时也不会意外解锁正在运行的操作。 */
  function syncButtons() {
    const connected = Boolean(state.status?.bridge?.connected);
    const recording = state.busy.has("record");
    const changing = state.busy.has("tuning") || state.busy.has("mode") || state.busy.has("assistant") || state.busy.has("context");
    const editing = Boolean(state.status?.writeEnabled);
    $("refresh-status").disabled = state.busy.has("status");
    $("read-project").disabled = !connected || state.busy.has("context") || recording || changing;
    $("read-selection").disabled = $("read-project").disabled;
    // 开启写入时由后端自动备份已保存工程；界面只保留明确的写入开关。
    $("write-mode").disabled = !connected || recording || changing;
    $("write-mode").checked = editing;
    $("record-a").disabled = !connected || !state.status?.capture?.available || recording || changing;
    $("record-b").disabled = $("record-a").disabled;
    $("start-seconds").disabled = recording;
    $("duration-seconds").disabled = recording;
    $("use-selection-range").disabled = !selectionRange() || recording;
    $("parameter").disabled = changing || recording;
    $("parameter-delta").disabled = changing || recording;
    const canExtendModes = connected && Boolean(selectionRange()) && resultData(state.selection)?.capabilities?.curves === true;
    $("vocal-mode-name").disabled = !canExtendModes || changing || recording;
    const modeName = $("vocal-mode-name").value.trim();
    $("add-vocal-mode").disabled = $("vocal-mode-name").disabled || !modeName || Boolean(vocalModeNameError(modeName));
    $("add-vocal-mode").title = canExtendModes ? "补充当前组的可选参数，不写入工程" : "请先连接新版桥接并读取有效选区";
    $("preview-change").disabled = !connected || recording || changing;
    if (parameterEditor) {
      parameterEditor.setBusy(changing || recording, connected);
      $("preview-change").disabled ||= !parameterEditor.canPreview();
    }
    $("apply-change").disabled = !connected || !editing || !state.preview || recording || changing;
    // 服务负责判断是否存在可恢复快照；刷新页面后仍允许请求恢复，避免隐藏有效的恢复能力。
    $("restore-change").disabled = !connected || !editing || recording || changing;
    $("compare-recordings").disabled = !bothRecordings() || state.busy.has("compare") || recording;
    $("review-recordings").disabled = !bothRecordings() || !state.status?.review?.configured || state.busy.has("review") || recording;
    // 对比或评审期间固定 A/B 选择，确保完成后的结果与界面显示的是同一组录音。
    $("select-a").disabled = recording || state.busy.has("compare") || state.busy.has("review");
    $("select-b").disabled = $("select-a").disabled;
    $("open-audio-settings").disabled = state.audioSettings.saving;
    $("configure-audio-model").disabled = state.audioSettings.saving;
    $("review-recordings").disabled ||= state.audioSettings.saving;
    // 仅广播公开状态；聊天模块据此即时更新按钮，不读取设置表单或密钥。
    window.dispatchEvent(new CustomEvent("synthv:state", { detail: {
      status: state.status, manualBusy: ["record", "tuning", "mode", "context"].some((key) => state.busy.has(key)),
      settingsSaving: state.audioSettings.saving,
    } }));
  }

  async function runBusy(key, feedbackId, task) {
    if (state.busy.has(key)) return;
    state.busy.add(key);
    syncButtons();
    try { await task(); }
    catch (error) { feedback(feedbackId, errorMessage(error), true); }
    finally { state.busy.delete(key); syncButtons(); }
  }

  function statusPill(id, ready, readyText, idleText, failure = false) {
    const element = $(id);
    element.dataset.state = ready ? "ready" : failure ? "error" : "pending";
    element.querySelector("span").textContent = ready ? readyText : idleText;
    // 窄屏会收起可见状态文字；保留读屏名称及悬浮提示，不只靠色点表达连接状态。
    element.setAttribute("aria-label", ready ? readyText : idleText);
    element.title = ready ? readyText : idleText;
  }

  /** 更新评审状态时只修改摘要和按钮，不触碰打开中的设置草稿或 A/B 选择。 */
  function renderReviewStatus(review = {}) {
    statusPill("review-status", review.configured, "默认模型 · 已配置", "默认模型 · 未配置");
    $("review-label").textContent = review.configured ? "已配置" : "未配置";
    $("review-label").classList.toggle("ready", Boolean(review.configured));
    $("review-unconfigured").hidden = Boolean(review.configured);
    $("configure-audio-model").textContent = review.configured ? "AI 模型设置" : "配置 AI 模型";
    $("review-model-summary").textContent = review.configured ? `${review.model || "音频模型"} · 未验证连接` : "配置仅保存在本机";
  }

  function normalizeEndpoint(value) { return String(value || "").trim().replace(/\/+$/, ""); }

  /** 只留存契约中的公开字段；即使服务误带未知字段，也不会被填入表单或详情。 */
  function publicSettings(value) {
    return {
      id: typeof value.id === "string" ? value.id : "default",
      name: typeof value.name === "string" ? value.name : "默认平台",
      provider: Object.hasOwn(audioDefaults, value.provider) ? value.provider : "none",
      model: typeof value.model === "string" ? value.model : "",
      baseUrl: typeof value.baseUrl === "string" ? value.baseUrl : "",
      timeoutSeconds: finite(value.timeoutSeconds) ? value.timeoutSeconds : 60,
      keyConfigured: Boolean(value.keyConfigured), configured: Boolean(value.configured),
      source: value.source === "local" ? "local" : "environment", storage: value.storage,
      revision: value.revision,
    };
  }

  /** 空白密钥只可保留同一协议、同一根地址的现有密钥，不能把凭据带到另一服务。 */
  function canKeepAudioKey() {
    const saved = state.audioSettings.snapshot;
    return Boolean(saved?.keyConfigured && saved.provider === $("settings-provider").value &&
      normalizeEndpoint(saved.baseUrl) === normalizeEndpoint($("settings-base-url").value));
  }

  /** 默认平台只能指向已保存配置；密码只当场判断是否有输入，绝不复制到状态中。 */
  function audioSettingsHasDraft() {
    const settings = state.audioSettings, saved = settings.snapshot;
    if (!saved || settings.isNew) return true;
    return $("settings-platform-name").value.trim() !== saved.name || $("settings-provider").value !== saved.provider ||
      $("settings-model").value.trim() !== saved.model || normalizeEndpoint($("settings-base-url").value) !== normalizeEndpoint(saved.baseUrl) ||
      Number($("settings-timeout").value) !== saved.timeoutSeconds || Boolean($("settings-api-key").value);
  }

  function syncAudioSettings() {
    const settings = state.audioSettings;
    const waiting = settings.loading || settings.saving;
    const enabled = $("settings-provider").value !== "none";
    $("settings-platform").disabled = waiting;
    $("new-model-platform").disabled = waiting || state.audioSettings.listRevision === null;
    $("set-default-model-platform").disabled = waiting || settings.conflict || settings.listRevision === null || !settings.snapshot?.configured || settings.selectedId === settings.defaultPlatformId || audioSettingsHasDraft();
    const defaultPlatform = settings.platforms.find((item) => item.id === settings.defaultPlatformId);
    $("settings-default-platform").textContent = `新会话默认：${defaultPlatform?.name || "默认平台"}`;
    $("set-default-model-platform").textContent = settings.selectedId === settings.defaultPlatformId && !settings.isNew ? "已是新会话默认" : "设为新会话默认";
    $("settings-platform-name").disabled = waiting || !settings.snapshot || (!settings.isNew && settings.selectedId === "default");
    $("settings-platform-name").title = !settings.isNew && settings.selectedId === "default" ? "默认平台保留旧配置兼容入口，名称固定。其他平台可自定义名称。" : "此名称只用于本机辨认平台。";
    // 兼容服务可能使用 namespace/model 路由；Gemini 的模型标识由服务端进一步校验。
    $("settings-model").maxLength = $("settings-provider").value === "gemini" ? 128 : 200;
    $("audio-settings-fields").disabled = waiting || !settings.snapshot;
    for (const id of ["settings-model", "settings-base-url", "settings-api-key", "settings-timeout"]) $(id).disabled = !enabled;
    $("settings-model").required = enabled;
    $("settings-base-url").required = enabled;
    $("settings-api-key").required = enabled && !canKeepAudioKey();
    $("save-audio-settings").disabled = waiting || !settings.snapshot || settings.conflict;
    $("save-audio-settings").textContent = settings.saving ? "正在保存…" : "保存并立即生效";
    $("clear-audio-settings").disabled = waiting || !settings.snapshot || settings.isNew;
    $("reload-audio-settings").disabled = waiting;
    $("close-audio-settings").disabled = settings.saving;
    $("cancel-audio-settings").disabled = settings.saving;
    $("settings-conflict-help").hidden = !settings.conflict;
    const keep = enabled && canKeepAudioKey();
    $("settings-key-state").textContent = keep ? "已保存 · 不回显" : enabled ? "需要填写" : "已停用";
    $("settings-api-key").placeholder = keep ? "留空保留已保存的密钥" : "输入当前服务的 API key";
    $("settings-key-help").textContent = !enabled ? "保存后停用模型服务并清除本机保存的密钥；也可直接使用下方清除按钮。" : keep ? "保持服务协议和根地址不变时，可留空保留密钥；输入新值可替换。" : "首次配置、更换服务协议或根地址后，需要重新填写对应密钥。";
    $("audio-settings-form").setAttribute("aria-busy", waiting ? "true" : "false");
    syncButtons();
  }

  function populateAudioSettings(snapshot) {
    state.audioSettings.snapshot = publicSettings(snapshot);
    const saved = state.audioSettings.snapshot;
    $("settings-platform-name").value = saved.name;
    $("settings-provider").value = saved.provider;
    $("settings-model").value = saved.model;
    $("settings-base-url").value = saved.baseUrl;
    $("settings-timeout").value = saved.timeoutSeconds;
    $("settings-api-key").value = "";
    $("settings-api-key").setCustomValidity("");
    $("settings-base-url").setCustomValidity("");
    $("settings-source").textContent = saved.source === "local" ? "本机保存 · 下次启动保留" : "来自当前进程环境";
  }

  /** 平台下拉框只使用公开摘要；不缓存或回显任何平台的密钥。 */
  function renderSettingsPlatforms() {
    const settings = state.audioSettings;
    $("settings-platform").replaceChildren();
    for (const item of settings.platforms) $("settings-platform").add(new Option(`${item.name || "未命名平台"}${item.id === settings.defaultPlatformId ? " · 新会话默认" : ""}${item.id === "default" ? " · 内置" : ""}`, item.id));
    if (settings.isNew) $("settings-platform").add(new Option("新平台 · 尚未保存", "__new__"));
    $("settings-platform").value = settings.isNew ? "__new__" : settings.selectedId;
  }

  /** 只有显式打开、选择平台或重新加载才读取设置，后台状态轮询不会覆盖用户编辑。 */
  async function loadAudioSettings(platformId = state.audioSettings.selectedId) {
    const settings = state.audioSettings;
    if (settings.saving || settings.loading) return;
    const requestId = ++settings.request;
    settings.loading = true; settings.conflict = false; settings.snapshot = null; settings.isNew = false;
    $("settings-api-key").value = "";
    feedback("settings-feedback", "正在读取已保存设置…");
    syncAudioSettings();
    try {
      const list = await api("/api/model-platforms");
      if (requestId !== settings.request || !isSettingsVisible()) return;
      settings.platforms = Array.isArray(list.items) ? list.items.map(publicSettings) : [];
      settings.listRevision = list.revision;
      settings.defaultPlatformId = list.defaultPlatformId || "default";
      settings.selectedId = settings.platforms.some((item) => item.id === platformId) ? platformId : list.defaultPlatformId || "default";
      renderSettingsPlatforms();
      const result = await api(`/api/model-platforms/${encodeURIComponent(settings.selectedId)}`);
      if (requestId !== settings.request || !isSettingsVisible()) return;
      populateAudioSettings(result);
      feedback("settings-feedback", "平台设置已读取。保存不验证连接、不调用供应商；会话选择与平台默认值分别保存。");
    } catch (error) {
      if (requestId === settings.request && isSettingsVisible()) feedback("settings-feedback", errorMessage(error), true);
    } finally {
      if (requestId === settings.request) { settings.loading = false; syncAudioSettings(); }
    }
  }

  function isSettingsVisible() { return window.SynthVPages?.current === "settings"; }

  /** 设置作为中央主页面打开；再次点击入口不会覆盖尚未保存的表单草稿。 */
  function openAudioSettings(opener) {
    if (state.audioSettings.saving) return;
    if (isSettingsVisible()) return;
    if (!window.SynthVPages?.go("settings")) return;
    state.audioSettings.opener = opener;
    state.audioSettings.snapshot = null;
    $("audio-settings-form").reset();
    $("settings-api-key").value = "";
    loadAudioSettings();
  }

  function closeAudioSettings() { window.SynthVPages?.go("chat"); }

  /** 新平台先成为本页草稿；点击保存前不分配服务端记录，也不继承其他平台密钥。 */
  function newModelPlatform() {
    const settings = state.audioSettings;
    if (settings.loading || settings.saving || settings.listRevision === null) return;
    settings.request++; settings.isNew = true; settings.selectedId = ""; settings.conflict = false;
    populateAudioSettings({ id: "", name: "", provider: "none", model: "", baseUrl: "", timeoutSeconds: 60,
      keyConfigured: false, configured: false, source: "local", revision: settings.listRevision });
    renderSettingsPlatforms(); syncAudioSettings(); $("settings-platform-name").focus();
    feedback("settings-feedback", "新平台尚未保存。填写名称、协议和对应密钥后保存；不会自动切换当前会话的平台。");
  }

  /** 离开设置使在途读取失效，密码及草稿立即清除，迟到响应不能重新填回。 */
  function leaveAudioSettings() {
    $("settings-api-key").value = "";
    state.audioSettings.request++;
    state.audioSettings.loading = false;
    state.audioSettings.snapshot = null;
    state.audioSettings.isNew = false;
    $("audio-settings-form").reset();
    $("settings-platform-name").value = "";
  }

  function applyAudioSettingsStatus(snapshot) {
    const saved = publicSettings(snapshot);
    // 其他命名平台只通知会话控件刷新，不改变旧版独立听评使用的默认配置。
    window.dispatchEvent(new CustomEvent("synthv:platforms-changed", { detail: { platform: saved } }));
    if (saved.id !== "default") return;
    state.settingsEpoch++;
    state.status ||= { bridge: { connected: false }, capture: { available: false }, writeEnabled: false };
    state.status.review = { ...state.status.review, configured: saved.configured, provider: saved.provider, model: saved.model };
    renderReviewStatus(state.status.review);
    syncButtons();
  }

  /** 只修改新会话默认指针；不保存表单草稿，也不切换已经存在的会话。 */
  async function setDefaultModelPlatform() {
    const settings = state.audioSettings;
    if ($("set-default-model-platform").disabled || settings.saving || settings.loading || audioSettingsHasDraft()) return;
    const id = settings.selectedId;
    settings.saving = true; syncAudioSettings();
    feedback("settings-feedback", "正在设置新会话默认平台；不会调用供应商…");
    try {
      const result = await api("/api/model-platforms/default-selection", { id, revision: settings.listRevision });
      if (!Array.isArray(result.items) || result.defaultPlatformId !== id || result.revision === undefined) throw new Error("服务未返回完整默认平台设置，请重新加载后核实；未自动重试。");
      settings.platforms = result.items.map(publicSettings);
      settings.listRevision = result.revision; settings.defaultPlatformId = result.defaultPlatformId;
      const current = settings.platforms.find((item) => item.id === id);
      if (current) populateAudioSettings(current);
      renderSettingsPlatforms();
      window.dispatchEvent(new CustomEvent("synthv:platforms-changed", { detail: { defaultPlatformId: settings.defaultPlatformId } }));
      feedback("settings-feedback", "已设为新会话默认。仅影响之后新建的会话，已有会话的选择保持不变。");
    } catch (error) {
      settings.conflict = error.httpStatus === 409 || /revision|版本冲突|设置已变化|设置已更改/i.test(errorMessage(error));
      feedback("settings-feedback", `${errorMessage(error)}${settings.conflict ? " 请重新加载已保存设置后再操作。" : ""}`, true);
    } finally { settings.saving = false; syncAudioSettings(); }
  }

  async function saveAudioSettings(clear = false) {
    const settings = state.audioSettings;
    if (settings.saving || settings.loading || !settings.snapshot) return;
    const keyInput = $("settings-api-key");
    if (!clear) {
      const enabled = $("settings-provider").value !== "none";
      if (enabled) {
        try {
          const url = new URL($("settings-base-url").value.trim());
          if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) throw new Error();
          $("settings-base-url").setCustomValidity("");
        } catch { $("settings-base-url").setCustomValidity("请输入不含凭据、查询参数或片段的 HTTPS API 根地址。"); }
      }
      if (!$("audio-settings-form").reportValidity()) { keyInput.value = ""; return; }
    }
    const payload = {
      name: clear ? settings.snapshot.name : $("settings-platform-name").value.trim(),
      provider: clear ? "none" : $("settings-provider").value, model: clear ? "" : $("settings-model").value.trim(),
      baseUrl: clear ? "" : normalizeEndpoint($("settings-base-url").value), timeoutSeconds: clear ? settings.snapshot.timeoutSeconds : Number($("settings-timeout").value),
      revision: settings.snapshot.revision,
    };
    if (!settings.isNew) payload.id = settings.selectedId;
    if (clear) payload.apiKey = "";
    // 密钥仅作为这一次请求的局部变量；提交后立即清空密码框，不复制进 state。
    const submittedKey = clear ? "" : keyInput.value.trim();
    if (!clear && payload.provider !== "none" && submittedKey) payload.apiKey = submittedKey;
    keyInput.value = "";
    settings.saving = true; settings.conflict = false;
    feedback("settings-feedback", clear ? "正在清除本机密钥并停用模型服务…" : "正在保存本机设置；不会调用供应商…");
    syncAudioSettings();
    try {
      const result = await api("/api/model-platforms", payload);
      settings.selectedId = result.id || settings.selectedId;
      settings.isNew = false;
      const publicResult = publicSettings(result);
      const existing = settings.platforms.findIndex((item) => item.id === publicResult.id);
      if (existing >= 0) settings.platforms[existing] = publicResult; else settings.platforms.push(publicResult);
      renderSettingsPlatforms();
      applyAudioSettingsStatus(result);
      if (isSettingsVisible()) {
        populateAudioSettings(result);
        feedback("settings-feedback", clear ? "已清除保存的密钥并停用模型服务，立即生效。" : "已加密保存并立即生效，下次启动保留。尚未验证连接，也未调用供应商。");
      }
      // 保存已被服务确认；列表刷新失败不能把成功保存误报为失败，也不重放写请求。
      try {
        const list = await api("/api/model-platforms");
        settings.listRevision = list.revision;
        settings.defaultPlatformId = list.defaultPlatformId || "default";
        if (Array.isArray(list.items)) settings.platforms = list.items.map(publicSettings);
        renderSettingsPlatforms();
      } catch { /* 当前平台公开结果仍可继续使用；下次重新加载会更新完整列表。 */ }
      feedback("review-feedback", clear ? "已清除密钥并停用评审；本地录音与指标比较仍可用。" : "模型设置已保存并立即生效；尚未验证供应商连接。");
      await refreshStatus();
    } catch (error) {
      keyInput.value = "";
      settings.conflict = error.httpStatus === 409 || /revision|版本冲突|设置已变化|设置已更改/i.test(errorMessage(error));
      // 即使异常服务把提交密钥放入错误文本，也不将其展示给用户。
      const safeMessage = submittedKey ? errorMessage(error).split(submittedKey).join("[已隐藏密钥]") : errorMessage(error);
      if (isSettingsVisible()) feedback("settings-feedback", `${safeMessage} 输入的密钥已清空，其他填写内容保留。`, true);
      else feedback("review-feedback", safeMessage, true);
    } finally {
      delete payload.apiKey;
      keyInput.value = "";
      settings.saving = false;
      syncAudioSettings();
    }
  }

  /** 后台状态刷新只读取信息，不自动启用写入，也不会启动录制或调用评审模型。 */
  async function refreshStatus(manual = false) {
    if (state.busy.has("status")) return;
    state.busy.add("status"); syncButtons();
    const settingsEpoch = state.settingsEpoch;
    try {
      const status = await api("/api/status");
      // 防止保存前已发出的状态请求在保存后才返回，从而用旧评审状态覆盖新设置。
      if (settingsEpoch !== state.settingsEpoch && state.status?.review) status.review = state.status.review;
      state.status = status;
      statusPill("bridge-status", status.bridge?.connected, "调教桥接 · 已连接", "调教桥接 · 未连接");
      statusPill("capture-status", status.capture?.available, "音频采集 · 已就绪", "音频采集 · 不可用");
      renderReviewStatus(status.review);
      $("host-version").textContent = status.bridge?.hostVersion || "—";
      if (status.bridge?.projectFile) showProjectName(status.bridge.projectFile);
      $("mode-badge").textContent = status.writeEnabled ? "允许修改选区" : "只读模式";
      $("mode-badge").classList.toggle("enabled", Boolean(status.writeEnabled));
      $("last-updated").textContent = "状态更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
      if (!status.bridge?.connected) notice("调教桥接尚未连接。请在 SynthV 中运行工作台桥接脚本，然后刷新状态。");
      else if (!status.capture?.available) notice(status.capture?.message || status.capture?.reason || "音频采集暂不可用。请检查本地服务的录音设备配置。");
      else notice();
      if (manual && status.bridge?.connected) feedback("context-feedback", "连接状态已更新。");
    } catch (error) {
      if (settingsEpoch !== state.settingsEpoch) return;
      state.status = null;
      statusPill("bridge-status", false, "", "调教桥接 · 未知", true);
      statusPill("capture-status", false, "", "音频采集 · 未知", true);
      statusPill("review-status", false, "", "模型服务 · 未知", true);
      $("last-updated").textContent = "本地服务连接中断";
      notice(errorMessage(error), true);
    } finally { state.busy.delete("status"); syncButtons(); }
  }

  function showProjectName(path) {
    $("project-name").textContent = String(path).split(/[\\/]/).pop() || "未命名工程";
    $("project-name").title = String(path);
  }

  /** 兼容桥接返回的扁平或嵌套选区结构；缺少时间信息时不猜测录制范围。 */
  function selectionRange() {
    const selection = resultData(state.selection);
    const notes = selection.notes || selection.selectedNotes;
    const count = selection.noteCount ?? selection.selectedNoteCount ?? selection.count;
    // 宿主明确返回空选区时，即使响应还有时间字段，也不能沿用为可录制的选区。
    if ((finite(count) && count <= 0) || (Array.isArray(notes) && notes.length === 0)) return null;
    const range = selection.timeRange || selection.range || selection;
    const start = range.startSeconds ?? range.start_seconds ?? range.start;
    const end = range.endSeconds ?? range.end_seconds ?? range.end;
    const duration = range.durationSeconds ?? range.duration_seconds ?? (finite(start) && finite(end) ? end - start : undefined);
    return finite(start) && start >= 0 && finite(duration) && duration > 0 ? { start, duration } : null;
  }

  function renderContext() {
    const project = resultData(state.project);
    const selection = resultData(state.selection);
    const file = project.projectFile || project.fileName || project.filename || project.project?.fileName;
    if (file) showProjectName(file);
    const notes = selection.notes || selection.selectedNotes;
    const count = selection.noteCount ?? selection.selectedNoteCount ?? selection.count ?? (Array.isArray(notes) ? notes.length : undefined);
    $("selection-count").textContent = finite(count) ? `${count} 个` : "—";
    const range = selectionRange();
    $("selection-time").textContent = range ? `${numberText(range.start)}–${numberText(range.start + range.duration)} 秒` : "—";
    $("context-details").textContent = printable({ 工程: state.project, 选区: state.selection });
    parameterEditor?.setSelection(state.selection ? selection : null);
    syncButtons();
  }

  function invalidatePreview(message = "", broadcast = true) {
    state.preview = null;
    $("preview-result").hidden = true;
    $("preview-curve-chart").replaceChildren();
    if (message) feedback("tuning-feedback", message);
    // 手动预览、重新读取选区等动作会使聊天中已确认的范围不再可靠。
    // 聊天自己的预览也会清空手动预览，但不回发事件，避免循环通知。
    if (broadcast) window.dispatchEvent(new CustomEvent("synthv:preview-invalidated"));
    syncButtons();
  }

  /**
   * 工程工具和独立录音页共用读取流程。新读取开始即废弃旧选区与旧预览，
   * 失败时不留下可误用的缓存；调用方可要求抛错，在自己的页面显示同一失败。
   */
  async function readContext(kind, { propagateError = false } = {}) {
    if (!["project", "selection"].includes(kind)) throw new Error("不支持的工程读取类型。");
    if (["context", "record", "tuning", "mode", "assistant"].some((key) => state.busy.has(key))) {
      const error = new Error("当前操作尚未结束，请稍后读取选区。");
      if (propagateError) throw error;
      feedback("context-feedback", error.message, true);
      return null;
    }
    state.busy.add("context");
    if (kind === "selection") {
      state.selection = null;
      feedback("vocal-mode-feedback");
      invalidatePreview("正在重新读取选区，读取完成后请重新预览。");
      renderContext();
    } else syncButtons();
    try {
      feedback("context-feedback", "正在读取…");
      const data = await api(`/api/${kind}`);
      state[kind] = data;
      renderContext();
      const empty = kind === "selection" && !selectionRange();
      feedback("context-feedback", empty ? "未读取到有效音符选区，请在 SynthV 中选中音符。" : kind === "project" ? "工程信息已读取。" : "选区信息已读取。", empty);
      // 结束读取阶段时也结束调参区的等待提示，不能让已完成的请求看起来仍在进行。
      if (kind === "selection") feedback("tuning-feedback", empty ? "没有有效选区，请先选中音符并重新读取。" : "选区已更新，请先生成预览。", empty);
      return data;
    } catch (error) {
      if (kind === "selection") { state.selection = null; renderContext(); feedback("tuning-feedback", "选区读取失败，请重新读取有效选区后再预览。", true); }
      feedback("context-feedback", errorMessage(error), true);
      if (propagateError) throw error;
      return null;
    } finally { state.busy.delete("context"); syncButtons(); }
  }

  function metricEntries(analysis = {}) {
    const choices = [
      ["均方根电平", analysis.rmsDbfs ?? analysis.rms_dbfs ?? analysis.rmsDb, "dBFS"],
      ["峰值", analysis.peakDbfs ?? analysis.peak_dbfs ?? analysis.peakDb, "dBFS"],
      ["削波比例", analysis.clippingRatio ?? analysis.clipping_ratio, "%", 100],
      ["平均基频", analysis.meanF0Hz ?? analysis.mean_f0_hz ?? analysis.pitch?.meanHz, "Hz"],
    ];
    return choices.filter((item) => finite(item[1])).map(([label, value, unit, factor = 1]) => ({ label, value: `${numberText(value * factor)} ${unit}` }));
  }

  /** 只允许访问本地服务提供的同源录音 URL，避免把任意远程资源当作录音。 */
  function recordingUrl(recording) {
    if (!recording?.url) return null;
    try {
      const url = new URL(recording.url, window.location.href);
      if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) return null;
      return url.href;
    } catch { return null; }
  }

  function renderRecordingOptions() {
    for (const slot of ["a", "b"]) {
      const select = $("select-" + slot);
      select.replaceChildren(new Option("选择已有录音…", ""));
      for (const recording of state.recordings) {
        const time = recording.createdAt ? new Date(recording.createdAt).toLocaleTimeString("zh-CN", { hour12: false }) : "";
        select.add(new Option(`${recording.label || "录音"}${time ? " · " + time : ""}`, recording.id));
      }
      if (!state.recordings.some((recording) => recording.id === state.selected[slot])) state.selected[slot] = "";
      select.value = state.selected[slot];
      renderRecording(slot);
    }
    syncButtons();
  }

  async function refreshRecordings() {
    const revision = state.recordingsRevision;
    const result = await api("/api/recordings");
    // 删除动作会提升版本，删除前发出的请求不能把旧录音重新加入 A/B 播放器。
    if (revision !== state.recordingsRevision) return state.recordings;
    state.recordings = Array.isArray(result.items) ? result.items : [];
    renderRecordingOptions();
    window.dispatchEvent(new CustomEvent("synthv:recordings", { detail: state.recordings }));
    return state.recordings;
  }

  function renderRecording(slot) {
    const recording = getRecording(slot);
    const audio = $("audio-" + slot);
    const url = recordingUrl(recording);
    if (audio.getAttribute("src") !== url) {
      audio.pause();
      if (url) audio.src = url;
      else audio.removeAttribute("src");
      audio.load();
    }
    $("meta-" + slot).textContent = recording ? `${numberText(recording.startSeconds)} 秒起 · ${numberText(recording.durationSeconds)} 秒${recording.label ? " · " + recording.label : ""}` : slot === "a" ? "还没有录制原始效果" : "还没有录制调整效果";
    // 静音、削波等实际检测结果直接显示在播放器旁，不能只藏在完整 JSON 中。
    $("warning-" + slot).textContent = Array.isArray(recording?.analysis?.warnings) ? recording.analysis.warnings.join(" ") : "";
    const metrics = $("metrics-" + slot);
    metrics.replaceChildren();
    for (const metric of metricEntries(recording?.analysis)) {
      const group = document.createElement("div");
      const label = document.createElement("dt"); label.textContent = metric.label;
      const value = document.createElement("dd"); value.textContent = metric.value;
      group.append(label, value); metrics.append(group);
    }
    updateWaveform(slot, recording, url);
  }

  /** 从实际 PCM 数据计算波形峰值；仅用于显示，不替代服务器上的音频质量分析。 */
  async function waveformFor(recording, url) {
    if (state.waveformCache.has(recording.id)) return state.waveformCache.get(recording.id);
    const envelope = recording.analysis?.envelope;
    const supplied = (Array.isArray(envelope) ? envelope.map((point) => point.peak) : null) || recording.analysis?.waveform?.peaks || recording.analysis?.waveform || recording.waveform;
    if (Array.isArray(supplied) && supplied.length && supplied.every(finite)) {
      state.waveformCache.set(recording.id, supplied); return supplied;
    }
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error("无法读取音频文件");
    const bytes = await response.arrayBuffer();
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("此浏览器不支持波形解码");
    const context = new AudioContextClass();
    try {
      const buffer = await context.decodeAudioData(bytes);
      const channels = Array.from({ length: buffer.numberOfChannels }, (_, index) => buffer.getChannelData(index));
      const bars = 240;
      const peaks = Array.from({ length: bars }, (_, index) => {
        const begin = Math.floor(index * buffer.length / bars);
        const end = Math.max(begin + 1, Math.floor((index + 1) * buffer.length / bars));
        let peak = 0;
        for (const channel of channels) for (let sample = begin; sample < end && sample < channel.length; sample++) peak = Math.max(peak, Math.abs(channel[sample]));
        return peak;
      });
      state.waveformCache.set(recording.id, peaks);
      return peaks;
    } finally { await context.close(); }
  }

  function drawWaveform(slot, peaks) {
    const canvas = $("waveform-" + slot);
    const bounds = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(bounds.width * ratio));
    canvas.height = Math.max(1, Math.round(bounds.height * ratio));
    const context = canvas.getContext("2d");
    if (!context) return;
    context.scale(ratio, ratio);
    const width = bounds.width; const height = bounds.height;
    context.clearRect(0, 0, width, height);
    if (!peaks?.length) return;
    context.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue(slot === "a" ? "--waveform-a" : "--waveform-b").trim() || "#808080";
    context.lineWidth = 1.3;
    context.beginPath();
    // A/B 保持相同振幅尺度，不分别归一化，避免把响度差异视觉上抹平。
    peaks.forEach((peak, index) => {
      const x = (index + 0.5) * width / peaks.length;
      const amplitude = Math.min(1, Math.abs(peak)) * (height * 0.44);
      context.moveTo(x, height / 2 - amplitude); context.lineTo(x, height / 2 + amplitude);
    });
    context.stroke();
  }

  async function updateWaveform(slot, recording, url) {
    const request = ++state.waveformRequest[slot];
    const empty = $("empty-" + slot);
    drawWaveform(slot, null);
    empty.hidden = false;
    empty.textContent = recording ? "正在读取音频波形…" : "录制后显示真实音频波形";
    $("waveform-" + slot).setAttribute("aria-label", `${slot.toUpperCase()} 录音波形${recording ? "，加载中" : "，暂无音频"}`);
    if (!recording || !url) return;
    try {
      const peaks = await waveformFor(recording, url);
      if (request !== state.waveformRequest[slot]) return;
      drawWaveform(slot, peaks); empty.hidden = true;
      $("waveform-" + slot).setAttribute("aria-label", `${slot.toUpperCase()} 录音的实际音频波形`);
    } catch {
      if (request === state.waveformRequest[slot]) empty.textContent = "波形暂不可用，可使用播放器试听";
    }
  }

  /**
   * 轮询同一个后台任务，由服务端的成功或失败状态结束等待。
   * 模型可能持续输出超过十分钟，浏览器不能再用固定总时限把活跃任务判成失败。
   * 服务端负责无输出超时和流式请求总上限；单次本地 HTTP 请求仍受 api() 保护。
   * 连接中断或状态异常直接报错，不重发创建任务的请求，避免重复计费或重复录音。
   */
  async function waitForJob(jobId, onProgress) {
    if (!jobId) throw new Error("服务未返回任务编号，无法确认操作结果。");
    while (true) {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (job.state === "done") return job.result;
      if (job.state === "error") throw new Error(errorMessage(job.error));
      if (job.state !== "running" && job.state !== "queued" && job.state !== "pending") throw new Error("任务状态无法识别，请查看本地服务状态。");
      if (onProgress) onProgress(job);
      await new Promise((resolve) => setTimeout(resolve, 850));
    }
  }

  async function record(slot) {
    if (!$("range-form").reportValidity()) return;
    await runBusy("record", "record-feedback", async () => {
      const startSeconds = Number($("start-seconds").value);
      const durationSeconds = Number($("duration-seconds").value);
      feedback("record-feedback", "");
      $("record-progress").hidden = false;
      $("record-progress-text").textContent = `正在录制 ${slot.toUpperCase()} · 片段 ${numberText(startSeconds)}–${numberText(startSeconds + durationSeconds)} 秒…`;
      const previousIds = new Set(state.recordings.map((item) => item.id));
      try {
        const job = await api("/api/record", { startSeconds, durationSeconds, label: slot === "a" ? "A · 原始效果" : "B · 调整效果" });
        const result = await waitForJob(job.jobId);
        await refreshRecordings();
        const id = result?.id || result?.recording?.id || state.recordings.find((item) => !previousIds.has(item.id))?.id;
        if (id) state.selected[slot] = id;
        renderRecordingOptions(); clearComparison();
        feedback("record-feedback", `${slot.toUpperCase()} 录制完成。请试听实际音频。`);
      } finally { $("record-progress").hidden = true; }
    });
  }

  function clearComparison() {
    $("compare-result").hidden = true;
    $("review-result").hidden = true;
    feedback("review-feedback", "");
  }

  function renderComparison(result) {
    const box = $("compare-result");
    box.replaceChildren(); box.hidden = false;
    const metricsA = metricEntries(getRecording("a")?.analysis);
    const metricsB = metricEntries(getRecording("b")?.analysis);
    if (metricsA.length || metricsB.length) {
      const table = document.createElement("table"); table.className = "comparison-table";
      const heading = table.createTHead().insertRow();
      for (const label of ["指标", "A · 原始效果", "B · 调整效果"]) { const th = document.createElement("th"); th.scope = "col"; th.textContent = label; heading.append(th); }
      const body = table.createTBody();
      for (const label of new Set([...metricsA, ...metricsB].map((item) => item.label))) {
        const row = body.insertRow();
        for (const value of [label, metricsA.find((item) => item.label === label)?.value || "—", metricsB.find((item) => item.label === label)?.value || "—"]) row.insertCell().textContent = value;
      }
      box.append(table);
    }
    const data = resultData(result);
    if (typeof data.summary === "string") { const summary = document.createElement("p"); summary.textContent = data.summary; box.append(summary); }
    if (Array.isArray(data.warnings) && data.warnings.length) {
      const warnings = document.createElement("p"); warnings.className = "inline-feedback error"; warnings.textContent = data.warnings.join(" "); box.append(warnings);
    }
    if (typeof data.interpretation === "string") {
      const interpretation = document.createElement("p"); interpretation.className = "field-help"; interpretation.textContent = data.interpretation; box.append(interpretation);
    }
    const details = document.createElement("details"); details.className = "data-details";
    const summary = document.createElement("summary"); summary.textContent = "查看完整指标结果";
    const pre = document.createElement("pre"); pre.textContent = printable(data);
    details.append(summary, pre); box.append(details);
    if (!metricsA.length && !metricsB.length && !data.summary) details.open = true;
  }

  /** 在界面注册一次事件；所有可能改变工程的动作都通过显式按钮触发。 */
  for (const id of ["open-audio-settings", "configure-audio-model"]) {
    $(id).addEventListener("click", (event) => openAudioSettings(event.currentTarget));
  }
  for (const id of ["close-audio-settings", "cancel-audio-settings"]) $(id).addEventListener("click", closeAudioSettings);
  window.addEventListener("synthv:before-page-change", (event) => {
    if (event.detail.from === "settings" && state.audioSettings.saving) {
      event.preventDefault();
      feedback("settings-feedback", "正在保存设置，请等待完成后再离开。", true);
    }
  });
  window.addEventListener("synthv:page-change", (event) => {
    if (event.detail.from === "settings" && event.detail.to !== "settings") leaveAudioSettings();
  });
  $("reload-audio-settings").addEventListener("click", () => loadAudioSettings());
  $("settings-platform").addEventListener("change", () => {
    if ($("settings-platform").value !== "__new__") loadAudioSettings($("settings-platform").value);
  });
  $("new-model-platform").addEventListener("click", newModelPlatform);
  $("set-default-model-platform").addEventListener("click", setDefaultModelPlatform);
  // 未保存字段（包括页外关联的平台名称）变化后，立即禁用默认指针操作。
  for (const id of ["settings-platform-name", "settings-model", "settings-timeout", "settings-api-key"]) $(id).addEventListener("input", syncAudioSettings);
  $("settings-provider").addEventListener("change", () => {
    const defaults = audioDefaults[$("settings-provider").value];
    $("settings-model").value = defaults.model;
    $("settings-base-url").value = defaults.baseUrl;
    $("settings-api-key").value = "";
    $("settings-api-key").setCustomValidity("");
    $("settings-base-url").setCustomValidity("");
    syncAudioSettings();
  });
  $("settings-base-url").addEventListener("input", () => {
    // 用户改变目的地址时舍弃尚未提交的密钥，避免误把旧服务凭据发送到新地址。
    $("settings-api-key").value = "";
    $("settings-api-key").setCustomValidity("");
    $("settings-base-url").setCustomValidity("");
    syncAudioSettings();
  });
  $("settings-api-key").addEventListener("input", () => $("settings-api-key").setCustomValidity(""));
  $("audio-settings-form").addEventListener("submit", (event) => { event.preventDefault(); saveAudioSettings(); });
  $("clear-audio-settings").addEventListener("click", () => saveAudioSettings(true));
  $("refresh-status").addEventListener("click", () => refreshStatus(true));
  $("read-project").addEventListener("click", () => readContext("project"));
  $("read-selection").addEventListener("click", () => readContext("selection"));
  $("write-mode").addEventListener("change", (event) => {
    const enabled = event.target.checked;
    runBusy("mode", "mode-feedback", async () => {
      await api("/api/write-mode", { enabled });
      if (state.status) state.status.writeEnabled = enabled;
      invalidatePreview();
      feedback("mode-feedback", enabled ? "已开启选区写入。每次修改仍需先预览。" : "已切换为只读模式。");
      await refreshStatus();
    });
  });
  $("use-selection-range").addEventListener("click", () => {
    const range = selectionRange(); if (!range) return;
    $("start-seconds").value = Math.round(range.start * 10) / 10;
    $("duration-seconds").value = Math.max(1, Math.min(30, Math.round(range.duration * 10) / 10));
    feedback("record-feedback", range.duration > 30 ? "已使用选区起点，时长限制为前 30 秒。" : range.duration < 1 ? "已使用选区起点，录制时长设为最少 1 秒。" : "已使用选区时间范围。");
  });
  $("range-form").addEventListener("submit", (event) => event.preventDefault());
  for (const slot of ["a", "b"]) {
    $("record-" + slot).addEventListener("click", () => record(slot));
    $("select-" + slot).addEventListener("change", (event) => { state.selected[slot] = event.target.value; renderRecording(slot); clearComparison(); syncButtons(); });
    $("audio-" + slot).addEventListener("play", () => $("audio-" + (slot === "a" ? "b" : "a")).pause());
  }
  // 草稿改变后同时废弃预览和旧成功提示，避免禁用按钮旁仍显示“预览已就绪”。
  parameterEditor = window.SynthVCurves.createEditor({ onChange: () => invalidatePreview("草稿已更改，请重新预览。"), onValidityChange: syncButtons });
  /** 用户明确补充声线名后提交原始选区快照，由服务校验组身份；不自行猜测声库模式。 */
  async function addVocalMode() {
    if ($("add-vocal-mode").disabled) return;
    const name = $("vocal-mode-name").value.trim(), selection = state.selection;
    await runBusy("context", "vocal-mode-feedback", async () => {
      invalidatePreview("正在补充参数目录，完成后请重新预览。");
      feedback("vocal-mode-feedback", "正在补充当前组的参数目录，不会写入工程…");
      let updatedSelection;
      try { updatedSelection = await api("/api/parameters/vocal-mode", { name, selection }); }
      catch (error) {
        // 超时可能发生在服务已更新目录之后，因此提示重新读取，不声称后端一定没有执行。
        feedback("tuning-feedback", "参数目录更新未确认，请重新读取选区。", true); throw error;
      }
      state.selection = updatedSelection;
      renderContext();
      parameterEditor.selectParameter("vocalMode_" + name);
      $("vocal-mode-name").value = "";
      feedback("vocal-mode-feedback", `已将“${name}”补充到本组参数目录，仅本次桥接会话有效。请核对声库面板后再预览。`);
    });
  }
  $("vocal-mode-name").addEventListener("input", () => {
    const message = vocalModeNameError($("vocal-mode-name").value.trim());
    feedback("vocal-mode-feedback", message, Boolean(message)); syncButtons();
  });
  $("add-vocal-mode").addEventListener("click", addVocalMode);
  $("vocal-mode-name").addEventListener("keydown", (event) => {
    // 补充名称位于调参表单内，按回车只补充目录，不能意外提交参数预览。
    if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); addVocalMode(); }
  });
  $("tuning-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!$("tuning-form").reportValidity()) return;
    runBusy("tuning", "tuning-feedback", async () => {
      const payload = parameterEditor.getPayload();
      invalidatePreview(); feedback("tuning-feedback", "正在读取当前选区并生成预览，尚未写入工程…");
      const result = await api("/api/preview", payload);
      if (!result.previewId) throw new Error("服务未返回有效的修改预览，尚未改动工程。");
      state.preview = result;
      const definition = parameterEditor.describe(payload.parameter);
      const parameterName = result.label || definition?.label || payload.parameter;
      const unit = result.unit || definition?.unit || "";
      const amount = Array.isArray(payload.curve) ? `${payload.curve.length} 个草稿点` : finite(result.delta) ? `${result.delta > 0 ? "+" : ""}${numberText(result.delta)} ${unit}` : "";
      const scope = finite(result.startSeconds) && finite(result.endSeconds) ? ` · ${numberText(result.startSeconds)}–${numberText(result.endSeconds)} 秒` : "";
      const notes = finite(result.noteCount) ? ` · ${result.noteCount} 个音符` : "";
      const representation = window.SynthVCurves.representation(result.representation, result.renderMode);
      const counts = `${finite(result.beforePointCount) ? ` · 原有 ${result.beforePointCount} 点` : ""}${finite(result.pointCount) ? ` · 预览 ${result.pointCount} 点` : ""}${finite(result.pointReduction) ? ` · 精简减少 ${result.pointReduction} 点` : ""}`;
      $("preview-summary").textContent = `${parameterName} ${amount}${scope}${notes} · ${representation}${counts}。${typeof result.summary === "string" ? result.summary : "预览已生成，尚未改动工程。"}`;
      $("preview-details").textContent = printable(result);
      const chart = window.SynthVCurves.createPreview(result);
      $("preview-curve-chart").replaceChildren(); if (chart) $("preview-curve-chart").append(chart);
      $("preview-result").hidden = false;
      feedback("tuning-feedback", state.status?.writeEnabled ? "预览已就绪，尚未改动工程。" : "预览已就绪。开启选区写入后才能应用。");
    });
  });
  $("apply-change").addEventListener("click", () => runBusy("tuning", "tuning-feedback", async () => {
    const preview = state.preview; if (!preview) return;
    // 提交前即失效，网络响应不明时不得重复套用同一个预览。
    invalidatePreview(); feedback("tuning-feedback", "正在应用修改…");
    const result = await api("/api/apply", { previewId: preview.previewId });
    state.lastApplied = true;
    feedback("tuning-feedback", typeof result.summary === "string" ? result.summary : "修改已应用。录制 B，检查这次调整的实际效果。");
  }));
  $("restore-change").addEventListener("click", () => runBusy("tuning", "tuning-feedback", async () => {
    invalidatePreview(); feedback("tuning-feedback", "正在恢复最后一次修改…");
    const result = await api("/api/restore", {});
    state.lastApplied = false;
    feedback("tuning-feedback", typeof result.summary === "string" ? result.summary : "最后一次修改已恢复。");
    window.dispatchEvent(new CustomEvent("synthv:restored", { detail: result }));
  }));
  $("compare-recordings").addEventListener("click", () => runBusy("compare", "record-feedback", async () => {
    feedback("record-feedback", "正在比较音频指标…");
    const result = await api("/api/compare", { beforeId: state.selected.a, afterId: state.selected.b });
    renderComparison(result); feedback("record-feedback", "指标对比已完成；数值变化不直接代表演唱质量提升。");
  }));
  $("review-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!bothRecordings() || !state.status?.review?.configured) return;
    runBusy("review", "review-feedback", async () => {
      $("review-result").hidden = true;
      feedback("review-feedback", "正在提交实际录音，等待音频模型评审…");
      const job = await api("/api/review", { recordingIds: [state.selected.a, state.selected.b], prompt: $("review-prompt").value.trim() || "比较 A、B 两个歌声片段的自然度、咬字、气声和尾音，指出有依据的差异，并给出保守的后续调教建议。" });
      const result = await waitForJob(job.jobId);
      const data = resultData(result);
      if (data?.status === "error" || data?.status === "not_configured") throw new Error(errorMessage(data));
      const reviewText = typeof data === "string" ? data : data.text || data.review || data.summary;
      $("review-result").textContent = typeof reviewText === "string" ? reviewText : printable(data);
      $("review-result").hidden = false;
      feedback("review-feedback", "评审已完成。建议仅供试听参考，不会自动修改工程。");
    });
  });
  window.addEventListener("resize", () => {
    for (const slot of ["a", "b"]) { const recording = getRecording(slot); if (recording) drawWaveform(slot, state.waveformCache.get(recording.id)); }
  });
  window.addEventListener("synthv:assets-changed", (event) => {
    if (event.detail?.kind !== "recording" || !event.detail.deleted) return;
    // 删除确认成功后立即清理 A/B 播放器和波形，不依赖后续列表请求成功。
    state.recordingsRevision++;
    state.recordings = state.recordings.filter((item) => item.id !== event.detail.id);
    state.waveformCache.delete(event.detail.id);
    renderRecordingOptions(); clearComparison();
  });

  /**
   * 对话层复用经过令牌保护的请求和实际状态，不复制设置逻辑。
   * 公开对象不包含密钥、表单草稿、录音缓存或后台轮询句柄。
   */
  window.SynthVWorkbench = Object.freeze({
    api, waitForJob, errorMessage, printable, refreshStatus, refreshRecordings,
    readSelection: () => readContext("selection", { propagateError: true }),
    getSelectionRange: () => selectionRange(),
    getParameterDefinition: (parameter) => parameterEditor?.describe(parameter),
    setRecordingBusy: (busy) => { if (busy) state.busy.add("record"); else state.busy.delete("record"); syncButtons(); },
    getStatus: () => state.status,
    getManualBusy: () => ["record", "tuning", "mode", "assistant", "context"].some((key) => state.busy.has(key)),
    clearManualPreview: () => invalidatePreview("已切换到对话中的参数预览。", false),
    setAssistantBusy: (busy) => { if (busy) state.busy.add("assistant"); else state.busy.delete("assistant"); syncButtons(); },
    openSettings: (opener) => openAudioSettings(opener),
  });

  async function initialize() {
    syncButtons();
    await refreshStatus();
    try { await refreshRecordings(); } catch (error) { feedback("record-feedback", errorMessage(error), true); }
    // 页面隐藏时暂停状态请求；回到页面后会在下一个周期恢复。
    setInterval(() => { if (!document.hidden && !state.busy.has("mode")) refreshStatus(); }, 8000);
  }
  initialize();
})();
