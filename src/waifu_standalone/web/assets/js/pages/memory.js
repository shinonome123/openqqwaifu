import { api } from "../api.js";
import { el, card, empty, chip, textInput, textarea, fieldRow, select, numberInput, switchControl } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog } from "../ui.js";

const SCOPE_OPTIONS = [
  { value: "group", labelKey: "memory.knowledge.scope.group" },
  { value: "person", labelKey: "memory.knowledge.scope.person" },
  { value: "member", labelKey: "memory.knowledge.scope.member" },
  { value: "global", labelKey: "memory.knowledge.scope.global" },
];

const MEMORY_TYPE_OPTIONS = [
  { value: "fact", labelKey: "memory.knowledge.type.fact" },
  { value: "preference", labelKey: "memory.knowledge.type.preference" },
  { value: "relationship", labelKey: "memory.knowledge.type.relationship" },
  { value: "event", labelKey: "memory.knowledge.type.event" },
  { value: "summary", labelKey: "memory.knowledge.type.summary" },
];

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let sessions = [];
  let selected = null;
  let detail = null;
  let knowledgeEntries = [];
  let selectedKnowledgeId = 0;
  let knowledgeDraft = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      const payload = await api.getMemoryPanel();
      if (stopped) return;
      sessions = payload?.sessions || [];
      knowledgeEntries = payload?.knowledge_entries || [];
      if (!selected && sessions.length) {
        await selectSession(sessions[0]);
      } else if (selected) {
        const next = sessions.find(
          (item) =>
            item.launcher_id === selected.launcher_id && item.launcher_type === selected.launcher_type,
        );
        if (next) {
          selected = next;
          await selectSession(next, { keepSelection: true });
        } else {
          selected = null;
          detail = null;
        }
      } else {
        render();
      }
      syncKnowledgeDraft();
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  function syncKnowledgeDraft() {
    if (!selectedKnowledgeId && knowledgeEntries.length) {
      selectedKnowledgeId = Number(knowledgeEntries[0].id || 0);
    }
    const selectedEntry = knowledgeEntries.find((item) => Number(item.id || 0) === Number(selectedKnowledgeId));
    knowledgeDraft = selectedEntry
      ? {
          ...selectedEntry,
          tags: [...(selectedEntry.tags || [])],
          source_message_ids: [...(selectedEntry.source_message_ids || [])],
        }
      : buildEmptyKnowledgeDraft();
  }

  async function selectSession(session, { keepSelection = false } = {}) {
    selected = session;
    if (!keepSelection) {
      detail = null;
      render();
    }
    if (!session) return;
    try {
      detail = await api.sessionDetail(session.launcher_type, session.launcher_id);
      if (!stopped) render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  async function onClear() {
    if (!selected) return;
    const ok = await confirmDialog({
      title: t("memory.detail.clear"),
      message: t("advanced.danger.purgeConfirm"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.saveSession(selected.launcher_type, selected.launcher_id, {
        history: [],
        metadata: detail?.metadata || {},
      });
      toastOk(t("memory.detail.cleared"));
      detail = { ...detail, history: [] };
      render();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  async function onRename(newName) {
    if (!selected) return;
    if (selected.launcher_type !== "person") {
      toastError(t("memory.detail.renameUnsupported"));
      return;
    }
    try {
      const payload = {
        group_id: "",
        user_id: selected.launcher_id,
        preferred_name: String(newName || "").trim(),
        onboarding_status: String(newName || "").trim() ? "ready" : "new",
      };
      const updated = await api.saveDirectoryMember(payload);
      const member = updated?.member || payload;
      detail = {
        ...detail,
        preferred_name: member.preferred_name || "",
      };
      toastOk(t("memory.detail.renamed"));
      render();
      await load();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  function renderRenameSection() {
    if (!selected) return null;
    if (selected.launcher_type !== "person") {
      return el("div", { class: "muted", text: t("memory.detail.renameHintGroup") });
    }
    const renameInput = textInput({
      value: detail?.preferred_name || "",
      placeholder: t("memory.table.name"),
    });
    return el("div", { class: "row-tight" }, [
      renameInput,
      el("button", {
        type: "button",
        class: "btn is-sm",
        text: t("memory.detail.rename"),
        onClick: () => onRename(renameInput.value),
      }),
    ]);
  }

  async function onSaveKnowledge() {
    if (!knowledgeDraft?.summary?.trim()) {
      toastError(t("memory.knowledge.summaryRequired"));
      return;
    }
    try {
      const result = await api.saveKnowledgeEntry(knowledgeDraft);
      selectedKnowledgeId = Number(result?.entry?.id || knowledgeDraft.id || 0);
      toastOk(t("memory.knowledge.saved"));
      await load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  async function onDeleteKnowledge() {
    if (!knowledgeDraft?.id) return;
    const ok = await confirmDialog({
      title: t("memory.knowledge.delete"),
      message: t("memory.knowledge.deleteConfirm"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteKnowledgeEntry(knowledgeDraft.id);
      selectedKnowledgeId = 0;
      knowledgeDraft = buildEmptyKnowledgeDraft();
      toastOk(t("memory.knowledge.deleted"));
      await load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  function onNewKnowledge() {
    selectedKnowledgeId = 0;
    knowledgeDraft = buildEmptyKnowledgeDraft();
    render();
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(header());
    container.appendChild(el("div", { class: "overview-split" }, [renderSessionList(), renderSessionDetail()]));
    container.appendChild(
      el("div", { class: "overview-split", style: { marginTop: "16px" } }, [
        renderKnowledgeList(),
        renderKnowledgeEditor(),
      ]),
    );
  }

  function header() {
    return el("div", { class: "page-header" }, [
      el("div", { class: "page-header-text" }, [
        el("div", { class: "page-title", text: t("page.memory.title") }),
        el("div", { class: "page-desc", text: t("page.memory.desc") }),
      ]),
      el("div", { class: "page-actions" }, [
        chip({ label: `${t("memory.stats.sessions")}: ${sessions.length}`, variant: "outline" }),
        chip({ label: `${t("memory.stats.knowledge")}: ${knowledgeEntries.length}`, variant: "outline" }),
        el("button", {
          type: "button",
          class: "btn",
          text: t("common.refresh"),
          onClick: load,
        }),
      ]),
    ]);
  }

  function renderSessionList() {
    if (!sessions.length) {
      return card({
        title: t("nav.memory"),
        body: [empty({ title: t("common.empty") })],
      });
    }
    const tbody = el("tbody");
    sessions.forEach((session) => {
      const isActive =
        selected &&
        selected.launcher_id === session.launcher_id &&
        selected.launcher_type === session.launcher_type;
      tbody.appendChild(
        el(
          "tr",
          {
            style: {
              cursor: "pointer",
              background: isActive ? "var(--accent-subtle)" : "transparent",
            },
            onClick: () => void selectSession(session),
          },
          [
            el("td", {}, [
              el("div", { text: session.preferred_name || session.launcher_id }),
              el("div", { class: "session-id", text: session.launcher_id }),
            ]),
            el("td", {}, [
              chip({
                label: session.launcher_type,
                variant: session.launcher_type === "group" ? "accent" : "info",
              }),
            ]),
            el("td", { class: "session-meta", text: String(session.message_count ?? (session.history?.length ?? 0)) }),
          ],
        ),
      );
    });
    return card({
      title: `${t("memory.sessions.title")} (${sessions.length})`,
      body: [
        el("div", { class: "table-wrap" }, [
          el("table", { class: "table session-table" }, [
            el("thead", {}, [
              el("tr", {}, [
                el("th", { text: t("memory.table.launcher") }),
                el("th", { text: t("memory.table.type") }),
                el("th", { text: t("memory.table.msgs") }),
              ]),
            ]),
            tbody,
          ]),
        ]),
      ],
      extraClass: "is-flush",
    });
  }

  function renderSessionDetail() {
    if (!selected) {
      return card({
        title: t("memory.detail.title"),
        body: [empty({ title: t("memory.detail.none") })],
      });
    }

    const history = detail?.history || [];
    const transcript = el("div", { class: "transcript" });
    if (!history.length) {
      transcript.appendChild(el("div", { class: "transcript-line muted", text: t("common.empty") }));
    } else {
      history.forEach((line) => {
        const trimmed = String(line || "").trim();
        let cls = "transcript-line";
        if (/^\s*(?:\u7528\u6237|user)\s*:/i.test(trimmed)) cls += " is-user";
        if (/^\s*(?:\u52a9\u624b|assistant)\s*:/i.test(trimmed)) cls += " is-assistant";
        transcript.appendChild(el("div", { class: cls, text: trimmed }));
      });
    }

    return card({
      title: `${selected.launcher_type}:${selected.launcher_id}`,
      subtitle: detail?.preferred_name || "",
      actions: [
        el("button", {
          type: "button",
          class: "btn is-sm is-danger",
          text: t("memory.detail.clear"),
          onClick: onClear,
        }),
      ],
      body: [
        renderRenameSection(),
        transcript,
        renderGraphPanel(detail?.memory_graph),
      ],
    });
  }

  function renderGraphPanel(graph) {
    if (!graph?.enabled) {
      return el("div", { class: "muted", text: t("memory.graph.empty") });
    }
    const highlights = el("div", { class: "row-tight", style: { flexWrap: "wrap" } });
    (graph.highlights || []).forEach((item) => {
      highlights.appendChild(chip({ label: String(item || ""), variant: "outline" }));
    });
    const nodeList = el("div", { class: "stack-list", style: { marginTop: "12px" } });
    (graph.nodes || []).forEach((node) => {
      nodeList.appendChild(
        el("div", { class: "card is-compact" }, [
          el("div", { class: "card-body" }, [
            el("div", { class: "row-tight" }, [
              chip({ label: node.kind || "node", variant: "accent" }),
              el("strong", { text: node.label || "-" }),
            ]),
          ]),
        ]),
      );
    });
    return card({
      title: t("memory.graph.title"),
      subtitle: `${t("memory.graph.nodes")}: ${(graph.nodes || []).length} / ${t("memory.graph.edges")}: ${(graph.edges || []).length}`,
      body: [highlights, nodeList],
      extraClass: "is-compact",
    });
  }

  function renderKnowledgeList() {
    if (!knowledgeEntries.length) {
      return card({
        title: t("memory.knowledge.title"),
        actions: [
          el("button", {
            type: "button",
            class: "btn",
            text: t("memory.knowledge.new"),
            onClick: onNewKnowledge,
          }),
        ],
        body: [empty({ title: t("memory.knowledge.empty") })],
      });
    }
    const rows = el("div", { class: "stack" });
    knowledgeEntries.forEach((entry) => {
      const active = Number(entry.id || 0) === Number(selectedKnowledgeId);
      rows.appendChild(
        el(
          "button",
          {
            type: "button",
            class: `rule-card${active ? " is-active" : ""}`,
            onClick: () => {
              selectedKnowledgeId = Number(entry.id || 0);
              syncKnowledgeDraft();
              render();
            },
          },
          [
            el("div", { class: "rule-card-head" }, [
              el("div", {}, [
                el("div", { class: "rule-card-title", text: entry.summary }),
                el("div", { class: "rule-card-desc", text: `${entry.scope_type}:${entry.scope_id || "-"}` }),
              ]),
              chip({ label: entry.memory_type, variant: entry.archived ? "outline" : "accent" }),
            ]),
            el("div", { class: "row" }, (entry.tags || []).slice(0, 4).map((tag) => chip({ label: tag, variant: "outline" }))),
          ],
        ),
      );
    });
    return card({
      title: `${t("memory.knowledge.title")} (${knowledgeEntries.length})`,
      actions: [
        el("button", {
          type: "button",
          class: "btn",
          text: t("memory.knowledge.new"),
          onClick: onNewKnowledge,
        }),
      ],
      body: [rows],
    });
  }

  function renderKnowledgeEditor() {
    if (!knowledgeDraft) {
      return card({
        title: t("memory.knowledge.editor"),
        body: [empty({ title: t("memory.knowledge.empty") })],
      });
    }
    return card({
      title: t("memory.knowledge.editor"),
      subtitle: knowledgeDraft.id ? `#${knowledgeDraft.id}` : t("memory.knowledge.new"),
      actions: [
        ...(knowledgeDraft.id
          ? [
              el("button", {
                type: "button",
                class: "btn is-danger",
                text: t("memory.knowledge.delete"),
                onClick: onDeleteKnowledge,
              }),
            ]
          : []),
        el("button", {
          type: "button",
          class: "btn is-primary",
          text: t("common.save"),
          onClick: onSaveKnowledge,
        }),
      ],
      body: [
        fieldRow({
          label: t("memory.knowledge.summary"),
          control: textarea({
            rows: 4,
            value: knowledgeDraft.summary || "",
            onChange: (value) => {
              knowledgeDraft.summary = value.trim();
            },
          }),
        }),
        fieldRow({
          label: t("memory.knowledge.scope"),
          control: el("div", { class: "row" }, [
            select({
              value: knowledgeDraft.scope_type || "group",
              options: SCOPE_OPTIONS.map((item) => ({ value: item.value, label: t(item.labelKey) })),
              onChange: (value) => {
                knowledgeDraft.scope_type = value;
              },
            }),
            textInput({
              value: knowledgeDraft.scope_id || "",
              placeholder: t("memory.knowledge.scopeId"),
              onChange: (value) => {
                knowledgeDraft.scope_id = value.trim();
              },
            }),
          ]),
        }),
        fieldRow({
          label: t("memory.knowledge.memoryType"),
          control: select({
            value: knowledgeDraft.memory_type || "fact",
            options: MEMORY_TYPE_OPTIONS.map((item) => ({ value: item.value, label: t(item.labelKey) })),
            onChange: (value) => {
              knowledgeDraft.memory_type = value;
            },
          }),
        }),
        fieldRow({
          label: t("memory.knowledge.tags"),
          control: textInput({
            value: (knowledgeDraft.tags || []).join(", "),
            onChange: (value) => {
              knowledgeDraft.tags = value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean);
            },
          }),
        }),
        fieldRow({
          label: t("memory.knowledge.sourceIds"),
          control: textInput({
            value: (knowledgeDraft.source_message_ids || []).join(", "),
            onChange: (value) => {
              knowledgeDraft.source_message_ids = value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean);
            },
          }),
        }),
        fieldRow({
          label: t("memory.knowledge.confidence"),
          control: numberInput({
            value: Number(knowledgeDraft.confidence ?? 0.5),
            min: 0,
            max: 1,
            step: 0.05,
            onChange: (value) => {
              knowledgeDraft.confidence = value;
            },
          }),
        }),
        fieldRow({
          label: t("memory.knowledge.archived"),
          control: switchControl({
            checked: !!knowledgeDraft.archived,
            onChange: (value) => {
              knowledgeDraft.archived = value;
            },
          }),
        }),
      ],
    });
  }

  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}

function buildEmptyKnowledgeDraft() {
  return {
    id: 0,
    scope_type: "group",
    scope_id: "",
    memory_type: "fact",
    summary: "",
    tags: [],
    source_message_ids: [],
    confidence: 0.5,
    archived: false,
  };
}
