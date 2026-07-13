(() => {
  "use strict";

  const API = Object.freeze({
    config: "/admin/api/config",
    native: "/admin/api/native",
  });

  const NATIVE_DEFINITIONS = Object.freeze([
    {
      key: "model",
      label: "Model",
      description: "The native model Codex uses for new work.",
      kind: "select",
    },
    {
      key: "reasoningEffort",
      label: "Reasoning effort",
      description: "Available efforts come from the selected model's native catalog.",
      kind: "select",
    },
    {
      key: "personality",
      label: "Personality",
      description: "Choose the communication style Codex applies by default.",
      kind: "select",
    },
    {
      key: "permissionMode",
      label: "Permission mode",
      description: "Controls the native sandbox and approval policy together.",
      kind: "select",
    },
    {
      key: "fast",
      label: "Fast mode",
      description: "Use Codex's faster service tier when it is available.",
      kind: "boolean",
    },
  ]);

  class ApiError extends Error {
    constructor(message, status = 0, body = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.body = body;
    }
  }

  const state = {
    csrfToken: "",
    revision: "",
    bridgeResponse: null,
    nativeResponse: null,
    bridgeFields: new Map(),
    bridgeBaseline: new Map(),
    bridgeDraft: new Map(),
    fieldSection: new Map(),
    secretActions: new Map(),
    nativeBaseline: new Map(),
    nativeDraft: new Map(),
    nativeForcedDirty: new Set(),
    nativeControls: new Map(),
    bridgeLoaded: false,
    nativeLoaded: false,
    bridgeError: null,
    nativeError: null,
    bridgeSaving: false,
    nativeSaving: false,
    refreshing: false,
    restartPending: false,
    activePanel: "native",
  };

  const elements = {
    connectionState: document.querySelector("#connection-state"),
    connectionStateLabel: document.querySelector("#connection-state-label"),
    refreshButton: document.querySelector("#refresh-button"),
    retryButton: document.querySelector("#retry-button"),
    globalError: document.querySelector("#global-error"),
    globalErrorTitle: document.querySelector("#global-error-title"),
    globalErrorMessage: document.querySelector("#global-error-message"),
    restartBanner: document.querySelector("#restart-banner"),
    dismissRestart: document.querySelector("#dismiss-restart"),
    nativeSettings: document.querySelector("#native-settings"),
    nativeWarnings: document.querySelector("#native-warnings"),
    nativeFooter: document.querySelector("#native-footer"),
    nativeSaveState: document.querySelector("#native-save-state"),
    saveNativeButton: document.querySelector("#save-native-button"),
    bridgeSections: document.querySelector("#bridge-sections"),
    saveDock: document.querySelector("#save-dock"),
    bridgeChangeCount: document.querySelector("#bridge-change-count"),
    saveBridgeButton: document.querySelector("#save-bridge-button"),
    discardBridgeButton: document.querySelector("#discard-bridge-button"),
    conflictDialog: document.querySelector("#conflict-dialog"),
    conflictCancel: document.querySelector("#conflict-cancel"),
    conflictReload: document.querySelector("#conflict-reload"),
    toastRegion: document.querySelector("#toast-region"),
    sectionNav: document.querySelector("#section-nav"),
    breadcrumbCurrent: document.querySelector("#breadcrumb-current"),
    breadcrumbGroup: document.querySelector("#breadcrumb-group"),
    breadcrumbGroupSep: document.querySelector("#breadcrumb-group-sep"),
    themeToggle: document.querySelector("#theme-toggle"),
    themeToggleLabel: document.querySelector("#theme-toggle-label"),
    mainContent: document.querySelector("#main-content"),
    nativePanel: document.querySelector('[data-panel="native"]'),
  };

  function createElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function safeId(value) {
    return String(value || "field")
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "field";
  }

  function hasOwn(object, key) {
    return object !== null && typeof object === "object" && Object.prototype.hasOwnProperty.call(object, key);
  }

  /* ------------------------------------------------------------------ theme */
  const THEME_KEY = "imcodex-admin-theme";
  const THEME_ORDER = ["auto", "light", "dark"];
  const THEME_LABEL = { auto: "Auto theme", light: "Light theme", dark: "Dark theme" };

  function applyTheme(pref) {
    const root = document.documentElement;
    if (pref === "light" || pref === "dark") {
      root.dataset.theme = pref;
    } else {
      delete root.dataset.theme;
      pref = "auto";
    }
    root.dataset.themePref = pref;
    if (elements.themeToggleLabel) elements.themeToggleLabel.textContent = THEME_LABEL[pref];
    if (elements.themeToggle) {
      elements.themeToggle.setAttribute("aria-label", `Color theme: ${THEME_LABEL[pref]}. Click to change.`);
    }
  }

  function initTheme() {
    let stored = "auto";
    try {
      stored = window.localStorage.getItem(THEME_KEY) || "auto";
    } catch (_error) {
      stored = "auto";
    }
    applyTheme(THEME_ORDER.includes(stored) ? stored : "auto");
  }

  function cycleTheme() {
    const current = document.documentElement.dataset.themePref || "auto";
    const next = THEME_ORDER[(THEME_ORDER.indexOf(current) + 1) % THEME_ORDER.length];
    applyTheme(next);
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch (_error) {
      /* storage unavailable — theme still applies for this session */
    }
  }

  /* --------------------------------------------------------- panels & nav */
  const NATIVE_PANEL = Object.freeze({ id: "native", label: "Native Codex" });
  const EMPTY_BRIDGE_PANEL = Object.freeze({
    id: "__bridge_empty__",
    label: "Bridge & channels",
  });

  // Which top-level group a bridge section belongs to. Sections not listed
  // fall back to the "Bridge" group so new backend sections still appear.
  const GROUP_FOR_SECTION = Object.freeze({
    runtime: "bridge",
    app_server: "bridge",
    webhooks: "bridge",
    qq: "channels",
    telegram: "channels",
    feishu: "channels",
    weixin: "channels",
  });
  const GROUP_LABEL = Object.freeze({ bridge: "Bridge", channels: "Channels" });
  const GROUP_ORDER = Object.freeze(["bridge", "channels"]);

  function groupForSection(id) {
    return GROUP_FOR_SECTION[id] || "bridge";
  }

  function panelEntries() {
    const entries = [{ ...NATIVE_PANEL, group: null }];
    const sections = state.bridgeResponse && Array.isArray(state.bridgeResponse.sections)
      ? state.bridgeResponse.sections
      : [];
    if (state.bridgeLoaded && sections.length === 0) {
      entries.push({ ...EMPTY_BRIDGE_PANEL, group: "bridge" });
    }
    for (const section of sections) {
      const id = String(section.id || "");
      if (!id) continue;
      entries.push({
        id,
        label: String(section.label || section.id || "Settings"),
        group: groupForSection(id),
      });
    }
    return entries;
  }

  function sectionDirtyCounts() {
    const counts = new Map();
    counts.set("native", nativeDirtyKeys().length);
    for (const key of bridgeDirtyKeys()) {
      const sectionId = state.fieldSection.get(key);
      if (!sectionId) continue;
      counts.set(sectionId, (counts.get(sectionId) || 0) + 1);
    }
    return counts;
  }

  function showPanel(id) {
    const entries = panelEntries();
    const target = entries.some((entry) => entry.id === id) ? id : NATIVE_PANEL.id;
    state.activePanel = target;

    for (const panel of document.querySelectorAll("[data-panel]")) {
      panel.classList.toggle("is-active", panel.dataset.panel === target);
    }
    for (const navItem of elements.sectionNav.querySelectorAll(".nav-item")) {
      const on = navItem.dataset.target === target;
      navItem.classList.toggle("is-active", on);
      if (on) navItem.setAttribute("aria-current", "true");
      else navItem.removeAttribute("aria-current");
    }
    const entry = entries.find((item) => item.id === target);
    if (entry) updateBreadcrumb(entry);
    if (elements.mainContent) elements.mainContent.scrollTop = 0;
    window.scrollTo({ top: 0 });
  }

  function updateBreadcrumb(entry) {
    if (!elements.breadcrumbCurrent) return;
    const groupLabel = entry.group ? GROUP_LABEL[entry.group] : "";
    if (elements.breadcrumbGroup) {
      elements.breadcrumbGroup.textContent = groupLabel;
      elements.breadcrumbGroup.hidden = !groupLabel;
    }
    if (elements.breadcrumbGroupSep) elements.breadcrumbGroupSep.hidden = !groupLabel;
    elements.breadcrumbCurrent.textContent = entry.label;
  }

  function updateNavDirty() {
    if (!elements.sectionNav) return;
    const counts = sectionDirtyCounts();
    for (const navItem of elements.sectionNav.querySelectorAll(".nav-item")) {
      const dot = navItem.querySelector(".nav-item__dirty");
      const dirtyLabel = navItem.querySelector(".nav-item__dirty-label");
      const dirty = counts.get(navItem.dataset.target) > 0;
      if (dot) dot.hidden = !dirty;
      if (dirtyLabel) dirtyLabel.hidden = !dirty;
    }
  }

  function appendNavItem(entry) {
    const item = createElement("button", "nav-item");
    item.type = "button";
    item.dataset.target = entry.id;
    const label = createElement("span", "nav-item__label", entry.label);
    const dot = createElement("span", "nav-item__dirty");
    dot.setAttribute("aria-hidden", "true");
    dot.hidden = true;
    const dirtyLabel = createElement("span", "sr-only nav-item__dirty-label", "Unsaved changes");
    dirtyLabel.hidden = true;
    item.append(label, dot, dirtyLabel);
    item.addEventListener("click", () => showPanel(entry.id));
    elements.sectionNav.append(item);
  }

  function rebuildNav() {
    if (!elements.sectionNav) return;
    const entries = panelEntries();
    elements.sectionNav.replaceChildren();

    // Native Codex sits on its own at the top.
    const nativeEntry = entries.find((entry) => entry.id === NATIVE_PANEL.id);
    if (nativeEntry) appendNavItem(nativeEntry);

    // Then one titled group per top-level category, in a stable order.
    for (const group of GROUP_ORDER) {
      const groupEntries = entries.filter((entry) => entry.group === group);
      if (!groupEntries.length) continue;
      const header = createElement("p", "nav-group", GROUP_LABEL[group]);
      elements.sectionNav.append(header);
      for (const entry of groupEntries) appendNavItem(entry);
    }

    if (!entries.some((entry) => entry.id === state.activePanel)) {
      state.activePanel = entries.some((entry) => entry.id === EMPTY_BRIDGE_PANEL.id)
        ? EMPTY_BRIDGE_PANEL.id
        : NATIVE_PANEL.id;
    }
    showPanel(state.activePanel);
    updateNavDirty();
  }


  function valuesEqual(left, right) {
    if (left === right) return true;
    if (left === null || left === undefined) return right === null || right === undefined;
    if (right === null || right === undefined) return false;
    if (typeof left === "number" && typeof right === "string" && right.trim() !== "") {
      return left === Number(right);
    }
    if (typeof right === "number" && typeof left === "string" && left.trim() !== "") {
      return right === Number(left);
    }
    return String(left) === String(right);
  }

  function displayError(error, fallback) {
    if (error instanceof ApiError && error.message) return error.message;
    if (error instanceof Error && error.message) return error.message;
    return fallback;
  }

  async function requestJson(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (options.csrf) headers.set("X-IMCodex-CSRF", options.csrf);

    let response;
    try {
      response = await fetch(url, {
        method: options.method || "GET",
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (error) {
      throw new ApiError("Could not reach the local IMCodex service.", 0, error);
    }

    const contentType = response.headers.get("content-type") || "";
    let body = null;
    if (response.status !== 204) {
      try {
        body = contentType.includes("application/json") ? await response.json() : await response.text();
      } catch (_error) {
        body = null;
      }
    }

    if (!response.ok) {
      const message =
        (body && typeof body === "object" && (body.message || body.error || body.detail)) ||
        (typeof body === "string" && body.trim()) ||
        `Request failed with status ${response.status}.`;
      throw new ApiError(String(message), response.status, body);
    }

    return body && typeof body === "object" ? body : {};
  }

  function setConnectionState(nextState, label) {
    elements.connectionState.dataset.state = nextState;
    elements.connectionStateLabel.textContent = label;
  }

  function updateOverallStatus() {
    const loadedCount = Number(state.nativeLoaded) + Number(state.bridgeLoaded);
    const errorCount = Number(Boolean(state.nativeError)) + Number(Boolean(state.bridgeError));

    if (state.nativeSaving || state.bridgeSaving) {
      setConnectionState("loading", "Saving");
    } else if (loadedCount === 2 && errorCount === 0) {
      setConnectionState("ready", "Up to date");
    } else if (loadedCount > 0) {
      setConnectionState(errorCount ? "error" : "loading", errorCount ? "Partially available" : "Loading");
    } else if (errorCount > 0) {
      setConnectionState("error", "Unavailable");
    } else {
      setConnectionState("loading", "Connecting");
    }

    if (errorCount === 0) {
      elements.globalError.hidden = true;
      return;
    }

    const failed = [];
    if (state.nativeError) failed.push("native Codex settings");
    if (state.bridgeError) failed.push("bridge settings");
    elements.globalErrorTitle.textContent = loadedCount ? "Some settings could not be loaded" : "Configuration is unavailable";
    elements.globalErrorMessage.textContent = `Could not load ${failed.join(" and ")}. You can retry without leaving this page.`;
    elements.globalError.hidden = false;
  }

  function setButtonLoading(button, loading, loadingText) {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent.trim();
    button.classList.toggle("is-loading", loading);
    button.textContent = loading ? loadingText : button.dataset.idleLabel;
    button.disabled = loading;
  }

  function showToast(message, kind = "success") {
    const modifier = kind === "error" ? " toast--error" : kind === "warning" ? " toast--warning" : "";
    const toast = createElement("div", `toast${modifier}`, message);
    elements.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 4400);
  }

  function showInlineError(container, title, message) {
    const panel = createElement("div", "inline-error");
    panel.append(createElement("strong", "", title), createElement("p", "", message));
    container.replaceChildren(panel);
    container.setAttribute("aria-busy", "false");
  }

  function normalizeOptions(rawOptions, preferredKeys = []) {
    const source = Array.isArray(rawOptions)
      ? rawOptions
      : rawOptions && Array.isArray(rawOptions.data)
        ? rawOptions.data
        : [];

    return source.flatMap((item) => {
      if (item === null || item === undefined) return [];
      if (["string", "number", "boolean"].includes(typeof item)) {
        return [{ value: item, label: String(item), description: "" }];
      }
      if (typeof item !== "object") return [];

      const valueKeys = [...preferredKeys, "value", "id", "key", "name", "mode", "reasoningEffort", "model"];
      const valueKey = valueKeys.find((key) => hasOwn(item, key));
      if (!valueKey) return [];
      const value = item[valueKey];
      const label = item.label || item.displayName || item.name || item.title || value;
      return [{
        value,
        label: String(label),
        description: String(item.description || item.details || ""),
        disabled: item.disabled === true || item.available === false,
      }];
    });
  }

  function optionToken(value) {
    if (value === null || value === undefined || value === "") return "__imcodex_default__";
    if (typeof value === "boolean") return value ? "__imcodex_true__" : "__imcodex_false__";
    return String(value);
  }

  function readConfigValue(config, keys, fallback) {
    const candidates = [
      config,
      config && config.config,
      config && config.effectiveConfig,
      config && config.effective,
    ].filter((item) => item && typeof item === "object");

    for (const candidate of candidates) {
      for (const key of keys) {
        if (hasOwn(candidate, key)) return candidate[key];
      }
    }
    return fallback;
  }

  function normalizePermissionValue(value) {
    if (value && typeof value === "object") {
      return value.mode || value.id || value.profile || value.value || "default";
    }
    return value === null || value === undefined || value === "" ? "default" : value;
  }

  function extractNativeValues(response) {
    const config = response && response.config && typeof response.config === "object" ? response.config : {};
    const fastValue = readConfigValue(config, ["fast", "fast_mode", "fastMode"], undefined);
    const serviceTier = readConfigValue(config, ["service_tier", "serviceTier"], undefined);
    return new Map([
      ["model", readConfigValue(config, ["model", "model_id", "modelId"], null)],
      [
        "reasoningEffort",
        readConfigValue(
          config,
          ["model_reasoning_effort", "modelReasoningEffort", "reasoning_effort", "reasoningEffort"],
          null,
        ),
      ],
      ["personality", readConfigValue(config, ["personality"], "default") || "default"],
      [
        "permissionMode",
        normalizePermissionValue(
          readConfigValue(config, ["permission_mode", "permissionMode", "default_permissions", "defaultPermissions"], "default"),
        ),
      ],
      ["fast", fastValue === undefined ? ["priority", "fast"].includes(String(serviceTier || "").toLowerCase()) : Boolean(fastValue)],
    ]);
  }

  function nativeOptionsFor(key, response) {
    if (key === "model") {
      const configured = normalizeOptions(response.models, ["id", "model"]);
      const models = Array.isArray(response.models) ? response.models : [];
      const managedEffort = nativeSettingReadOnly("reasoningEffort", response)
        ? state.nativeDraft.get("reasoningEffort")
        : null;
      const managedFast = nativeSettingReadOnly("fast", response) && state.nativeDraft.get("fast") === true;
      const modelIsIncompatible = (model) => {
        const efforts = model && Array.isArray(model.supportedReasoningEfforts)
          ? normalizeOptions(model.supportedReasoningEfforts, ["reasoningEffort", "value"])
          : null;
        const incompatibleEffort = managedEffort !== null
          && managedEffort !== undefined
          && efforts !== null
          && !efforts.some((item) => valuesEqual(item.value, managedEffort));
        return incompatibleEffort || (managedFast && !modelSupportsFastMode(model));
      };
      const options = configured.map((option) => {
        const model = models.find((item) => item && valuesEqual(item.id || item.model, option.value));
        return {
          ...option,
          disabled: option.disabled === true || modelIsIncompatible(model),
        };
      });
      const defaultModel = models.find((model) => model && model.isDefault === true);
      options.unshift({
        value: null,
        label: nativeDefaultLabel("model", response),
        description: "",
        disabled: defaultModel ? modelIsIncompatible(defaultModel) : false,
      });
      return options;
    }
    if (key === "reasoningEffort") {
      const model = selectedModel(response);
      if (model && Array.isArray(model.supportedReasoningEfforts)) {
        return normalizeOptions(model.supportedReasoningEfforts, ["reasoningEffort", "value"]);
      }
      return normalizeOptions(response.reasoningEfforts, ["reasoningEffort", "value"]);
    }
    if (key === "permissionMode") {
      return normalizeOptions(response.permissionModes, ["mode", "id", "value"]);
    }
    if (key === "personality") {
      const configured = normalizeOptions(response.personalityOptions, ["value", "id", "name"]);
      const defaults = ["default", "none", "friendly", "pragmatic"];
      const byValue = new Map(configured.map((option) => [String(option.value), option]));
      for (const value of defaults) {
        if (!byValue.has(value)) {
          byValue.set(value, {
            value,
            label: value === "default" ? "Default" : value.charAt(0).toUpperCase() + value.slice(1),
            description: "",
          });
        }
      }
      const personalitySupported = response.personalityAvailable !== false
        && selectedModel(response)?.supportsPersonality !== false;
      return [...byValue.values()].map((option) => ({
        ...option,
        disabled: option.disabled === true || (!personalitySupported && option.value !== "default"),
      }));
    }
    return [];
  }

  function selectedModel(response) {
    const models = Array.isArray(response.models) ? response.models : [];
    const selected = state.nativeDraft.get("model");
    if (selected === null || selected === undefined || selected === "") {
      const defaultModel = models.find((model) => model && model.isDefault === true);
      if (defaultModel) return defaultModel;
      const baseline = state.nativeBaseline.get("model");
      if (baseline === null || baseline === undefined || baseline === "") {
        const nativeSelected = response && response.selectedModel;
        return models.find((model) => model && valuesEqual(model.id || model.model, nativeSelected)) || null;
      }
      return null;
    }
    return models.find((model) => model && valuesEqual(model.id || model.model, selected)) || null;
  }

  function modelSupportsFastMode(model) {
    if (!model || typeof model !== "object") return false;
    const defaultTier = String(model.defaultServiceTier || "").trim().toLowerCase();
    if (["priority", "fast"].includes(defaultTier)) return true;

    const serviceTiers = Array.isArray(model.serviceTiers) ? model.serviceTiers : [];
    if (serviceTiers.some((tier) => {
      if (!tier || typeof tier !== "object") return false;
      const id = String(tier.id || "").trim().toLowerCase();
      return ["priority", "fast"].includes(id);
    })) return true;

    const additional = Array.isArray(model.additionalSpeedTiers) ? model.additionalSpeedTiers : [];
    return additional.some((tier) => ["priority", "fast"].includes(String(tier || "").trim().toLowerCase()));
  }

  function fastModeControlDisabled(response) {
    if (nativeSettingReadOnly("fast", response)) return true;
    if (response && response.fastAvailable === false) {
      return state.nativeDraft.get("fast") !== true;
    }
    if (modelSupportsFastMode(selectedModel(response))) return false;
    return state.nativeBaseline.get("fast") !== true && state.nativeDraft.get("fast") !== true;
  }

  function nativeSettingReadOnly(key, response) {
    const readOnly = response && Array.isArray(response.readOnlySettings)
      ? response.readOnlySettings
      : [];
    return readOnly.includes(key);
  }

  function nativeDefaultLabel(key, response) {
    if (key === "model") {
      const model = (Array.isArray(response.models) ? response.models : [])
        .find((item) => item && item.isDefault === true);
      const label = model && (model.displayName || model.label || model.id || model.model);
      return label ? `Use Codex default (${label})` : "Use Codex default";
    }
    if (key === "reasoningEffort") {
      const model = selectedModel(response);
      const effort = (model && model.defaultReasoningEffort) || response.defaultReasoningEffort;
      return effort ? `Use Codex default (${effort})` : "Use Codex default";
    }
    return "Use Codex default";
  }

  function nativeOptionDescription(key, value, response) {
    if (value === null || value === undefined || value === "") {
      const defaultLabel = nativeDefaultLabel(key, response);
      return defaultLabel === "Use Codex default"
        ? ""
        : defaultLabel.replace("Use Codex default (", "Currently resolves to ").replace(/\)$/, ".");
    }
    const option = nativeOptionsFor(key, response).find((item) => valuesEqual(item.value, value));
    return option ? String(option.description || "") : "";
  }

  function reconcileReasoningEffortForModel(response) {
    if (nativeSettingReadOnly("reasoningEffort", response)) return;
    const model = selectedModel(response);
    if (!model || !Array.isArray(model.supportedReasoningEfforts)) return;
    const supported = normalizeOptions(model.supportedReasoningEfforts, ["reasoningEffort", "value"]);
    const current = state.nativeDraft.get("reasoningEffort");
    if (current === null || current === undefined || supported.some((item) => valuesEqual(item.value, current))) {
      return;
    }
    const modelDefault = model && model.defaultReasoningEffort;
    state.nativeDraft.set(
      "reasoningEffort",
      supported.some((item) => valuesEqual(item.value, modelDefault)) ? modelDefault : null,
    );
  }

  function reconcileModelCapabilities(response) {
    reconcileReasoningEffortForModel(response);
    const model = selectedModel(response);
    if (
      (response.personalityAvailable === false || (model && model.supportsPersonality === false))
      && !nativeSettingReadOnly("personality", response)
      && state.nativeDraft.get("personality") !== "default"
    ) {
      state.nativeDraft.set("personality", "default");
    }
    if (
      state.nativeDraft.get("fast") === true
      && !nativeSettingReadOnly("fast", response)
      && (response.fastAvailable === false || !modelSupportsFastMode(model))
    ) {
      state.nativeDraft.set("fast", false);
    }
  }

  function makeSelectControl({
    id,
    value,
    options,
    includeDefault = false,
    defaultLabel = "Use Codex default",
    disabled = false,
    onChange,
  }) {
    const wrapper = createElement("div", "select-wrap");
    const select = createElement("select", "select");
    select.id = id;
    select.disabled = disabled;
    const valueByToken = new Map();
    const available = [...options];

    if (includeDefault && !available.some((option) => optionToken(option.value) === "__imcodex_default__")) {
      available.unshift({ value: null, label: defaultLabel, description: "" });
    }

    if (!available.some((option) => valuesEqual(option.value, value))) {
      available.unshift({
        value,
        label: value === null || value === undefined || value === "" ? "Use Codex default" : String(value),
        description: "Current value",
      });
    }

    for (const [index, option] of available.entries()) {
      let token = optionToken(option.value);
      if (valueByToken.has(token) && !valuesEqual(valueByToken.get(token), option.value)) token = `${token}-${index}`;
      valueByToken.set(token, option.value);
      const optionElement = createElement("option", "", option.label);
      optionElement.value = token;
      optionElement.disabled = Boolean(option.disabled);
      optionElement.title = option.description || "";
      select.append(optionElement);
      if (valuesEqual(option.value, value)) select.value = token;
    }

    select.addEventListener("change", () => onChange(valueByToken.get(select.value), select));
    wrapper.append(select);
    return { wrapper, input: select, setValue(nextValue) {
      const match = [...valueByToken.entries()].find((entry) => valuesEqual(entry[1], nextValue));
      if (match) select.value = match[0];
    } };
  }

  function makeToggleControl({ id, value, disabled = false, onChange }) {
    const row = createElement("div", "toggle-row");
    const stateLabel = createElement("span", "toggle-row__state", value ? "On" : "Off");
    const label = createElement("label", "toggle");
    const input = createElement("input");
    input.id = id;
    input.type = "checkbox";
    input.checked = Boolean(value);
    input.disabled = disabled;
    input.setAttribute("role", "switch");
    const track = createElement("span", "toggle__track");
    label.append(input, track);
    row.append(stateLabel, label);
    input.addEventListener("change", () => {
      stateLabel.textContent = input.checked ? "On" : "Off";
      onChange(input.checked, input);
    });
    return { wrapper: row, input, setValue(nextValue) {
      input.checked = Boolean(nextValue);
      stateLabel.textContent = input.checked ? "On" : "Off";
    } };
  }

  function renderNativeWarnings(warnings) {
    elements.nativeWarnings.replaceChildren();
    let messages = [];
    if (Array.isArray(warnings)) {
      messages = warnings.map((warning) => typeof warning === "string" ? warning : warning && (warning.message || warning.detail));
    } else if (warnings && typeof warnings === "object") {
      messages = Object.values(warnings).map((warning) => typeof warning === "string" ? warning : warning && (warning.message || warning.detail));
    } else if (typeof warnings === "string") {
      messages = [warnings];
    }
    for (const message of messages.filter(Boolean)) {
      elements.nativeWarnings.append(createElement("div", "warning-item", message));
    }
  }

  function renderNativeSettings() {
    const response = state.nativeResponse || {};
    elements.nativeSettings.replaceChildren();
    elements.nativeSettings.setAttribute("aria-busy", "false");
    state.nativeControls.clear();

    for (const definition of NATIVE_DEFINITIONS) {
      const card = createElement("div", "native-setting");
      card.dataset.setting = definition.key;
      const copy = createElement("div", "native-setting__copy");
      const label = createElement("label", "", definition.label);
      const managed = nativeSettingReadOnly(definition.key, response);
      const description = createElement(
        "p",
        "",
        managed ? `${definition.description} Managed by Codex requirements.` : definition.description,
      );
      const inputId = `native-${safeId(definition.key)}`;
      description.id = `${inputId}-description`;
      label.htmlFor = inputId;
      copy.append(label, description);

      const currentValue = state.nativeDraft.get(definition.key);
      const baseDisabled = managed || (definition.key === "fast" && fastModeControlDisabled(response));
      let optionDetail = null;
      const onChange = (nextValue) => {
        state.nativeDraft.set(definition.key, nextValue);
        if (definition.key === "model") {
          if (!nativeSettingReadOnly("fast", response)) {
            if (valuesEqual(nextValue, state.nativeBaseline.get("model"))) {
              state.nativeForcedDirty.delete("fast");
            } else {
              state.nativeForcedDirty.add("fast");
            }
          }
          reconcileModelCapabilities(response);
          renderNativeSettings();
          window.requestAnimationFrame(() => {
            const modelControl = state.nativeControls.get("model");
            if (modelControl) modelControl.control.input.focus();
          });
          return;
        }
        if (optionDetail) {
          const detail = nativeOptionDescription(definition.key, nextValue, response);
          optionDetail.textContent = detail;
          optionDetail.hidden = !detail;
        }
        updateNativeDirtyState();
      };
      const control = definition.kind === "boolean"
        ? makeToggleControl({ id: inputId, value: currentValue, disabled: baseDisabled, onChange })
        : makeSelectControl({
            id: inputId,
            value: currentValue,
            options: nativeOptionsFor(definition.key, response),
            includeDefault: definition.key === "model" || definition.key === "reasoningEffort",
            defaultLabel: nativeDefaultLabel(definition.key, response),
            onChange,
          });
      const controlContainer = createElement("div", "native-setting__control");
      controlContainer.append(control.wrapper);
      const describedBy = [description.id];
      if (definition.kind === "select") {
        const detail = nativeOptionDescription(definition.key, currentValue, response);
        optionDetail = createElement("p", "native-setting__option-detail", detail);
        optionDetail.id = `${inputId}-option-detail`;
        optionDetail.hidden = !detail;
        controlContainer.append(optionDetail);
        describedBy.push(optionDetail.id);
      }
      control.input.setAttribute("aria-describedby", describedBy.join(" "));
      if (baseDisabled) {
        control.input.title = managed
          ? "Managed by Codex requirements."
          : "Fast mode is not available for the selected model.";
      }

      card.append(copy, controlContainer);
      elements.nativeSettings.append(card);
      state.nativeControls.set(definition.key, { card, control, baseDisabled });
    }

    renderNativeWarnings(response.warnings);
    elements.nativeFooter.hidden = false;
    updateNativeDirtyState();
  }

  function nativeDirtyKeys() {
    return NATIVE_DEFINITIONS
      .map((definition) => definition.key)
      .filter((key) => (
        state.nativeForcedDirty.has(key)
        || !valuesEqual(state.nativeBaseline.get(key), state.nativeDraft.get(key))
      ));
  }

  function updateNativeDirtyState() {
    const dirtyKeys = nativeDirtyKeys();
    for (const [key, references] of state.nativeControls.entries()) {
      references.card.classList.toggle("is-dirty", dirtyKeys.includes(key));
      references.control.input.disabled = references.baseDisabled || state.nativeSaving || state.refreshing;
    }
    elements.saveNativeButton.disabled =
      dirtyKeys.length === 0 || state.nativeSaving || state.refreshing || !state.csrfToken;
    elements.nativeSaveState.dataset.state = dirtyKeys.length ? "dirty" : "clean";
    elements.nativeSaveState.textContent = dirtyKeys.length
      ? `${dirtyKeys.length} native ${dirtyKeys.length === 1 ? "setting" : "settings"} changed`
      : "No native changes";
    updateNavDirty();
  }

  async function loadNative({ preserveOnError = false } = {}) {
    state.nativeError = null;
    try {
      const response = await requestJson(API.native);
      state.nativeResponse = response;
      state.csrfToken = typeof response.csrfToken === "string" ? response.csrfToken : "";
      state.nativeBaseline = extractNativeValues(response);
      state.nativeDraft = new Map(state.nativeBaseline);
      state.nativeForcedDirty = new Set();
      state.nativeLoaded = true;
      renderNativeSettings();
      return true;
    } catch (error) {
      state.nativeError = error;
      if (preserveOnError && state.nativeResponse) {
        state.nativeLoaded = true;
        renderNativeSettings();
        elements.nativeWarnings.append(createElement(
          "div",
          "warning-item",
          displayError(error, "The applied settings could not be refreshed from Codex."),
        ));
        return false;
      }
      state.nativeLoaded = false;
      showInlineError(
        elements.nativeSettings,
        "Native settings unavailable",
        displayError(error, "Codex did not return its current configuration."),
      );
      elements.nativeFooter.hidden = true;
      renderNativeWarnings([]);
      return false;
    } finally {
      updateBridgeDirtyState();
      updateOverallStatus();
    }
  }

  async function saveNative() {
    const dirtyKeys = nativeDirtyKeys();
    if (!dirtyKeys.length || state.nativeSaving || state.refreshing) return;
    if (!state.csrfToken) {
      showToast("Reload the page before saving native settings.", "error");
      return;
    }

    state.nativeSaving = true;
    setButtonLoading(elements.saveNativeButton, true, "Applying");
    updateNativeDirtyState();
    updateBusyActions();
    updateOverallStatus();
    const failures = [];
    const appliedKeys = [];
    const overriddenKeys = [];
    const requestedValues = new Map(dirtyKeys.map((key) => [key, state.nativeDraft.get(key)]));
    const requestedForcedDirty = new Set(state.nativeForcedDirty);
    const preferenceKeys = dirtyKeys.filter((key) => key !== "permissionMode");
    const operations = [];
    if (preferenceKeys.length) {
      operations.push({
        setting: "preferences",
        keys: preferenceKeys,
        value: Object.fromEntries(preferenceKeys.map((key) => [key, state.nativeDraft.get(key)])),
      });
    }
    if (dirtyKeys.includes("permissionMode")) {
      operations.push({
        setting: "permissionMode",
        keys: ["permissionMode"],
        value: state.nativeDraft.get("permissionMode"),
      });
    }
    let appliedCount = 0;

    for (const operation of operations) {
      try {
        const writeResponse = await requestJson(API.native, {
          method: "PUT",
          csrf: state.csrfToken,
          body: { setting: operation.setting, value: operation.value },
        });
        appliedCount += operation.keys.length;
        appliedKeys.push(...operation.keys);
        const status = String(writeResponse?.result?.status || "").toLowerCase();
        if (status.includes("overridden")) overriddenKeys.push(...operation.keys);
      } catch (error) {
        failures.push(...operation.keys.map((key) => ({ key, error })));
      }
    }

    if (appliedCount > 0) {
      for (const key of appliedKeys) {
        state.nativeBaseline.set(key, requestedValues.get(key));
      }
      const refreshed = await loadNative({ preserveOnError: true });
      if (refreshed && failures.length) {
        for (const failure of failures) {
          state.nativeDraft.set(failure.key, requestedValues.get(failure.key));
          if (requestedForcedDirty.has(failure.key)) state.nativeForcedDirty.add(failure.key);
        }
        renderNativeSettings();
      }
    }

    if (failures.length) {
      state.nativeSaving = false;
      setButtonLoading(elements.saveNativeButton, false, "Applying");
      updateNativeDirtyState();
      updateBusyActions();
      updateOverallStatus();
      const labelByKey = new Map(NATIVE_DEFINITIONS.map((definition) => [definition.key, definition.label]));
      const labels = failures.map((failure) => labelByKey.get(failure.key) || failure.key).join(", ");
      elements.nativeSaveState.dataset.state = "error";
      elements.nativeSaveState.textContent = `Could not apply: ${labels}`;
      showToast(displayError(failures[0].error, "Some native settings were not applied."), "error");
      return;
    }

    state.nativeSaving = false;
    setButtonLoading(elements.saveNativeButton, false, "Applying");
    updateNativeDirtyState();
    updateBusyActions();
    updateOverallStatus();
    if (overriddenKeys.length) {
      const labelByKey = new Map(NATIVE_DEFINITIONS.map((definition) => [definition.key, definition.label]));
      const labels = overriddenKeys.map((key) => labelByKey.get(key) || key).join(", ");
      showToast(
        `${labels} saved, but higher-priority native Codex configuration remains effective.`,
        "warning",
      );
    } else {
      showToast(
        state.nativeLoaded
          ? "Native Codex settings applied."
          : "Settings were applied, but Codex could not be refreshed.",
        state.nativeLoaded ? "success" : "error",
      );
    }
  }

  function normalizeBridgeKind(field) {
    const kind = String(field.kind || field.type || "text").trim().toLowerCase();
    if (["bool", "boolean", "switch", "toggle"].includes(kind)) return "boolean";
    if (["int", "integer"].includes(kind)) return "integer";
    if (["float", "number", "decimal"].includes(kind)) return "number";
    if (["multiline", "textarea", "list"].includes(kind)) return "textarea";
    if (["password", "token", "secret"].includes(kind)) return "secret";
    if (["select", "choice", "enum"].includes(kind)) return "select";
    if (["url", "uri"].includes(kind)) return "url";
    return "text";
  }

  function bridgeFieldValue(field) {
    const kind = normalizeBridgeKind(field);
    if (kind === "boolean") return Boolean(field.value);
    if ((kind === "integer" || kind === "number") && field.value !== "" && field.value !== null && field.value !== undefined) {
      return Number(field.value);
    }
    return field.value === null || field.value === undefined ? "" : field.value;
  }

  function bridgeDirtyKeys() {
    const keys = [];
    for (const [key, field] of state.bridgeFields.entries()) {
      if (normalizeBridgeKind(field) === "secret") {
        if ((state.secretActions.get(key) || {}).action !== "preserve") keys.push(key);
      } else if (!valuesEqual(state.bridgeBaseline.get(key), state.bridgeDraft.get(key))) {
        keys.push(key);
      }
    }
    return keys;
  }

  function updateBridgeDirtyState() {
    const dirtyKeys = bridgeDirtyKeys();
    const dirty = new Set(dirtyKeys);
    for (const field of elements.bridgeSections.querySelectorAll(".field[data-field-key]")) {
      field.classList.toggle("is-dirty", dirty.has(field.dataset.fieldKey));
    }

    elements.saveDock.hidden = dirtyKeys.length === 0;
    elements.bridgeChangeCount.textContent = `${dirtyKeys.length} unsaved bridge ${dirtyKeys.length === 1 ? "change" : "changes"}`;
    elements.saveBridgeButton.disabled =
      dirtyKeys.length === 0 || state.bridgeSaving || state.refreshing || !state.csrfToken;
    elements.discardBridgeButton.disabled = state.bridgeSaving || state.refreshing;
    updateBridgeControlAvailability();
    updateNavDirty();
  }

  function updateBridgeControlAvailability() {
    const locked = state.bridgeSaving || state.refreshing;
    for (const control of elements.bridgeSections.querySelectorAll("input, select, textarea")) {
      if (!hasOwn(control.dataset, "baseDisabled")) {
        control.dataset.baseDisabled = control.disabled ? "true" : "false";
      }
      control.disabled = locked || control.dataset.baseDisabled === "true";
    }
  }

  function updateBusyActions() {
    const busy = state.nativeSaving || state.bridgeSaving || state.refreshing;
    elements.refreshButton.disabled = busy;
    elements.retryButton.disabled = busy;
  }

  function makeBridgeInput(field, id) {
    const kind = normalizeBridgeKind(field);
    const editable = field.editable !== false;
    const currentValue = state.bridgeDraft.get(field.key);
    const options = normalizeOptions(field.options, ["value", "id", "key"]);

    const onChange = (nextValue) => {
      state.bridgeDraft.set(field.key, nextValue);
      updateBridgeDirtyState();
    };

    if (kind === "boolean") {
      return makeToggleControl({ id, value: currentValue, disabled: !editable, onChange }).wrapper;
    }

    if (kind === "select" || options.length) {
      return makeSelectControl({
        id,
        value: currentValue,
        options,
        disabled: !editable,
        onChange,
      }).wrapper;
    }

    if (kind === "textarea") {
      const textarea = createElement("textarea", "textarea");
      textarea.id = id;
      textarea.value = String(currentValue ?? "");
      textarea.disabled = !editable;
      textarea.spellcheck = false;
      if (Number.isInteger(field.maxLength) && field.maxLength > 0) textarea.maxLength = field.maxLength;
      if (Number.isInteger(field.max_length) && field.max_length > 0) textarea.maxLength = field.max_length;
      textarea.addEventListener("input", () => onChange(textarea.value));
      return textarea;
    }

    const input = createElement("input", "input");
    input.id = id;
    input.disabled = !editable;
    input.value = String(currentValue ?? "");
    input.type = kind === "integer" || kind === "number" ? "number" : kind === "url" ? "url" : "text";
    if (kind === "integer") input.step = "1";
    if (kind === "number") input.step = "any";
    if (Number.isFinite(field.minimum)) input.min = String(field.minimum);
    if (Number.isFinite(field.maximum)) input.max = String(field.maximum);
    if (Number.isInteger(field.maxLength) && field.maxLength > 0) input.maxLength = field.maxLength;
    if (Number.isInteger(field.max_length) && field.max_length > 0) input.maxLength = field.max_length;
    input.spellcheck = false;
    input.addEventListener("input", () => {
      if (kind === "integer" || kind === "number") {
        onChange(input.value === "" ? "" : Number(input.value));
      } else {
        onChange(input.value);
      }
    });
    return input;
  }

  function makeSecretControl(field, id) {
    const secretConfigured = field.secretConfigured === true || field.configured === true;
    const box = createElement("div", "secret-box");
    const status = createElement("div", "secret-status");
    const statusValue = createElement("span", "secret-status__value");
    const statusDot = createElement("span", `secret-status__dot${secretConfigured ? " is-configured" : ""}`);
    statusDot.setAttribute("aria-hidden", "true");
    statusValue.append(statusDot, document.createTextNode(secretConfigured ? "Configured" : "Not configured"));
    const masked = createElement("span", "secret-status__masked", secretConfigured ? "••••••••" : "No stored value");
    status.append(statusValue, masked);

    const actions = createElement("div", "secret-actions");
    const segmented = createElement("div", "segmented");
    segmented.setAttribute("role", "radiogroup");
    segmented.setAttribute("aria-label", `${field.label || field.key} secret action`);
    const replacement = createElement("input", "input secret-replacement");
    replacement.id = id;
    replacement.type = "password";
    replacement.autocomplete = "new-password";
    replacement.spellcheck = false;
    replacement.placeholder = "Enter a new secret";
    replacement.hidden = true;
    const replacementLabel = createElement(
      "label",
      "sr-only",
      `New value for ${field.label || field.key}`,
    );
    replacementLabel.htmlFor = id;
    const clearNote = createElement("p", "secret-clear-note", "The stored secret will be removed when you save changes.");
    clearNote.hidden = true;

    const updateAction = (action, shouldFocus = false) => {
      const previousValue = (state.secretActions.get(field.key) || {}).value || "";
      state.secretActions.set(field.key, { action, value: action === "replace" ? replacement.value || previousValue : "" });
      replacement.hidden = action !== "replace";
      clearNote.hidden = action !== "clear";
      if (shouldFocus && action === "replace") replacement.focus();
      updateBridgeDirtyState();
    };

    const actionsList = [
      { value: "preserve", label: "Keep" },
      { value: "replace", label: "Replace" },
      { value: "clear", label: "Clear", disabled: !secretConfigured },
    ];
    for (const action of actionsList) {
      const label = createElement("label");
      const radio = createElement("input");
      radio.type = "radio";
      radio.name = `${id}-action`;
      radio.value = action.value;
      radio.checked = action.value === "preserve";
      radio.disabled = field.editable === false || action.disabled;
      const labelText = createElement("span", "", action.label);
      radio.addEventListener("change", () => {
        if (radio.checked) updateAction(action.value, true);
      });
      label.append(radio, labelText);
      segmented.append(label);
    }

    replacement.disabled = field.editable === false;
    replacement.addEventListener("input", () => {
      state.secretActions.set(field.key, { action: "replace", value: replacement.value });
      updateBridgeDirtyState();
    });
    actions.append(segmented, replacementLabel, replacement, clearNote);
    box.append(status, actions);
    return box;
  }

  function renderBridgeField(field, sectionIndex, fieldIndex) {
    const wrapper = createElement("div", "field");
    wrapper.dataset.fieldKey = String(field.key);
    const id = `bridge-${sectionIndex}-${fieldIndex}-${safeId(field.key)}`;
    const kind = normalizeBridgeKind(field);
    const heading = createElement("div", "field__heading");
    const label = createElement(kind === "secret" ? "span" : "label", "field__label", field.label || field.key);
    if (kind === "secret") {
      label.id = `${id}-label`;
    } else {
      label.htmlFor = id;
    }
    heading.append(label);

    if (field.editable === false) {
      heading.append(createElement("span", "field__readonly", "Read only"));
    } else if (field.default !== null && field.default !== undefined && normalizeBridgeKind(field) !== "secret") {
      const defaultValue = typeof field.default === "boolean" ? (field.default ? "on" : "off") : String(field.default);
      heading.append(createElement("span", "field__default", `default: ${defaultValue}`));
    }
    wrapper.append(heading);

    if (field.description) {
      const description = createElement("p", "field__description", field.description);
      description.id = `${id}-description`;
      wrapper.append(description);
    }

    const control = kind === "secret"
      ? makeSecretControl(field, id)
      : makeBridgeInput(field, id);
    if (kind === "secret") {
      control.setAttribute("role", "group");
      control.setAttribute("aria-labelledby", label.id);
    }
    if (field.description) {
      const input = control.matches && control.matches("input, select, textarea")
        ? control
        : control.querySelector("input:not([type=radio]), select, textarea");
      if (input) input.setAttribute("aria-describedby", `${id}-description`);
    }
    wrapper.append(control);
    return wrapper;
  }

  function renderBridgeSettings() {
    const sections = Array.isArray(state.bridgeResponse && state.bridgeResponse.sections)
      ? state.bridgeResponse.sections
      : [];
    elements.bridgeSections.replaceChildren();
    elements.bridgeSections.setAttribute("aria-busy", "false");
    state.bridgeFields.clear();
    state.bridgeBaseline.clear();
    state.bridgeDraft.clear();
    state.secretActions.clear();
    state.fieldSection.clear();

    if (!sections.length) {
      const panel = createElement("section", "panel panel--bridge-empty");
      panel.dataset.panel = EMPTY_BRIDGE_PANEL.id;
      panel.dataset.navLabel = EMPTY_BRIDGE_PANEL.label;
      const heading = createElement("div", "panel-heading");
      const row = createElement("div", "panel-heading__row");
      row.append(
        createElement("h2", "", EMPTY_BRIDGE_PANEL.label),
        createElement("span", "authority-badge authority-badge--bridge", "IMCodex-owned"),
      );
      heading.append(row);
      const empty = createElement("div", "empty-state");
      empty.append(
        createElement("strong", "", "No bridge settings exposed"),
        createElement("p", "", "This IMCodex build did not return any editable bridge sections."),
      );
      panel.append(heading, empty);
      elements.bridgeSections.append(panel);
      updateBridgeDirtyState();
      rebuildNav();
      return;
    }

    sections.forEach((section, sectionIndex) => {
      const fields = Array.isArray(section.fields) ? section.fields.filter((field) => field && field.key) : [];
      const sectionId = String(section.id || `section-${sectionIndex}`);

      const panel = createElement("section", "panel panel--bridge");
      panel.dataset.panel = sectionId;
      panel.dataset.navLabel = section.label || sectionId;
      const panelHeadingId = `panel-${sectionIndex}-${safeId(sectionId)}-heading`;
      panel.setAttribute("aria-labelledby", panelHeadingId);

      const panelHeading = createElement("div", "panel-heading");
      const headingRow = createElement("div", "panel-heading__row");
      const heading = createElement("h2", "", section.label || sectionId || "Settings");
      heading.id = panelHeadingId;
      headingRow.append(
        heading,
        createElement("span", "authority-badge authority-badge--bridge", "IMCodex-owned"),
        createElement("span", "restart-pill", "Restart after save"),
      );
      panelHeading.append(headingRow);
      if (section.description) panelHeading.append(createElement("p", "", section.description));
      panel.append(panelHeading);

      const card = createElement("article", "config-card");
      card.dataset.sectionId = sectionId;

      const fieldsContainer = createElement("div", "config-card__fields");
      fields.forEach((field, fieldIndex) => {
        const key = String(field.key);
        const normalizedField = { ...field, key };
        state.bridgeFields.set(key, normalizedField);
        state.fieldSection.set(key, sectionId);
        if (normalizeBridgeKind(normalizedField) === "secret") {
          state.secretActions.set(key, { action: "preserve", value: "" });
        } else {
          const value = bridgeFieldValue(normalizedField);
          state.bridgeBaseline.set(key, value);
          state.bridgeDraft.set(key, value);
        }
        fieldsContainer.append(renderBridgeField(normalizedField, sectionIndex, fieldIndex));
      });

      card.append(fieldsContainer);
      panel.append(card);
      elements.bridgeSections.append(panel);
    });

    updateBridgeDirtyState();
    rebuildNav();
  }

  function clearBridgeDraftState() {
    state.bridgeFields.clear();
    state.bridgeBaseline.clear();
    state.bridgeDraft.clear();
    state.secretActions.clear();
    state.fieldSection.clear();
  }

  function clearNativeDraftState() {
    state.nativeBaseline.clear();
    state.nativeDraft.clear();
    state.nativeForcedDirty.clear();
  }

  async function loadBridge() {
    state.bridgeError = null;
    try {
      const response = await requestJson(API.config);
      state.bridgeResponse = response;
      state.revision = String(response.revision || "");
      if (typeof response.csrfToken === "string" && response.csrfToken) {
        state.csrfToken = response.csrfToken;
      }
      state.bridgeLoaded = true;
      renderBridgeSettings();
      state.restartPending = response.restartRequired === true;
      elements.restartBanner.hidden = !state.restartPending;
    } catch (error) {
      state.bridgeError = error;
      state.bridgeLoaded = false;
      showInlineError(
        elements.bridgeSections,
        "Bridge settings unavailable",
        displayError(error, "IMCodex did not return its bridge configuration."),
      );
      state.bridgeResponse = null;
      rebuildNav();
      updateBridgeDirtyState();
    } finally {
      updateOverallStatus();
    }
  }

  function validateBridgeChanges() {
    let firstInvalid = null;
    for (const [key, field] of state.bridgeFields.entries()) {
      const fieldElement = [...elements.bridgeSections.querySelectorAll(".field[data-field-key]")]
        .find((element) => element.dataset.fieldKey === key);
      if (!fieldElement) continue;
      const oldError = fieldElement.querySelector(".field__error");
      if (oldError) oldError.remove();
      const input = fieldElement.querySelector("input:not([type=radio]), select, textarea");
      if (input) {
        input.removeAttribute("aria-invalid");
        const describedBy = (input.getAttribute("aria-describedby") || "")
          .split(/\s+/)
          .filter((id) => id && !id.endsWith("-validation-error"));
        if (describedBy.length) input.setAttribute("aria-describedby", describedBy.join(" "));
        else input.removeAttribute("aria-describedby");
      }

      let errorMessage = "";
      if (normalizeBridgeKind(field) === "secret") {
        const secret = state.secretActions.get(key);
        if (secret && secret.action === "replace" && !String(secret.value || "").trim()) {
          errorMessage = "Enter a new secret, or choose Keep.";
        }
      } else {
        if (input && typeof input.checkValidity === "function" && !input.checkValidity()) {
          errorMessage = input.validationMessage || "Check this value.";
        }
      }

      if (errorMessage) {
        const error = createElement("p", "field__error", errorMessage);
        error.id = `${safeId(key)}-validation-error`;
        error.setAttribute("role", "alert");
        fieldElement.append(error);
        if (input) {
          input.setAttribute("aria-invalid", "true");
          const describedBy = (input.getAttribute("aria-describedby") || "").trim();
          input.setAttribute("aria-describedby", `${describedBy} ${error.id}`.trim());
        }
        if (!firstInvalid) firstInvalid = fieldElement;
      }
    }

    if (firstInvalid) {
      const sectionId = state.fieldSection.get(firstInvalid.dataset.fieldKey);
      if (sectionId && sectionId !== state.activePanel) showPanel(sectionId);
      firstInvalid.scrollIntoView({ behavior: "smooth", block: "center" });
      const input = firstInvalid.querySelector("input:not([type=radio]), select, textarea");
      if (input) input.focus({ preventScroll: true });
      return false;
    }
    return true;
  }

  function bridgePayload() {
    const values = {};
    const secrets = {};
    for (const key of bridgeDirtyKeys()) {
      const field = state.bridgeFields.get(key);
      if (normalizeBridgeKind(field) === "secret") {
        const change = state.secretActions.get(key);
        secrets[key] = change.action === "replace"
          ? { action: "replace", value: change.value }
          : { action: "clear" };
      } else {
        values[key] = state.bridgeDraft.get(key);
      }
    }
    return { revision: state.revision, values, secrets };
  }

  function acceptSavedBridgeDraft(response) {
    for (const [key, field] of state.bridgeFields.entries()) {
      if (normalizeBridgeKind(field) === "secret") {
        state.secretActions.set(key, { action: "preserve", value: "" });
      } else {
        state.bridgeBaseline.set(key, state.bridgeDraft.get(key));
      }
    }
    if (response && response.revision) state.revision = String(response.revision);
    updateBridgeDirtyState();
  }

  function openConflictDialog() {
    if (typeof elements.conflictDialog.showModal === "function") {
      elements.conflictDialog.showModal();
    } else {
      const shouldReload = window.confirm("Configuration changed elsewhere. Reload the latest values?");
      if (shouldReload) void reloadBridgeAfterConflict();
    }
  }

  async function saveBridge() {
    if (!bridgeDirtyKeys().length || state.bridgeSaving || state.refreshing) return;
    if (!state.csrfToken) {
      showToast("Native security token is unavailable. Refresh and try again.", "error");
      return;
    }
    if (!validateBridgeChanges()) return;

    state.bridgeSaving = true;
    setButtonLoading(elements.saveBridgeButton, true, "Saving");
    updateBridgeDirtyState();
    updateBusyActions();
    updateOverallStatus();
    try {
      const response = await requestJson(API.config, {
        method: "PUT",
        csrf: state.csrfToken,
        body: bridgePayload(),
      });

      acceptSavedBridgeDraft(response);
      state.restartPending = response.restartRequired === true;
      elements.restartBanner.hidden = !state.restartPending;
      showToast(
        state.restartPending
          ? "Bridge configuration saved. Restart IMCodex to apply it."
          : "Bridge configuration saved. No restart is pending.",
      );
      await loadBridge();
    } catch (error) {
      if (error instanceof ApiError && (error.status === 409 || error.status === 412)) {
        openConflictDialog();
      } else {
        showToast(displayError(error, "Bridge configuration could not be saved."), "error");
      }
    } finally {
      state.bridgeSaving = false;
      setButtonLoading(elements.saveBridgeButton, false, "Saving");
      updateBridgeDirtyState();
      updateBusyActions();
      updateOverallStatus();
    }
  }

  function discardBridgeChanges() {
    if (!bridgeDirtyKeys().length || state.bridgeSaving) return;
    renderBridgeSettings();
    showToast("Unsaved bridge changes discarded.");
  }

  async function reloadBridgeAfterConflict() {
    if (state.refreshing || state.nativeSaving || state.bridgeSaving) return;
    if (elements.conflictDialog.open) elements.conflictDialog.close();
    state.refreshing = true;
    setButtonLoading(elements.refreshButton, true, "Reloading");
    clearBridgeDraftState();
    updateNativeDirtyState();
    updateBridgeDirtyState();
    updateBusyActions();
    updateOverallStatus();
    try {
      await loadBridge();
    } finally {
      state.refreshing = false;
      setButtonLoading(elements.refreshButton, false, "Reloading");
      updateNativeDirtyState();
      updateBridgeDirtyState();
      updateBusyActions();
      updateOverallStatus();
    }
    if (state.bridgeLoaded) showToast("Latest bridge configuration loaded.");
  }

  function hasUnsavedChanges() {
    return nativeDirtyKeys().length > 0 || bridgeDirtyKeys().length > 0;
  }

  async function refreshAll({ confirmDiscard = true } = {}) {
    if (state.refreshing || state.nativeSaving || state.bridgeSaving) return;
    const discardingChanges = confirmDiscard && hasUnsavedChanges();
    if (discardingChanges) {
      const proceed = window.confirm("Discard unsaved changes and reload configuration?");
      if (!proceed) return;
    }

    state.refreshing = true;
    if (discardingChanges) {
      clearNativeDraftState();
      clearBridgeDraftState();
    }
    setButtonLoading(elements.refreshButton, true, "Refreshing");
    updateNativeDirtyState();
    updateBridgeDirtyState();
    updateBusyActions();
    state.nativeError = null;
    state.bridgeError = null;
    updateOverallStatus();
    await Promise.all([loadNative(), loadBridge()]);
    state.refreshing = false;
    setButtonLoading(elements.refreshButton, false, "Refreshing");
    updateNativeDirtyState();
    updateBridgeDirtyState();
    updateBusyActions();
  }

  elements.refreshButton.addEventListener("click", () => void refreshAll());
  elements.retryButton.addEventListener("click", () => void refreshAll());
  elements.saveNativeButton.addEventListener("click", () => void saveNative());
  elements.saveBridgeButton.addEventListener("click", () => void saveBridge());
  elements.discardBridgeButton.addEventListener("click", discardBridgeChanges);
  elements.dismissRestart.addEventListener("click", () => {
    elements.restartBanner.hidden = true;
  });
  elements.conflictCancel.addEventListener("click", () => elements.conflictDialog.close());
  elements.conflictReload.addEventListener("click", () => void reloadBridgeAfterConflict());
  elements.conflictDialog.addEventListener("click", (event) => {
    if (event.target === elements.conflictDialog) elements.conflictDialog.close();
  });

  window.addEventListener("beforeunload", (event) => {
    if (!hasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  document.addEventListener("keydown", (event) => {
    if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "s") return;
    if (elements.conflictDialog.open) return;
    event.preventDefault();
    if (bridgeDirtyKeys().length) {
      void saveBridge();
    } else if (nativeDirtyKeys().length) {
      void saveNative();
    }
  });

  if (elements.themeToggle) {
    elements.themeToggle.addEventListener("click", cycleTheme);
  }

  initTheme();
  void refreshAll({ confirmDiscard: false });
})();
