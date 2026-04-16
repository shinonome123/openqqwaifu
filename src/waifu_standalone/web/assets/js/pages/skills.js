// Skills page: toggle skills, marketplace search and import.

import { api } from "../api.js";
import { el, card, chip, empty, switchControl, textInput } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let panel = null;
  let results = [];
  let query = "";

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      panel = await api.getSkillsPanel();
      if (stopped) return;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  async function toggle(skillId, enabled) {
    try {
      await api.toggleSkill(skillId, enabled);
      toastOk(t("actions.savedOk"));
      load();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  async function removeSkill(skillId) {
    const ok = await confirmDialog({
      title: t("common.delete"),
      message: `${t("common.delete")}: ${skillId}`,
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteSkill(skillId);
      toastOk(t("skills.deleted"));
      load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  async function search() {
    try {
      const payload = await api.searchMarketplace(query, "", 12);
      results = payload?.items || [];
      render();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  async function importFromResult(sourceId, githubUrl) {
    try {
      await api.importMarketplace(sourceId, githubUrl);
      toastOk(t("actions.savedOk"));
      load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.skills.title") }),
          el("div", { class: "page-desc", text: t("page.skills.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          el("button", {
            type: "button",
            class: "btn",
            text: t("common.refresh"),
            onClick: load,
          }),
          el("button", {
            type: "button",
            class: "btn",
            text: t("skills.reload"),
            onClick: async () => {
              try {
                await api.reloadSkills();
                toastOk(t("skills.reloaded"));
                load();
              } catch (err) {
                toastError(err?.message || String(err));
              }
            },
          }),
        ]),
      ]),
    );

    if (!panel) {
      container.appendChild(empty({ title: t("common.loading") }));
      return;
    }

    container.appendChild(renderSkillsList());
    container.appendChild(renderToolsList());
    container.appendChild(renderMarketplace());
  }

  function renderSkillsList() {
    const skills = panel?.skills?.items || [];
    if (!Array.isArray(skills) || !skills.length) {
      return card({
        title: t("nav.skills"),
        body: [empty({ title: t("common.empty") })],
      });
    }
    return card({
      title: `${t("nav.skills")} (${skills.length})`,
      body: skills.map((skill) =>
        el("div", { class: "rule-card" }, [
          el("div", { class: "rule-card-head" }, [
            el("div", {}, [
              el("div", { class: "rule-card-title", text: skill.name || skill.id }),
              el("div", { class: "rule-card-desc", text: skill.description || "" }),
            ]),
            el("div", { class: "row-tight" }, [
              switchControl({
                checked: !!skill.enabled,
                onChange: (v) => toggle(skill.id, v),
              }),
              el("button", {
                type: "button",
                class: "btn is-sm is-danger",
                text: t("common.delete"),
                onClick: () => removeSkill(skill.id),
              }),
            ]),
          ]),
            el("div", { class: "row" }, [
              chip({ label: skill.id, variant: "outline" }),
              skill.source_kind ? chip({ label: skill.source_kind, variant: "info" }) : null,
              safeSourceLabel(skill.source)
                ? chip({ label: safeSourceLabel(skill.source), variant: "outline" })
                : null,
              skill.command_dispatch === "tool"
                ? chip({ label: `tool:${skill.command_tool || "unknown"}`, variant: "ok" })
                : chip({ label: "prompt", variant: "outline" }),
          ]),
        ]),
      ),
    });
  }

  function renderToolsList() {
    const tools = panel?.tools?.items || [];
    if (!Array.isArray(tools) || !tools.length) {
      return card({
        title: t("skills.tools.title"),
        body: [empty({ title: t("common.empty") })],
      });
    }
    return card({
      title: `${t("skills.tools.title")} (${tools.length})`,
      subtitle: t("skills.tools.desc"),
      body: tools.map((tool) =>
        el("div", { class: "rule-card" }, [
          el("div", { class: "rule-card-head" }, [
            el("div", {}, [
              el("div", { class: "rule-card-title", text: tool.name || tool.id }),
              el("div", { class: "rule-card-desc", text: tool.description || "" }),
            ]),
            chip({ label: tool.id || "tool", variant: "info" }),
          ]),
        ]),
      ),
    });
  }

  function renderMarketplace() {
    const marketplace = panel?.marketplace || {};
    const sources = marketplace.sources || [];
    const searchInput = textInput({
      value: query,
      placeholder: marketplace.default_query || "codex",
      onChange: (v) => {
        query = v;
      },
      onInput: (v) => {
        query = v;
      },
    });
    const searchBtn = el("button", {
      type: "button",
      class: "btn is-primary is-sm",
      text: t("common.search"),
      onClick: search,
    });

    const resultsList = el("div", { class: "stack" });
    if (!results.length) {
      resultsList.appendChild(empty({ title: t("skills.marketplace.empty") }));
    } else {
      results.forEach((item) => {
        resultsList.appendChild(
          el("div", { class: "rule-card" }, [
            el("div", { class: "rule-card-head" }, [
              el("div", {}, [
                el("div", { class: "rule-card-title", text: item.name || item.title || item.skill_id || "?" }),
                el("div", { class: "rule-card-desc", text: item.description || "" }),
              ]),
              el("button", {
                type: "button",
                class: "btn is-sm is-primary",
                text: t("common.add"),
                onClick: () =>
                  importFromResult(
                    item.source_id || sources[0]?.id || "",
                    item.github_url || item.skill_url || item.raw_url || "",
                  ),
              }),
            ]),
            el("div", { class: "row" }, [
              item.source_id ? chip({ label: item.source_id, variant: "info" }) : null,
              item.author ? chip({ label: item.author, variant: "outline" }) : null,
            ]),
          ]),
        );
      });
    }

    return card({
      title: t("skills.marketplace.title"),
      body: [
        el("div", { class: "row-tight" }, [searchInput, searchBtn]),
        el("div", { class: "row" }, sources.map((s) =>
          chip({
            label: `${s.name || s.source_id} ${s.enabled ? "on" : "off"}`,
            variant: s.enabled ? "ok" : "outline",
          }),
        )),
        resultsList,
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

function safeSourceLabel(source) {
  const value = String(source || "").trim();
  if (!value) return "";
  try {
    const parsed = new URL(value);
    return parsed.hostname || parsed.origin || value;
  } catch (err) {
    const normalized = value.replace(/\\/g, "/");
    return normalized.split("/").pop() || "";
  }
}
