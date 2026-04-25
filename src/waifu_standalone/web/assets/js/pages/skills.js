// Skills page: normalized capabilities, grouped skills, tool safety and marketplace import.

import { api } from "../api.js";
import { card, chip, el, empty, statCard, switchControl, textInput } from "../components.js";
import { getLang, onLangChange, t } from "../i18n.js";
import { confirmDialog, toastError, toastOk } from "../ui.js";

const COPY = {
  en: {
    capabilitiesTitle: "Capability board",
    capabilitiesDesc: "Normalized built-in abilities and prompt-only overlays currently wired into the runtime.",
    capabilityDispatchCount: "{count} dispatch skill(s)",
    capabilityAliasCount: "{count} alias(es)",
    skillSourcesTitle: "Skill sources",
    skillSourcesDesc: "Loaded skills grouped by where they come from.",
    skillModesTitle: "Dispatch modes",
    skillModesDesc: "Manifest handlers, prompt policies, validation state and recent executions.",
    builtin: "Built-in",
    workspace: "Workspace",
    plugin: "Plugin",
    prompt: "Prompt-only",
    toolDispatch: "Tool dispatch",
    invalid: "Invalid",
    restricted: "Restricted",
    aliases: "Aliases",
    commandArgMode: "Argument mode",
    modelCallable: "Model callable",
    manualOnly: "Manual only",
    typeTool: "tool",
    typePrompt: "prompt",
    safetyTitle: "Safety policy",
    safetyDesc: "Write/exec tools are restricted by configured roots and command allowlists.",
    allowedRoots: "Allowed roots",
    writeRoots: "Write roots",
    execRoots: "Exec roots",
    execAllowlist: "Exec allowlist",
    enabled: "enabled",
    disabled: "disabled",
    noAliases: "No aliases",
    keywords: "Keywords",
    validation: "Validation",
    telemetry: "Telemetry",
    neverSucceeded: "never succeeded",
    bindingErrorsTitle: "Tool binding errors",
    bindingErrorsDesc: "Runtime kept the previous valid binding map. Fix data/tool_bindings.yaml and reload skills.",
    clawTitle: "Claw runtime",
    clawDesc: "Managed OpenClaw-compatible runtime status, plugin routing owner, and capability diagnostics.",
    clawPlugins: "Installed plugins",
    clawRouting: "Routing",
    clawState: "State",
    clawHealthy: "Healthy",
    clawRefresh: "Refresh runtime",
    clawCheck: "Check plugins",
    clawUpdate: "Update",
    clawDetectOnly: "Detect only",
    clawUnsupported: "Unsupported",
    clawWired: "Wired",
    clawPluginCount: "{count} plugin(s)",
    clawCapabilityCount: "{count} capability item(s)",
    clawToolCount: "{count} tool(s)",
    clawAcp: "ACP",
    clawCodexHarness: "Codex Harness",
  },
  zh: {
    capabilitiesTitle: "能力面板",
    capabilitiesDesc: "把内置能力和 Prompt 型附加行为归一化后展示，便于确认哪些真的已经接上 tool。",
    capabilityDispatchCount: "{count} 个分发技能",
    capabilityAliasCount: "{count} 个语义别名",
    skillSourcesTitle: "技能来源",
    skillSourcesDesc: "按来源查看当前已加载技能。",
    skillModesTitle: "技能模式",
    skillModesDesc: "按 Manifest handler、Prompt Policy、校验状态和最近调用查看技能。",
    builtin: "内置",
    workspace: "工作区",
    plugin: "插件",
    prompt: "Prompt 型",
    toolDispatch: "Tool 分发",
    invalid: "校验失败",
    restricted: "受限",
    aliases: "别名",
    commandArgMode: "参数模式",
    modelCallable: "模型可自主调用",
    manualOnly: "仅显式调用",
    typeTool: "tool",
    typePrompt: "prompt",
    safetyTitle: "安全策略",
    safetyDesc: "写文件 / 执行命令都会受到目录白名单和命令白名单限制。",
    allowedRoots: "通用允许目录",
    writeRoots: "写入允许目录",
    execRoots: "执行允许目录",
    execAllowlist: "执行命令白名单",
    enabled: "启用",
    disabled: "禁用",
    noAliases: "无别名",
    keywords: "关键词",
    validation: "校验",
    telemetry: "调用统计",
    neverSucceeded: "从未成功",
    bindingErrorsTitle: "工具绑定错误",
    bindingErrorsDesc: "运行时会继续沿用上一份有效绑定。修复 data/tool_bindings.yaml 后重新加载技能。",
    clawTitle: "Claw 运行时",
    clawDesc: "受管的 OpenClaw 兼容运行时状态、插件路由归属和能力诊断。",
    clawPlugins: "已安装插件",
    clawRouting: "路由",
    clawState: "状态",
    clawHealthy: "健康",
    clawRefresh: "刷新运行时",
    clawCheck: "检查插件",
    clawUpdate: "更新",
    clawDetectOnly: "仅检测",
    clawUnsupported: "未接线",
    clawWired: "已接线",
    clawPluginCount: "{count} 个插件",
    clawCapabilityCount: "{count} 条能力项",
    clawToolCount: "{count} 个工具",
    clawAcp: "ACP",
    clawCodexHarness: "Codex Harness",
  },
};

