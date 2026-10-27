import { api } from "../api.js";
import { el, card, chip, select } from "../components.js";
import { t } from "../i18n.js";
import { confirmDialog, toastOk } from "../ui.js";

export function createPrivacyPanel({run, isBusy, refresh, getCharacters, showDelivery}) {
  let id = "", data = null, editing = false, typed = "", acknowledged = false, requestId = "", character = "";
  const label = (key, params) => t(`privacyV2.${key}`, params);
  const text = (value, cls = "muted") => el("div", {class: cls, text: value});
  const button = (caption, fn, disabled = false) => el("button", {type: "button", class: "btn", text: caption,
    disabled: isBusy() || disabled, onClick: () => run(fn)});

  async function load(conversationId) {
    data = await api.runtimePrivacy(conversationId);
    id = conversationId;
    editing = false; typed = ""; acknowledged = false;
    character = getCharacters()[0]?.character || "";
  }

  async function erase() {
    if (!await confirmDialog({title: label("erase"), message: label("eraseConfirm", {id}), danger: true})) return;
    await api.runtimeCommand({action: "privacy", operation: "erase", conversation_id: id,
      request_id: requestId, preview_token: data.preview_token, confirmed: acknowledged, confirm_conversation_id: typed});
    editing = false; typed = ""; acknowledged = false;
    toastOk(label("erasedHint"));
    await refresh();
  }

  async function resume() {
    if (!await confirmDialog({title: label("resume"), message: label("resumeConfirm", {id})})) return;
    await api.runtimeCommand({action: "privacy", operation: "resume", conversation_id: id,
      expected_generation: data.data_generation, character_id: character});
    await refresh();
    toastOk(label("resumed"));
  }

  function render() {
    if (!data) return null;
    const body = [text(label("scope", {id})), text(label(`state.${data.state}`)),
      text(t(data.mode === "capture" ? "conversations.captureHint" : "conversations.liveHint"))];
    if (data.state === "active") {
      body.push(button(label("preview"), async () => {await load(id); editing = true; requestId = crypto.randomUUID();}));
      if (editing) {
        body.push(text(label("scopeDetails")), text(label("retained")));
        for (const [key, count] of Object.entries(data.counts)) {
          body.push(text(`${label(`count.${key}`)}：${count}`));
        }
        let submit;
        const sync = () => {submit.disabled = isBusy() || typed !== id || !acknowledged;};
        body.push(el("label", {class: "conversation-note"}, [text(label("typeId", {id})),
          el("input", {type: "text", value: typed, maxLength: 100, disabled: isBusy(), "aria-label": label("typeId", {id}),
            onInput: e => {typed = e.target.value; sync();}})]));
        body.push(el("label", {class: "row-tight"}, [el("input", {type: "checkbox", checked: acknowledged, disabled: isBusy(),
          onChange: e => {acknowledged = e.target.checked; sync();}}), label("acknowledge")]));
        submit = button(label("erase"), erase, typed !== id || !acknowledged);
        submit.classList.add("is-danger");
        body.push(submit);
      }
    } else {
      body.push(text(label("erasedHint")), text(label("retained")));
      const picker = select({value: character, options: getCharacters().map(c => ({value: c.character, label: `${c.assistant_name || c.character} · ${c.character}`})),
        onChange: value => {character = value;}});
      picker.disabled = isBusy(); picker.setAttribute("aria-label", label("character"));
      body.push(el("label", {class: "conversation-note"}, [text(label("character")), picker]),
        button(label("resume"), resume, !character || data.pending_deliveries.length > 0));
    }
    if (data.pending_deliveries.length) {
      body.push(text(label("inflight"), "conversation-error"));
      for (const item of data.pending_deliveries) {
        body.push(el("div", {class: "conversation-review"}, [text(`${item.outbox_id} · ${t(`conversations.state.${item.state}`)}`),
          button(t("conversations.delivery"), () => showDelivery(item.outbox_id))]));
      }
    }
    if (data.deletions.length) body.push(el("details", {}, [el("summary", {text: label("audit")}),
      ...data.deletions.map(r => text(label("auditEntry", {generation: r.generation, actor: r.actor, time: new Date(r.created_at * 1000).toISOString()})))]));
    return card({title: label("title"), actions: [chip({label: label(`state.${data.state}`), variant: data.state === "active" ? "outline" : "warn"})],
      body, extraClass: "conversation-privacy"});
  }

  return {load, render};
}
