import fs from "node:fs";
import fsp from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import url from "node:url";
import childProcess from "node:child_process";

const RUNTIME_VERSION = "0.1.0";
const CAPABILITY_WIRED = "wired";
const CAPABILITY_DETECT_ONLY = "detect_only";
const CAPABILITY_UNSUPPORTED = "unsupported";
const INSTALL_META_FILENAME = ".openqqwaifu-claw-install.json";
const BUNDLE_MANIFESTS = [
  [".codex-plugin/plugin.json", "codex"],
  [".claude-plugin/plugin.json", "claude"],
  [".cursor-plugin/plugin.json", "cursor"],
];
const NATIVE_MANIFESTS = [
  "openclaw.plugin.json",
  ".openclaw-plugin/plugin.json",
];
const SKILL_ROOTS = ["skills", "commands", ".cursor/commands"];
const MCP_CACHE_TTL_MS = 10_000;
const ACP_OUTPUT_CHUNK_LIMIT = 256;
const ACP_MESSAGE_LIMIT = 128;
const ACP_SESSION_CLOSE_TIMEOUT_MS = 3_000;

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseArgs(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = String(argv[index] || "");
    if (!token.startsWith("--")) {
      continue;
    }
    const key = token.slice(2).replace(/-([a-z])/g, (_, chr) => chr.toUpperCase());
    const value = argv[index + 1];
    parsed[key] = value === undefined || String(value).startsWith("--") ? "true" : String(value);
    if (value !== undefined && !String(value).startsWith("--")) {
      index += 1;
    }
  }
  return parsed;
}

function asBool(value, fallback = false) {
  if (value === undefined || value === null) {
    return fallback;
  }
  const normalized = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return fallback;
}

function slugify(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "plugin";
}

function safeJoin(root, target) {
  const resolvedRoot = path.resolve(root);
  const resolvedTarget = path.resolve(resolvedRoot, target);
  if (
    resolvedTarget !== resolvedRoot &&
    !resolvedTarget.startsWith(`${resolvedRoot}${path.sep}`)
  ) {
    throw new Error(`path escapes runtime root: ${target}`);
  }
  return resolvedTarget;
}

function readJsonIfExists(filePath) {
  try {
    if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      return null;
    }
    return JSON.parse(fs.readFileSync(filePath, "utf-8"));
  } catch (error) {
    return null;
  }
}

function coerceStringList(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) {
    return value.split(",").map((item) => item.trim()).filter(Boolean);
  }
  return [];
}

function parseStringListArg(value) {
  if (Array.isArray(value)) {
    return coerceStringList(value);
  }
  const text = String(value || "").trim();
  if (!text) {
    return [];
  }
  if (text.startsWith("[")) {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) {
        return coerceStringList(parsed);
      }
    } catch (error) {
      // Fall back to comma-separated parsing.
    }
  }
  return coerceStringList(text);
}

function clampNumber(value, fallback, min, max) {
  const parsed = Number.parseFloat(String(value ?? ""));
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.min(max, Math.max(min, parsed));
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, Math.max(0, Number(ms) || 0));
  });
}

function trimString(value) {
  return String(value || "").trim();
}

function pushLimited(list, value, limit) {
  list.push(value);
  if (list.length > limit) {
    list.splice(0, list.length - limit);
  }
}

function sanitizeSafeToken(value, fallback) {
  const normalized = String(value || "")
    .trim()
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "");
  return normalized || fallback;
}

function safeServerPrefix(value) {
  return sanitizeSafeToken(value, "mcp").slice(0, 30);
}

function safeToolLeaf(value) {
  return sanitizeSafeToken(value, "tool");
}

function buildSafeMcpToolName(serverName, toolName, usedNames) {
  const prefix = safeServerPrefix(serverName);
  const rawLeaf = safeToolLeaf(toolName);
  const maxTotalLength = 64;
  let suffix = "";
  let counter = 2;
  while (true) {
    const leafMax = Math.max(1, maxTotalLength - prefix.length - 2 - suffix.length);
    const candidate = `${prefix}__${rawLeaf.slice(0, leafMax)}${suffix}`;
    if (!usedNames.has(candidate)) {
      usedNames.add(candidate);
      return candidate;
    }
    suffix = `-${counter}`;
    counter += 1;
  }
}

function resolveMcpServerEntry(rootPath, serverName, rawConfig) {
  const payload = isObject(rawConfig) ? JSON.parse(JSON.stringify(rawConfig)) : {};
  const command = typeof payload.command === "string" ? payload.command.trim() : "";
  const args = Array.isArray(payload.args) ? payload.args.map((item) => String(item || "")).filter(Boolean) : [];
  const endpoint = typeof payload.url === "string" && payload.url.trim()
    ? payload.url.trim()
    : typeof payload.endpoint === "string" && payload.endpoint.trim()
      ? payload.endpoint.trim()
      : "";
  let cwd = "";
  let cwdReason = "";
  const cwdToken = typeof payload.cwd === "string" && payload.cwd.trim() ? payload.cwd.trim() : ".";
  try {
    cwd = safeJoin(rootPath, cwdToken);
  } catch (error) {
    cwdReason = String(error?.message || error);
  }
  const env = {};
  if (isObject(payload.env)) {
    for (const [key, value] of Object.entries(payload.env)) {
      const name = String(key || "").trim();
      if (!name) {
        continue;
      }
      env[name] = String(value ?? "");
    }
  }
  const connectionTimeoutMs = Number.parseInt(
    String(payload.connectionTimeoutMs || payload.timeoutMs || 30_000),
    10,
  );
  const transport = command ? "stdio" : endpoint ? "http" : "unknown";
  let supported = false;
  let reason = "";
  if (transport === "stdio") {
    supported = Boolean(command && cwd);
    if (!supported) {
      reason = cwdReason || "invalid stdio MCP server configuration";
    }
  } else if (transport === "http") {
    supported = Boolean(endpoint);
    if (!supported) {
      reason = "missing MCP endpoint URL";
    }
  } else {
    reason = "missing MCP transport";
  }
  return {
    key: String(serverName || "").trim() || "mcp",
    safe_prefix: safeServerPrefix(serverName),
    transport,
    supported,
    reason,
    command,
    args,
    cwd,
    env,
    url: endpoint,
    connection_timeout_ms: Number.isFinite(connectionTimeoutMs) ? connectionTimeoutMs : 30_000,
    raw: payload,
  };
}

function listMcpServerEntries(rootPath) {
  return Object.entries(loadMcpConfig(rootPath))
    .map(([name, config]) => resolveMcpServerEntry(rootPath, name, config))
    .sort((left, right) => String(left.key).localeCompare(String(right.key)));
}

function encodeJsonRpcFrame(payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf-8");
  return Buffer.concat([
    Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, "utf-8"),
    body,
  ]);
}

function createJsonRpcFrameParser(onMessage) {
  let buffer = Buffer.alloc(0);
  return (chunk) => {
    buffer = Buffer.concat([buffer, Buffer.from(chunk)]);
    while (true) {
      const headerEnd = buffer.indexOf("\r\n\r\n");
      if (headerEnd < 0) {
        return;
      }
      const headerText = buffer.slice(0, headerEnd).toString("utf-8");
      const lengthMatch = /content-length:\s*(\d+)/i.exec(headerText);
      if (!lengthMatch) {
        throw new Error("missing Content-Length in MCP response");
      }
      const bodyLength = Number.parseInt(lengthMatch[1], 10);
      const bodyStart = headerEnd + 4;
      if (buffer.length < bodyStart + bodyLength) {
        return;
      }
      const body = buffer.slice(bodyStart, bodyStart + bodyLength).toString("utf-8");
      buffer = buffer.slice(bodyStart + bodyLength);
      onMessage(JSON.parse(body));
    }
  };
}

