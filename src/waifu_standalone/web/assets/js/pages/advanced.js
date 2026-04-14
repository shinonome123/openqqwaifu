// Advanced page: raw JSON snapshot of all panels + danger-zone actions.

import { api } from "../api.js";
import { el, card, textarea, jsonView } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog, copyToClipboard } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let panels = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      panels = await api.console();
      if (stopped) return;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  function render() {
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

    if (!panels) return;

    container.appendChild(
      card({
        title: t("advanced.snapshot.title"),
        subtitle: t("advanced.snapshot.desc"),
        body: [jsonView(panels)],
      }),
    );

    container.appendChild(renderDangerZone());
  }

  function renderDangerZone() {
    return card({
      title: t("advanced.danger.title"),
      extraClass: "danger-zone",
      body: [
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
      await Promise.all(
        sessions.map((s) =>
          api.saveSession(s.launcher_type, s.launcher_id, {
            history: [],
            preferred_name: s.preferred_name || "",
            metadata: s.metadata || {},
          }),
        ),
      );
      toastOk(t("memory.detail.cleared"));
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}
