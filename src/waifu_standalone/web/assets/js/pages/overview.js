// Overview page: KPIs, identity, providers, recent traffic.

import { api } from "../api.js";
import {
  el,
  card,
  statCard,
  chip,
  statusChip,
  empty,
} from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { formatDuration, formatTimestamp, toastError } from "../ui.js";

const POLL_MS = 6000;

export function mount(root) {
  let stopped = false;
  let pollTimer = null;
  let unsubLang = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  renderSkeleton(container);

  async function refresh() {
    try {
      const [dash, runtime, events] = await Promise.all([
        api.dashboard(),
        api.runtime(),
        api.recentEvents(12),
      ]);
      if (stopped) return;
      renderContent(container, { dash, runtime, events: events?.events || [] });
    } catch (err) {
      if (stopped) return;
      toastError(String(err?.message || err));
    }
  }

  refresh();
  pollTimer = setInterval(refresh, POLL_MS);
  unsubLang = onLangChange(() => refresh());

  return () => {
    stopped = true;
    if (pollTimer) clearInterval(pollTimer);
    if (unsubLang) unsubLang();
  };
}

function renderSkeleton(container) {
  container.appendChild(
    el("div", { class: "page-header" }, [
      el("div", { class: "page-header-text" }, [
        el("div", { class: "page-title", i18n: "page.overview.title" }),
        el("div", { class: "page-desc", i18n: "page.overview.desc" }),
      ]),
    ]),
  );
  container.appendChild(
    el("div", { class: "kpi-row" }, [
      skeletonStat(),
      skeletonStat(),
      skeletonStat(),
      skeletonStat(),
    ]),
  );
}

function skeletonStat() {
  return el("div", { class: "card is-compact" }, [
    el("div", { class: "skeleton", style: { height: "14px", width: "60%" } }),
    el("div", { class: "skeleton", style: { height: "24px", width: "50%", marginTop: "8px" } }),
  ]);
}

function renderContent(container, { dash, runtime, events }) {
  container.innerHTML = "";

  container.appendChild(
    el("div", { class: "page-header" }, [
      el("div", { class: "page-header-text" }, [
        el("div", { class: "page-title", text: t("page.overview.title") }),
        el("div", { class: "page-desc", text: t("page.overview.desc") }),
      ]),
    ]),
  );

  const kpis = el("div", { class: "kpi-row" }, [
    statCard({
      label: t("overview.kpi.uptime"),
      value: formatDuration(runtime?.uptime_seconds ?? 0),
      meta: `${runtime?.total_events ?? 0} events`,
    }),
    statCard({
      label: t("overview.kpi.inbound"),
      value: String(runtime?.recent_inbound ?? 0),
    }),
    statCard({
      label: t("overview.kpi.outbound"),
      value: String(runtime?.recent_outbound ?? 0),
    }),
    statCard({
      label: t("overview.kpi.followups"),
      value: String(runtime?.active_followups ?? 0),
    }),
  ]);
  container.appendChild(kpis);

  container.appendChild(
    el("div", { class: "overview-split" }, [
      renderIdentity(dash),
      renderProviders(dash),
    ]),
  );

  container.appendChild(renderRecent(events));
}