async function withStdioMcpSession(server, operation) {
  const stderrChunks = [];
  const proc = childProcess.spawn(server.command, server.args, {
    cwd: server.cwd || state.root,
    env: { ...process.env, ...server.env },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let nextId = 1;
  const pending = new Map();
  const settlePending = (error) => {
    for (const entry of pending.values()) {
      clearTimeout(entry.timeout);
      entry.reject(error);
    }
    pending.clear();
  };
  const parser = createJsonRpcFrameParser((message) => {
    const entry = pending.get(message.id);
    if (!entry) {
      return;
    }
    clearTimeout(entry.timeout);
    pending.delete(message.id);
    if (message.error) {
      entry.reject(new Error(String(message.error.message || "MCP request failed")));
      return;
    }
    entry.resolve(message.result ?? {});
  });
  proc.stdout.on("data", (chunk) => {
    try {
      parser(chunk);
    } catch (error) {
      settlePending(error);
    }
  });
  proc.stderr.on("data", (chunk) => {
    stderrChunks.push(Buffer.from(chunk).toString("utf-8"));
  });
  proc.on("error", (error) => {
    settlePending(error);
  });
  proc.on("exit", (code, signal) => {
    if (!pending.size) {
      return;
    }
    const stderrText = stderrChunks.join("").trim();
    const reason = stderrText || `MCP stdio server exited code=${code ?? "?"} signal=${signal ?? "-"}`;
    settlePending(new Error(reason));
  });
  const sendPayload = (payload) => {
    proc.stdin.write(encodeJsonRpcFrame(payload));
  };
  const sendRequest = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`MCP request timed out: ${method}`));
      }, Math.max(2_000, server.connection_timeout_ms));
      pending.set(id, { resolve, reject, timeout });
      sendPayload({ jsonrpc: "2.0", id, method, params });
    });
  const sendNotification = (method, params = {}) => {
    sendPayload({ jsonrpc: "2.0", method, params });
  };
  try {
    await sendRequest("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: { tools: {} },
      clientInfo: {
        name: "openqqwaifu-claw-runtime",
        version: RUNTIME_VERSION,
      },
    });
    sendNotification("notifications/initialized", {});
    return await operation({ sendRequest, sendNotification });
  } finally {
    proc.stdin.end();
    if (!proc.killed) {
      proc.kill();
    }
  }
}

async function withHttpMcpSession(server, operation) {
  let nextId = 1;
  const sendRequest = async (method, params = {}) => {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      Math.max(2_000, server.connection_timeout_ms),
    );
    try {
      const response = await fetch(server.url, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`MCP HTTP ${response.status}`);
      }
      const payload = await response.json();
      if (!isObject(payload)) {
        throw new Error("invalid MCP HTTP response");
      }
      if (payload.error) {
        throw new Error(String(payload.error.message || "MCP HTTP request failed"));
      }
      return payload.result ?? {};
    } finally {
      clearTimeout(timeout);
    }
  };
  const sendNotification = async (method, params = {}) => {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      Math.max(2_000, server.connection_timeout_ms),
    );
    try {
      await fetch(server.url, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ jsonrpc: "2.0", method, params }),
        signal: controller.signal,
      }).catch(() => {});
    } finally {
      clearTimeout(timeout);
    }
  };
  await sendRequest("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: { tools: {} },
    clientInfo: {
      name: "openqqwaifu-claw-runtime",
      version: RUNTIME_VERSION,
    },
  });
  await sendNotification("notifications/initialized", {});
  return operation({ sendRequest, sendNotification });
}

async function withMcpSession(server, operation) {
  if (!server.supported) {
    throw new Error(server.reason || "unsupported MCP server");
  }
  if (server.transport === "stdio") {
    return withStdioMcpSession(server, operation);
  }
  if (server.transport === "http") {
    return withHttpMcpSession(server, operation);
  }
  throw new Error(server.reason || "unsupported MCP transport");
}

function mcpCacheKey(pluginId, server) {
  return JSON.stringify({
    pluginId,
    key: server.key,
    transport: server.transport,
    command: server.command,
    args: server.args,
    cwd: server.cwd,
    url: server.url,
    env: server.env,
  });
}

async function listMcpToolsForServer(pluginId, server) {
  if (!server.supported) {
    return [];
  }
  const cacheKey = mcpCacheKey(pluginId, server);
  const cached = state.mcpToolCache.get(cacheKey);
  if (cached && cached.expires_at > Date.now()) {
    return cached.items;
  }
  const result = await withMcpSession(server, async ({ sendRequest }) => {
    const payload = await sendRequest("tools/list", {});
    return Array.isArray(payload.tools) ? payload.tools : [];
  });
  const items = result
    .filter((item) => isObject(item) && typeof item.name === "string" && item.name.trim())
    .map((item) => ({
      name: String(item.name || "").trim(),
      description: String(item.description || "").trim(),
      inputSchema: isObject(item.inputSchema) ? item.inputSchema : {},
    }))
    .sort((left, right) => left.name.localeCompare(right.name));
  state.mcpToolCache.set(cacheKey, {
    expires_at: Date.now() + MCP_CACHE_TTL_MS,
    items,
  });
  return items;
}

async function callMcpTool(server, toolName, argumentsPayload) {
  return withMcpSession(server, async ({ sendRequest }) =>
    sendRequest("tools/call", {
      name: toolName,
      arguments: isObject(argumentsPayload) ? argumentsPayload : {},
    }));
}

function mcpCallResultToText(result) {
  const lines = [];
  if (isObject(result) && Array.isArray(result.content)) {
    for (const item of result.content) {
      if (!isObject(item)) {
        continue;
      }
      if (item.type === "text" && typeof item.text === "string" && item.text.trim()) {
        lines.push(item.text.trim());
      }
    }
  }
  if (!lines.length && isObject(result) && isObject(result.structuredContent)) {
    lines.push(JSON.stringify(result.structuredContent, null, 2));
  }
  if (!lines.length && isObject(result) && typeof result.text === "string" && result.text.trim()) {
    lines.push(result.text.trim());
  }
  return lines.join("\n\n").trim();
}

function splitFrontmatter(raw) {
  const text = String(raw || "");
  if (!text.startsWith("---")) {
    return ["", text];
  }
  const lines = text.split(/\r?\n/);
  if ((lines[0] || "").trim() !== "---") {
    return ["", text];
  }
  const frontmatter = [];
  const body = [];
  let inFrontmatter = true;
  for (const line of lines.slice(1)) {
    if (inFrontmatter && line.trim() === "---") {
      inFrontmatter = false;
      continue;
    }
    if (inFrontmatter) {
      frontmatter.push(line);
    } else {
      body.push(line);
    }
  }
  return [frontmatter.join("\n"), body.join("\n")];
}

function parseScalar(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "";
  }
  const lowered = raw.toLowerCase();
  if (lowered === "true") {
    return true;
  }
  if (lowered === "false") {
    return false;
  }
  if (lowered === "null" || lowered === "none") {
    return null;
  }
  if ((raw.startsWith("[") || raw.startsWith("{") || raw.startsWith("\"")) && raw.endsWith("]")) {
    try {
      return JSON.parse(raw);
    } catch (error) {
      return raw;
    }
  }
  if ((raw.startsWith("[") || raw.startsWith("{") || raw.startsWith("\"")) && raw.endsWith("}")) {
    try {
      return JSON.parse(raw);
    } catch (error) {
      return raw;
    }
  }
  if (raw.startsWith("\"") && raw.endsWith("\"")) {
    return raw.slice(1, -1);
  }
  if (/^-?\d+$/.test(raw)) {
    return Number.parseInt(raw, 10);
  }
  if (/^-?\d+\.\d+$/.test(raw)) {
    return Number.parseFloat(raw);
  }
  return raw;
}

