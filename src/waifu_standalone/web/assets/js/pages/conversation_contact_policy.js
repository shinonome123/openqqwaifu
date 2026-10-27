import { api } from "../api.js";
import { el, card, chip } from "../components.js";
import { t } from "../i18n.js";
import { confirmDialog, toastOk } from "../ui.js";

export function createContactPolicyPanel({run, isBusy, refresh}) {
  let data = null, conversationId = "", form = null;
  const label = (key, params) => t(`contactV2.${key}`, params);
  const text = value => el("div", {class: "muted", text: value});
  const button = (key, action, disabled = false) => el("button", {type: "button", class: "btn", text: label(key),
    disabled: isBusy() || disabled, onClick: () => run(action)});

  async function load(id) {
    const result = await api.runtimeContactPolicy(id);
    if (conversationId !== id || data?.data_generation !== result.data_generation) form = null;
    data = result; conversationId = id;
  }

  async function togglePause() {
    const operation = data.policy.paused ? "resume" : "pause";
    if (operation === "resume" && !await confirmDialog({title: label("resume"), message: label("resumeHint")})) return;
    await api.runtimeCommand({action: "contact_policy", operation, conversation_id: conversationId,
      request_id: crypto.randomUUID(), expected_version: data.policy.version, expected_data_generation: data.data_generation,
      authorized: true, note: label(operation)});
    form = null; toastOk(label("saved")); await refresh();
  }

  function edit() {
    const p = data.policy;
    form = {expected_version: p.version, expected_data_generation: data.data_generation, request_id: crypto.randomUUID(),
      note: "", authorized: false, changes: Object.fromEntries(["proactive_enabled", "timezone", "quiet_enabled", "quiet_start",
        "quiet_end", "daily_limit", "min_interval_minutes"].map(key => [key, p[key]]))};
  }

  async function save() {
    if (!await confirmDialog({title: label("save"), message: label("saveHint")})) return;
    // An unset zone is allowed while opt-in remains off. Do not submit an empty
    // value as if an operator had confirmed a valid zone.
    const changes = {...form.changes};
    if (!changes.timezone.trim()) delete changes.timezone;
    await api.runtimeCommand({action: "contact_policy", operation: "update", conversation_id: conversationId, ...form, changes});
    form = null; toastOk(label("saved")); await refresh();
  }

  function renderForm() {
    if (!form) return null;
    const nodes = []; let submit;
    const valid = () => form.authorized && form.note.trim() &&
      (!(form.changes.proactive_enabled || data.policy.timezone) || form.changes.timezone.trim());
    const changed = () => {form.request_id = crypto.randomUUID(); if (submit) submit.disabled = isBusy() || !valid();};
    function check(key) {
      nodes.push(el("label", {class: "row-tight"}, [el("input", {type: "checkbox", checked: form.changes[key],
        disabled: isBusy(), onChange: e => {form.changes[key] = e.target.checked; changed();}}), label(key)]));
    }
    function input(key, type = "text", max = 100) {
      nodes.push(el("label", {class: "conversation-note"}, [text(label(key)), el("input", {
        type, value: form.changes[key], min: type === "number" ? 1 : undefined, max: type === "number" ? max : undefined,
        maxLength: 100, disabled: isBusy(), "aria-label": label(key),
        onInput: e => {form.changes[key] = type === "number" ? Number(e.target.value) : e.target.value; changed();},
      })]));
    }
    check("proactive_enabled"); nodes.push(text(label("futureHint")));
    input("timezone"); check("quiet_enabled"); input("quiet_start", "time"); input("quiet_end", "time");
    input("daily_limit", "number"); input("min_interval_minutes", "number", 1440);
    nodes.push(el("label", {class: "conversation-note"}, [text(label("note")), el("textarea", {rows: 2, value: form.note,
      maxLength: 1000, disabled: isBusy(), "aria-label": label("note"), onInput: e => {form.note = e.target.value; changed();}})]));
    nodes.push(el("label", {class: "row-tight"}, [el("input", {type: "checkbox", checked: form.authorized, disabled: isBusy(),
      onChange: e => {form.authorized = e.target.checked; changed();}}), label("authorized")]));
    submit = button("save", save, !valid());
    nodes.push(el("div", {class: "row-tight"}, [submit, button("close", async () => {form = null;})]));
    return el("div", {class: "contact-policy-editor"}, nodes);
  }

  function render() {
    if (!data) return null;
    const p = data.policy, decision = data.reminder_decision;
    return card({title: label("title"), extraClass: "conversation-contact-policy", actions: [
      chip({label: label(p.paused ? "paused" : "running"), variant: p.paused ? "warn" : "outline"}),
    ], body: [
      text(label("scope")), text(label("futureHint")),
      text(label("summary", {opt: label(p.proactive_enabled ? "on" : "off"), zone: p.timezone || label("unset"),
        quiet: p.quiet_enabled ? `${p.quiet_start}–${p.quiet_end}` : label("off")})),
      text(label("quota", {used: data.attempts_24h, total: p.daily_limit, left: data.remaining_24h, minutes: p.min_interval_minutes})),
      text(label(`reason.${decision.reason || "allowed"}`)), text(label("inflightHint")),
      el("div", {class: "row-tight"}, [button(p.paused ? "resume" : "pause", togglePause), button("edit", async () => edit())]),
      renderForm(),
      el("details", {}, [el("summary", {text: label("audit")}),
        ...data.audit.map(row => el("div", {class: "conversation-review"}, [
          text(`${label(row.operation)} · ${row.actor} · ${new Date(row.created_at * 1000).toISOString()}`),
          text(`${label(row.status)}${row.version ? ` · v${row.version}` : ""}`), text(row.note),
          ...Object.entries(row.changes).map(([key, value]) => text(`${label(key)}: ${typeof value === "boolean" ? label(value ? "on" : "off") : value}`)),
        ])), !data.audit.length ? text(label("noAudit")) : null,
      ]),
    ]});
  }

  return {load, render, reset: () => {data = null; form = null; conversationId = "";}};
}