function renderIdentity(dash) {
  return card({
    title: t("overview.identity.title"),
    subtitle: t("overview.identity.desc"),
    body: [
      el("div", { class: "profile-header" }, [
        el("div", { class: "portrait", text: initialsOf(dash?.assistant_name || "W") }),
        el("div", {}, [
          el("div", { class: "profile-name", text: dash?.assistant_name || "-" }),
          el("div", { class: "profile-handle", text: `@${dash?.bot_account_id || "unset"}` }),
          el("div", { class: "muted text-sm", text: `${dash?.character || "default"}` }),
        ]),
      ]),
      el("div", { class: "row" }, [
        chip({ label: dash?.service_name || "-", variant: "outline" }),
        dash?.summarization_mode ? chip({ label: t("overview.flag.summarize"), variant: "info" }) : null,
        dash?.search_enabled ? chip({ label: t("overview.flag.search"), variant: "ok" }) : null,
      ]),
      el("div", { class: "row" }, [
        chip({
          label: dash?.group_reply_requires_mention
            ? t("overview.message.mentionRequired")
            : t("overview.message.openGroups"),
          variant: "outline",
        }),
        chip({
          label: t("overview.message.followup", { seconds: dash?.reply_window_seconds ?? 0 }),
          variant: "outline",
        }),
        chip({
          label: t("overview.message.delay", { seconds: dash?.group_response_delay_seconds ?? 0 }),
          variant: "outline",
        }),
        dash?.multimodal_enabled
          ? chip({ label: t("overview.message.multimodal"), variant: "info" })
          : null,
        Number(dash?.repeat_trigger_count ?? 0) > 0
          ? chip({
              label: t("overview.message.repeat", { count: dash?.repeat_trigger_count ?? 0 }),
              variant: "warn",
            })
          : null,
      ]),
    ],
  });
}

function renderProviders(dash) {
  const llm = dash?.llm || {};
  const img = dash?.image_generation || {};
  const sidecar = dash?.qq_sidecar || {};
  const sessionCount = dash?.session_count ?? 0;
  const knowledgeCount = dash?.knowledge_count ?? 0;
  const memberCount = dash?.member_count ?? 0;
  return card({
    title: t("overview.providers.title"),
    subtitle: t("overview.providers.desc"),
    body: [
      providerRow(t("overview.provider.llm"), llm.enabled, llm.ready, llm.base_url || llm.backend || ""),
      providerRow(t("overview.provider.image"), img.enabled, img.ready, img.model || img.base_url || ""),
      providerRow(
        t("overview.provider.sidecar"),
        !!sidecar.outbound_base_url,
        !!sidecar.outbound_base_url,
        sidecar.outbound_base_url || t("overview.provider.live"),
      ),
      providerRow(
        t("overview.provider.memory"),
        true,
        true,
        t("overview.provider.memoryMeta", {
          sessions: sessionCount,
          knowledge: knowledgeCount,
          members: memberCount,
        }),
      ),
    ],
  });
}

function providerRow(label, enabled, ready, meta) {
  const labels = {
    ok: t("common.enabled"),
    off: t("common.disabled"),
    neutral: "-",
    warn: t("overview.state.degraded"),
    danger: t("overview.state.error"),
  };
  let state = "off";
  if (enabled && ready) state = "ok";
  else if (enabled && !ready) state = "warn";
  return el("div", { class: "row", style: { justifyContent: "space-between" } }, [
    el("div", {}, [
      el("div", { text: label, style: { fontWeight: "500" } }),
      meta ? el("div", { class: "muted text-xs", text: meta }) : null,
    ]),
    statusChip(state, labels),
  ]);
}

function renderRecent(events) {
  if (!events.length) {
    return card({
      title: t("overview.recent.title"),
      body: [empty({ title: t("overview.recent.empty") })],
    });
  }
  const list = el("div", { class: "list" });
  events.forEach((entry) => {
    list.appendChild(
      el("div", { class: "list-item" }, [
        el("div", {
          class: `list-item-icon is-${entry.kind === "outbound" ? "outbound" : "inbound"}`,
          text: entry.kind === "outbound" ? "OUT" : "IN",
        }),
        el("div", { class: "list-item-body" }, [
          el("div", { class: "list-item-meta" }, [
            chip({
              label: entry.kind,
              variant: entry.kind === "outbound" ? "accent" : "info",
            }),
            el("span", { text: `${entry.launcher_type || "?"}:${entry.launcher_id || "?"}` }),
            el("span", { text: formatTimestamp(entry.timestamp) }),
          ]),
          el("div", { class: "list-item-text", text: entry.text || "-" }),
        ]),
      ]),
    );
  });
  return card({ title: t("overview.recent.title"), body: [list] });
}

function initialsOf(name) {
  if (!name) return "W";
  const trimmed = String(name).trim();
  if (!trimmed) return "W";
  return trimmed.slice(0, 1).toUpperCase();
}
