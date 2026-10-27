import { el, card, select } from "../components.js";
import { t } from "../i18n.js";

const label = (key, params) => t(`episodeTime.${key}`, params);
const text = (value, cls = "muted") => el("div", {class: cls, text: value});

export function formatEpisodeTime(value) {
  if (!value?.date) return label("unconfirmed");
  return `${value.date}${value.precision === "period" ? ` · ${label(`period.${value.period}`)}` : value.clock ? ` ${value.clock}` : ""}${value.fold === 0 || value.fold === 1 ? ` · ${t(`remindersV2.fold${value.fold ? "Second" : "First"}`)}` : ""}`;
}

export function episodeTimeSummary(timing) {
  if (!timing) return null;
  return el("div", {class: "conversation-review episode-time-summary"}, [
    text(label("title"), "conversation-speaker"), text(label(`state.${timing.state}`)),
    timing.state === "confirmed" ? text(`${formatEpisodeTime(timing.confirmed)} · ${timing.timezone}`, "conversation-message") : null,
    timing.confirmed.provenance === "user_confirmed_in_dialogue" ? text(label("userConfirmed")) : null,
    timing.reason ? text(label(`reason.${timing.reason}`)) : null,
    ...(timing.state === "proposed" ? timing.proposal.reasons.map(reason => text(label(`reason.${reason}`))) : []),
    text(label("separate")),
  ]);
}

export function episodeTimeEditor({form, timing, busy, run, save, close}) {
  const nodes = [text(label("hint")), text(label("anchor", {date: timing.proposal.source_local_date || label("unset"), zone: timing.timezone || label("unset")})),
    ...timing.proposal.reasons.map(reason => text(label(`reason.${reason}`))),
    text(label("question", {quote: timing.proposal.mentions.map(m => m.text).join("、") || label("timeMention")}))];
  let saveButton, clearButton;
  const valid = () => form.authorized && form.note.trim() && form.date && form.confirmation_source_event_id.trim() && form.confirmation_quote.trim()
    && (form.precision !== "period" || form.period) && (form.precision !== "minute" || form.clock);
  function changed() {
    form.request_id = crypto.randomUUID();
    if (saveButton) saveButton.disabled = busy || !valid() || !timing.timezone;
    if (clearButton) clearButton.disabled = busy || !form.authorized || !form.note.trim();
  }
  const button = (caption, action, disabled = false) => el("button", {type: "button", class: "btn", text: caption,
    disabled: busy || disabled, onClick: () => run(action)});
  if (timing.proposal.options.length) {
    nodes.push(text(label("options")), el("div", {class: "row-tight"}, timing.proposal.options.map(option =>
      button(formatEpisodeTime(option), async () => {Object.assign(form, option); changed();}))));
  }
  const precision = select({value: form.precision, options: ["date", "period", "minute"].map(value => ({value, label: label(`precision.${value}`)})),
    onChange: value => run(async () => {form.precision = value; form.clock = ""; form.period = ""; form.fold = null; changed();})});
  precision.disabled = busy; precision.setAttribute("aria-label", label("precision"));
  nodes.push(el("label", {class: "conversation-note"}, [text(label("precision")), precision]));
  function input(key, type = "text", rows = 1, max = 1000) {
    nodes.push(el("label", {class: "conversation-note"}, [text(label(key)), el(rows > 1 ? "textarea" : "input", {
      type, rows, maxLength: max, value: form[key], disabled: busy, "aria-label": label(key),
      onInput: e => {form[key] = e.target.value; changed();},
    })]));
  }
  input("date", "date", 1, 10);
  if (form.precision === "period") {
    const period = select({value: form.period, options: ["", "dawn", "morning", "noon", "afternoon", "evening", "night"].map(value =>
      ({value, label: value ? label(`period.${value}`) : label("choosePeriod")})), onChange: value => {form.period = value; changed();}});
    period.disabled = busy; period.setAttribute("aria-label", label("period")); nodes.push(period);
  }
  if (form.precision === "minute") {
    input("clock", "time", 1, 5);
    const fold = select({value: form.fold === null ? "" : String(form.fold), options: [
      {value: "", label: t("remindersV2.foldAuto")}, {value: "0", label: t("remindersV2.foldFirst")}, {value: "1", label: t("remindersV2.foldSecond")},
    ], onChange: value => {form.fold = value === "" ? null : Number(value); changed();}});
    fold.disabled = busy; fold.setAttribute("aria-label", t("remindersV2.fold")); nodes.push(fold);
  }
  nodes.push(text(label("sourceHint")));
  input("confirmation_source_event_id", "text", 1, 100);
  input("confirmation_quote", "text", 3, 500);
  input("note", "text", 2);
  nodes.push(el("label", {class: "row-tight"}, [el("input", {type: "checkbox", checked: form.authorized, disabled: busy,
    onChange: e => {form.authorized = e.target.checked; changed();}}), label("authorized")]));
  saveButton = button(label("save"), () => save("confirm"), !valid() || !timing.timezone);
  clearButton = button(label("clear"), () => save("clear"), !form.authorized || !form.note.trim());
  nodes.push(el("div", {class: "row-tight"}, [saveButton, timing.state === "confirmed" ? clearButton : null, button(label("close"), close)]));
  return card({title: label("edit"), body: nodes, extraClass: "episode-time-editor"});
}

export function episodeTimeAudit(records = []) {
  return records.map(record => el("div", {class: "conversation-review"}, [
    text(`${label(record.operation === "confirm" ? "save" : "clear")} · ${record.actor} · v${record.timing.version}`),
    text(`${formatEpisodeTime(record.timing.confirmed)} · ${record.timing.timezone}`), text(record.note, "conversation-message"),
    record.timing.confirmed.confirmation_source_event_id ? text(`${record.timing.confirmed.confirmation_source_event_id} · ${record.timing.confirmed.confirmation_quote}`, "conversation-message") : null,
    record.timing.confirmed.selection_event_id ? text(`${label("selectionSource")} · ${record.timing.confirmed.selection_event_id} · ${record.timing.confirmed.selection_quote}`, "conversation-message") : null,
    record.approval_event_id ? text(`${label("approvalSource")} · ${record.approval_event_id} · ${record.approval_quote}`, "conversation-message") : null,
  ]));
}
