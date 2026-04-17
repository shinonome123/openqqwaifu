import { api } from "../api.js";
import { el, card, chip, empty, segmented, statCard, switchControl } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { formatTimestamp, toastError } from "../ui.js";

const POLL_MS = 4000;

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let pollTimer = null;
  let panel = null;
  let autoRefresh = true;
  let levelFilter = "all";

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      panel = await api.getObservabilityPanel(180, 40);
      if (!stopped) render();
    } catch (err) {
      if (!stopped) toastError(String(err?.message || err));
    }
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    if (autoRefresh) pollTimer = setInterval(load, POLL_MS);
  }

  function render() {
    container.innerHTML = "";
    const runtime = panel?.runtime || {};
    const logs = filterLogs(panel?.logs || [], levelFilter);
    const upstream = panel?.upstream || { total: 0, error_total: 0, targets: [], rows: [] };
    const http = panel?.http || { total: 0, rows: [] };
    const onebot = panel?.onebot || { total: 0, rows: [] };

    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.observability.title") }),
          el("div", { class: "page-desc", text: t("page.observability.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          segmented({
            value: levelFilter,
            options: [
              { value: "all", label: t("observability.filter.all") },
              { value: "warn", label: t("observability.filter.warn") },
              { value: "error", label: t("observability.filter.error") },
            ],
            onChange: (value) => {
              levelFilter = value;
              render();
            },
          }),
          switchControl({
            checked: autoRefresh,
            label: t("observability.autorefresh"),
            onChange: (value) => {
              autoRefresh = value;
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
      el("div", { class: "kpi-row" }, [
        statCard({
          label: t("overview.kpi.uptime"),
          value: `${Number(runtime.uptime_seconds || 0).toFixed(0)}s`,
          meta: `${runtime.total_events || 0} events`,
        }),
        statCard({
          label: t("observability.kpi.logs"),
          value: String(logs.length),
          meta: `${runtime.background_tasks || 0} bg`,
        }),
        statCard({
          label: t("observability.kpi.upstream"),
          value: String(upstream.total || 0),
          meta: `${http.total || 0} http / ${onebot.total || 0} onebot`,
        }),
        statCard({
          label: t("observability.kpi.errors"),
          value: String(upstream.error_total || 0),
          meta: `${runtime.recent_behavior || 0} behavior`,
          metaVariant: Number(upstream.error_total || 0) > 0 ? "down" : "up",
        }),
      ]),
    );

    container.appendChild(
      el("div", { class: "observability-grid" }, [
        renderTargetsCard(upstream.targets || []),
        renderLogsCard(logs),
      ]),
    );

    container.appendChild(
      el("div", { class: "overview-split", style: { marginTop: "16px" } }, [
        renderHttpCard(http.rows || []),
        renderOneBotCard(onebot.rows || []),
      ]),
    );
  }

  function renderTargetsCard(rows) {
    if (!rows.length) {
      return card({
        title: t("observability.targets.title"),
        subtitle: t("observability.targets.desc"),
        body: [empty({ title: t("observability.targets.empty") })],
      });
    }
    return card({
      title: t("observability.targets.title"),
      subtitle: t("observability.targets.desc"),
      body: [
        tableView(
          [
            t("observability.table.kind"),
            t("observability.table.target"),
            t("observability.table.requests"),
            t("observability.table.errors"),
            t("observability.table.latency"),
          ],
          rows.map((row) => [
            `${row.kind}\n${row.host}`,
            row.target,
            String(row.total || 0),
            String(row.error_total || 0),
            `${Number(row.avg_ms || 0).toFixed(1)} ms`,
          ]),
        ),
      ],
    });
  }

  function renderLogsCard(rows) {
    if (!rows.length) {
      return card({
        title: t("observability.logs.title"),
        subtitle: t("observability.logs.desc"),
        body: [empty({ title: t("observability.logs.empty") })],
      });
    }
    const feed = el("div", { class: "observability-log-feed" });
    rows.forEach((row) => {
      feed.appendChild(
        el("div", { class: "observability-log-row" }, [
          chip({ label: row.level || "INFO", variant: levelVariant(row.level) }),
          el("div", { class: "observability-log-body" }, [
            el("div", { class: "observability-log-meta" }, [
              el("span", { class: "mono", text: formatTimestamp(row.timestamp) }),
              el("span", { class: "mono", text: row.logger || "-" }),
            ]),
            el("div", { class: "observability-log-message", text: row.message || "-" }),
          ]),
        ]),
      );
    });
    return card({
      title: t("observability.logs.title"),
      subtitle: t("observability.logs.desc"),
      body: [feed],
    });
  }

  function renderHttpCard(rows) {
    return card({
      title: t("observability.http.title"),
      subtitle: t("observability.http.desc"),
      body: [
        rows.length
          ? tableView(
              [
                t("observability.table.method"),
                t("observability.table.path"),
                t("observability.table.status"),
                t("observability.table.requests"),
                t("observability.table.latency"),
              ],
              rows.map((row) => [
                row.method,
                row.path,
                row.status,
                String(row.total || 0),
                `${Number(row.avg_ms || 0).toFixed(1)} ms`,
              ]),
            )
          : empty({ title: t("observability.http.empty") }),
      ],
    });
  }

  function renderOneBotCard(rows) {
    return card({
      title: t("observability.onebot.title"),
      subtitle: t("observability.onebot.desc"),
      body: [
        rows.length
          ? tableView(
              [
                t("observability.table.target"),
                t("observability.table.outcome"),
                t("observability.table.requests"),
                t("observability.table.latency"),
              ],
              rows.map((row) => [
                row.post_type,
                row.outcome,
                String(row.total || 0),
                `${Number(row.avg_ms || 0).toFixed(1)} ms`,
              ]),
            )
          : empty({ title: t("observability.onebot.empty") }),
      ],
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

function filterLogs(rows, levelFilter) {
  return rows.filter((row) => {
    const level = String(row?.level || "INFO").toUpperCase();
    const bucket = levelFilterState(level);
    if (levelFilter === "error") return bucket === "error";
    if (levelFilter === "warn") return bucket === "error" || bucket === "warn";
    return true;
  });
}

function levelFilterState(level) {
  const normalized = String(level || "").toUpperCase();
  if (normalized === "ERROR" || normalized === "CRITICAL") return "error";
  if (normalized === "WARNING") return "warn";
  return "all";
}

function levelVariant(level) {
  const bucket = levelFilterState(level);
  if (bucket === "error") return "danger";
  if (bucket === "warn") return "warn";
  return "outline";
}

function tableView(headers, rows) {
  const table = el("table", { class: "table" });
  table.appendChild(
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        headers.map((title) => el("th", { text: title })),
      ),
    ),
  );
  table.appendChild(
    el(
      "tbody",
      {},
      rows.map((row) =>
        el(
          "tr",
          {},
          row.map((value) => el("td", { text: value })),
        ),
      ),
    ),
  );
  return el("div", { class: "table-wrap" }, [table]);
}