function parseFrontmatter(raw) {
  const payload = {};
  let currentKey = "";
  for (const line of String(raw || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) {
      continue;
    }
    if (trimmed.startsWith("- ") && currentKey) {
      const existing = payload[currentKey];
      if (Array.isArray(existing)) {
        existing.push(parseScalar(trimmed.slice(2)));
      }
      continue;
    }
    const index = trimmed.indexOf(":");
    if (index <= 0) {
      currentKey = "";
      continue;
    }
    const key = trimmed.slice(0, index).trim();
    const value = trimmed.slice(index + 1).trim();
    if (!value) {
      payload[key] = [];
      currentKey = key;
      continue;
    }
    payload[key] = parseScalar(value);
    currentKey = Array.isArray(payload[key]) ? key : "";
  }
  return payload;
}

function parseSkillFile(filePath, rootPath) {
  const raw = fs.readFileSync(filePath, "utf-8");
  const [frontmatter] = splitFrontmatter(raw);
  const payload = parseFrontmatter(frontmatter);
  const fileName = path.basename(filePath);
  const relativePath = path.relative(rootPath, filePath).replaceAll("\\", "/");
  let skillId = String(payload.id || "").trim();
  if (!skillId) {
    if (fileName.toLowerCase() === "skill.md") {
      skillId = slugify(payload.name || path.basename(path.dirname(filePath)));
    } else {
      skillId = slugify(path.parse(fileName).name);
    }
  }
  return {
    id: skillId,
    name: String(payload.name || skillId).trim() || skillId,
    description: String(payload.description || "").trim(),
    triggers: coerceStringList(payload.triggers),
    command_dispatch: String(payload["command-dispatch"] || "").trim().toLowerCase(),
    command_tool: String(payload["command-tool"] || "").trim().toLowerCase(),
    relative_path: relativePath,
  };
}

function walkFiles(rootPath) {
  const files = [];
  if (!fs.existsSync(rootPath)) {
    return files;
  }
  const stack = [rootPath];
  while (stack.length) {
    const current = stack.pop();
    const entries = fs.readdirSync(current, { withFileTypes: true });
    for (const entry of entries) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(target);
      } else if (entry.isFile()) {
        files.push(target);
      }
    }
  }
  return files.sort();
}

function discoverSkillFiles(rootPath) {
  const candidates = [];
  const directSkill = path.join(rootPath, "SKILL.md");
  if (fs.existsSync(directSkill) && fs.statSync(directSkill).isFile()) {
    candidates.push(directSkill);
  }
  for (const relPath of SKILL_ROOTS) {
    const skillRoot = path.join(rootPath, relPath);
    if (!fs.existsSync(skillRoot)) {
      continue;
    }
    for (const filePath of walkFiles(skillRoot)) {
      if (filePath.toLowerCase().endsWith(".md")) {
        candidates.push(filePath);
      }
    }
  }
  if (!candidates.length) {
    for (const entry of fs.readdirSync(rootPath, { withFileTypes: true })) {
      if (entry.isFile() && entry.name.toLowerCase().endsWith(".md")) {
        candidates.push(path.join(rootPath, entry.name));
      }
    }
  }
  if (!candidates.length) {
    for (const filePath of walkFiles(rootPath)) {
      if (path.basename(filePath).toLowerCase() === "skill.md") {
        candidates.push(filePath);
      }
    }
  }
  return [...new Set(candidates.map((item) => path.resolve(item)))];
}

function discoverHookPacks(rootPath) {
  const results = [];
  for (const filePath of walkFiles(rootPath)) {
    if (path.basename(filePath).toLowerCase() !== "hook.md") {
      continue;
    }
    const parent = path.dirname(filePath);
    const handlerCandidates = ["handler.js", "handler.mjs", "handler.cjs", "handler.ts"];
    const handler = handlerCandidates
      .map((name) => path.join(parent, name))
      .find((candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile());
    if (!handler) {
      results.push({
        name: path.basename(parent),
        relative_path: path.relative(rootPath, parent).replaceAll("\\", "/"),
        handler: "",
        status: CAPABILITY_UNSUPPORTED,
        reason: "missing handler.js/mjs/cjs/ts",
      });
      continue;
    }
    results.push({
      name: path.basename(parent),
      relative_path: path.relative(rootPath, parent).replaceAll("\\", "/"),
      handler: path.relative(rootPath, handler).replaceAll("\\", "/"),
      status: CAPABILITY_WIRED,
      reason: "",
    });
  }
  return results;
}

function sanitizeSettingsPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return {};
  }
  const sanitized = JSON.parse(JSON.stringify(payload));
  if (typeof sanitized.shellPath === "string") {
    sanitized.shellPath = sanitized.shellPath.trim();
  }
  if (typeof sanitized.shellCommandPrefix === "string") {
    sanitized.shellCommandPrefix = sanitized.shellCommandPrefix.trim();
  } else if (Array.isArray(sanitized.shellCommandPrefix)) {
    sanitized.shellCommandPrefix = sanitized.shellCommandPrefix
      .map((item) => String(item || "").trim())
      .filter(Boolean)
      .slice(0, 16);
  }
  const shell = sanitized.shell;
  if (isObject(shell)) {
    const nextShell = {};
    if (typeof shell.command === "string" && shell.command.trim()) {
      nextShell.command = shell.command.trim();
    }
    if (Array.isArray(shell.args)) {
      nextShell.args = shell.args.map((item) => String(item || "").trim()).filter(Boolean).slice(0, 16);
    }
    sanitized.shell = nextShell;
  }
  return sanitized;
}

function mergeLspConfig(rootPath, manifest) {
  const merged = {};
  for (const candidate of [".lsp.json", path.join(".claude", ".lsp.json")]) {
    const payload = readJsonIfExists(path.join(rootPath, candidate));
    if (payload && typeof payload === "object" && !Array.isArray(payload)) {
      Object.assign(merged, payload);
    }
  }
  if (manifest && typeof manifest === "object" && manifest.lspServers && typeof manifest.lspServers === "object") {
    merged.lspServers = {
      ...(merged.lspServers && typeof merged.lspServers === "object" ? merged.lspServers : {}),
      ...manifest.lspServers,
    };
  }
  return merged;
}

function loadMcpConfig(rootPath) {
  const payload = readJsonIfExists(path.join(rootPath, ".mcp.json"));
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return {};
  }
  const servers = payload.servers && typeof payload.servers === "object" ? payload.servers : payload.mcpServers;
  return servers && typeof servers === "object" ? servers : {};
}

function detectInstallFormat(rootPath) {
  for (const relPath of NATIVE_MANIFESTS) {
    const candidate = path.join(rootPath, relPath);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      return {
        format: "native",
        bundle_type: "none",
        manifest_path: candidate,
      };
    }
  }
  for (const [relPath, bundleType] of BUNDLE_MANIFESTS) {
    const candidate = path.join(rootPath, relPath);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      return {
        format: "bundle",
        bundle_type: bundleType,
        manifest_path: candidate,
      };
    }
  }
  return {
    format: "bundle",
    bundle_type: "none",
    manifest_path: "",
  };
}

function countByStatus(capabilities) {
  return capabilities.reduce(
    (acc, capability) => {
      const status = String(capability.status || CAPABILITY_UNSUPPORTED);
      if (!acc[status]) {
        acc[status] = 0;
      }
      acc[status] += 1;
      return acc;
    },
    { wired: 0, detect_only: 0, unsupported: 0 },
  );
}

