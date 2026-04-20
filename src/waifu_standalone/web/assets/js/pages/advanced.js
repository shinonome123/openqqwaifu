// Advanced page: summarized diagnostics + raw panel explorer + danger actions.

import { api } from "../api.js";
import { el, card, jsonView, statCard, chip } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog, copyToClipboard } from "../ui.js";

const PANEL_ORDER = [
  "character",
  "ai",
  "memory",
  "abilities",
  "skills",
  "qq_login",
  "sidecar",
  "observability",
  "other",
];

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let panels = null;
  let selectedPanelKey = "character";

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      panels = await api.console();
      ensureSelectedPanel();
      if (stopped) return;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  function ensureSelectedPanel() {
    const keys = panelKeys();
    if (!keys.length) {
      selectedPanelKey = "";
      return;
    }
    if (!keys.includes(selectedPanelKey)) {
      selectedPanelKey = keys[0];
    }
  }

  function panelKeys() {
    if (!panels || typeof panels !== "object") return [];
    return PANEL_ORDER.filter((key) => Object.prototype.hasOwnProperty.call(panels, key));
  }

  function selectPanel(panelKey, { scroll = false } = {}) {
    selectedPanelKey = panelKey;
    render();
    if (scroll) {
      requestAnimationFrame(() => {
        document.getElementById("advanced-explorer")?.scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
      });
    }
  }

  function render() {
    const keys = panelKeys();
    const selectedPanel = selectedPanelKey ? panels?.[selectedPanelKey] : null;

    container.innerHTML = "";
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.advanced.title") }),
          el("div", { class: "page-desc", text: t("page.advanced.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          el("button", {
            type: "button",
            class: "btn",
            text: t("common.copy"),
            disabled: !panels,
            onClick: () => copyToClipboard(JSON.stringify(panels, null, 2)),
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

    if (!panels) {
      container.appendChild(
        card({
          title: t("advanced.panels.title"),
          subtitle: t("advanced.panels.desc"),
          body: [el("div", { class: "empty", text: t("common.loading") })],
        }),
      );
      return;
    }

    container.appendChild(renderSummaryStrip(keys));
    container.appendChild(renderPanelGrid(keys));
    container.appendChild(renderExplorer(keys, selectedPanel));
    container.appendChild(renderDangerZone());
  }

  function renderSummaryStrip(keys) {
    const memory = panels?.memory || {};
    const skills = panels?.skills || {};
    const observability = panels?.observability || {};
    const panelPreview = keys
      .slice(0, 3)
      .map((key) => panelTitle(key))
      .join(" · ");
    const previewMeta = panelPreview
      ? `${panelPreview}${keys.length > 3 ? ` +${keys.length - 3}` : ""}`
      : t("common.none");
    const httpRows = observability?.http?.rows || [];

    return el("div", { class: "kpi-row advanced-summary-row" }, [
      statCard({
        label: t("advanced.summary.panels"),
        value: String(keys.length),
        meta: previewMeta,
      }),
      statCard({
        label: t("advanced.summary.sessions"),
        value: String((memory.sessions || []).length),
        meta: t("advanced.summary.sessionsMeta", { count: memory.knowledge_count || 0 }),
      }),
      statCard({
        label: t("advanced.summary.skills"),
        value: String(skills?.skills?.count || 0),
        meta: t("advanced.summary.skillsMeta", { count: skills?.tools?.count || 0 }),
      }),
      statCard({
        label: t("advanced.summary.traffic"),
        value: String(httpRows.length),
        meta: t("advanced.summary.trafficMeta", { count: observability?.http?.total || 0 }),
      }),
    ]);
  }

  function renderPanelGrid(keys) {
    return card({
      title: t("advanced.panels.title"),
      subtitle: t("advanced.panels.desc"),
      body: keys.length
        ? [
            el(
              "div",
              { class: "advanced-panel-grid" },
              keys.map((key) => renderPanelCard(key, panels[key])),
            ),
          ]
        : [el("div", { class: "empty", text: t("advanced.explorer.empty") })],
    });
  }

  function renderPanelCard(key, panel) {
    const state = panelState(key, panel);
    return el("button", {
      type: "button",
      class: `advanced-panel-card is-${state.tone}${key === selectedPanelKey ? " is-active" : ""}`,
      onClick: () => selectPanel(key, { scroll: true }),
    }, [
      el("div", { class: "advanced-panel-card-head" }, [
        el("div", { class: "advanced-panel-card-copy" }, [
          el("div", { class: "advanced-panel-card-title", text: panelTitle(key) }),
          el("div", { class: "advanced-panel-card-subtitle", text: panelSubtitle(key, panel) }),
        ]),
        panelStateChip(state),
      ]),
      renderFacts(panelFacts(key, panel)),
    ]);
  }

  function renderExplorer(keys, panel) {
    const key = selectedPanelKey;
    return card({
      title: t("advanced.explorer.title"),
      subtitle: t("advanced.explorer.desc"),
      actions: [
        el("button", {
          type: "button",
          class: "btn is-sm",
          text: t("advanced.explorer.copyCurrent"),
          onClick: () => copyToClipboard(JSON.stringify(panel, null, 2)),
          disabled: !key,
        }),
      ],
      body: [
        keys.length
          ? el(
              "div",
              { class: "advanced-panel-picker" },
              keys.map((panelKey) =>
                el("button", {
                  type: "button",
                  class: `advanced-panel-pill${panelKey === key ? " is-active" : ""}`,
                  text: panelTitle(panelKey),
                  onClick: () => selectPanel(panelKey),
                }),
              ),
            )
          : null,
        key && panel
          ? el("div", { class: "advanced-json-shell", id: "advanced-explorer" }, [
              el("div", { class: "advanced-panel-focus" }, [
                el("div", { class: "advanced-panel-focus-copy" }, [
                  el("div", { class: "advanced-panel-focus-title", text: panelTitle(key) }),
                  el("div", { class: "advanced-panel-focus-subtitle", text: panelSubtitle(key, panel) }),
                ]),
                panelStateChip(panelState(key, panel)),
              ]),
              renderFacts(panelFacts(key, panel), "is-compact"),
              jsonView(panel),
            ])
          : el("div", { class: "empty", text: t("advanced.explorer.empty") }),
      ],
    });
  }

  function renderDangerZone() {
    const memory = panels?.memory || {};
    return card({
      title: t("advanced.danger.title"),
      subtitle: t("advanced.danger.desc"),
      extraClass: "danger-zone",
      body: [
        el("div", { class: "advanced-danger-copy" }, [
          el("div", { class: "advanced-danger-title", text: t("advanced.danger.purge") }),
          el("div", {
            class: "advanced-danger-desc",
            text: t("advanced.danger.detail", {
              sessions: (memory.sessions || []).length,
              knowledge: memory.knowledge_count || 0,
            }),
          }),
        ]),
        el("div", { class: "row" }, [
          el("button", {
            type: "button",
            class: "btn is-danger",
            text: t("advanced.danger.purge"),
            onClick: purge,
          }),
        ]),
      ],
    });
  }

  async function purge() {
    const ok = await confirmDialog({
      title: t("advanced.danger.purge"),
      message: t("advanced.danger.purgeConfirm"),
      danger: true,
    });
    if (!ok) return;
    try {
      const memoryPanel = await api.getMemoryPanel();
      const sessions = memoryPanel?.sessions || [];
      const knowledgeEntries = (memoryPanel?.knowledge_entries || []).filter((entry) => !!entry?.id);
      await Promise.all([
        ...sessions.map((session) =>
          api.saveSession(session.launcher_type, session.launcher_id, {
            history: [],
            preferred_name: session.preferred_name || "",
            metadata: session.metadata || {},
          }),
        ),
        ...knowledgeEntries.map((entry) => api.deleteKnowledgeEntry(entry.id)),
      ]);
      toastOk(t("memory.detail.cleared"));
      await load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  render();
  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}

function renderFacts(facts, extraClass = "") {
  return el(
    "div",
    { class: `advanced-facts ${extraClass}`.trim() },
    facts.map((item) =>
      el("div", { class: "advanced-fact-row" }, [
        el("div", { class: "advanced-fact-label", text: item.label }),
        el("div", { class: "advanced-fact-value", text: item.value }),
      ]),
    ),
  );
}

function panelTitle(key) {
  return t(`advanced.panel.${key}`);
}

function panelSubtitle(key, panel) {
  switch (key) {
    case "character":
      return [displayValue(panel?.shared?.assistant_name), displayValue(panel?.current_character)].join(" · ");
    case "ai":
      return [formatProvider(panel?.llm), formatProvider(panel?.image_generation)].join(" · ");
    case "memory":
      return [countLabel("advanced.fact.sessions", (panel?.sessions || []).length), countLabel("advanced.fact.knowledge", panel?.knowledge_count || 0)].join(" · ");
    case "skills":
      return [countLabel("advanced.fact.skillCount", panel?.skills?.count || 0), countLabel("advanced.fact.toolCount", panel?.tools?.count || 0)].join(" · ");
    case "qq_login":
      return displayValue(panel?.resolved_api_base || panel?.webui_base_url);
    case "sidecar":
      return [displayValue(panel?.adapter_name), displayValue(panel?.mode)].join(" · ");
    case "observability":
      return [countLabel("advanced.fact.http", panel?.http?.total || 0), countLabel("advanced.fact.upstream", panel?.upstream?.total || 0)].join(" · ");
    case "abilities": {
      const enabled = [
        panel?.search_enabled ? t("advanced.fact.search") : "",
        panel?.thinking_mode ? t("advanced.fact.thinking") : "",
        panel?.proactive_mode ? t("advanced.fact.proactive") : "",
      ].filter(Boolean);
      return enabled.join(" · ") || t("common.disabled");
    }
    case "other":
      return [displayValue(panel?.service_name), displayValue(panel?.bot_account_id)].join(" · ");
    default:
      return "";
  }
}

function panelStateChip(state) {
  return chip({ label: state.label, variant: state.variant });
}

function panelState(key, panel) {
  switch (key) {
    case "character":
      return stateOf(
        panel?.current_character ? t("common.online") : t("common.offline"),
        panel?.current_character ? "info" : "outline",
      );
    case "ai": {
      const llmEnabled = !!panel?.llm?.enabled;
      const imageEnabled = !!panel?.image_generation?.enabled;
      if (llmEnabled && imageEnabled) return stateOf(t("common.online"), "ok");
      if (llmEnabled || imageEnabled) return stateOf(t("advanced.state.partial"), "info");
      return stateOf(t("common.offline"), "outline");
    }
    case "memory": {
      const footprint = (panel?.sessions || []).length + Number(panel?.knowledge_count || 0);
      return footprint
        ? stateOf(t("common.online"), "info")
        : stateOf(t("advanced.state.empty"), "outline");
    }
    case "skills": {
      const enabledCount = (panel?.skills?.items || []).filter((item) => !!item?.enabled).length;
      return enabledCount
        ? stateOf(String(enabledCount), "ok")
        : stateOf(t("advanced.state.empty"), "outline");
    }
    case "qq_login":
      if (panel?.status?.is_login) return stateOf(t("common.online"), "ok");
      if (panel?.configured) return stateOf(t("common.waiting"), "warn");
      return stateOf(t("common.offline"), "outline");
    case "sidecar":
      if (panel?.mode === "online") return stateOf(t("common.online"), "ok");
      if (panel?.mode === "configured") return stateOf(t("common.configured"), "info");
      return stateOf(t("common.offline"), "outline");
    case "observability": {
      const errors = Number(panel?.upstream?.error_total || 0);
      return errors
        ? stateOf(`${errors} ${t("advanced.fact.errors").toLowerCase()}`, "warn")
        : stateOf(t("advanced.state.healthy"), "ok");
    }
    case "abilities":
      return panel?.thinking_mode
        ? stateOf(t("advanced.fact.thinking"), "info")
        : stateOf(t("common.disabled"), "outline");
    case "other":
      return panel?.bot_account_id
        ? stateOf(t("common.configured"), "info")
        : stateOf(t("common.none"), "outline");
    default:
      return stateOf(t("common.none"), "outline");
  }
}

function panelFacts(key, panel) {
  switch (key) {
    case "character": {
      const available = panel?.available || [];
      const readyPortraits = available.filter((item) => item?.portrait?.available).length;
      return [
        fact("advanced.fact.currentCharacter", panel?.current_character),
        fact("advanced.fact.assistant", panel?.shared?.assistant_name),
        fact("advanced.fact.portraits", `${readyPortraits}/${available.length}`),
      ];
    }
    case "ai":
      return [
        fact("advanced.fact.llm", formatProvider(panel?.llm)),
        fact("advanced.fact.image", formatProvider(panel?.image_generation)),
        fact("advanced.fact.embedding", formatProvider(panel?.embedding)),
      ];
    case "memory":
      return [
        fact("advanced.fact.sessions", (panel?.sessions || []).length),
        fact("advanced.fact.knowledge", panel?.knowledge_count || 0),
        fact("advanced.fact.members", panel?.member_count || 0),
      ];
    case "skills": {
      const sources = panel?.marketplace?.sources || [];
      const enabledSources = sources.filter((item) => !!item?.enabled).length;
      return [
        fact("advanced.fact.skillCount", panel?.skills?.count || 0),
        fact("advanced.fact.toolCount", panel?.tools?.count || 0),
        fact("advanced.fact.marketplaces", `${enabledSources}/${sources.length}`),
      ];
    }
    case "qq_login":
      return [
        fact("advanced.fact.bridge", panel?.configured ? t("common.configured") : t("common.disabled")),
        fact(
          "advanced.fact.login",
          panel?.status?.is_login
            ? t("common.online")
            : panel?.configured
              ? t("common.waiting")
              : t("common.offline"),
        ),
        fact("advanced.fact.endpoint", panel?.resolved_api_base || panel?.webui_base_url),
      ];
    case "sidecar":
      return [
        fact("advanced.fact.mode", panel?.mode),
        fact("advanced.fact.adapter", panel?.adapter_name),
        fact("advanced.fact.endpoint", panel?.outbound_base_url),
      ];
    case "observability":
      return [
        fact("advanced.fact.http", panel?.http?.total || 0),
        fact("advanced.fact.upstream", panel?.upstream?.total || 0),
        fact("advanced.fact.errors", panel?.upstream?.error_total || 0),
      ];
    case "abilities":
      return [
        fact("advanced.fact.search", boolLabel(panel?.search_enabled)),
        fact("advanced.fact.thinking", boolLabel(panel?.thinking_mode)),
        fact("advanced.fact.proactive", boolLabel(panel?.proactive_mode)),
      ];
    case "other":
      return [
        fact("advanced.fact.service", panel?.service_name),
        fact("advanced.fact.assistant", panel?.assistant_name),
        fact("advanced.fact.bot", panel?.bot_account_id),
      ];
    default:
      return [];
  }
}

function stateOf(label, variant) {
  const toneMap = {
    ok: "ok",
    warn: "warn",
    danger: "warn",
    info: "info",
    accent: "info",
    outline: "neutral",
  };
  return {
    label,
    variant,
    tone: toneMap[variant] || "neutral",
  };
}

function fact(labelKey, value) {
  return { label: t(labelKey), value: displayValue(value) };
}

function boolLabel(value) {
  return value ? t("common.enabled") : t("common.disabled");
}

function formatProvider(provider) {
  if (!provider) return t("common.none");
  if (provider.enabled === false) return t("common.disabled");
  return displayValue(provider.model || provider.backend);
}

function countLabel(labelKey, count) {
  return `${Number(count || 0)} ${t(labelKey).toLowerCase()}`;
}

function displayValue(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string") {
    const text = value.trim();
    return text || "-";
  }
  return String(value);
}
