import { api } from "../api.js";
import { el, card, empty, segmented, switchControl, chip } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastError, toastOk, formatTimestamp } from "../ui.js";

const REFRESH_MS = 3000;

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let transportEvents = [];
  let behaviorEvents = [];
  let proactivePanel = { candidates: [], enabled: false };
  let filter = "all";
  let autoRefresh = true;
  let pollTimer = null;
  let activeDraftKey = "";

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      const [transportPayload, behaviorPayload, proactivePayload] = await Promise.all([
        api.recentEvents(200),
        api.behaviorEvents(120),
        api.getProactivePanel(10),
      ]);
      if (stopped) return;
      transportEvents = transportPayload?.events || [];
      behaviorEvents = behaviorPayload?.events || [];
      proactivePanel = proactivePayload || { candidates: [], enabled: false };
      render();
    } catch (err) {
      if (!stopped) toastError(String(err?.message || err));
    }
  }

  async function generateDraft(candidate) {
    try {
      activeDraftKey = memberKey(candidate);
      render();
      const payload = {
        group_id: candidate.group_id || "",
        user_id: candidate.user_id || "",
      };
      const result = await api.generateProactiveDraft(payload);
      const draft = result?.draft || {};
      toastOk(`${t("events.proactive.draftReady")}\n${draft.text || ""}`);
    } catch (err) {
      toastError(String(err?.message || err));
    } finally {
      activeDraftKey = "";
      render();
    }
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    if (autoRefresh) pollTimer = setInterval(load, REFRESH_MS);
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.events.title") }),
          el("div", { class: "page-desc", text: t("page.events.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          segmented({
            value: filter,
            options: [
              { value: "all", label: t("events.filter.all") },
              { value: "inbound", label: t("events.filter.inbound") },
              { value: "outbound", label: t("events.filter.outbound") },
            ],
            onChange: (v) => {
              filter = v;
              render();
            },
          }),
          switchControl({
            checked: autoRefresh,
            label: t("events.autorefresh"),
            onChange: (v) => {
              autoRefresh = v;
              startPolling();
            },
          }),
          el("button", {
            type: "button",
            class: "btn",
            text: t("common.refresh"),
            onClick: load,
          }),
        ]),
      ]),
    );

    container.appendChild(
      el("div", { class: "overview-split" }, [renderTransportFeed(), renderBehaviorFeed()]),
    );
    container.appendChild(
      el("div", { class: "overview-split", style: { marginTop: "16px" } }, [renderProactiveCard()]),
    );
  }

  function renderTransportFeed() {
    const visible = transportEvents.filter((e) => filter === "all" || e.kind === filter);
    if (!visible.length) {
      return card({
        title: t("events.transport.title"),
        body: [empty({ title: t("events.empty") })],
      });
    }
    const feed = el("div", { class: "event-feed" });
    visible.forEach((entry) => {
      feed.appendChild(
        el("div", { class: "event-row" }, [
          el("span", {
            class: "status-dot",
            style: {
              background: entry.kind === "outbound" ? "var(--accent)" : "var(--info)",
            },
          }),
          el("span", { class: `event-kind is-${entry.kind}`, text: entry.kind }),
          el("span", {
            class: "event-launcher",
            text: `${entry.launcher_type || "?"}:${entry.launcher_id || "?"}`,
          }),
          el("span", { class: "event-text", text: entry.text || "-" }),
          el("span", { class: "event-time", text: formatTimestamp(entry.timestamp) }),
        ]),
      );
    });
    return card({
      title: `${t("events.transport.title")} (${visible.length})`,
      actions: [chip({ label: t("events.buffered", { count: transportEvents.length }), variant: "outline" })],
      body: [feed],
    });
  }

  function renderBehaviorFeed() {
    if (!behaviorEvents.length) {
      return card({
        title: t("events.behavior.title"),
        body: [empty({ title: t("events.behavior.empty") })],
      });
    }
    const feed = el("div", { class: "event-feed" });
    behaviorEvents.forEach((entry) => {
      feed.appendChild(
        el("div", { class: "event-row" }, [
          chip({ label: entry.kind || "event", variant: "accent" }),
          el("span", {
            class: "event-launcher",
            text: `${entry.launcher_type || "?"}:${entry.launcher_id || "?"}`,
          }),
          el("span", { class: "event-text", text: entry.summary || "-" }),
          el("span", { class: "event-time", text: formatTimestamp(entry.timestamp) }),
        ]),
      );
    });
    return card({
      title: `${t("events.behavior.title")} (${behaviorEvents.length})`,
      body: [feed],
    });
  }

  function renderProactiveCard() {
    const candidates = proactivePanel?.candidates || [];
    if (!candidates.length) {
      return card({
        title: t("events.proactive.title"),
        subtitle: proactivePanel?.enabled
          ? t("events.proactive.desc")
          : t("events.proactive.disabled"),
        body: [empty({ title: t("events.proactive.empty") })],
      });
    }
    const list = el("div", { class: "stack-list" });
    candidates.forEach((candidate) => {
      const key = memberKey(candidate);
      list.appendChild(
        el("div", { class: "card is-compact" }, [
          el("div", { class: "card-body" }, [
            el("div", { class: "card-header" }, [
              el("div", {}, [
                el("div", { class: "card-title", text: candidate.display_name || candidate.user_id }),
                el("div", { class: "card-subtitle", text: candidate.reason || "-" }),
              ]),
              el("div", { class: "row-tight" }, [
                chip({
                  label: `${t("user.directory.affinity")}: ${Number(candidate.affinity_score || 0).toFixed(2)}`,
                  variant: "outline",
                }),
                chip({ label: candidate.bond_stage || "-", variant: "ok" }),
              ]),
            ]),
            el("div", { class: "row-tight", style: { marginTop: "12px" } }, [
              el("button", {
                type: "button",
                class: "btn is-primary",
                disabled: activeDraftKey === key,
                text:
                  activeDraftKey === key
                    ? t("common.loading")
                    : t("events.proactive.generate"),
                onClick: () => generateDraft(candidate),
              }),
            ]),
          ]),
        ]),
      );
    });
    return card({
      title: t("events.proactive.title"),
      subtitle: proactivePanel?.enabled
        ? t("events.proactive.desc")
        : t("events.proactive.disabled"),
      body: [list],
    });
  }

  load();
  startPolling();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (pollTimer) clearInterval(pollTimer);
    if (unsubLang) unsubLang();
  };
}

function memberKey(candidate) {
  return `${candidate?.group_id || ""}:${candidate?.user_id || ""}`;
}