function copy(key, params = {}) {
  const dict = COPY[getLang()] || COPY.en;
  const fallback = COPY.en[key] || key;
  const template = dict[key] || fallback;
  return String(template).replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
}

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
      await load();
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
      await load();
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
      await load();
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
                await load();
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

    container.appendChild(renderSummary());
    const bindingErrors = renderToolBindingErrors();
    if (bindingErrors) container.appendChild(bindingErrors);
    container.appendChild(renderClawRuntime());
    container.appendChild(renderCapabilities());
    container.appendChild(
      el("div", { class: "skills-secondary-grid" }, [
        renderSkillSources(),
        renderSkillModes(),
        renderToolsList(),
        renderMarketplace(),
      ]),
    );
  }

  function renderSummary() {
    const skills = panel?.skills?.items || [];
    const capabilities = panel?.capabilities || [];
    const tools = panel?.tools?.items || [];
    const restrictedTools = panel?.tool_groups?.restricted || [];
    const enabledCount = skills.filter((item) => !!item?.enabled).length;
    return el("div", { class: "kpi-row skills-summary-row" }, [
      statCard({
        label: t("skills.summary.enabled"),
        value: String(enabledCount),
        meta: t("skills.summary.enabledMeta", { count: skills.length }),
      }),
      statCard({
        label: copy("capabilitiesTitle"),
        value: String(capabilities.length),
        meta: `${capabilities.filter((item) => item?.source_kind === "builtin").length} builtin`,
      }),
      statCard({
        label: t("skills.summary.tools"),
        value: String(tools.length),
        meta: t("skills.summary.toolsMeta", { count: tools.length }),
      }),
      statCard({
        label: copy("restricted"),
        value: String(restrictedTools.length),
        meta: panel?.safety?.enabled ? copy("enabled") : copy("disabled"),
      }),
    ]);
  }

  function renderToolBindingErrors() {
    const errors = Array.isArray(panel?.skills?.tool_binding_errors) ? panel.skills.tool_binding_errors : [];
    if (!errors.length) return null;
    return card({
      title: copy("bindingErrorsTitle"),
      subtitle: copy("bindingErrorsDesc"),
      body: [
        el(
          "div",
          { class: "row" },
          errors.map((item) => chip({ label: `${item.code || "error"}: ${item.message || ""}`, variant: "danger" })),
        ),
      ],
    });
  }

  function renderCapabilities() {
    const capabilities = Array.isArray(panel?.capabilities) ? panel.capabilities : [];
    if (!capabilities.length) {
      return card({
        title: copy("capabilitiesTitle"),
        subtitle: copy("capabilitiesDesc"),
        body: [empty({ title: t("common.empty") })],
      });
    }
    return card({
      title: `${copy("capabilitiesTitle")} (${capabilities.length})`,
      subtitle: copy("capabilitiesDesc"),
      body: [
        el(
          "div",
          { class: "skills-capability-grid" },
          capabilities.map((capability) => renderCapabilityCard(capability)),
        ),
      ],
    });
  }

  function renderClawRuntime() {
    const runtime = panel?.claw_runtime || {};
    const plugins = runtime?.plugins?.items || [];
    const tools = runtime?.tools?.items || [];
    return card({
      title: copy("clawTitle"),
      subtitle: copy("clawDesc"),
      body: [
        el("div", { class: "row" }, [
          chip({ label: `enabled:${runtime?.enabled ? copy("enabled") : copy("disabled")}`, variant: runtime?.enabled ? "ok" : "outline" }),
          chip({ label: `${copy("clawState")}: ${runtime?.state || "idle"}`, variant: runtime?.healthy ? "ok" : "outline" }),
          chip({ label: `${copy("clawRouting")}: ${runtime?.routing_mode || "shadow"}`, variant: "info" }),
          chip({ label: `${copy("clawHealthy")}: ${runtime?.healthy ? copy("enabled") : copy("disabled")}`, variant: runtime?.healthy ? "ok" : "danger" }),
          chip({ label: `${copy("clawAcp")}: ${runtime?.acp_enabled ? copy("enabled") : copy("disabled")}`, variant: runtime?.acp_enabled ? "ok" : "outline" }),
          chip({ label: `${copy("clawCodexHarness")}: ${runtime?.codex_harness_configured ? copy("enabled") : copy("disabled")}`, variant: runtime?.codex_harness_configured ? "ok" : "outline" }),
          chip({ label: copy("clawPluginCount", { count: plugins.length }), variant: "outline" }),
          chip({ label: copy("clawToolCount", { count: tools.length }), variant: "outline" }),
        ]),
        el("div", { class: "row-tight" }, [
          el("button", {
            type: "button",
            class: "btn is-sm",
            text: copy("clawRefresh"),
            onClick: async () => {
              try {
                panel.claw_runtime = await api.getClawRuntimePanel(true);
                render();
              } catch (err) {
                toastError(err?.message || String(err));
              }
            },
          }),
          el("button", {
            type: "button",
            class: "btn is-sm",
            text: copy("clawCheck"),
            onClick: async () => {
              try {
                const checked = await api.checkClawPlugins();
                panel.claw_runtime = { ...(panel?.claw_runtime || {}), plugins: checked };
                render();
              } catch (err) {
                toastError(err?.message || String(err));
              }
            },
          }),
        ]),
        runtime?.error ? chip({ label: runtime.error, variant: "danger" }) : null,
        tools.length
          ? el(
              "div",
              { class: "row" },
              tools.slice(0, 10).map((tool) =>
                chip({
                  label: `${tool.id}:${tool.status}`,
                  variant:
                    tool.status === "wired"
                      ? "ok"
                      : tool.status === "detect_only"
                        ? "info"
                        : "outline",
                }),
              ),
            )
          : null,
        plugins.length
          ? el(
              "div",
              { class: "skill-panel-stack" },
              plugins.map((plugin) => renderClawPluginCard(plugin)),
            )
          : empty({ title: t("common.empty") }),
      ],
    });
  }

  function renderClawPluginCard(plugin) {
    const capabilities = Array.isArray(plugin?.capabilities) ? plugin.capabilities : [];
    const counts = plugin?.capability_counts || {};
    const canUpdate = !!plugin?.source_id && !!plugin?.source_url;
    return el("div", { class: "rule-card skill-rule-card" }, [
      el("div", { class: "rule-card-head" }, [
        el("div", {}, [
          el("div", { class: "rule-card-title", text: plugin.name || plugin.id || "plugin" }),
          el("div", { class: "rule-card-desc", text: `${plugin.format || "bundle"} / ${plugin.bundle_type || "none"}` }),
        ]),
        el("div", { class: "row-tight" }, [
          canUpdate
            ? el("button", {
                type: "button",
                class: "btn is-sm",
                text: copy("clawUpdate"),
                onClick: async () => {
                  try {
                    await api.updateClawPlugin(plugin.id);
                    await load();
                  } catch (err) {
                    toastError(err?.message || String(err));
                  }
                },
              })
            : null,
          chip({ label: plugin.id || "plugin", variant: "info" }),
        ]),
      ]),
      el("div", { class: "row" }, [
        chip({ label: `owner:${plugin.owner_routing || "python"}`, variant: plugin.owner_routing === "claw" ? "ok" : "outline" }),
        chip({ label: `${copy("clawWired")}: ${counts.wired || 0}`, variant: "ok" }),
        chip({ label: `${copy("clawDetectOnly")}: ${counts.detect_only || 0}`, variant: (counts.detect_only || 0) > 0 ? "info" : "outline" }),
        chip({ label: `${copy("clawUnsupported")}: ${counts.unsupported || 0}`, variant: (counts.unsupported || 0) > 0 ? "danger" : "outline" }),
      ]),
      capabilities.length
        ? el(
            "div",
            { class: "row" },
            capabilities.slice(0, 12).map((capability) =>
              chip({
                label: `${capability.kind}:${capability.status}`,
                variant:
                  capability.status === "wired"
                    ? "ok"
                    : capability.status === "detect_only"
                      ? "info"
                      : "danger",
              }),
            ),
          )
        : null,
      Array.isArray(plugin?.diagnostics) && plugin.diagnostics.length
        ? el(
            "div",
            { class: "skill-meta-block" },
            plugin.diagnostics.slice(0, 6).map((item) =>
              chip({
                label: `${item.kind}${item.reason ? `: ${item.reason}` : ""}`,
                variant: item.status === "detect_only" ? "info" : "danger",
              }),
            ),
          )
        : null,
    ]);
  }

  function renderCapabilityCard(capability) {
    const aliases = Array.isArray(capability?.aliases) ? capability.aliases : [];
    return el("div", { class: "rule-card skill-capability-card" }, [
      el("div", { class: "rule-card-head" }, [
        el("div", {}, [
          el("div", { class: "rule-card-title", text: capability.title || capability.id || "capability" }),
          el("div", { class: "rule-card-desc", text: capability.summary || "" }),
        ]),
        chip({
          label: capability.type === "prompt" ? copy("typePrompt") : copy("typeTool"),
          variant: capability.type === "prompt" ? "outline" : "ok",
        }),
      ]),
      el("div", { class: "row" }, [
        capability.id ? chip({ label: capability.id, variant: "info" }) : null,
        capability.category ? chip({ label: capability.category, variant: "outline" }) : null,
        capability.source_kind ? chip({ label: capability.source_kind, variant: "outline" }) : null,
        capability.model_callable ? chip({ label: copy("modelCallable"), variant: "ok" }) : null,
        capability.restricted ? chip({ label: copy("restricted"), variant: "danger" }) : null,
      ]),
      el("div", { class: "skill-meta-block" }, [
        chip({
          label: copy("capabilityDispatchCount", { count: capability.dispatch_skill_count || 0 }),
          variant: "outline",
        }),
        chip({
          label: copy("capabilityAliasCount", { count: aliases.length }),
          variant: aliases.length ? "info" : "outline",
        }),
        aliases.length
          ? el(
              "div",
              { class: "row" },
              aliases.slice(0, 8).map((alias) => chip({ label: alias, variant: "outline" })),
            )
          : null,
      ]),
    ]);
  }

  function renderSkillSources() {
    const groups = panel?.skill_groups || {};
    return card({
      title: copy("skillSourcesTitle"),
      subtitle: copy("skillSourcesDesc"),
      body: [
        renderSkillGroup(copy("builtin"), groups.builtin || []),
        renderSkillGroup(copy("workspace"), groups.workspace || []),
        renderSkillGroup(copy("plugin"), groups.plugin || []),
      ],
    });
  }

  function renderSkillModes() {
    const groups = panel?.skill_groups || {};
    return card({
      title: copy("skillModesTitle"),
      subtitle: copy("skillModesDesc"),
      body: [
        renderSkillGroup(copy("prompt"), groups.prompt || []),
        renderSkillGroup(copy("toolDispatch"), groups.tool_dispatch || []),
        renderSkillGroup(copy("restricted"), groups.restricted || []),
        renderSkillGroup(copy("invalid"), groups.invalid || []),
      ],
    });
  }

  function renderSkillGroup(title, items) {
    const list = Array.isArray(items) ? items : [];
    return el("section", { class: "skill-section" }, [
      el("div", { class: "skill-section-head" }, [
        el("div", { class: "skill-section-title", text: title }),
        chip({ label: String(list.length), variant: list.length ? "info" : "outline" }),
      ]),
      list.length
        ? el(
            "div",
            { class: "skill-panel-stack" },
            list.map((skill) => renderSkillCard(skill)),
          )
        : empty({ title: t("common.empty") }),
    ]);
  }

  function renderSkillCard(skill) {
    const manifest = skill?.manifest || {};
    const trigger = manifest?.trigger || {};
    const handler = manifest?.handler || {};
    const policy = manifest?.policy || {};
    const metadata = manifest?.metadata || {};
    const aliases = Array.isArray(metadata?.aliases) ? metadata.aliases : [];
    const keywords = Array.isArray(trigger?.keywords) ? trigger.keywords : [];
    const validationErrors = Array.isArray(skill?.validation_errors) ? skill.validation_errors : [];
    const telemetry = skill?.telemetry || {};
    const lastExecution = skill?.last_execution && Object.keys(skill.last_execution).length
      ? skill.last_execution
      : telemetry?.last || {};
    return el("div", { class: "rule-card skill-rule-card" }, [
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
          skill.deletable
            ? el("button", {
                type: "button",
                class: "btn is-sm is-danger",
                text: t("common.delete"),
                onClick: () => removeSkill(skill.id),
              })
            : null,
        ]),
      ]),
      el("div", { class: "row" }, [
        chip({ label: skill.id, variant: "outline" }),
        skill.source_kind ? chip({ label: skill.source_kind, variant: "info" }) : null,
        safeSourceLabel(skill.source)
          ? chip({ label: safeSourceLabel(skill.source), variant: "outline" })
          : null,
        chip({
          label: skill.status || "unknown",
          variant: skill.status === "ready" ? "ok" : skill.status === "disabled" ? "outline" : "danger",
        }),
        handler?.type === "tool_id"
          ? chip({ label: `tool:${handler?.target || "unknown"}`, variant: "ok" })
          : chip({ label: copy("typePrompt"), variant: "outline" }),
        trigger?.llm_tool ? chip({ label: "llm tool", variant: "ok" }) : chip({ label: "prompt policy", variant: "outline" }),
        trigger?.command ? chip({ label: `/skill ${trigger.command}`, variant: "info" }) : null,
        policy?.risk_level ? chip({ label: `risk:${policy.risk_level}`, variant: policy.risk_level === "safe" ? "outline" : "danger" }) : null,
        handler?.arg_mode && handler?.type === "tool_id"
          ? chip({ label: `${copy("commandArgMode")}: ${handler.arg_mode}`, variant: "outline" })
          : null,
      ]),
      keywords.length
        ? el("div", { class: "skill-trigger-list" }, [
            el("div", { class: "skill-trigger-label", text: copy("keywords") }),
            el(
              "div",
              { class: "row" },
              keywords.map((keyword) => chip({ label: keyword, variant: "outline" })),
            ),
          ])
        : null,
      validationErrors.length
        ? el("div", { class: "skill-trigger-list" }, [
            el("div", { class: "skill-trigger-label", text: copy("validation") }),
            el(
              "div",
              { class: "row" },
              validationErrors.map((item) =>
                chip({ label: `${item.code || "error"}: ${item.message || ""}`, variant: "danger" }),
              ),
            ),
          ])
        : null,
      el("div", { class: "skill-trigger-list" }, [
        el("div", { class: "skill-trigger-label", text: copy("telemetry") }),
        el("div", { class: "row" }, [
          chip({ label: `calls:${telemetry?.calls || 0}`, variant: "outline" }),
          chip({ label: `ok:${telemetry?.success || 0}`, variant: "ok" }),
          chip({ label: `fail:${telemetry?.failure || 0}`, variant: telemetry?.failure ? "danger" : "outline" }),
          telemetry?.never_succeeded ? chip({ label: copy("neverSucceeded"), variant: "danger" }) : null,
          lastExecution?.trace_id ? chip({ label: `trace:${lastExecution.trace_id}`, variant: "info" }) : null,
          lastExecution?.error_code ? chip({ label: lastExecution.error_code, variant: "danger" }) : null,
        ]),
      ]),
      el("div", { class: "skill-trigger-list" }, [
        el("div", { class: "skill-trigger-label", text: copy("aliases") }),
        aliases.length
          ? el(
              "div",
              { class: "row" },
              aliases.map((alias) => chip({ label: alias, variant: "outline" })),
            )
          : chip({ label: copy("noAliases"), variant: "outline" }),
      ]),
    ]);
  }

  function renderToolsList() {
    const tools = panel?.tools?.items || [];
    const safety = panel?.safety || {};
    if (!Array.isArray(tools) || !tools.length) {
      return card({
        title: t("skills.tools.title"),
        body: [empty({ title: t("common.empty") })],
      });
    }
    return card({
      title: `${t("skills.tools.title")} (${tools.length})`,
      subtitle: t("skills.tools.desc"),
      body: [
        renderSafetyBlock(safety),
        ...tools.map((tool) => renderToolCard(tool)),
      ],
    });
  }

  function renderSafetyBlock(safety) {
    const allowlist = Array.isArray(safety?.exec_allowlist) ? safety.exec_allowlist : [];
    return el("div", { class: "skill-safety-block" }, [
      el("div", { class: "rule-card-title", text: copy("safetyTitle") }),
      el("div", { class: "rule-card-desc", text: copy("safetyDesc") }),
      el("div", { class: "row" }, [
        chip({ label: `${copy("allowedRoots")}: ${(safety?.resolved_allowed_roots || []).length}`, variant: "outline" }),
        chip({
          label: `${copy("writeRoots")}: ${(safety?.resolved_write_allowed_roots || []).length}`,
          variant: safety?.write_enabled ? "ok" : "danger",
        }),
        chip({
          label: `${copy("execRoots")}: ${(safety?.resolved_exec_allowed_roots || []).length}`,
          variant: safety?.exec_enabled ? "ok" : "danger",
        }),
      ]),
      el("div", { class: "skill-meta-block" }, [
        renderPathGroup(copy("allowedRoots"), safety?.resolved_allowed_roots || []),
        renderPathGroup(copy("writeRoots"), safety?.resolved_write_allowed_roots || []),
        renderPathGroup(copy("execRoots"), safety?.resolved_exec_allowed_roots || []),
        el("div", { class: "skill-trigger-list" }, [
          el("div", { class: "skill-trigger-label", text: copy("execAllowlist") }),
          allowlist.length
            ? el(
                "div",
                { class: "row" },
                allowlist.map((item) => chip({ label: item, variant: "outline" })),
              )
            : chip({ label: copy("disabled"), variant: "danger" }),
        ]),
      ]),
    ]);
  }

  function renderPathGroup(title, items) {
    return el("div", { class: "skill-trigger-list" }, [
      el("div", { class: "skill-trigger-label", text: title }),
      items.length
        ? el(
            "div",
            { class: "row" },
            items.map((item) => chip({ label: item, variant: "outline" })),
          )
        : chip({ label: t("common.none"), variant: "outline" }),
    ]);
  }

  function renderToolCard(tool) {
    const aliases = Array.isArray(tool?.aliases) ? tool.aliases : [];
    const restricted = panel?.tool_groups?.restricted?.some((item) => item?.id === tool?.id);
    return el("div", { class: "rule-card skill-tool-card" }, [
      el("div", { class: "rule-card-head" }, [
        el("div", {}, [
          el("div", { class: "rule-card-title", text: tool.name || tool.id }),
          el("div", { class: "rule-card-desc", text: tool.description || "" }),
        ]),
        chip({ label: tool.id || "tool", variant: "info" }),
      ]),
      el("div", { class: "row" }, [
        tool.model_callable
          ? chip({ label: copy("modelCallable"), variant: "ok" })
          : chip({ label: copy("manualOnly"), variant: "outline" }),
        restricted ? chip({ label: copy("restricted"), variant: "danger" }) : null,
        aliases.length
          ? chip({ label: `${copy("aliases")}: ${aliases.length}`, variant: "outline" })
          : chip({ label: copy("noAliases"), variant: "outline" }),
      ]),
      aliases.length
        ? el(
            "div",
            { class: "row" },
            aliases.map((alias) => chip({ label: alias, variant: "outline" })),
          )
        : null,
    ]);
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
    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void search();
      }
    });
    const searchBtn = el("button", {
      type: "button",
      class: "btn is-primary is-sm",
      text: t("common.search"),
      onClick: search,
    });

    const resultsList = el("div", { class: "skills-marketplace-results" });
    if (!results.length) {
      resultsList.appendChild(empty({ title: t("skills.marketplace.empty") }));
    } else {
      results.forEach((item) => {
        resultsList.appendChild(
          el("div", { class: "rule-card skill-marketplace-card" }, [
            el("div", { class: "rule-card-head" }, [
              el("div", {}, [
                el("div", {
                  class: "rule-card-title",
                  text: item.name || item.title || item.skill_id || "?",
                }),
                el("div", { class: "rule-card-desc", text: item.description || "" }),
              ]),
              el("button", {
                type: "button",
                class: "btn is-sm is-primary",
                text: t("common.add"),
                onClick: () =>
                  importFromResult(
                    item.source_id || sources[0]?.id || "",
                    item.install_url || item.github_url || item.skill_url || item.raw_url || "",
                  ),
              }),
            ]),
            el("div", { class: "row" }, [
              item.source_name
                ? chip({ label: item.source_name, variant: "info" })
                : item.source_id
                  ? chip({ label: item.source_id, variant: "info" })
                  : null,
              item.author ? chip({ label: item.author, variant: "outline" }) : null,
            ]),
          ]),
        );
      });
    }

    return card({
      title: t("skills.marketplace.title"),
      subtitle: t("skills.section.resultsDesc"),
      body: [
        el("div", { class: "row-tight" }, [searchInput, searchBtn]),
        el(
          "div",
          { class: "row" },
          sources.map((source) =>
            chip({
              label: `${source.name || source.source_id} ${source.enabled ? "on" : "off"}`,
              variant: source.enabled ? "ok" : "outline",
            }),
          ),
        ),
        results.length
          ? chip({ label: `${t("skills.section.results")}: ${results.length}`, variant: "info" })
          : null,
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
