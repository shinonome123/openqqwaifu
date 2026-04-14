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
  recentEvents: (limit = 50) =>
    request("GET", `/api/events/recent?limit=${encodeURIComponent(limit)}`),

  getCharacterPanel: (character = "") =>
    request(
      "GET",
      character
        ? `/api/panels/character?character=${encodeURIComponent(character)}`
        : "/api/panels/character",
    ),
  saveCharacterPanel: (payload) => request("POST", "/api/panels/character", payload),

  getAiPanel: () => request("GET", "/api/panels/ai"),
  saveAiPanel: (payload) => request("POST", "/api/panels/ai", payload),

  getMemoryPanel: () => request("GET", "/api/panels/memory"),
  getAbilitiesPanel: () => request("GET", "/api/panels/abilities"),
  saveAbilitiesPanel: (payload) => request("POST", "/api/panels/abilities", payload),
  getUserPanel: () => request("GET", "/api/panels/user"),

  getSkillsPanel: () => request("GET", "/api/panels/skills"),
  getSidecarPanel: (refresh = false) =>
    request("GET", refresh ? "/api/panels/sidecar?refresh=1" : "/api/panels/sidecar"),
  saveSidecarPanel: (payload) => request("POST", "/api/panels/sidecar", payload),

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
