// Memory page: session list + transcript viewer.

import { api } from "../api.js";
import { el, card, empty, chip, textInput } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let sessions = [];
  let selected = null;
  let detail = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      const payload = await api.getMemoryPanel();
      if (stopped) return;
      sessions = payload?.sessions || [];
      if (!selected && sessions.length) {
        await select(sessions[0]);
      } else {
        render();
      }
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  async function select(session) {
    selected = session;
    detail = null;
    render();
    if (!session) return;
    try {
      detail = await api.sessionDetail(session.launcher_type, session.launcher_id);
      if (stopped) return;
      render();
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
        preferred_name: detail?.preferred_name || "",
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
    try {
      const updated = await api.saveSession(selected.launcher_type, selected.launcher_id, {
        preferred_name: newName,
        history: detail?.history || [],
        metadata: detail?.metadata || {},
      });
      detail = updated?.session || { ...detail, preferred_name: newName };
      toastOk(t("memory.detail.renamed"));
      render();
      load();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(header());
    container.appendChild(
      el("div", { class: "overview-split" }, [renderList(), renderDetail()]),
    );
  }

  function header() {
    return el("div", { class: "page-header" }, [
      el("div", { class: "page-header-text" }, [
        el("div", { class: "page-title", text: t("page.memory.title") }),
        el("div", { class: "page-desc", text: t("page.memory.desc") }),
      ]),
      el("div", { class: "page-actions" }, [
        el("button", {
          type: "button",
          class: "btn",
          text: t("common.refresh"),
          onClick: load,
        }),
      ]),
    ]);
  }

  function renderList() {
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
            onClick: () => select(session),
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
            el("td", {
              class: "session-meta",
              text: String(session.message_count ?? (session.history?.length ?? 0)),
            }),
          ],
        ),
      );
    });
    const wrap = el("div", { class: "table-wrap" }, [
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
    ]);
    return card({
      title: `${t("nav.memory")} (${sessions.length})`,
      body: [wrap],
      extraClass: "is-flush",
    });
  }

  function renderDetail() {
    if (!selected) {
      return card({
        title: t("memory.detail.title"),
        body: [empty({ title: t("memory.detail.none") })],
      });
    }

    const history = detail?.history || [];
    const transcript = el("div", { class: "transcript" });
    if (!history.length) {
      transcript.appendChild(
        el("div", { class: "transcript-line muted", text: t("common.empty") }),
      );
    } else {
      history.forEach((line) => {
        const trimmed = String(line || "").trim();
        let cls = "transcript-line";
        if (/^\s*(?:\u7528\u6237|user)\s*:/i.test(trimmed)) cls += " is-user";
        if (/^\s*(?:\u52a9\u624b|assistant|\u7409\u7483)\s*:/i.test(trimmed)) cls += " is-assistant";
        transcript.appendChild(el("div", { class: cls, text: trimmed }));
      });
    }

    const renameInput = textInput({
      value: detail?.preferred_name || "",
      placeholder: t("memory.table.name"),
    });

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
        el("div", { class: "row-tight" }, [
          renameInput,
          el("button", {
            type: "button",
            class: "btn is-sm",
            text: t("memory.detail.rename"),
            onClick: () => onRename(renameInput.value),
          }),
        ]),
        transcript,
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
