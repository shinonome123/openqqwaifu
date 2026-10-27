import { api } from "../api.js";
import { el, card, empty, chip, select } from "../components.js";
import { t } from "../i18n.js";
import { confirmDialog, toastOk } from "../ui.js";

export function createMemoryPanel({run, isBusy, renderPage, refresh, getCharacters}) {
  let conversationId = "", characterId = "", records = [], cursor = null, form = null, revisions = null;
  let autoExtraction = false;
  const label = (key, params) => t(`memoriesV2.${key}`, params);
  const text = (value, cls = "muted") => el("div", {class: cls, text: value});
  const button = (name, action, disabled = false) => el("button", {type: "button", class: "btn", text: name,
    disabled: isBusy() || disabled, onClick: () => run(action)});

  async function load(id, character, append = false) {
    const result = await api.runtimeMemories(id, character, append && cursor ? {before_id: cursor} : {});
    if (conversationId !== id || characterId !== character) { form = null; revisions = null; }
    conversationId = id; characterId = character;
    records = append ? [...records, ...result.records] : result.records;
    cursor = result.next_before_id;
    autoExtraction = !!result.structured_extraction_enabled;
  }

  async function startCreate(id, item) {
    await load(id, item.turn.character_id);
    const quote = item.user_message.segments.find(p => p.type === "text" && p.text.trim())?.text.slice(0, 500) || "";
    form = {source_event_id: item.event_id, note: "", request_id: crypto.randomUUID(),
      change: {op: "create", memory_id: "", expected_version: 0, kind: "episode", state: "open", title: "", text: quote, quote}};
    revisions = null;
  }

  async function inspect(record, edit = false) {
    const result = await api.runtimeMemories(conversationId, characterId, {memory_id: record.memory_id});
    revisions = result;
    form = edit ? {source_event_id: result.record.source_event_id, note: "", request_id: crypto.randomUUID(), change: {
      op: "revise", memory_id: result.record.memory_id, expected_version: result.record.version,
      kind: result.record.kind, state: result.record.state, title: result.record.title,
      text: result.record.text, quote: result.record.quote,
    }} : null;
  }

  async function submit(withdraw = false) {
    const payload = {action: "memory", conversation_id: conversationId, character_id: characterId,
      ...form, change: {...form.change, ...(withdraw ? {op: "withdraw", state: "withdrawn"} : {})}};
    if (!await confirmDialog({title: label(withdraw ? "withdraw" : "save"),
      message: label(withdraw ? "withdrawConfirm" : "saveConfirm")})) return;
    await api.runtimeCommand(payload);
    form = null; revisions = null;
    toastOk(label("queued"));
    await refresh();
  }

  function renderForm() {
    if (!form) return null;
    const inputs = [];
    let saveButton, withdrawButton;
    const valid = () => form && [form.change.title, form.change.text, form.change.quote, form.note].every(v => v.trim());
    function changed() {
      form.request_id = crypto.randomUUID();
      if (saveButton) saveButton.disabled = isBusy() || !valid();
      if (withdrawButton) withdrawButton.disabled = isBusy() || !valid();
    }
    function input(key, caption, max, rows = 1) {
      const value = key === "note" ? form.note : form.change[key];
      const node = el(rows > 1 ? "textarea" : "input", {value, rows, maxLength: max, disabled: isBusy(),
        "aria-label": label(caption), onInput: (event) => {
          if (key === "note") form.note = event.target.value;
          else form.change[key] = event.target.value;
          changed();
        }});
      inputs.push(el("label", {class: "conversation-note"}, [text(label(caption)), node]));
    }
    const kind = select({value: form.change.kind,
      options: ["fact", "episode"].map(value => ({value, label: label(value)})),
      onChange: (value) => {form.change.kind = value; form.change.state = value === "fact" ? "active" : "open"; changed(); renderPage();}});
    kind.disabled = isBusy() || form.change.op !== "create";
    kind.setAttribute("aria-label", label("kind"));
    inputs.push(kind);
    const state = select({value: form.change.state,
      options: (form.change.kind === "fact" ? ["active"] : ["open", "completed", "cancelled"]).map(value => ({value, label: label(value)})),
      onChange: (value) => {form.change.state = value; changed();}});
    state.disabled = isBusy(); state.setAttribute("aria-label", label("state")); inputs.push(state);
    input("title", "titleField", 80);
    input("text", "textField", 500, 3);
    input("quote", "quote", 500, 3);
    input("note", "note", 1000, 2);
    saveButton = button(label("save"), () => submit(), !valid());
    withdrawButton = form.change.op === "revise" ? button(label("withdraw"), () => submit(true), !valid()) : null;
    return card({title: label(form.change.op === "create" ? "add" : "edit"), subtitle: label("source", {id: form.source_event_id}),
      extraClass: "memory-editor", body: [text(label("manualHint")), ...inputs, el("div", {class: "row"}, [saveButton, withdrawButton,
        button(t("common.cancel"), async () => {form = null;})])]});
  }

  function render() {
    const options = getCharacters().map(c => ({value: c.character, label: c.assistant_name ? `${c.assistant_name} · ${c.character}` : c.character}));
    if (!options.some(o => o.value === characterId)) options.unshift({value: characterId, label: characterId});
    const scope = select({value: characterId, options, onChange: value => run(() => load(conversationId, value))});
    scope.disabled = isBusy(); scope.setAttribute("aria-label", label("scope"));
    const body = [text(label("hint")), el("label", {class: "conversation-note"}, [text(label("scope")), scope]),
      text(label(autoExtraction ? "extractionOn" : "extractionOff")), renderForm()];
    if (!records.length) body.push(empty({title: label("empty")}));
    records.forEach(record => body.push(el("div", {class: "conversation-memory-record"}, [
      text(`${record.title} · v${record.version}`, "conversation-speaker"),
      el("div", {class: "row"}, [chip({label: label(record.kind), variant: "outline"}),
        chip({label: label(record.state), variant: "outline"}), chip({label: label(record.provenance), variant: "outline"})]),
      text(record.text, "conversation-message"),
      text(label("sourceTime", {time: new Date(record.source_received_at * 1000).toLocaleString()})),
      text(label("source", {id: record.source_event_id}), "conversation-meta"),
      text(`“${record.quote}”`, "conversation-message"),
      el("div", {class: "row"}, [button(label("history"), () => inspect(record)),
        button(label("edit"), () => inspect(record, true), record.state === "withdrawn")]),
    ])));
    if (cursor) body.push(button(label("more"), () => load(conversationId, characterId, true)));
    if (revisions) body.push(card({title: label("history"), extraClass: "memory-revisions", body: revisions.revisions.map(revision =>
      el("div", {class: "conversation-review"}, [text(`v${revision.version} · ${revision.actor} · ${label(revision.record.state)}`),
        text(revision.record.text, "conversation-message"), text(`“${revision.record.quote}”`, "conversation-message"),
        text(revision.note), text(label("source", {id: revision.record.source_event_id}), "conversation-meta")]))}));
    return card({title: label("title"), subtitle: `${conversationId} · ${characterId}`, body, extraClass: "conversation-memories"});
  }

  return {load, render, startCreate, reset: () => {records = []; form = null; revisions = null; cursor = null;}};
}