function buildDiagnostics(capabilities) {
  return capabilities
    .filter((item) => item.status !== CAPABILITY_WIRED)
    .map((item) => ({
      kind: item.kind,
      status: item.status,
      reason: item.reason || "",
      path: item.path || "",
    }));
}

function inspectPluginRoot(rootPath, installMeta = {}) {
  const resolvedRoot = path.resolve(rootPath);
  const installFormat = detectInstallFormat(resolvedRoot);
  const manifest = installFormat.manifest_path ? readJsonIfExists(installFormat.manifest_path) || {} : {};
  const skillFiles = discoverSkillFiles(resolvedRoot);
  const skills = skillFiles.map((filePath) => parseSkillFile(filePath, resolvedRoot));
  const hookPacks = discoverHookPacks(resolvedRoot);
  const settingsFiles = [path.join(resolvedRoot, "settings.json"), path.join(resolvedRoot, ".claude", "settings.json")]
    .filter((candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile());
  const sanitizedSettings = settingsFiles.map((candidate) => ({
      path: path.relative(resolvedRoot, candidate).replaceAll("\\", "/"),
      data: sanitizeSettingsPayload(readJsonIfExists(candidate) || {}),
    }));
  const lspConfig = mergeLspConfig(resolvedRoot, manifest);
  const mcpServerEntries = listMcpServerEntries(resolvedRoot);
  const detectOnlyEntries = [];
  const detectOnlyPaths = [
    ["claude_agents", "agents"],
    ["claude_hooks_json", "hooks.json"],
    ["claude_output_styles", "outputStyles"],
    ["cursor_agents", path.join(".cursor", "agents")],
    ["cursor_hooks_json", path.join(".cursor", "hooks.json")],
    ["cursor_rules", path.join(".cursor", "rules")],
  ];
  for (const [kind, relPath] of detectOnlyPaths) {
    const candidate = path.join(resolvedRoot, relPath);
    if (!fs.existsSync(candidate)) {
      continue;
    }
    detectOnlyEntries.push({
      kind,
      status: CAPABILITY_DETECT_ONLY,
      path: relPath.replaceAll("\\", "/"),
      reason: "detected but not executed by the current bridge",
    });
  }
  const codexMetaRoot = path.join(resolvedRoot, ".codex-plugin");
  if (fs.existsSync(codexMetaRoot) && fs.statSync(codexMetaRoot).isDirectory()) {
    for (const entry of fs.readdirSync(codexMetaRoot, { withFileTypes: true })) {
      if (entry.name === "plugin.json") {
        continue;
      }
      detectOnlyEntries.push({
        kind: "codex_extra_metadata",
        status: CAPABILITY_DETECT_ONLY,
        path: path.join(".codex-plugin", entry.name).replaceAll("\\", "/"),
        reason: "detected codex metadata outside the wired compatibility surface",
      });
    }
  }

  const capabilities = [];
  if (installFormat.format === "native") {
    capabilities.push({
      kind: "native_plugin",
      status: CAPABILITY_WIRED,
      path: path.relative(resolvedRoot, installFormat.manifest_path).replaceAll("\\", "/"),
      reason: "",
    });
  }
  if (skills.length) {
    capabilities.push({
      kind: "skills",
      status: CAPABILITY_WIRED,
      count: skills.length,
      path: "skills",
      reason: "",
    });
  }
  const claudeCommands = skills.filter((item) => item.relative_path.startsWith("commands/"));
  if (claudeCommands.length) {
    capabilities.push({
      kind: "claude_commands",
      status: CAPABILITY_WIRED,
      count: claudeCommands.length,
      path: "commands",
      reason: "",
    });
  }
  const cursorCommands = skills.filter((item) => item.relative_path.startsWith(".cursor/commands/"));
  if (cursorCommands.length) {
    capabilities.push({
      kind: "cursor_commands",
      status: CAPABILITY_WIRED,
      count: cursorCommands.length,
      path: ".cursor/commands",
      reason: "",
    });
  }
  if (hookPacks.length) {
    const unsupportedHook = hookPacks.find((item) => item.status !== CAPABILITY_WIRED);
    capabilities.push({
      kind: "hook_pack",
      status: unsupportedHook ? CAPABILITY_UNSUPPORTED : CAPABILITY_WIRED,
      count: hookPacks.length,
      path: "hooks",
      reason: unsupportedHook ? unsupportedHook.reason : "",
    });
  }
  if (mcpServerEntries.length) {
    const mcpStatuses = mcpServerEntries.map((item) => {
      if (!item.supported) {
        return CAPABILITY_UNSUPPORTED;
      }
      if (state.pluginToolsMcpBridge || state.acpEnabled) {
        return CAPABILITY_WIRED;
      }
      return CAPABILITY_DETECT_ONLY;
    });
    const mcpStatus = mcpStatuses.includes(CAPABILITY_WIRED)
      ? CAPABILITY_WIRED
      : mcpStatuses.includes(CAPABILITY_DETECT_ONLY)
        ? CAPABILITY_DETECT_ONLY
        : CAPABILITY_UNSUPPORTED;
    const mcpReason = mcpServerEntries
      .filter((item) => !item.supported)
      .map((item) => `${item.key}: ${item.reason}`)
      .join("; ");
    capabilities.push({
      kind: "mcp",
      status: mcpStatus,
      count: mcpServerEntries.length,
      path: ".mcp.json",
      reason: mcpStatus === CAPABILITY_DETECT_ONLY
        ? "MCP config loaded, but bridge exposure is disabled"
        : mcpReason,
    });
  }
  if (sanitizedSettings.length) {
    capabilities.push({
      kind: "claude_settings",
      status: CAPABILITY_WIRED,
      count: sanitizedSettings.length,
      path: sanitizedSettings[0].path,
      reason: "",
    });
  }
  if (Object.keys(lspConfig).length) {
    capabilities.push({
      kind: "lsp",
      status: CAPABILITY_WIRED,
      count: Object.keys(lspConfig).length,
      path: ".lsp.json",
      reason: "",
    });
  }
  capabilities.push(...detectOnlyEntries);

  const pluginId = slugify(
    manifest.id ||
      manifest.name ||
      installMeta.plugin_id ||
      skills[0]?.id ||
      path.basename(resolvedRoot),
  );
  const pluginName = String(
    manifest.name ||
      installMeta.name ||
      skills[0]?.name ||
      pluginId,
  ).trim() || pluginId;
  const tools = [];
  for (const hook of hookPacks) {
    tools.push({
      id: `hook:${pluginId}:${slugify(hook.name)}`,
      name: hook.name,
      owner_runtime: "claw",
      kind: "hook-pack",
      status: hook.status,
      path: hook.relative_path,
    });
  }
  for (const server of mcpServerEntries) {
    const status = !server.supported
      ? CAPABILITY_UNSUPPORTED
      : state.pluginToolsMcpBridge || state.acpEnabled
        ? CAPABILITY_WIRED
        : CAPABILITY_DETECT_ONLY;
    tools.push({
      id: `mcp:${pluginId}:${slugify(server.key)}`,
      name: server.key,
      owner_runtime: "claw",
      kind: "mcp-server",
      status,
      path: ".mcp.json",
      reason: status === CAPABILITY_DETECT_ONLY
        ? "MCP config loaded, but bridge exposure is disabled"
        : server.reason,
      transport: server.transport,
    });
  }

  const counts = countByStatus(capabilities);
  return {
    id: pluginId,
    name: pluginName,
    format: installFormat.format,
    bundle_type: installFormat.bundle_type,
    owner_runtime: "claw",
    owner_routing: state.routingMode === "shadow" ? "python" : "claw",
    routing_mode: state.routingMode,
    effective_source: installMeta.source_id || installMeta.source_url ? "marketplace" : "local",
    requires_status: counts.unsupported ? "needs_setup" : "ready",
    installed_path: resolvedRoot,
    source_id: installMeta.source_id || "",
    source_url: installMeta.source_url || "",
    bundle_url: installMeta.bundle_url || "",
    page_url: installMeta.page_url || "",
    installed_at: installMeta.installed_at || "",
    capabilities,
    capability_counts: counts,
    diagnostics: buildDiagnostics(capabilities),
    skills,
    tools,
    hook_packs: hookPacks,
    settings: sanitizedSettings,
    lsp: lspConfig,
    mcp_servers: Object.fromEntries(
      mcpServerEntries.map((item) => [slugify(item.key), item.raw]),
    ),
    mcp_servers_detail: mcpServerEntries.map((item) => ({
      key: item.key,
      safe_prefix: item.safe_prefix,
      transport: item.transport,
      supported: item.supported,
      reason: item.reason,
    })),
  };
}

async function collectRuntimeTools() {
  const plugins = listInstalledPluginIds()
    .map((pluginId) => inspectInstalledPlugin(pluginId))
    .filter(Boolean);
  const items = [];
  for (const plugin of plugins) {
    for (const tool of plugin.tools) {
      items.push({ ...tool, plugin_id: plugin.id });
    }
  }
  const usedMcpNames = new Set(
    items.map((item) => String(item.id || "")).filter(Boolean),
  );
  for (const plugin of plugins) {
    const servers = listMcpServerEntries(plugin.installed_path);
    for (const server of servers) {
      const exposureStatus = !server.supported
        ? CAPABILITY_UNSUPPORTED
        : state.pluginToolsMcpBridge || state.acpEnabled
          ? CAPABILITY_WIRED
          : CAPABILITY_DETECT_ONLY;
      if (!server.supported) {
        continue;
      }
      let mcpTools = [];
      let loadError = "";
      try {
        mcpTools = await listMcpToolsForServer(plugin.id, server);
      } catch (error) {
        loadError = String(error?.message || error);
      }
      if (loadError) {
        items.push({
          id: `mcp:${plugin.id}:${slugify(server.key)}`,
          plugin_id: plugin.id,
          name: server.key,
          owner_runtime: "claw",
          kind: "mcp-server",
          status: CAPABILITY_UNSUPPORTED,
          path: ".mcp.json",
          reason: loadError,
          transport: server.transport,
        });
        continue;
      }
      for (const tool of mcpTools) {
        items.push({
          id: buildSafeMcpToolName(server.key, tool.name, usedMcpNames),
          plugin_id: plugin.id,
          name: tool.name,
          description: tool.description,
          owner_runtime: "claw",
          kind: "mcp",
          status: exposureStatus,
          reason: exposureStatus === CAPABILITY_DETECT_ONLY
            ? "MCP config loaded, but bridge exposure is disabled"
            : "",
          path: ".mcp.json",
          transport: server.transport,
          server_name: server.key,
          mcp_tool_name: tool.name,
          input_schema: tool.inputSchema,
        });
      }
    }
  }
  return items.sort((left, right) => String(left.id || "").localeCompare(String(right.id || "")));
}

async function invokeHookTool(plugin, tool, argumentsPayload) {
  const hookPacks = Array.isArray(plugin.hook_packs) ? plugin.hook_packs : [];
  const hook = hookPacks.find((item) => `hook:${plugin.id}:${slugify(item.name)}` === tool.id);
  if (!hook || !hook.handler) {
    return {
      status: "error",
      tool_id: tool.id,
      plugin_id: plugin.id,
      reason: "hook handler not found",
    };
  }
  const handlerPath = safeJoin(plugin.installed_path, hook.handler);
  const extension = path.extname(handlerPath).toLowerCase();
  const command = [process.execPath];
  if (extension === ".ts") {
    command.push("--experimental-strip-types");
  }
  command.push(handlerPath);
  const payload = {
    plugin_id: plugin.id,
    tool_id: tool.id,
    arguments: isObject(argumentsPayload) ? argumentsPayload : {},
  };
  const result = await new Promise((resolve) => {
    const proc = childProcess.spawn(command[0], command.slice(1), {
      cwd: plugin.installed_path,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    proc.stdout.on("data", (chunk) => stdout.push(Buffer.from(chunk).toString("utf-8")));
    proc.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk).toString("utf-8")));
    proc.on("error", (error) => {
      resolve({
        status: "error",
        tool_id: tool.id,
        plugin_id: plugin.id,
        reason: String(error?.message || error),
      });
    });
    proc.on("exit", (code) => {
      const text = stdout.join("").trim();
      const stderrText = stderr.join("").trim();
      if (code === 0) {
        resolve({
          status: "ok",
          tool_id: tool.id,
          plugin_id: plugin.id,
          kind: "hook-pack",
          text,
          metadata: {
            exit_code: code,
            stderr: stderrText,
            handler: hook.handler,
          },
        });
        return;
      }
      resolve({
        status: "error",
        tool_id: tool.id,
        plugin_id: plugin.id,
        reason: stderrText || `hook handler exited with code ${code ?? "?"}`,
      });
    });
    proc.stdin.end(`${JSON.stringify(payload)}\n`);
  });
  return result;
}

