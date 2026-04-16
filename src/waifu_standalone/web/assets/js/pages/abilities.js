// Abilities page: message behavior, reply cadence, memory windows, search tuning.

import { api } from "../api.js";
import {
  el,
  card,
  fieldRow,
  textInput,
  numberInput,
  switchControl,
  tagInput,
} from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let state = null;
  let other = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      const [nextState, nextOther] = await Promise.all([
        api.getAbilitiesPanel(),
        api.getOtherPanel(),
      ]);
      if (stopped) return;
      state = nextState;
      other = nextOther;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  function patch(next) {
    state = { ...state, ...next };
  }

  function patchOther(next) {
    other = { ...other, ...next };
  }

  async function save() {
    try {
      const saves = [
        api.saveAbilitiesPanel({
          search_enabled: !!state.search_enabled,
          search_result_limit: Number(state.search_result_limit || 3),
          search_timeout_seconds: Number(state.search_timeout_seconds || 8),
          summarization_mode: !!state.summarization_mode,
          member_auto_sync: !!state.member_auto_sync,
          knowledge_auto_extract: !!state.knowledge_auto_extract,
          knowledge_auto_extract_limit: Number(state.knowledge_auto_extract_limit || 2),
          event_mode: !!state.event_mode,
          event_buffer_limit: Number(state.event_buffer_limit || 120),
          value_game_mode: !!state.value_game_mode,
          value_game_reply_bonus: Number(state.value_game_reply_bonus || 0.08),
          proactive_mode: !!state.proactive_mode,
          proactive_inactive_hours: Number(state.proactive_inactive_hours || 6),
          proactive_candidate_limit: Number(state.proactive_candidate_limit || 6),
          proactive_min_affinity: Number(state.proactive_min_affinity || 0.12),
          max_active_skills: Number(state.max_active_skills || 3),
          history_window_messages: Number(state.history_window_messages || 8),
          memory_recall_limit: Number(state.memory_recall_limit || 3),
          short_term_memory_limit: Number(state.short_term_memory_limit || 30),
          memory_summary_batch_size: Number(state.memory_summary_batch_size || 12),
        }),
      ];
      if (other) {
        saves.push(api.saveOtherPanel(other));
      }
      await Promise.all(saves);
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

    if (other) {
      container.appendChild(renderMessageBehaviorCard());
    }

    container.appendChild(
      card({
        title: t("character.tab.pipeline"),
        body: [
          fieldRow({
            label: t("character.field.summarization"),
            hint: t("character.field.summarization.hint"),
            control: switchControl({
              checked: !!state.summarization_mode,
              onChange: (v) => patch({ summarization_mode: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.memberAutoSync"),
            hint: t("abilities.field.memberAutoSync.hint"),
            control: switchControl({
              checked: !!state.member_auto_sync,
              onChange: (v) => patch({ member_auto_sync: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.knowledgeAutoExtract"),
            hint: t("abilities.field.knowledgeAutoExtract.hint"),
            control: switchControl({
              checked: !!state.knowledge_auto_extract,
              onChange: (v) => patch({ knowledge_auto_extract: v }),
            }),
          }),
          fieldRow({
            label: t("abilities.field.knowledgeAutoExtractLimit"),
            hint: t("abilities.field.knowledgeAutoExtractLimit.hint"),
            control: numberInput({
              value: Number(state.knowledge_auto_extract_limit || 2),
              min: 1,
              max: 8,
              onChange: (v) => patch({ knowledge_auto_extract_limit: v }),
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

  function renderMessageBehaviorCard() {
    return card({
      title: t("abilities.card.messageBehavior"),
      subtitle: t("abilities.card.messageBehavior.desc"),
      body: [
        fieldRow({
          label: t("character.field.serviceName"),
          hint: t("character.field.serviceName.hint"),
          control: textInput({
            value: other.service_name || "",
            onChange: (v) => patchOther({ service_name: v }),
          }),
        }),
        fieldRow({
          label: t("character.field.groupReplyRequiresMention"),
          hint: t("character.field.groupReplyRequiresMention.hint"),
          control: switchControl({
            checked: other.group_reply_requires_mention !== false,
            onChange: (v) => patchOther({ group_reply_requires_mention: v }),
          }),
        }),
        fieldRow({
          label: t("character.pipeline.followup"),
          control: numberInput({
            value: Number(other.group_follow_up_window_seconds || 5),
            min: 0,
            max: 60,
            step: 1,
            onChange: (v) => patchOther({ group_follow_up_window_seconds: v }),
          }),
        }),
        fieldRow({
          label: t("character.field.groupReplyDelay"),
          hint: t("character.field.groupReplyDelay.hint"),
          control: numberInput({
            value: Number(other.group_response_delay_seconds || 0),
            min: 0,
            max: 15,
            step: 0.5,
            onChange: (v) => patchOther({ group_response_delay_seconds: v }),
          }),
        }),
        fieldRow({
          label: t("character.field.repeatTrigger"),
          hint: t("character.field.repeatTrigger.hint"),
          control: numberInput({
            value: Number(other.repeat_trigger_count || 0),
            min: 0,
            max: 10,
            step: 1,
            onChange: (v) => patchOther({ repeat_trigger_count: v }),
          }),
        }),
        fieldRow({
          label: t("character.field.multimodal"),
          hint: t("character.field.multimodal.hint"),
          control: switchControl({
            checked: other.multimodal_enabled !== false,
            onChange: (v) => patchOther({ multimodal_enabled: v }),
          }),
        }),
        fieldRow({
          label: t("character.pipeline.imageCmd"),
          control: textInput({
            value: other.image_command_prefix || "",
            onChange: (v) => patchOther({ image_command_prefix: v.trim() }),
          }),
        }),
        fieldRow({
          label: t("character.pipeline.aliases"),
          control: tagInput({
            values: other.image_command_aliases || [],
            onChange: (v) => patchOther({ image_command_aliases: v }),
          }),
        }),
        fieldRow({
          label: t("character.field.ignorePrefixes"),
          hint: t("character.field.ignorePrefixes.hint"),
          control: tagInput({
            values: other.ignore_prefixes || [],
            onChange: (v) => patchOther({ ignore_prefixes: v }),
          }),
        }),
      ],
    });
  }

  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}
