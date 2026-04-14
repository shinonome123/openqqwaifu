// Abilities page: reply cadence, memory windows, search tuning.

import { api } from "../api.js";
import {
  el,
  card,
  fieldRow,
  numberInput,
  switchControl,
} from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let state = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      state = await api.getAbilitiesPanel();
      if (stopped) return;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  function patch(next) {
    state = { ...state, ...next };
  }

  async function save() {
    try {
      await api.saveAbilitiesPanel({
        search_enabled: !!state.search_enabled,
        search_result_limit: Number(state.search_result_limit || 3),
        search_timeout_seconds: Number(state.search_timeout_seconds || 8),
        thinking_mode: !!state.thinking_mode,
        conversation_analysis: !!state.conversation_analysis,
        summarization_mode: !!state.summarization_mode,
        max_active_skills: Number(state.max_active_skills || 3),
        history_window_messages: Number(state.history_window_messages || 8),
        memory_recall_limit: Number(state.memory_recall_limit || 3),
        max_thinking_words: Number(state.max_thinking_words || 30),
        short_term_memory_limit: Number(state.short_term_memory_limit || 30),
        memory_summary_batch_size: Number(state.memory_summary_batch_size || 12),
      });
      toastOk(t("actions.savedOk"));
      load();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.abilities.title") }),
          el("div", { class: "page-desc", text: t("page.abilities.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          el("button", {
            type: "button",
            class: "btn is-primary",
            text: t("common.save"),
            onClick: save,
          }),
        ]),
      ]),
    );

    if (!state) return;

    container.appendChild(
      card({
        title: t("character.tab.pipeline"),
        body: [
          fieldRow({
            label: t("character.field.thinking"),
            hint: t("character.field.thinking.hint"),
            control: switchControl({
              checked: !!state.thinking_mode,
              onChange: (v) => patch({ thinking_mode: v }),
            }),
          }),
          fieldRow({
            label: t("character.field.conversationAnalysis"),
            hint: t("character.field.conversationAnalysis.hint"),
            control: switchControl({
              checked: !!state.conversation_analysis,
              onChange: (v) => patch({ conversation_analysis: v }),
            }),
          }),
          fieldRow({
            label: t("character.field.summarization"),
            hint: t("character.field.summarization.hint"),
            control: switchControl({
              checked: !!state.summarization_mode,
              onChange: (v) => patch({ summarization_mode: v }),
            }),
          }),
          fieldRow({
            label: t("character.field.maxThinkingWords"),
            hint: t("character.field.maxThinkingWords.hint"),
            control: numberInput({
              value: Number(state.max_thinking_words || 30),
              min: 1,
              max: 200,
              onChange: (v) => patch({ max_thinking_words: v }),
            }),
          }),
        ],
      }),
    );

    container.appendChild(
      card({
        title: t("nav.memory"),
        body: [
          fieldRow({
            label: t("character.field.historyWindow"),
            hint: t("character.field.historyWindow.hint"),
            control: numberInput({
              value: Number(state.history_window_messages || 8),
              min: 1,
              max: 128,
              onChange: (v) => patch({ history_window_messages: v }),
            }),
          }),
          fieldRow({
            label: t("character.field.memoryLimit"),
            hint: t("character.field.memoryLimit.hint"),
            control: numberInput({
              value: Number(state.memory_recall_limit || 3),
              min: 0,
              max: 20,
              onChange: (v) => patch({ memory_recall_limit: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.shortTerm"),
            control: numberInput({
              value: Number(state.short_term_memory_limit || 30),
              min: 1,
              max: 200,
              onChange: (v) => patch({ short_term_memory_limit: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.summaryBatch"),
            control: numberInput({
              value: Number(state.memory_summary_batch_size || 12),
              min: 1,
              max: 100,
              onChange: (v) => patch({ memory_summary_batch_size: v }),
            }),
          }),
        ],
      }),
    );

    container.appendChild(
      card({
        title: t("nav.skills"),
        body: [
          fieldRow({
            label: t("skills.field.maxActive"),
            control: numberInput({
              value: Number(state.max_active_skills || 3),
              min: 0,
              max: 20,
              onChange: (v) => patch({ max_active_skills: v }),
            }),
          }),
          fieldRow({
            label: t("skills.field.search"),
            control: switchControl({
              checked: !!state.search_enabled,
              onChange: (v) => patch({ search_enabled: v }),
            }),
          }),
          fieldRow({
            label: t("skills.field.searchLimit"),
            control: numberInput({
              value: Number(state.search_result_limit || 3),
              min: 1,
              max: 20,
              onChange: (v) => patch({ search_result_limit: v }),
            }),
          }),
          fieldRow({
            label: t("skills.field.searchTimeout"),
            control: numberInput({
              value: Number(state.search_timeout_seconds || 8),
              min: 1,
              max: 120,
              onChange: (v) => patch({ search_timeout_seconds: v }),
            }),
          }),
        ],
      }),
    );
  }

  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}