async function invokeRuntimeTool(payload) {
  const toolId = String(payload.tool_id || "").trim();
  if (!toolId) {
    return { status: "error", reason: "tool_id is required" };
  }
  const argumentsPayload = isObject(payload.arguments) ? payload.arguments : {};
  const plugins = listInstalledPluginIds()
    .map((pluginId) => inspectInstalledPlugin(pluginId))
    .filter(Boolean);
  for (const plugin of plugins) {
    const hookTool = plugin.tools.find((item) => String(item.id || "") === toolId);
    if (hookTool && String(hookTool.kind || "") === "hook-pack") {
      return invokeHookTool(plugin, hookTool, argumentsPayload);
    }
  }
  const tools = await collectRuntimeTools();
  const tool = tools.find((item) => String(item.id || "") === toolId);
  if (!tool) {
    return { status: "not_found", tool_id: toolId };
  }
  if (tool.kind !== "mcp") {
    return {
      status: "unsupported",
      tool_id: toolId,
      reason: "runtime tool is not invokable",
    };
  }
  if (tool.status !== CAPABILITY_WIRED) {
    return {
      status: "unsupported",
      tool_id: toolId,
      reason: tool.reason || "runtime tool is not exposed in the current routing mode",
    };
  }
  const plugin = plugins.find((item) => item.id === tool.plugin_id);
  if (!plugin) {
    return { status: "error", tool_id: toolId, reason: "plugin not found" };
  }
  const server = listMcpServerEntries(plugin.installed_path).find((item) => item.key === tool.server_name);
  if (!server) {
    return { status: "error", tool_id: toolId, reason: "MCP server not found" };
  }
  try {
    const result = await callMcpTool(server, tool.mcp_tool_name, argumentsPayload);
    return {
      status: "ok",
      tool_id: tool.id,
      plugin_id: plugin.id,
      kind: "mcp",
      text: mcpCallResultToText(result),
      metadata: {
        server_name: server.key,
        tool_name: tool.mcp_tool_name,
        structured_content: isObject(result?.structuredContent) ? result.structuredContent : {},
      },
      raw_result: result,
    };
  } catch (error) {
    return {
      status: "error",
      tool_id: tool.id,
      plugin_id: plugin.id,
      reason: String(error?.message || error),
    };
  }
}

