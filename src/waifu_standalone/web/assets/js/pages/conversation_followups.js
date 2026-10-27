import { api } from "../api.js";
import { el, card, chip, empty, select } from "../components.js";
import { t } from "../i18n.js";
import { confirmDialog, toastOk } from "../ui.js";
import { episodeTimeSummary, episodeTimeEditor, episodeTimeAudit } from "./episode_time.js";

export function createFollowupPanel({run, isBusy, refresh, showDelivery}) {
  let id = "", character = "", data = null, items = [], cursor = null, form = null, audit = null;
  const label = (key, params) => t(`followupsV2.${key}`, params);
  const draftReason = value => value.startsWith("generation_") ? label("draftFailure", {code: value.slice(11)}) : label(`draftReason.${value}`);
  const text = (value, cls = "muted") => el("div", {class: cls, text: value});
  const button = (key, action, disabled = false) => el("button", {type: "button", class: "btn", text: label(key),
    disabled: isBusy() || disabled, onClick: () => run(action)});

  async function load(conversationId, characterId, append = false) {
    const result = await api.runtimeFollowups(conversationId, append && cursor ? {before_id: cursor} : {});
    if (id !== conversationId || character !== characterId || data?.data_generation !== result.data_generation) {form = null; audit = null;}
    id = conversationId; character = characterId; data = result;
    items = append ? [...items, ...result.candidates] : result.candidates; cursor = result.next_before_id;
  }

  async function edit(item, operation, draft = null) {
    audit = await api.runtimeFollowups(id, {candidate_id: item.candidate_id});
    const current = audit.candidate;
    form = {operation, candidate_id: current.candidate_id, expected_version: current.version,
      expected_data_generation: audit.data_generation, request_id: crypto.randomUUID(), text: current.text,
      expected_policy_version: audit.policy_version,
      when: "", fold: null, note: "", authorized: false};
    if (draft) {
      const fresh = audit.drafts.find(d => d.job_id === draft.job_id && d.state === "ready");
      if (!fresh) {form = null; throw new Error(label("staleDraft"));}
      form.text = fresh.text; form.draft_job_id = fresh.job_id;
    }
  }

  async function submit() {
    if (!await confirmDialog({title: label(form.operation), message: label(`${form.operation}Confirm`), danger: ["cancel", "dismiss"].includes(form.operation)})) return;
    const result = await api.runtimeCommand({action: "followup", conversation_id: id, ...form});
    form = null; audit = null;
    toastOk(label(result.draft ? "draftQueued" : result.status === "needs_review" ? "inflight" : "saved"));
    await refresh();
  }

  async function editTime(item) {
    audit = await api.runtimeFollowups(id, {candidate_id: item.candidate_id});
    const current = audit.candidate, timing = current.timing;
    if (!timing || timing.state === "invalidated") throw new Error(t("episodeTime.stale"));
    form = {action: "episode_time", memory_id: current.memory_id, expected_memory_version: current.memory_version,
      expected_timing_version: timing.version, expected_data_generation: audit.data_generation,
      expected_policy_version: audit.policy_version, expected_binding_generation: audit.binding_generation,
      expected_next_seq: audit.next_seq, request_id: crypto.randomUUID(), authorized: false, note: "",
      precision: timing.confirmed.precision || "date", date: timing.confirmed.date || "", period: timing.confirmed.period || "",
      clock: timing.confirmed.clock || "", fold: timing.confirmed.fold ?? null,
      confirmation_source_event_id: timing.confirmed.confirmation_source_event_id || current.source_event_id,
      confirmation_quote: timing.confirmed.confirmation_quote || current.evidence};
  }

  async function saveTime(operation) {
    if (!await confirmDialog({title: t(`episodeTime.${operation === "confirm" ? "save" : "clear"}`), message: t("episodeTime.confirmChange")})) return;
    await api.runtimeCommand({...form, operation, conversation_id: id});
    form = null; audit = null; toastOk(t("episodeTime.saved")); await refresh();
  }

  function renderForm() {
    if (!form) return null;
    if (form.action === "episode_time") return episodeTimeEditor({form, timing: audit.candidate.timing, busy: isBusy(), run,
      save: saveTime, close: async () => {form = null; audit = null;}});
    const nodes = []; let submitButton;
    const valid = () => form.note.trim() && form.authorized && (form.operation !== "schedule" || (form.when && form.text.trim()));
    const changed = () => {form.request_id = crypto.randomUUID(); if (submitButton) submitButton.disabled = isBusy() || !valid();};
    function input(key, rows = 1, type = "text", max = 1000) {
      nodes.push(el("label", {class: "conversation-note"}, [text(label(key)), el(rows > 1 ? "textarea" : "input", {
        type, rows, value: form[key], maxLength: max, disabled: isBusy(), "aria-label": label(key),
        onInput: e => {form[key] = e.target.value; changed();},
      })]));
    }
    nodes.push(text(label("source", {id: audit.candidate.source_event_id})), text(audit.candidate.evidence, "conversation-message"));
    if (form.operation === "generate") nodes.push(text(label("generateHint")));
    if (form.draft_job_id) nodes.push(text(label("usingDraft", {id: form.draft_job_id})));
    if (form.operation === "schedule") {
      nodes.push(episodeTimeSummary(audit.candidate.timing));
      nodes.push(text(label("reviewHint")), text(label("zone", {zone: audit.timezone || label("unset")})));
      input("text", 3, "text", 500); input("when", 1, "datetime-local", 80);
      const fold = select({value: form.fold === null ? "" : String(form.fold), options: [
        {value: "", label: t("remindersV2.foldAuto")}, {value: "0", label: t("remindersV2.foldFirst")}, {value: "1", label: t("remindersV2.foldSecond")},
      ], onChange: value => {form.fold = value === "" ? null : Number(value); changed();}});
      fold.disabled = isBusy(); fold.setAttribute("aria-label", t("remindersV2.fold"));
      nodes.push(fold);
    }
    input("note", 2);
    nodes.push(el("label", {class: "row-tight"}, [el("input", {type: "checkbox", checked: form.authorized, disabled: isBusy(),
      onChange: e => {form.authorized = e.target.checked; changed();}}), label("authorized")]));
    submitButton = button(form.operation, submit, !valid());
    nodes.push(el("div", {class: "row-tight"}, [submitButton, button("close", async () => {form = null; audit = null;})]));
    return card({title: label(form.operation), extraClass: "followup-editor", body: nodes});
  }

  function renderAudit() {
    if (!audit || form) return null;
    return card({title: label("audit"), body: [
      text(label("source", {id: audit.candidate.source_event_id})), text(audit.candidate.evidence, "conversation-message"),
      ...episodeTimeAudit(audit.time_audit),
      ...(audit.drafts || []).map(d => el("div", {class: "conversation-review"}, [
        text(`${label(`draftState.${d.state}`)} · ${d.actor} · ${d.job_id}`), text(d.text, "conversation-message"),
        text(d.note, "conversation-message"),
        d.reason ? text(draftReason(d.reason)) : null,
        text(label("provenance", {character: d.snapshot.character_id, version: d.snapshot.character_version.slice(0, 12), memory: d.snapshot.memory_id, revision: d.snapshot.memory_version})),
        el("details", {}, [el("summary", {text: label("contextReport")}), el("pre", {class: "conversation-message", text: JSON.stringify(d.report, null, 2)})]),
      ])),
      ...audit.requests.map(r => el("div", {class: "conversation-review"}, [
        text(`${label(r.operation)} · ${r.actor} · ${new Date(r.created_at * 1000).toISOString()}`),
        text(r.note, "conversation-message"), text(r.candidate.text, "conversation-message"), text(r.candidate.local_time),
        r.draft_job_id ? text(label("usingDraft", {id: r.draft_job_id})) : null,
      ])),
      ...audit.occurrences.map(r => el("div", {class: "conversation-review"}, [
        text(`v${r.candidate_version} · ${r.event_id} · ${t(`conversations.state.${r.delivery_state || r.state}`)}`),
        r.outbox_id ? button("delivery", () => showDelivery(r.outbox_id)) : null,
      ])),
    ]});
  }

  function render() {
    if (!data) return null;
    const decision = data.decision;
    return card({title: label("title"), extraClass: "conversation-followups", body: [
      text(label("hint")), text(label("policy")), text(t(data.mode === "capture" ? "conversations.captureHint" : "conversations.liveHint")),
      button("refreshDrafts", refresh),
      !data.enabled ? text(label("reason.runtime_disabled")) : null,
      decision && !decision.allowed ? text(label(`reason.${decision.reason}`)) : null,
      ...items.map(item => el("div", {class: "conversation-followup-record"}, [
        text(`${item.title} · ${item.character_id}`, "conversation-speaker"),
        chip({label: label(`state.${item.state}`), variant: item.state === "needs_review" ? "warn" : "outline"}),
        text(label("episode", {id: item.memory_id, version: item.memory_version})),
        text(`“${item.evidence}”`, "conversation-message"), text(label("draft")), text(item.text, "conversation-message"),
        episodeTimeSummary(item.timing),
        text(item.local_time ? `${item.local_time} · ${item.timezone}` : label("unscheduled")),
        item.reason ? text(label(`reason.${item.reason}`)) : null,
        item.deferred_until ? text(label("deferred", {time: new Date(item.deferred_until * 1000).toISOString()})) : null,
        item.cancel_requested ? text(label("inflight")) : null,
        item.draft ? el("div", {class: "conversation-review"}, [
          text(label(`draftState.${item.draft.state}`), "conversation-speaker"),
          item.draft.reason ? text(draftReason(item.draft.reason)) : null,
          item.draft.text ? text(item.draft.text, "conversation-message") : null,
          item.draft.state === "ready" ? button("useDraft", () => edit(item, "schedule", item.draft), !!form) : null,
        ]) : null,
        el("div", {class: "row-tight"}, [
          el("button", {type: "button", class: "btn", text: t("episodeTime.edit"), onClick: () => run(() => editTime(item)),
            disabled: isBusy() || !!form || !data.enabled || !item.timing || item.timing.state === "invalidated" || item.character_id !== character}),
          button("generate", () => edit(item, "generate"), !!form || !data.enabled || !data.proactive_enabled || data.paused ||
            item.character_id !== character || item.state !== "candidate" || ["queued", "generating"].includes(item.draft?.state)),
          button("schedule", () => edit(item, "schedule"), !data.enabled || !data.proactive_enabled || data.paused ||
            item.character_id !== character || !["candidate", "scheduled", "enqueued", "expired"].includes(item.state)),
          button(item.state === "candidate" ? "dismiss" : "cancel", () => edit(item, item.state === "candidate" ? "dismiss" : "cancel"),
            !!item.cancel_requested || !["candidate", "scheduled", "enqueued", "dispatching", "needs_review"].includes(item.state)),
          button("audit", async () => {audit = await api.runtimeFollowups(id, {candidate_id: item.candidate_id}); form = null;}),
        ]),
      ])),
      !items.length ? empty({title: label("empty")}) : null,
      cursor ? button("more", () => load(id, character, true)) : null, renderForm(), renderAudit(),
    ]});
  }

  return {load, render, reset: () => {id = ""; data = null; items = []; cursor = null; form = null; audit = null;}};
}
