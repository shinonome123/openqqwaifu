// Events page: live tail of inbound and outbound traffic.

import { api } from "../api.js";
import { el, card, empty, segmented, switchControl, chip } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastError, formatTimestamp } from "../ui.js";

const REFRESH_MS = 3000;

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let events = [];
  let filter = "all";
  let autoRefresh = true;
  let pollTimer = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      const payload = await api.recentEvents(200);
      if (stopped) return;
      events = payload?.events || [];
      render();
    } catch (err) {
      if (!stopped) toastError(String(err?.message || err));
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

    const visible = events.filter((e) => filter === "all" || e.kind === filter);
    if (!visible.length) {
      container.appendChild(
        card({
          title: `${t("nav.events")} (0)`,
          body: [empty({ title: t("events.empty") })],
        }),
      );
      return;
    }

    const feed = el("div", { class: "event-feed" });
    visible.forEach((entry) => {
      feed.appendChild(
        el("div", { class: "event-row" }, [
          el("span", {
            class: "status-dot",
            style: {
              background:
                entry.kind === "outbound"
                  ? "var(--accent)"
                  : "var(--info)",
            },
          }),
          el("span", {
            class: `event-kind is-${entry.kind}`,
            text: entry.kind,
          }),
          el("span", {
            class: "event-launcher",
            text: `${entry.launcher_type || "?"}:${entry.launcher_id || "?"}`,
          }),
          el("span", { class: "event-text", text: entry.text || "-" }),
          el("span", { class: "event-time", text: formatTimestamp(entry.timestamp) }),
        ]),
      );
    });

    container.appendChild(
      card({
        title: `${t("nav.events")} (${visible.length})`,
        actions: [chip({ label: t("events.buffered", { count: events.length }), variant: "outline" })],
        body: [feed],
      }),
    );
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