function buildAcpBridgeEnv() {
  const env = { ...process.env };
  if (state.listenPort > 0) {
    const runtimeUrl = `http://127.0.0.1:${state.listenPort}`;
    env.OPENQQWAIFU_CLAW_RUNTIME_URL = runtimeUrl;
    env.OPENQQWAIFU_CLAW_RUNTIME_MCP_TOOLS_URL = `${runtimeUrl}/tools`;
    env.OPENQQWAIFU_CLAW_RUNTIME_MCP_INVOKE_URL = `${runtimeUrl}/tools/invoke`;
  }
  env.OPENQQWAIFU_CLAW_RUNTIME_ROUTING_MODE = String(state.routingMode || "shadow");
  env.OPENQQWAIFU_CLAW_RUNTIME_ACP_ENABLED = state.acpEnabled ? "true" : "false";
  env.OPENQQWAIFU_PLUGIN_TOOLS_MCP_BRIDGE = state.pluginToolsMcpBridge ? "true" : "false";
  return env;
}

function resolveAcpCwd(value) {
  const text = trimString(value);
  if (!text) {
    return state.root;
  }
  const candidate = path.isAbsolute(text) ? path.resolve(text) : safeJoin(state.root, text);
  try {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return candidate;
    }
  } catch (error) {
    // Fall back to the runtime root when the requested cwd is not accessible.
  }
  return state.root;
}

function resolveAcpHarnessSpec(payload) {
  if (!state.acpEnabled) {
    return {
      supported: false,
      harness_kind: "disabled",
      command: "",
      args: [],
      reason: "ACP is disabled in the current runtime configuration",
      cwd: state.root,
    };
  }
  const agent = trimString(payload?.agent || payload?.agent_id || payload?.model || payload?.harness).toLowerCase();
  const wantsCodex = agent.includes("codex");
  const codexCommand = trimString(state.codexHarness.command);
  const defaultCommand = trimString(state.acpDefault.command);
  if (wantsCodex && codexCommand) {
    return {
      supported: true,
      harness_kind: "codex",
      command: codexCommand,
      args: [...state.codexHarness.args],
      cwd: resolveAcpCwd(payload?.cwd),
      reason: "",
    };
  }
  if (defaultCommand) {
    return {
      supported: true,
      harness_kind: "default",
      command: defaultCommand,
      args: [...state.acpDefault.args],
      cwd: resolveAcpCwd(payload?.cwd),
      reason: "",
    };
  }
  if (codexCommand) {
    return {
      supported: true,
      harness_kind: "codex",
      command: codexCommand,
      args: [...state.codexHarness.args],
      cwd: resolveAcpCwd(payload?.cwd),
      reason: "",
    };
  }
  return {
    supported: false,
    harness_kind: wantsCodex ? "codex" : "default",
    command: "",
    args: [],
    cwd: state.root,
    reason: "No ACP harness command is configured",
  };
}

function createAcpSession(sessionId, payload) {
  const now = new Date().toISOString();
  const harness = resolveAcpHarnessSpec(payload);
  return {
    id: sessionId,
    created_at: now,
    updated_at: now,
    payload: isObject(payload) ? payload : {},
    messages: [],
    supported: harness.supported,
    harness_kind: harness.harness_kind,
    agent: trimString(payload?.agent || payload?.agent_id || payload?.model || payload?.harness),
    command: harness.command,
    args: [...harness.args],
    cwd: harness.cwd,
    reason: harness.reason,
    running: false,
    exit_code: null,
    signal: "",
    last_error: "",
    proc: null,
    stdout_chunks: [],
    stderr_chunks: [],
    activity_id: 0,
    closed_at: "",
  };
}

function appendSessionOutput(session, stream, chunk) {
  const text = Buffer.from(chunk).toString("utf-8");
  if (!text) {
    return;
  }
  if (stream === "stderr") {
    pushLimited(session.stderr_chunks, text, ACP_OUTPUT_CHUNK_LIMIT);
  } else {
    pushLimited(session.stdout_chunks, text, ACP_OUTPUT_CHUNK_LIMIT);
  }
  session.updated_at = new Date().toISOString();
  session.activity_id += 1;
}

function extractSessionOutput(session, stdoutIndex, stderrIndex) {
  const stdout = session.stdout_chunks.slice(stdoutIndex).join("");
  const stderr = session.stderr_chunks.slice(stderrIndex).join("");
  const text = [stdout.trim(), stderr.trim()].filter(Boolean).join("\n").trim();
  return { stdout, stderr, text };
}

async function settleSessionOutput(session, baselineActivity, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let observedActivity = baselineActivity;
  let sawNewOutput = false;
  while (Date.now() < deadline) {
    if (session.activity_id > observedActivity) {
      observedActivity = session.activity_id;
      sawNewOutput = true;
      const idleDeadline = Date.now() + 150;
      while (Date.now() < idleDeadline) {
        await sleep(25);
        if (session.activity_id > observedActivity) {
          observedActivity = session.activity_id;
        }
      }
      break;
    }
    if (!session.running) {
      break;
    }
    await sleep(25);
  }
  if (!sawNewOutput && session.running) {
    await sleep(25);
  }
}

function resolveAcpInputText(payload) {
  if (typeof payload?.text === "string" && payload.text.trim()) {
    return payload.text;
  }
  if (typeof payload?.input === "string" && payload.input.trim()) {
    return payload.input;
  }
  if (typeof payload?.message === "string" && payload.message.trim()) {
    return payload.message;
  }
  const body = isObject(payload) ? payload : {};
  return JSON.stringify(body);
}

function sessionStatus(session) {
  if (!session.supported) {
    return "unsupported";
  }
  if (session.last_error && !session.running) {
    return "error";
  }
  return "ok";
}

function serializeAcpSession(session, extra = {}) {
  const stdoutTail = session.stdout_chunks.join("").trim().slice(-4000);
  const stderrTail = session.stderr_chunks.join("").trim().slice(-4000);
  const payload = {
    status: sessionStatus(session),
    session_id: session.id,
    supported: session.supported,
    running: session.running,
    harness_kind: session.harness_kind,
    agent: session.agent,
    command: session.command ? [session.command, ...session.args] : [],
    cwd: session.cwd,
    reason: session.reason || session.last_error || "",
    exit_code: session.exit_code,
    signal: session.signal,
    created_at: session.created_at,
    updated_at: session.updated_at,
    closed_at: session.closed_at,
    message_count: session.messages.length,
  };
  if (stdoutTail) {
    payload.stdout_tail = stdoutTail;
  }
  if (stderrTail) {
    payload.stderr_tail = stderrTail;
  }
  return { ...payload, ...extra };
}

async function startAcpHarness(session) {
  if (!session.supported) {
    return session;
  }
  const proc = childProcess.spawn(session.command, session.args, {
    cwd: session.cwd,
    env: buildAcpBridgeEnv(),
    stdio: ["pipe", "pipe", "pipe"],
  });
  session.proc = proc;
  session.running = true;
  session.updated_at = new Date().toISOString();
  proc.stdout.on("data", (chunk) => {
    appendSessionOutput(session, "stdout", chunk);
  });
  proc.stderr.on("data", (chunk) => {
    appendSessionOutput(session, "stderr", chunk);
  });
  proc.on("error", (error) => {
    session.last_error = String(error?.message || error);
    session.running = false;
    session.updated_at = new Date().toISOString();
    session.activity_id += 1;
  });
  proc.on("exit", (code, signal) => {
    session.running = false;
    session.exit_code = typeof code === "number" ? code : null;
    session.signal = signal ? String(signal) : "";
    session.closed_at = new Date().toISOString();
    session.updated_at = session.closed_at;
    if (session.exit_code && !session.last_error) {
      const stderr = session.stderr_chunks.join("").trim();
      session.last_error = stderr || `ACP harness exited with code ${session.exit_code}`;
    }
    session.activity_id += 1;
  });
  await sleep(75);
  return session;
}

