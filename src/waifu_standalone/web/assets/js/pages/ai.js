// AI providers page: LLM + image generation.

import { api } from "../api.js";
import {
  el,
  card,
  fieldRow,
  textInput,
  numberInput,
  switchControl,
  select,
} from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, toastInfo } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let state = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      state = await api.getAiPanel();
      if (stopped) return;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(header());
    container.appendChild(llmCard());
    container.appendChild(imageCard());
  }

  function header() {
    return el("div", { class: "page-header" }, [
      el("div", { class: "page-header-text" }, [
        el("div", { class: "page-title", text: t("page.ai.title") }),
        el("div", { class: "page-desc", text: t("page.ai.desc") }),
      ]),
      el("div", { class: "page-actions" }, [
        el("button", {
          type: "button",
          class: "btn is-primary",
          text: t("common.save"),
          onClick: save,
        }),
      ]),
    ]);
  }

  async function save() {
    try {
      await api.saveAiPanel({
        llm: state.llm,
        image_generation: state.image_generation,
      });
      toastOk(t("actions.savedOk"));
      load();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  function patch(key, patchObj) {
    state[key] = { ...(state[key] || {}), ...patchObj };
  }

  function llmCard() {
    const llm = state.llm || {};
    return card({
      title: t("ai.llm.title"),
      subtitle: t("ai.llm.desc"),
      actions: [
        el("button", {
          type: "button",
          class: "btn is-sm",
          text: t("common.test"),
          onClick: () => doTest("llm", state.llm?.base_url, state.llm?.api_key),
        }),
      ],
      body: [
        fieldRow({
          label: t("ai.field.enabled"),
          control: switchControl({
            checked: !!llm.enabled,
            onChange: (v) => patch("llm", { enabled: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.backend"),
          control: select({
            value: llm.backend || "dify",
            options: [
              { value: "dify", label: "Dify" },
              { value: "openai", label: "OpenAI-compatible" },
              { value: "claude", label: "Anthropic" },
              { value: "custom", label: "Custom" },
            ],
            onChange: (v) => patch("llm", { backend: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.baseUrl"),
          control: textInput({
            value: llm.base_url || "",
            placeholder: "https://api.example.com/v1",
            onChange: (v) => patch("llm", { base_url: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.apiKey"),
          control: textInput({
            type: "password",
            value: llm.api_key || "",
            onChange: (v) => patch("llm", { api_key: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.appType"),
          control: select({
            value: llm.app_type || "chat",
            options: [
              { value: "chat", label: "chat" },
              { value: "completion", label: "completion" },
              { value: "workflow", label: "workflow" },
            ],
            onChange: (v) => patch("llm", { app_type: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.timeout"),
          control: numberInput({
            value: Number(llm.timeout_seconds || 45),
            min: 1,
            max: 600,
            onChange: (v) => patch("llm", { timeout_seconds: v }),
          }),
        }),
      ],
    });
  }

  function imageCard() {
    const img = state.image_generation || {};
    return card({
      title: t("ai.image.title"),
      subtitle: t("ai.image.desc"),
      actions: [
        el("button", {
          type: "button",
          class: "btn is-sm",
          text: t("common.test"),
          onClick: () => doTest("image", state.image_generation?.base_url, state.image_generation?.api_key),
        }),
      ],
      body: [
        fieldRow({
          label: t("ai.field.enabled"),
          control: switchControl({
            checked: !!img.enabled,
            onChange: (v) => patch("image_generation", { enabled: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.baseUrl"),
          control: textInput({
            value: img.base_url || "",
            placeholder: "https://api.x.ai/v1",
            onChange: (v) => patch("image_generation", { base_url: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.apiKey"),
          control: textInput({
            type: "password",
            value: img.api_key || "",
            onChange: (v) => patch("image_generation", { api_key: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.model"),
          control: textInput({
            value: img.model || "",
            onChange: (v) => patch("image_generation", { model: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.responseFormat"),
          control: select({
            value: img.response_format || "b64_json",
            options: [
              { value: "b64_json", label: "b64_json" },
              { value: "url", label: "url" },
            ],
            onChange: (v) => patch("image_generation", { response_format: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.aspectRatio"),
          control: select({
            value: img.aspect_ratio || "1:1",
            options: [
              { value: "1:1", label: "1:1" },
              { value: "3:4", label: "3:4" },
              { value: "4:3", label: "4:3" },
              { value: "9:16", label: "9:16" },
              { value: "16:9", label: "16:9" },
            ],
            onChange: (v) => patch("image_generation", { aspect_ratio: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.resolution"),
          control: textInput({
            value: img.resolution || "",
            placeholder: "1024x1024",
            onChange: (v) => patch("image_generation", { resolution: v }),
          }),
        }),
        fieldRow({
          label: t("ai.field.timeout"),
          control: numberInput({
            value: Number(img.timeout_seconds || 180),
            min: 1,
            max: 900,
            onChange: (v) => patch("image_generation", { timeout_seconds: v }),
          }),
        }),
      ],
    });
  }

  async function doTest(kind, baseUrl, apiKey) {
    if (!baseUrl) {
      toastError(t("ai.test.fail", { msg: "base_url is empty" }));
      return;
    }
    toastInfo(t("ai.test.probing"));
    try {
      const result = await api.testProvider(kind, baseUrl, apiKey || "");
      if (result?.ok) {
        toastOk(t("ai.test.ok", { ms: Math.round(result.elapsed_ms || 0) }));
      } else {
        toastError(t("ai.test.fail", { msg: result?.error || result?.status || "unknown" }));
      }
    } catch (err) {
      toastError(t("ai.test.fail", { msg: err?.message || err }));
    }
  }

  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}