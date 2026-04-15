// Abilities page: reply cadence, memory windows, search tuning.

import { api } from "../api.js";
import {
  el,
  card,
  fieldRow,
  numberInput,
  select,
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
        event_mode: !!state.event_mode,
        event_buffer_limit: Number(state.event_buffer_limit || 120),
        narrator_mode: !!state.narrator_mode,
        narrator_style: String(state.narrator_style || "subtle"),
        narrator_detail_level: Number(state.narrator_detail_level || 2),
        value_game_mode: !!state.value_game_mode,
        value_game_reply_bonus: Number(state.value_game_reply_bonus || 0.08),
        memory_graph_mode: !!state.memory_graph_mode,
        memory_graph_limit: Number(state.memory_graph_limit || 8),
        proactive_mode: !!state.proactive_mode,
        proactive_inactive_hours: Number(state.proactive_inactive_hours || 6),
        proactive_candidate_limit: Number(state.proactive_candidate_limit || 6),
        proactive_min_affinity: Number(state.proactive_min_affinity || 0.12),
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
            label: t("abilities.field.events"),
            hint: t("abilities.field.events.hint"),
            control: switchControl({
              checked: !!state.event_mode,
              onChange: (v) => patch({ event_mode: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.narrator"),
            hint: t("abilities.field.narrator.hint"),
            control: switchControl({
              checked: !!state.narrator_mode,
              onChange: (v) => patch({ narrator_mode: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.narratorStyle"),
            control: select({
              value: String(state.narrator_style || "subtle"),
              options: [
                { value: "subtle", label: t("abilities.option.narrator.subtle") },
                { value: "cinematic", label: t("abilities.option.narrator.cinematic") },
                { value: "diary", label: t("abilities.option.narrator.diary") },
              ],
              onChange: (v) => patch({ narrator_style: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.narratorDetail"),
            control: numberInput({
              value: Number(state.narrator_detail_level || 2),
              min: 1,
              max: 4,
              onChange: (v) => patch({ narrator_detail_level: v }),
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
        title: t("abilities.card.relationships"),
        body: [
          fieldRow({
            label: t("abilities.field.valueGame"),
            hint: t("abilities.field.valueGame.hint"),
            control: switchControl({
              checked: !!state.value_game_mode,
              onChange: (v) => patch({ value_game_mode: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.replyBonus"),
            hint: t("abilities.field.replyBonus.hint"),
            control: numberInput({
              value: Number(state.value_game_reply_bonus || 0.08),
              min: -1,
              max: 1,
              step: 0.01,
              onChange: (v) => patch({ value_game_reply_bonus: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.proactive"),
            hint: t("abilities.field.proactive.hint"),
            control: switchControl({
              checked: !!state.proactive_mode,
              onChange: (v) => patch({ proactive_mode: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.proactiveInactive"),
            control: numberInput({
              value: Number(state.proactive_inactive_hours || 6),
              min: 1,
              max: 336,
              onChange: (v) => patch({ proactive_inactive_hours: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.proactiveLimit"),
            control: numberInput({
              value: Number(state.proactive_candidate_limit || 6),
              min: 1,
              max: 50,
              onChange: (v) => patch({ proactive_candidate_limit: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.proactiveMinAffinity"),
            control: numberInput({
              value: Number(state.proactive_min_affinity || 0.12),
              min: -1,
              max: 1,
              step: 0.01,
              onChange: (v) => patch({ proactive_min_affinity: v }),
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
          fieldRow({
            label: t("abilities.field.memoryGraph"),
            hint: t("abilities.field.memoryGraph.hint"),
            control: switchControl({
              checked: !!state.memory_graph_mode,
              onChange: (v) => patch({ memory_graph_mode: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.memoryGraphLimit"),
            control: numberInput({
              value: Number(state.memory_graph_limit || 8),
              min: 1,
              max: 24,
              onChange: (v) => patch({ memory_graph_limit: v }),
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
          fieldRow({
            label: t("abilities.field.eventBuffer"),
            control: numberInput({
              value: Number(state.event_buffer_limit || 120),
              min: 20,
              max: 500,
              onChange: (v) => patch({ event_buffer_limit: v }),
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