async function sendAcpInput(session, payload) {
  if (!session.supported || !session.proc || !session.running || !session.proc.stdin) {
    return serializeAcpSession(session);
  }
  const inputText = resolveAcpInputText(payload);
  const baselineActivity = session.activity_id;
  const stdoutIndex = session.stdout_chunks.length;
  const stderrIndex = session.stderr_chunks.length;
  session.messages.push({
    at: new Date().toISOString(),
    payload: isObject(payload) ? payload : {},
    text: inputText,
  });
  if (session.messages.length > ACP_MESSAGE_LIMIT) {
    session.messages.splice(0, session.messages.length - ACP_MESSAGE_LIMIT);
  }
  try {
    session.proc.stdin.write(inputText.endsWith("\n") ? inputText : `${inputText}\n`);
  } catch (error) {
    session.last_error = String(error?.message || error);
    session.running = false;
    session.updated_at = new Date().toISOString();
    return serializeAcpSession(session);
  }
  await settleSessionOutput(
    session,
    baselineActivity,
    Math.round(Math.max(0.25, state.acpSessionTimeoutSeconds) * 1000),
  );
  return serializeAcpSession(session, extractSessionOutput(session, stdoutIndex, stderrIndex));
}

async function closeAcpHarness(session) {
  if (!session || !session.proc || !session.running) {
    return serializeAcpSession(session || createAcpSession("missing", {}));
  }
  const proc = session.proc;
  await new Promise((resolve) => {
    let settled = false;
    const finalize = () => {
      if (settled) {
        return;
      }
      settled = true;
      resolve();
    };
    proc.once("exit", finalize);
    try {
      proc.kill();
    } catch (error) {
      finalize();
      return;
    }
    setTimeout(() => {
      if (settled) {
        return;
      }
      try {
        proc.kill("SIGKILL");
      } catch (error) {
        // Ignore kill races during shutdown.
      }
      finalize();
    }, ACP_SESSION_CLOSE_TIMEOUT_MS);
  });
  session.running = false;
  if (!session.closed_at) {
    session.closed_at = new Date().toISOString();
  }
  session.updated_at = session.closed_at;
  return serializeAcpSession(session);
}

function listInstalledPluginIds() {
  const pluginsRoot = safeJoin(state.root, "plugins");
  if (!fs.existsSync(pluginsRoot)) {
    return [];
  }
  return fs
    .readdirSync(pluginsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
}

function inspectInstalledPlugin(pluginId) {
  const pluginsRoot = safeJoin(state.root, "plugins");
  const pluginRoot = safeJoin(pluginsRoot, pluginId);
  if (!fs.existsSync(pluginRoot) || !fs.statSync(pluginRoot).isDirectory()) {
    return null;
  }
  const installMeta = readJsonIfExists(path.join(pluginRoot, INSTALL_META_FILENAME)) || {};
  return inspectPluginRoot(pluginRoot, installMeta);
}

async function ensureDirectory(targetPath) {
  await fsp.mkdir(targetPath, { recursive: true });
}

async function copyRecursive(sourcePath, destinationPath) {
  const stats = await fsp.stat(sourcePath);
  if (stats.isDirectory()) {
    await ensureDirectory(destinationPath);
    const entries = await fsp.readdir(sourcePath, { withFileTypes: true });
    for (const entry of entries) {
      await copyRecursive(
        path.join(sourcePath, entry.name),
        path.join(destinationPath, entry.name),
      );
    }
    return;
  }
  await ensureDirectory(path.dirname(destinationPath));
  await fsp.copyFile(sourcePath, destinationPath);
}

async function materializeInstallRoot(sourcePath, destinationRoot) {
  const stats = await fsp.stat(sourcePath);
  if (stats.isDirectory()) {
    const entries = await fsp.readdir(sourcePath, { withFileTypes: true });
    for (const entry of entries) {
      await copyRecursive(
        path.join(sourcePath, entry.name),
        path.join(destinationRoot, entry.name),
      );
    }
    return;
  }
  const fileName = path.basename(sourcePath);
  const targetName = fileName.toLowerCase() === "skill.md" ? "SKILL.md" : fileName;
  await copyRecursive(sourcePath, path.join(destinationRoot, targetName));
}

async function installPlugin(payload) {
  const sourcePath = path.resolve(String(payload.source_path || ""));
  const overwrite = Boolean(payload.overwrite !== false);
  const installMeta = {
    ...(payload.metadata && typeof payload.metadata === "object" ? payload.metadata : {}),
    installed_at: new Date().toISOString(),
  };
  const stats = await fsp.stat(sourcePath).catch(() => null);
  if (!stats) {
    throw new Error("source_path does not exist");
  }
  const previewRoot = stats.isDirectory() ? sourcePath : path.dirname(sourcePath);
  const preview = inspectPluginRoot(previewRoot, installMeta);
  const pluginsRoot = safeJoin(state.root, "plugins");
  const pluginRoot = safeJoin(pluginsRoot, preview.id);
  if (fs.existsSync(pluginRoot)) {
    if (!overwrite) {
      throw new Error(`plugin already exists: ${preview.id}`);
    }
    await fsp.rm(pluginRoot, { recursive: true, force: true });
  }
  await ensureDirectory(pluginRoot);
  await materializeInstallRoot(sourcePath, pluginRoot);
  await fsp.writeFile(
    path.join(pluginRoot, INSTALL_META_FILENAME),
    JSON.stringify(installMeta, null, 2),
    "utf-8",
  );
  return inspectInstalledPlugin(preview.id);
}

function summarizePlugins(plugins) {
  const summary = {
    total: plugins.length,
    wired: 0,
    detect_only: 0,
    unsupported: 0,
  };
  for (const plugin of plugins) {
    summary.wired += plugin.capability_counts?.wired || 0;
    summary.detect_only += plugin.capability_counts?.detect_only || 0;
    summary.unsupported += plugin.capability_counts?.unsupported || 0;
  }
  return summary;
}

function jsonResponse(response, statusCode, payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf-8");
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": String(body.length),
    "Cache-Control": "no-store, max-age=0",
  });
  response.end(body);
}

function parseRequestBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      if (!chunks.length) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf-8")));
      } catch (error) {
        reject(new Error("invalid json body"));
      }
    });
    request.on("error", reject);
  });
}

