// Thin fetch wrapper against the Python HTTP API (panel-oriented).

async function request(method, path, body) {
  const init = {
    method,
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    throw new Error(err?.message || String(err));
  }
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = { raw: text };
    }
  }
  if (!response.ok) {
    const message =
      (payload && (payload.reason || payload.error || payload.message)) ||
      `${response.status} ${response.statusText || "Error"}`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export const api = {
  healthz: () => request("GET", "/healthz"),
  authState: () => request("GET", "/api/auth/state"),
  bootstrapAuth: (payload) => request("POST", "/api/auth/bootstrap", payload),
  login: (payload) => request("POST", "/api/auth/login", payload),
  logout: () => request("POST", "/api/auth/logout", {}),
  changePassword: (payload) => request("POST", "/api/auth/change-password", payload),

  dashboard: () => request("GET", "/api/dashboard"),
  console: () => request("GET", "/api/console"),
  runtime: () => request("GET", "/api/runtime"),
  getObservabilityPanel: (logLimit = 120, rowLimit = 60) =>
    request(
      "GET",
      `/api/panels/observability?log_limit=${encodeURIComponent(logLimit)}&row_limit=${encodeURIComponent(rowLimit)}`,
    ),
  recentEvents: (limit = 50) =>
    request("GET", `/api/events/recent?limit=${encodeURIComponent(limit)}`),
  behaviorEvents: (limit = 80, launcherType = "", launcherId = "") => {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    if (launcherType) params.set("launcher_type", launcherType);
    if (launcherId) params.set("launcher_id", launcherId);
    return request("GET", `/api/events/behavior?${params.toString()}`);
  },

  getCharacterPanel: (character = "") =>
    request(
      "GET",
      character
        ? `/api/panels/character?character=${encodeURIComponent(character)}`
        : "/api/panels/character",
    ),
  saveCharacterPanel: (payload) => request("POST", "/api/panels/character", payload),
  previewCharacter: (payload) => request("POST", "/api/character/preview", payload),

  getAiPanel: () => request("GET", "/api/panels/ai"),
  saveAiPanel: (payload) => request("POST", "/api/panels/ai", payload),

  getMemoryPanel: () => request("GET", "/api/panels/memory"),
  saveKnowledgeEntry: (payload) => request("POST", "/api/knowledge/save", payload),
  deleteKnowledgeEntry: (id) => request("DELETE", `/api/knowledge/${encodeURIComponent(id)}`),
  getAbilitiesPanel: () => request("GET", "/api/panels/abilities"),
  saveAbilitiesPanel: (payload) => request("POST", "/api/panels/abilities", payload),
  getProactivePanel: (limit = 12) =>
    request("GET", `/api/panels/proactive?limit=${encodeURIComponent(limit)}`),
  generateProactiveDraft: (payload) => request("POST", "/api/proactive/draft", payload),
  getUserPanel: () => request("GET", "/api/panels/user"),
  saveDirectoryMember: (payload) => request("POST", "/api/users/directory/save", payload),
  resetDirectoryMemberPersona: (groupId, userId) =>
    request("POST", "/api/users/directory/reset-persona", {
      group_id: groupId,
      user_id: userId,
    }),
  syncDirectoryGroup: (groupId) =>
    request("POST", "/api/users/directory/sync", { group_id: groupId }),

  getSkillsPanel: () => request("GET", "/api/panels/skills"),
  getClawRuntimePanel: (refresh = false) =>
    request("GET", refresh ? "/api/claw/runtime?refresh=1" : "/api/claw/runtime"),
  listClawPlugins: () => request("GET", "/api/claw/plugins"),
  inspectClawPlugin: (id) => request("GET", `/api/claw/plugins/${encodeURIComponent(id)}`),
  checkClawPlugins: () => request("POST", "/api/claw/plugins/check", {}),
  updateClawPlugin: (pluginId) =>
    request("POST", "/api/claw/plugins/update", {
      plugin_id: pluginId,
    }),
  getSidecarPanel: (refresh = false) =>
    request("GET", refresh ? "/api/panels/sidecar?refresh=1" : "/api/panels/sidecar"),
  saveSidecarPanel: (payload) => request("POST", "/api/panels/sidecar", payload),
  getQqLoginPanel: (refresh = false) =>
    request("GET", refresh ? "/api/panels/qq-login?refresh=1" : "/api/panels/qq-login"),
  saveQqLoginPanel: (payload) => request("POST", "/api/panels/qq-login", payload),
  refreshQqLoginPanel: () => request("POST", "/api/qq-login/refresh", {}),
  qqLoginQrcodeImageUrl: (stamp = "") =>
    `/api/qq-login/qrcode-image${stamp ? `?t=${encodeURIComponent(stamp)}` : ""}`,

  getOtherPanel: () => request("GET", "/api/panels/other"),
  saveOtherPanel: (payload) => request("POST", "/api/panels/other", payload),

  listSkills: () => request("GET", "/api/skills"),
  listTools: () => request("GET", "/api/tools"),
  skillDetail: (id) => request("GET", `/api/skills/${encodeURIComponent(id)}`),
  toggleSkill: (id, enabled) =>
    request("POST", `/api/skills/${encodeURIComponent(id)}/toggle`, { enabled }),
  saveSkill: (id, markdown) =>
    request("POST", `/api/skills/${encodeURIComponent(id)}/save`, { markdown }),
  deleteSkill: (id) => request("DELETE", `/api/skills/${encodeURIComponent(id)}`),
  installSkill: (markdown, filename) =>
    request("POST", "/api/skills/install", filename ? { markdown, filename } : { markdown }),
  reloadSkills: () => request("POST", "/api/skills/reload", {}),
  newSkillTemplate: () => request("GET", "/api/skills/template"),

  searchMarketplace: (q, sourceId = "", limit = 12) => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (sourceId) params.set("source_id", sourceId);
    params.set("limit", String(limit));
    return request("GET", `/api/marketplace/search?${params.toString()}`);
  },
  importMarketplace: (sourceId, githubUrl) =>
    request("POST", "/api/marketplace/import", {
      source_id: sourceId,
      github_url: githubUrl,
    }),
  importSkillBundle: (path, overwrite = true) =>
    request("POST", "/api/skill-bundles/import-local", { path, overwrite }),

  listSessions: (limit = 60) =>
    request("GET", `/api/sessions?limit=${encodeURIComponent(limit)}`),
  sessionDetail: (launcherType, launcherId) =>
    request(
      "GET",
      `/api/sessions/${encodeURIComponent(launcherType)}/${encodeURIComponent(launcherId)}`,
    ),
  saveSession: (launcherType, launcherId, payload) =>
    request(
      "POST",
      `/api/memory/sessions/${encodeURIComponent(launcherType)}/${encodeURIComponent(launcherId)}/save`,
      payload,
    ),

  testProvider: (kind, baseUrl, apiKey) =>
    request("POST", "/api/providers/test", {
      kind,
      base_url: baseUrl,
      api_key: apiKey,
    }),
  testSidecar: () => request("POST", "/api/sidecar/test", {}),
};
