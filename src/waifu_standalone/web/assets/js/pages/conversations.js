import { api } from "../api.js";
import { el, card, empty, chip, select } from "../components.js";
import { t, getLang, onLangChange } from "../i18n.js";
import { toastError, toastOk, confirmDialog } from "../ui.js";
import { createMemoryPanel } from "./conversation_memories.js";
import { createReminderPanel } from "./conversation_reminders.js";
import { createPrivacyPanel } from "./conversation_privacy.js";
import { createContactPolicyPanel } from "./conversation_contact_policy.js";
import { createFollowupPanel } from "./conversation_followups.js";

function formatTimestamp(value) {
  if (!Number.isFinite(value)) return "—";
  return new Date(value * 1000).toLocaleString(getLang() === "zh" ? "zh-CN" : "en-US", { hour12: false });
}

export function mount(root) {
  let stopped = false, busy = false, loaded = false, enabled = false;
  let rows = [], afterId = null, meta = {}, selected = "", detail = null, delivery = null;
  let characters = [], targetCharacter = "", note = "", acknowledge = false, bindRequest = null;
  const container = el("div", { class: "page conversations-page" });
  root.replaceChildren(container);
  const memories = createMemoryPanel({run, isBusy: () => busy, renderPage: render, refresh, getCharacters: () => characters});
  const reminders = createReminderPanel({run, isBusy: () => busy, refresh, showDelivery});
  const privacy = createPrivacyPanel({run, isBusy: () => busy, refresh, getCharacters: () => characters, showDelivery});
  const contact = createContactPolicyPanel({run, isBusy: () => busy, refresh});
  const followups = createFollowupPanel({run, isBusy: () => busy, refresh, showDelivery});
  const label = (key, params) => t(`conversations.${key}`, params);
  const stateLabel = (state) => label(`state.${state}`);
  const text = (value, cls = "muted") => el("div", { class: cls, text: value });
  const button = (caption, action, disabled = false, cls = "btn") => el("button", {
    type: "button", class: cls, text: caption, disabled: busy || disabled, onClick: action,
  });
  const badge = (state) => chip({ label: stateLabel(state), variant:
    ["unknown", "needs_review", "rejected", "failed"].includes(state) ? "warn" : "outline" });

  async function run(action) {
    if (busy || stopped) return;
    busy = true;
    render();
    try { await action(); }
    catch (err) { if (!stopped) toastError(String(err?.message || err)); }
    finally { busy = false; if (!stopped) render(); }
  }

  async function loadList(append = false) {
    const result = await api.runtimeConversations(append ? afterId : "");
    if (stopped) return;
    loaded = true;
    enabled = !!result.enabled;
    meta = result.meta || {};
    rows = append ? [...rows, ...result.conversations] : result.conversations;
    afterId = result.next_after_id;
  }

  async function loadDetail(id, older = false) {
    const result = await api.runtimeConversation(id, older ? detail.next_before_seq : "");
    if (stopped) return;
    if (older && result.binding.data_generation === detail.binding.data_generation) result.items = [...result.items, ...detail.items];
    if (detail?.binding.data_generation !== result.binding.data_generation || result.binding.privacy_state !== "active") {
      memories.reset(); reminders.reset(); contact.reset(); followups.reset();
    }
    if (result.binding.privacy_state === "active") {
      await memories.load(id, result.binding.character_id);
      await reminders.load(id, result.binding.character_id);
      await contact.load(id);
      await followups.load(id, result.binding.character_id);
    }
    await privacy.load(id);
    if (stopped) return;
    if (selected !== id || detail?.binding.data_generation !== result.binding.data_generation || detail?.binding.privacy_state !== result.binding.privacy_state) {
      targetCharacter = result.binding.character_id; bindRequest = null;
    }
    selected = id;
    detail = result;
    delivery = null;
    note = "";
    acknowledge = false;
  }

  async function refresh() {
    await loadList();
    if (enabled && selected) await loadDetail(selected);
  }

  async function showDelivery(id) {
    delivery = await api.runtimeDelivery(id);
    note = "";
    acknowledge = false;
    if (!stopped) {
      render();
      container.querySelector(".conversation-delivery")?.scrollIntoView({ block: "nearest" });
    }
  }

  async function bind() {
    const id = selected, character = targetCharacter;
    if (!await confirmDialog({ title: label("bind"),
      message: label("bindConfirm", { id, character }) }) || stopped) return;
    if (!bindRequest || bindRequest.conversation_id !== id || bindRequest.character_id !== character) {
      bindRequest = { action: "bind", conversation_id: id, character_id: character, request_id: crypto.randomUUID() };
    }
    await api.runtimeCommand(bindRequest);
    bindRequest = null;
    if (stopped) return;
    toastOk(label("bindQueued"));
    await refresh();
  }

  async function review(action) {
    const current = delivery;
    if (!note.trim() || (action === "retry" && !acknowledge)) return;
    if (!await confirmDialog({ title: label(action), message: label(`${action}Confirm`, {
      id: current.conversation_id, seq: current.seq,
    }), danger: action === "retry" }) || stopped) return;
    await api.runtimeCommand({ action, outbox_id: current.outbox_id, note: note.trim(),
      acknowledge_duplicate_risk: acknowledge, expected_attempts: current.attempts, expected_state: current.state });
    if (stopped) return;
    toastOk(label("reviewSaved"));
    await refresh();
    await showDelivery(current.outbox_id);
  }

  function render() {
    container.replaceChildren(el("div", { class: "page-header" }, [
      el("div", {}, [text(label("title"), "page-title"), text(label("desc"), "page-desc")]),
      button(t("common.refresh"), () => run(refresh)),
    ]));
    if (!loaded) { container.append(empty({ title: busy ? t("common.loading") : label("loadFailed") })); return; }
    if (!enabled) { container.append(card({ title: label("disabled"), body: [text(label("disabledHint"))] })); return; }
    container.append(el("div", { class: "conversation-mode" }, [
      chip({ label: label(meta.mode === "capture" ? "capture" : "live"), variant: "accent" }),
      text(label(meta.mode === "capture" ? "captureHint" : "liveHint")),
    ]));
    const list = el("div", { class: "conversation-list" });
    rows.forEach((row) => list.append(el("button", {
      type: "button", class: `conversation-select${selected === row.conversation_id ? " is-active" : ""}`,
      disabled: busy, "aria-pressed": selected === row.conversation_id ? "true" : "false",
      onClick: () => run(() => loadDetail(row.conversation_id)),
    }, [text(row.conversation_id, "card-title"), text(`${row.assistant_name || row.character_id} · ${row.character_id}`),
      text(formatTimestamp(row.last_received_at)),
      row.counts.needs_review ? chip({ label: label("reviewCount", { count: row.counts.needs_review }), variant: "warn" }) : null,
      row.counts.failed ? chip({ label: label("failedCount", { count: row.counts.failed }), variant: "warn" }) : null,
    ])));
    if (!rows.length) list.append(empty({ title: label("empty") }));
    if (afterId) list.append(button(label("moreConversations"), () => run(() => loadList(true))));
    container.append(el("div", { class: "conversation-layout" }, [
      card({ title: label("list"), body: [list] }),
      el("div", { class: "conversation-main" }, detail ? renderDetail() : [empty({ title: label("select") })]),
    ]));
  }

  function renderDetail() {
    const binding = detail.binding;
    if (binding.privacy_state !== "active") return [privacy.render(), delivery ? renderDelivery() : null];
    const choices = characters.map((c) => ({ value: c.character, label: c.assistant_name ? `${c.assistant_name} · ${c.character}` : c.character }));
    if (targetCharacter && !choices.some((c) => c.value === targetCharacter)) {
      choices.unshift({ value: targetCharacter, label: targetCharacter });
    }
    const chooser = select({ value: targetCharacter,
      options: choices,
      onChange: (v) => { targetCharacter = v; bindRequest = null; },
    });
    chooser.disabled = busy;
    chooser.setAttribute("aria-label", label("targetCharacter"));
    const bindingCard = card({ title: `${binding.assistant_name || binding.character_id} · ${selected}`,
      subtitle: label("bindingVersion", { id: binding.character_id, version: binding.character_version.slice(0, 12), generation: binding.generation }),
      body: [text(label("bindingHint")), el("div", { class: "row-tight conversation-bind" }, [chooser,
        button(label("bind"), () => run(bind), !targetCharacter || !characters.length)]),
        !characters.length ? button(label("loadCharacters"), () => run(async () => {
          characters = (await api.getCharacterPanel()).available || [];
        })) : null],
    });
    const timeline = el("div", { class: "conversation-timeline" });
    timeline.append(text(label("newestFirst")));
    [...detail.items].reverse().forEach((item) => timeline.append(renderTurn(item)));
    if (detail.next_before_seq) timeline.append(button(label("older"), () => run(() => loadDetail(selected, true))));
    if (!detail.items.length) timeline.append(empty({ title: label("empty") }));
    return [bindingCard, contact.render(), privacy.render(), reminders.render(), followups.render(), memories.render(), timeline, delivery ? renderDelivery() : null];
  }

  function renderTurn(item) {
    const body = [];
    if (item.kind === "binding") {
      const change = item.binding_change;
      body.push(text(label("bindingChange", { id: change.character_id, version: change.character_version.slice(0, 12) })));
    } else if (item.kind === "memory") {
      body.push(text(t("memoriesV2.command", {title: item.memory_change.title, character: item.memory_change.character_id})),
        text(item.memory_change.note));
    } else {
      const user = item.user_message;
      if (item.content_erased) {
        body.push(text(t("privacyV2.contentErased")));
      } else if (item.kind === "reminder_due") {
        body.push(text(t("remindersV2.trigger", {id: item.reminder.task_id})),
          text(`${item.reminder.local_time} · ${item.reminder.timezone}`));
      } else if (item.kind === "followup_due") {
        body.push(text(t("followupsV2.trigger", {title: item.followup.title})),
          text(`${item.followup.local_time} · ${item.followup.timezone}`));
      } else if (user) {
        const content = user.segments.map((part) => part.type === "text" ? part.text : part.type === "mention"
          ? `@${part.display || part.target}` : label(part.type === "image" ? "image" : "attachment")).join("");
        body.push(text(`${user.speaker_name || user.speaker_id} · ${user.speaker_id}`, "conversation-speaker"),
          text(content || label("noText"), "conversation-message"));
      }
      if (item.turn && !item.content_erased) body.push(text(label("turnCharacter", { id: item.turn.character_id,
        version: item.turn.character_version.slice(0, 12) }), "conversation-meta"));
      if (item.assistant_message) body.push(el("div", { class: "conversation-reply" }, [
        text(item.turn?.assistant_name || item.turn?.character_id, "conversation-speaker"),
        badge(item.assistant_message.delivery_state), text(item.assistant_message.content, "conversation-message"),
      ]));
      if (item.outbox?.draft_text !== undefined && !item.content_erased) body.push(el("div", { class: "conversation-draft" }, [
        text(label("draft"), "conversation-speaker"), badge(item.outbox.state),
        text(item.outbox.draft_text, "conversation-message"),
      ]));
      if (item.turn?.context_report) body.push(renderContext(item.turn.context_report));
      if (item.outbox) body.push(button(label("delivery"), () => run(() => showDelivery(item.outbox.outbox_id))));
      if (item.turn && user?.segments.some(p => p.type === "text" && p.text.trim())) {
        body.push(button(t("remindersV2.create"), () => run(async () => {
          reminders.startCreate(item);
          render();
          container.querySelector(".reminder-editor")?.scrollIntoView({block: "center"});
        }), !reminders.canCreate(item)));
        body.push(button(t("memoriesV2.add"), () => run(async () => {
          await memories.startCreate(selected, item);
          if (!stopped) {
            render();
            container.querySelector(".memory-editor")?.scrollIntoView({block: "center"});
          }
        })));
      }
    }
    if (item.error_code) body.push(text(item.error_code === "memory_revision_conflict" ? t("memoriesV2.conflict")
      : label("error", { code: item.error_code }), "conversation-error"));
    const status = item.kind === "memory" && item.state === "failed"
      ? chip({label: t("memoriesV2.changeFailed"), variant: "warn"}) : badge(item.state);
    return card({ title: `#${item.seq} · ${formatTimestamp(item.received_at)}`, actions: [status], body });
  }

  function renderContext(report) {
    const entries = [text(label(`contextStatus.${report.status}`))];
    if (report.input_budget !== undefined) entries.push(text(label("budget", {
      used: report.estimated_input_tokens, budget: report.input_budget, output: report.reserved_output_tokens,
    })), text(label("omitted", { count: report.omitted_history_ids?.length || 0,
      style: report.style_examples_omitted ? label("yes") : label("no"), images: report.image_count || 0 })));
    if (report.error_type) entries.push(text(report.error_type, "conversation-error"));
    entries.push(el("details", {}, [el("summary", { text: label("sources") }),
      el("pre", { class: "conversation-diagnostics", text: JSON.stringify(report, null, 2) })]));
    return el("details", { class: "conversation-context" }, [el("summary", { text: label("context") }), ...entries]);
  }

  function renderDelivery() {
    const current = delivery;
    const body = [text(label("deliveryTarget", { id: current.conversation_id, seq: current.seq })),
      !current.erased ? text(label("turnCharacter", { id: current.character_id, version: current.character_version.slice(0, 12) })) : null,
      text(current.erased ? t("privacyV2.contentErased") : current.message.text, "conversation-message"),
      text(label("platformId", { id: current.platform_message_id || "—" })),
      ...current.delivery_attempts.map((a) => text(`#${a.attempt} · ${stateLabel(a.state)} · ${formatTimestamp(a.started_at)}`)),
    ];
    if (["unknown", "rejected"].includes(current.state)) {
      const cancelledReminder = !!current.reminder?.cancel_requested || !!current.followup?.cancel_requested || !!current.erased;
      const contactBlocked = current.contact_decision && !current.contact_decision.allowed;
      if (contactBlocked) body.push(text(t(`${current.followup ? "followupsV2" : "contactV2"}.reason.${current.contact_decision.reason}`)));
      if (cancelledReminder) body.push(text(t(current.erased ? "privacyV2.inflight" : "remindersV2.cancelPending"), "conversation-error"));
      body.push(text(label(current.state === "unknown" ? "unknownHint" : "rejectedHint")));
      const noteInput = el("textarea", { rows: 3, value: note, maxLength: 1000, disabled: busy,
        "aria-label": label("note"), placeholder: label("note"), onInput: (event) => {
          note = event.target.value; syncButtons();
        } });
      const check = el("input", { type: "checkbox", checked: acknowledge, disabled: busy,
        onChange: (event) => { acknowledge = event.target.checked; syncButtons(); } });
      const buttons = ["confirm_sent", "discard", "retry"].filter((a) => a !== "confirm_sent" || current.state === "unknown")
        .filter(a => a !== "retry" || !cancelledReminder)
        .map((action) => ({ action, node: button(label(action), () => run(() => review(action))) }));
      function syncButtons() {
        buttons.forEach(({ action, node }) => { node.disabled = busy || !note.trim() || (action === "retry" && (!acknowledge || contactBlocked)); });
      }
      syncButtons();
      body.push(el("label", { class: "conversation-note" }, [text(label("note")), noteInput]),
        !cancelledReminder ? el("label", { class: "row-tight" }, [check, label("duplicateRisk")]) : null,
        el("div", { class: "row-tight" }, buttons.map((b) => b.node)));
    }
    body.push(text(label("reviews"), "conversation-speaker"));
    body.push(...(current.reviews.length ? current.reviews.map((r) => el("div", { class: "conversation-review" }, [
      text(`${label(r.action)} · ${formatTimestamp(r.created_at)}`), text(r.note, "conversation-message"),
    ])) : [text(label("noReviews"))]));
    return card({ title: label("delivery"), actions: [badge(current.state)], body, extraClass: "conversation-delivery" });
  }

  const unsub = onLangChange(render);
  run(async () => {
    await loadList();
    // This existing endpoint supplies available card IDs; binding stays scoped
    // to the selected conversation and never activates a global character.
    if (enabled && !stopped) characters = (await api.getCharacterPanel()).available || [];
  });
  return () => { stopped = true; unsub(); };
}