async function handleRequest(request, response) {
  const parsed = new url.URL(request.url, "http://127.0.0.1");
  try {
    if (request.method === "GET" && parsed.pathname === "/healthz") {
      const plugins = listInstalledPluginIds()
        .map((pluginId) => inspectInstalledPlugin(pluginId))
        .filter(Boolean);
      jsonResponse(response, 200, {
        status: "ok",
        version: RUNTIME_VERSION,
        runtime_root: state.root,
        routing_mode: state.routingMode,
        acp_enabled: state.acpEnabled,
        acp_default_configured: Boolean(trimString(state.acpDefault.command)),
        codex_harness_configured: Boolean(trimString(state.codexHarness.command)),
        acp_session_timeout_seconds: state.acpSessionTimeoutSeconds,
        acp_session_count: state.sessions.size,
        plugin_tools_mcp_bridge: state.pluginToolsMcpBridge,
        plugin_count: plugins.length,
      });
      return;
    }
    if (request.method === "GET" && parsed.pathname === "/plugins") {
      const plugins = listInstalledPluginIds()
        .map((pluginId) => inspectInstalledPlugin(pluginId))
        .filter(Boolean);
      jsonResponse(response, 200, {
        items: plugins,
        summary: summarizePlugins(plugins),
      });
      return;
    }
    if (request.method === "POST" && parsed.pathname === "/plugins/check") {
      const plugins = listInstalledPluginIds()
        .map((pluginId) => inspectInstalledPlugin(pluginId))
        .filter(Boolean);
      jsonResponse(response, 200, {
        items: plugins,
        summary: summarizePlugins(plugins),
      });
      return;
    }
    if (request.method === "POST" && parsed.pathname === "/plugins/install") {
      const payload = await parseRequestBody(request);
      const plugin = await installPlugin(payload);
      jsonResponse(response, 200, {
        status: "ok",
        plugin,
      });
      return;
    }
    if (request.method === "POST" && parsed.pathname === "/plugins/update") {
      const payload = await parseRequestBody(request);
      const pluginId = slugify(payload.plugin_id || "");
      if (!pluginId) {
        throw new Error("plugin_id is required");
      }
      const installed = inspectInstalledPlugin(pluginId);
      if (!installed) {
        throw new Error(`plugin not found: ${pluginId}`);
      }
      if (!payload.source_path) {
        jsonResponse(response, 200, {
          status: "ok",
          plugin: installed,
        });
        return;
      }
      const plugin = await installPlugin({
        source_path: payload.source_path,
        overwrite: true,
        metadata: {
          ...(payload.metadata && typeof payload.metadata === "object" ? payload.metadata : {}),
          source_id: payload.metadata?.source_id || installed.source_id,
          source_url: payload.metadata?.source_url || installed.source_url,
          bundle_url: payload.metadata?.bundle_url || installed.bundle_url,
          page_url: payload.metadata?.page_url || installed.page_url,
          plugin_id: installed.id,
        },
      });
      jsonResponse(response, 200, {
        status: "ok",
        plugin,
      });
      return;
    }
    if (request.method === "GET" && parsed.pathname.startsWith("/plugins/")) {
      const pluginId = slugify(decodeURIComponent(parsed.pathname.split("/")[2] || ""));
      const plugin = inspectInstalledPlugin(pluginId);
      if (!plugin) {
        jsonResponse(response, 404, { status: "not_found" });
        return;
      }
      jsonResponse(response, 200, plugin);
      return;
    }
    if (request.method === "GET" && parsed.pathname === "/skills") {
      const items = listInstalledPluginIds()
        .map((pluginId) => inspectInstalledPlugin(pluginId))
        .filter(Boolean)
        .flatMap((plugin) =>
          plugin.skills.map((skill) => ({
            ...skill,
            plugin_id: plugin.id,
            owner_runtime: "claw",
            routing_owner: plugin.owner_routing,
          })),
        );
      jsonResponse(response, 200, { items });
      return;
    }
    if (request.method === "GET" && parsed.pathname.startsWith("/skills/")) {
      const skillId = slugify(decodeURIComponent(parsed.pathname.split("/")[2] || ""));
      const items = listInstalledPluginIds()
        .map((pluginId) => inspectInstalledPlugin(pluginId))
        .filter(Boolean);
      for (const plugin of items) {
        const skill = plugin.skills.find((item) => item.id === skillId);
        if (!skill) {
          continue;
        }
        jsonResponse(response, 200, {
          ...skill,
          plugin_id: plugin.id,
          owner_runtime: "claw",
          routing_owner: plugin.owner_routing,
        });
        return;
      }
      jsonResponse(response, 404, { status: "not_found" });
      return;
    }
    if (request.method === "GET" && parsed.pathname === "/tools") {
      const items = await collectRuntimeTools();
      jsonResponse(response, 200, { items });
      return;
    }
    if (request.method === "POST" && parsed.pathname === "/tools/invoke") {
      const payload = await parseRequestBody(request);
      const result = await invokeRuntimeTool(payload);
      jsonResponse(response, 200, result);
      return;
    }
    if (request.method === "POST" && parsed.pathname === "/acp/sessions") {
      const payload = await parseRequestBody(request);
      const sessionId = `acp_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`;
      const session = createAcpSession(sessionId, payload);
      state.sessions.set(sessionId, session);
      await startAcpHarness(session);
      jsonResponse(response, 200, serializeAcpSession(session));
      return;
    }
    if (request.method === "POST" && /^\/acp\/sessions\/[^/]+\/input$/.test(parsed.pathname)) {
      const sessionId = decodeURIComponent(parsed.pathname.split("/")[3] || "");
      const payload = await parseRequestBody(request);
      const session = state.sessions.get(sessionId);
      if (!session) {
        jsonResponse(response, 404, { status: "not_found" });
        return;
      }
      const result = await sendAcpInput(session, payload);
      jsonResponse(response, 200, result);
      return;
    }
    if (request.method === "DELETE" && parsed.pathname.startsWith("/acp/sessions/")) {
      const sessionId = decodeURIComponent(parsed.pathname.split("/")[3] || "");
      const session = state.sessions.get(sessionId);
      const result = await closeAcpHarness(session);
      state.sessions.delete(sessionId);
      jsonResponse(response, 200, result);
      return;
    }
    jsonResponse(response, 404, { status: "not_found" });
  } catch (error) {
    jsonResponse(response, 400, {
      status: "error",
      reason: String(error?.message || error),
    });
  }
}

const args = parseArgs(process.argv.slice(2));
const port = Number.parseInt(String(args.port || "0"), 10);
if (!Number.isFinite(port) || port <= 0) {
  throw new Error("--port is required");
}

const runtimeRoot = path.resolve(String(args.root || process.cwd()));
const state = {
  root: runtimeRoot,
  routingMode: String(args.routingMode || "shadow"),
  acpEnabled: asBool(args.acpEnabled, false),
  acpDefault: {
    command: trimString(args.acpDefaultCommand),
    args: parseStringListArg(args.acpDefaultArgs),
  },
  codexHarness: {
    command: trimString(args.codexHarnessCommand),
    args: parseStringListArg(args.codexHarnessArgs),
  },
  acpSessionTimeoutSeconds: clampNumber(args.acpSessionTimeoutSeconds, 2.0, 0.25, 30.0),
  pluginToolsMcpBridge: asBool(args.pluginToolsMcpBridge, false),
  sessions: new Map(),
  mcpToolCache: new Map(),
  listenPort: port,
};

await ensureDirectory(safeJoin(runtimeRoot, "plugins"));

const server = http.createServer((request, response) => {
  handleRequest(request, response);
});

async function shutdownAcpSessions() {
  const sessions = [...state.sessions.values()];
  await Promise.all(
    sessions.map(async (session) => {
      try {
        await closeAcpHarness(session);
      } catch (error) {
        // Ignore cleanup races during process shutdown.
      }
    }),
  );
}

process.on("SIGINT", async () => {
  await shutdownAcpSessions();
  process.exit(0);
});

process.on("SIGTERM", async () => {
  await shutdownAcpSessions();
  process.exit(0);
});

server.listen(port, "127.0.0.1", () => {
  const address = server.address();
  const resolvedPort = typeof address === "object" && address ? address.port : port;
  state.listenPort = resolvedPort;
  process.stdout.write(
    JSON.stringify({
      status: "listening",
      port: resolvedPort,
      runtime_root: runtimeRoot,
      version: RUNTIME_VERSION,
      pid: process.pid,
      host: "127.0.0.1",
      hostname: os.hostname(),
    }) + "\n",
  );
});
