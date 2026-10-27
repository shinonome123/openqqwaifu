import { api } from "../api.js";
import { el, card, empty, chip, select } from "../components.js";
import { t } from "../i18n.js";
import { confirmDialog, toastOk } from "../ui.js";

export function createReminderPanel({run, isBusy, refresh, showDelivery}) {
  let conversationId = "", characterId = "", data = null, tasks = [], cursor = null, form = null, audit = null;
  const label = (key, params) => t(`remindersV2.${key}`, params);
  const text = (value, cls = "muted") => el("div", {class: cls, text: value});
  const button = (name, action, disabled = false) => el("button", {type: "button", class: "btn", text: name,
    disabled: isBusy() || disabled, onClick: () => run(action)});

  async function load(id, character, append = false) {
    const result = await api.runtimeReminders(id, append && cursor ? {before_id: cursor} : {});
    if (conversationId !== id || characterId !== character) { form = null; audit = null; }
    conversationId = id; characterId = character; data = result;
    tasks = append ? [...tasks, ...result.tasks] : result.tasks;
    cursor = result.next_before_id;
  }

  function startCreate(item) {
    const evidence = item.user_message.segments.find(p => p.type === "text" && p.text.trim())?.text.slice(0, 1000) || "";
    form = {operation: "create", character_id: item.turn.character_id, source_event_id: item.event_id,
      evidence, text: "", when: "", timezone: data.default_timezone || "", fold: null,
      linked_memory_id: "", note: "", authorized: false, allow_quiet: false, request_id: crypto.randomUUID()};
    audit = null;
  }

  async function edit(task, operation) {
    // Always load the current version before presenting a destructive action.
    const result = await api.runtimeReminders(conversationId, {task_id: task.task_id});
    const current = result.task;
    form = {operation, task_id: current.task_id, expected_version: current.version,
      when: "", timezone: current.timezone, fold: null, note: "", authorized: false, allow_quiet: false, request_id: crypto.randomUUID()};
    audit = result;
  }

  async function submit() {
    if (!await confirmDialog({title: label(form.operation), message: label(`${form.operation}Confirm`),
      danger: form.operation === "cancel"})) return;
    const result = await api.runtimeCommand({action: "reminder", conversation_id: conversationId, ...form});
    form = null; audit = null;
    toastOk(label(["in_flight", "needs_review"].includes(result.status) ? "cancelPending" : "saved"));
    await refresh();
  }

  function renderForm() {
    if (!form) return null;
    const nodes = [];
    let save;
    const valid = () => form.note.trim() && form.authorized && (form.operation === "cancel" ||
      (form.when && form.timezone.trim() && (form.operation !== "create" || (form.text.trim() && form.evidence.trim()))));
    const changed = () => {
      form.request_id = crypto.randomUUID();
      if (save) save.disabled = isBusy() || !valid();
    };
    function input(key, maxLength, type = "text", rows = 1) {
      const node = el(rows > 1 ? "textarea" : "input", {type, rows, value: form[key], maxLength,
        disabled: isBusy(), "aria-label": label(key), onInput: e => {form[key] = e.target.value; changed();}});
      nodes.push(el("label", {class: "conversation-note"}, [text(label(key)), node]));
    }
    if (form.operation === "create") {
      nodes.push(text(label("source", {id: form.source_event_id})), text(label("manualHint")));
      input("text", 500, "text", 3);
      input("evidence", 1000, "text", 3);
      input("linked_memory_id", 100);
    }
    if (form.operation !== "cancel") {
      input("when", 80, "datetime-local");
      input("timezone", 100);
      const fold = select({value: form.fold === null ? "" : String(form.fold), options: [
        {value: "", label: label("foldAuto")}, {value: "0", label: label("foldFirst")}, {value: "1", label: label("foldSecond")},
      ], onChange: value => {form.fold = value === "" ? null : Number(value); changed();}});
      fold.disabled = isBusy(); fold.setAttribute("aria-label", label("fold"));
      nodes.push(el("label", {class: "conversation-note"}, [text(label("fold")), fold]), text(label("timeHint")));
      nodes.push(el("label", {class: "row-tight"}, [el("input", {type: "checkbox", checked: form.allow_quiet,
        disabled: isBusy(), onChange: e => {form.allow_quiet = e.target.checked; changed();}}), label("allowQuiet")]));
    } else {
      nodes.push(text(label("cancelPending")));
    }
    input("note", 1000, "text", 2);
    nodes.push(el("label", {class: "row-tight"}, [el("input", {type: "checkbox", checked: form.authorized,
      disabled: isBusy(), onChange: e => {form.authorized = e.target.checked; changed();}}), label("authorized")]));
    save = button(label(form.operation), submit, !valid());
    nodes.push(el("div", {class: "row-tight"}, [save, button(label("close"), async () => {form = null;})]));
    return card({title: label(form.operation), body: nodes, extraClass: "reminder-editor"});
  }

  function renderAudit() {
    if (!audit) return null;
    const task = audit.task;
    return card({title: label("details"), extraClass: "reminder-audit", body: [
      text(label("source", {id: task.source_event_id})), text(task.evidence, "conversation-message"),
      text(`${label(task.authorization_kind)} · ${task.actor}`),
      task.linked_memory_id ? text(label("linked", {id: task.linked_memory_id, version: task.linked_memory_version})) : null,
      ...audit.requests.map(r => el("div", {class: "conversation-review"}, [
        text(`${label(r.operation)} · ${r.actor} · ${new Date(r.created_at * 1000).toISOString()}`),
        text(r.local_time), text(r.note, "conversation-message"),
      ])),
      ...audit.occurrences.map(r => el("div", {class: "conversation-review"}, [
        text(`v${r.task_version} · ${r.event_id} · ${r.delivery_state ? t(`conversations.state.${r.delivery_state}`) : t(`conversations.state.${r.state}`)}`),
        r.outbox_id ? button(t("conversations.delivery"), () => showDelivery(r.outbox_id)) : null,
      ])),
    ]});
  }

  function render() {
    if (!data) return null;
    return card({title: label("title"), extraClass: "conversation-reminders", body: [
      text(label(data.enabled ? "enabled" : "disabled")), text(t(data.mode === "capture" ? "conversations.captureHint" : "conversations.liveHint")),
      text(label("policy", {seconds: data.grace_seconds})),
      ...tasks.map(task => el("div", {class: "conversation-reminder-record"}, [
        text(task.text, "conversation-message"),
        chip({label: label(`state.${task.state}`), variant: ["needs_review", "paused"].includes(task.state) ? "warn" : "outline"}),
        text(`${task.local_time} · ${task.timezone}`), text(`${task.task_id} · v${task.version} · ${task.character_id}`),
        task.reason ? text(label(`reason.${task.reason}`)) : null,
        task.allow_quiet ? text(label("quietException")) : null,
        task.deferred_until ? text(label("deferred", {time: new Date(task.deferred_until * 1000).toISOString()})) : null,
        task.cancel_requested ? text(label("cancelPending"), "conversation-error") : null,
        el("div", {class: "row-tight"}, [
          button(label("details"), async () => {audit = await api.runtimeReminders(conversationId, {task_id: task.task_id}); form = null;}),
          button(label("postpone"), () => edit(task, "postpone"), !data.enabled || task.character_id !== characterId || !["scheduled", "enqueued", "paused", "expired"].includes(task.state)),
          button(label("cancel"), () => edit(task, "cancel"), !!task.cancel_requested || ["completed", "cancelled"].includes(task.state)),
        ]),
      ])),
      !tasks.length ? empty({title: label("empty")}) : null,
      cursor ? button(label("more"), () => load(conversationId, characterId, true)) : null,
      renderForm(), renderAudit(),
    ]});
  }

  return {load, render, startCreate, reset: () => {data = null; tasks = []; form = null; audit = null;},
    canCreate: item => !!data?.enabled && item.turn?.character_id === characterId};
}
