// Toasts, modal helpers, clipboard helpers.

import { t } from "./i18n.js";

const ICONS = {
  info: svgIcon("M12 16v-4M12 8h.01", "circle"),
  ok: svgIcon("M20 6L9 17l-5-5"),
  warn: svgIcon("M12 9v4M12 17h.01 M10.29 3.86l-8.18 14.18A2 2 0 0 0 3.84 21h16.32a2 2 0 0 0 1.73-2.96L13.71 3.86a2 2 0 0 0-3.42 0z"),
  danger: svgIcon("M18 6L6 18 M6 6l12 12"),
};

function svgIcon(path) {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${path}" /></svg>`;
}

let toastContainer = null;

function ensureToastContainer() {
  if (toastContainer) return toastContainer;
  toastContainer = document.getElementById("toast-container");
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.className = "toast-container";
    toastContainer.setAttribute("aria-live", "polite");
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

export function toast({ title = "", message = "", kind = "info", timeout = 4200 }) {
  const container = ensureToastContainer();
  const el = document.createElement("div");
  el.className = `toast is-${kind}`;
  const icon = document.createElement("div");
  icon.className = "toast-icon";
  icon.innerHTML = ICONS[kind] || ICONS.info;
  const body = document.createElement("div");
  body.className = "toast-body";
  if (title) {
    const titleEl = document.createElement("div");
    titleEl.className = "toast-title";
    titleEl.textContent = title;
    body.appendChild(titleEl);
  }
  if (message) {
    const msgEl = document.createElement("div");
    msgEl.className = "toast-msg";
    msgEl.textContent = message;
    body.appendChild(msgEl);
  }
  el.appendChild(icon);
  el.appendChild(body);
  container.appendChild(el);
  requestAnimationFrame(() => el.classList.add("is-visible"));
  const dismiss = () => {
    el.classList.remove("is-visible");
    setTimeout(() => {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 300);
  };
  if (timeout > 0) setTimeout(dismiss, timeout);
  el.addEventListener("click", dismiss);
  return dismiss;
}

export function toastOk(message, title = "") {
  return toast({ kind: "ok", title, message });
}
export function toastError(message, title = "") {
  return toast({ kind: "danger", title, message });
}
export function toastInfo(message, title = "") {
  return toast({ kind: "info", title, message });
}
export function toastWarn(message, title = "") {
  return toast({ kind: "warn", title, message });
}

let modalRoot = null;
function ensureModalRoot() {
  if (modalRoot) return modalRoot;
  modalRoot = document.getElementById("modal-root");
  if (!modalRoot) {
    modalRoot = document.createElement("div");
    modalRoot.id = "modal-root";
    modalRoot.className = "modal-overlay";
    modalRoot.hidden = true;
    document.body.appendChild(modalRoot);
  }
  return modalRoot;
}

export function openModal({ title, body, actions }) {
  return new Promise((resolve) => {
    const root = ensureModalRoot();
    const modal = document.createElement("div");
    let closed = false;
    modal.className = "modal";
    modal.innerHTML = `
      <div class="modal-header">
        <div class="modal-title"></div>
        <button type="button" class="icon-btn" data-dismiss>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body"></div>
      <div class="modal-footer"></div>`;
    modal.querySelector(".modal-title").textContent = title || "";
    const bodyEl = modal.querySelector(".modal-body");
    if (typeof body === "string") {
      bodyEl.textContent = body;
    } else if (body instanceof Node) {
      bodyEl.appendChild(body);
    }
    const footer = modal.querySelector(".modal-footer");
    const actionList = actions || [
      { label: t("common.cancel"), value: false },
      { label: t("common.confirm"), value: true, variant: "primary" },
    ];
    actionList.forEach((action) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `btn${action.variant === "primary" ? " is-primary" : ""}${action.variant === "danger" ? " is-danger" : ""}`;
      btn.textContent = action.label;
      btn.addEventListener("click", () => finish(action.value));
      footer.appendChild(btn);
    });

    root.innerHTML = "";
    root.appendChild(modal);
    root.hidden = false;
    requestAnimationFrame(() => root.classList.add("is-open"));

    function finish(value) {
      if (closed) return;
      closed = true;
      root.classList.remove("is-open");
      setTimeout(() => {
        root.hidden = true;
        root.innerHTML = "";
      }, 200);
      document.removeEventListener("keydown", keyHandler);
      root.removeEventListener("click", clickHandler);
      resolve(value);
    }

    function keyHandler(e) {
      if (e.key === "Escape") finish(null);
    }

    function clickHandler(e) {
      if (e.target === root || e.target.closest("[data-dismiss]")) finish(null);
    }

    document.addEventListener("keydown", keyHandler);
    root.addEventListener("click", clickHandler);
  });
}

export async function confirmDialog({ title, message, danger = false }) {
  const bodyEl = document.createElement("div");
  bodyEl.style.fontSize = "13px";
  bodyEl.style.lineHeight = "1.55";
  bodyEl.textContent = message;
  const result = await openModal({
    title,
    body: bodyEl,
    actions: [
      { label: t("common.cancel"), value: false },
      { label: t("common.confirm"), value: true, variant: danger ? "danger" : "primary" },
    ],
  });
  return result === true;
}

export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    toastOk(t("common.copied"));
    return true;
  } catch (err) {
    toastError(String(err?.message || err));
    return false;
  }
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "-";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h || d) parts.push(`${h}h`);
  if (m || h || d) parts.push(`${m}m`);
  parts.push(`${s}s`);
  return parts.join(" ");
}

export function formatTimestamp(ts) {
  if (!ts) return "-";
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleTimeString([], { hour12: false });
}
