// Entrypoint: auth gate, navigation shell and page routing.

import { createRouter } from "./router.js";
import { applyI18nTo, setLang, getLang, onLangChange, t } from "./i18n.js";
import { toggleThemeMode, syncThemeToggleUI } from "./theme.js";
import { api } from "./api.js";
import { el, fieldRow, textInput } from "./components.js";
import { toastError } from "./ui.js";

import * as overview from "./pages/overview.js";
import * as character from "./pages/character.js";
import * as ai from "./pages/ai.js";
import * as memory from "./pages/memory.js";
import * as skills from "./pages/skills.js";
import * as abilities from "./pages/abilities.js";
import * as sidecar from "./pages/sidecar.js";
import * as qqLogin from "./pages/qq_login.js";
import * as events from "./pages/events.js";
import * as advanced from "./pages/advanced.js";
import * as userPage from "./pages/user.js";
import * as members from "./pages/members.js";

const ROUTES = [
  {
    id: "overview",
    group: "control",
    labelKey: "nav.overview",
    icon: iconPath("M3 12l2-2 4 4 8-8 2 2-10 10z"),
    mount: overview.mount,
  },
  {
    id: "events",
    group: "control",
    labelKey: "nav.events",
    icon: iconPath("M4 6h16M4 12h16M4 18h7"),
    mount: events.mount,
  },
  {
    id: "character",
    group: "identity",
    labelKey: "nav.character",
    icon: iconPath("M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z"),
    mount: character.mount,
  },
  {
    id: "user",
    group: "identity",
    labelKey: "nav.user",
    icon: iconPath("M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4 21a8 8 0 0 1 16 0"),
    mount: userPage.mount,
  },
  {
    id: "members",
    group: "identity",
    labelKey: "nav.members",
    icon: iconPath("M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75"),
    mount: members.mount,
  },
  {
    id: "ai",
    group: "intelligence",
    labelKey: "nav.ai",
    icon: iconPath("M12 3a9 9 0 1 0 9 9h-9V3z M12 3v9h9a9 9 0 0 0-9-9z"),
    mount: ai.mount,
  },
  {
    id: "memory",
    group: "intelligence",
    labelKey: "nav.memory",
    icon: iconPath("M21 15a4 4 0 0 1-4 4H7l-4 4V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"),
    mount: memory.mount,
  },
  {
    id: "abilities",
    group: "behavior",
    labelKey: "nav.abilities",
    icon: iconPath("M13 2L3 14h9l-1 8 10-12h-9l1-8z"),
    mount: abilities.mount,
  },
  {
    id: "skills",
    group: "behavior",
    labelKey: "nav.skills",
    icon: iconPath("M12 2l3 7h7l-5.5 4.5L18 21l-6-4-6 4 1.5-7.5L2 9h7z"),
    mount: skills.mount,
  },
  {
    id: "qq-login",
    group: "runtime",
    labelKey: "nav.qqLogin",
    icon: iconPath("M12 2a10 10 0 1 0 10 10 M8 12h8 M12 8v8"),
    mount: qqLogin.mount,
  },
  {
    id: "sidecar",
    group: "runtime",
    labelKey: "nav.sidecar",
    icon: iconPath("M4 4h16v4H4z M4 10h16v4H4z M4 16h16v4H4z"),
    mount: sidecar.mount,
  },
  {
    id: "advanced",
    group: "system",
    labelKey: "nav.advanced",
    icon: iconPath("M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"),
    mount: advanced.mount,
  },
];

function iconPath(d) {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${d}" /></svg>`;
}

function buildNav(onNavigate, currentId) {
  const nav = document.getElementById("nav");
  if (!nav) return;
  nav.innerHTML = "";

  const groupsOrder = ["control", "identity", "intelligence", "behavior", "runtime", "system"];
  const byGroup = new Map();
  ROUTES.forEach((route) => {
    const list = byGroup.get(route.group) || [];
    list.push(route);
    byGroup.set(route.group, list);
  });

  groupsOrder.forEach((group) => {
    const list = byGroup.get(group);
    if (!list) return;
    const section = document.createElement("div");
    section.className = "nav-group";
    const label = document.createElement("div");
    label.className = "nav-group-label";
    label.dataset.i18n = `nav.group.${group}`;
    label.textContent = t(`nav.group.${group}`);
    section.appendChild(label);

    list.forEach((route) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `nav-item${route.id === currentId ? " is-active" : ""}`;
      btn.dataset.route = route.id;
      btn.innerHTML = `<span class="nav-icon">${route.icon}</span><span class="nav-item-label" data-i18n="${route.labelKey}">${t(route.labelKey)}</span>`;
      btn.addEventListener("click", () => onNavigate(route.id));
      section.appendChild(btn);
    });

    nav.appendChild(section);
  });
}

function setTopbarForRoute(route) {
  const pageTitle = document.getElementById("page-title");
  const crumb = document.getElementById("page-crumb");
  if (pageTitle) pageTitle.textContent = t(route.labelKey);
  if (crumb) crumb.textContent = t(`nav.group.${route.group}`);
  document.title = `${t(route.labelKey)} · ${t("brand.name")}`;
}

function setActiveNav(id) {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.route === id);
  });
}

async function pollHealth() {
  const dot = document.getElementById("health-dot");
  const label = document.getElementById("health-text");
  if (!dot || !label) return;
  try {
    await api.healthz();
    dot.className = "sidebar-footer-dot is-ok";
    label.textContent = t("sidebar.health.ok");
  } catch (err) {
    dot.className = "sidebar-footer-dot is-danger";
    label.textContent = t("sidebar.health.down");
  }
}

function initSidebarToggle() {
  const mobileToggle = document.getElementById("sidebar-toggle");
  const app = document.getElementById("app");
  const sidebar = document.getElementById("sidebar");
  const scrim = document.getElementById("sidebar-scrim");
  if (!app || !sidebar || !scrim) return;

  if (mobileToggle) {
    mobileToggle.addEventListener("click", () => {
      sidebar.classList.toggle("is-open");
      scrim.classList.toggle("is-visible");
    });
  }
  scrim.addEventListener("click", () => {
    sidebar.classList.remove("is-open");
    scrim.classList.remove("is-visible");
  });
}

function initLangPicker() {
  const picker = document.querySelector(".lang-picker");
  if (!picker) return;
  picker.querySelectorAll("button[data-lang]").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.lang === getLang());
    btn.addEventListener("click", () => {
      setLang(btn.dataset.lang);
      picker.querySelectorAll("button[data-lang]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.lang === getLang());
      });
    });
  });
}

function initThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  syncThemeToggleUI(btn);
  btn.addEventListener("click", () => {
    toggleThemeMode();
    syncThemeToggleUI(btn);
  });
}

function initTopbarUser(user, router) {
  const btn = document.getElementById("topbar-user");
  if (!btn || !user) return;
  btn.hidden = false;
  btn.textContent = `${user.username} · ${roleLabel(user.role)}`;
  btn.addEventListener("click", () => router.navigate("user"));
}

async function main() {
  applyI18nTo(document.body);

  let authState = null;
  try {
    authState = await api.authState();
  } catch (err) {
    renderAuthScreen({
      authenticated: false,
      requires_setup: false,
      load_error: err?.message || String(err),
    });
    return;
  }

  if (!authState?.authenticated) {
    renderAuthScreen(authState || {});
    return;
  }

  mountConsole(authState.user);
}

function mountConsole(currentUser) {
  const app = document.getElementById("app");
  const outlet = document.getElementById("page-outlet");
  if (!app || !outlet) return;
  app.className = "app";

  const router = createRouter(ROUTES, "overview");
  let healthTimer = null;
  let currentTeardown = null;

  buildNav((id) => router.navigate(id), router.current());
  applyI18nTo(document.body);
  initSidebarToggle();
  initLangPicker();
  initThemeToggle();
  initTopbarUser(currentUser, router);

  router.onChange((route) => {
    if (typeof currentTeardown === "function") {
      try {
        currentTeardown();
      } catch (err) {
        console.error(err);
      }
    }
    currentTeardown = null;
    setActiveNav(route.id);
    setTopbarForRoute(route);
    if (typeof route.mount === "function") {
      try {
        currentTeardown = route.mount(outlet) || null;
      } catch (err) {
        toastError(err?.message || String(err));
      }
    }
  });

  router.start();

  onLangChange(() => {
    const route = router.route(router.current());
    if (route) setTopbarForRoute(route);
    document.querySelectorAll(".nav-group-label").forEach((node) => {
      if (node.dataset.i18n) node.textContent = t(node.dataset.i18n);
    });
    document.querySelectorAll(".nav-item-label").forEach((node) => {
      if (node.dataset.i18n) node.textContent = t(node.dataset.i18n);
    });
    const topbarUser = document.getElementById("topbar-user");
    if (topbarUser && currentUser) {
      topbarUser.textContent = `${currentUser.username} · ${roleLabel(currentUser.role)}`;
    }
  });

  pollHealth();
  healthTimer = window.setInterval(pollHealth, 10_000);

  const saveBtn = document.getElementById("save-config");
  if (saveBtn) {
    saveBtn.addEventListener("click", () => {
      const active = document.querySelector(".page-actions .btn.is-primary");
      if (active && active !== saveBtn) active.click();
    });
  }

  return () => {
    if (healthTimer) window.clearInterval(healthTimer);
  };
}

function renderAuthScreen(state) {
  document.title = `${t("auth.title")} \u00b7 ${t("brand.name")}`;
  const app = document.getElementById("app");
  if (!app) return;
  app.className = "auth-shell";
  app.innerHTML = "";

  const requiresSetup = !!state?.requires_setup;
  const loadError = String(state?.load_error || "");

  const usernameInput = textInput({
    value: requiresSetup ? "admin" : "",
    placeholder: t("auth.usernamePlaceholder"),
  });
  const passwordInput = textInput({
    type: "password",
    value: "",
    placeholder: t("auth.passwordPlaceholder"),
  });
  const confirmInput = textInput({
    type: "password",
    value: "",
    placeholder: t("auth.confirmPasswordPlaceholder"),
  });
  const errorBox = el("div", { class: "auth-error", text: loadError || "", hidden: !loadError });
  const submitBtn = el("button", {
    type: "submit",
    class: "btn is-primary is-lg auth-submit",
    text: requiresSetup ? t("auth.setupAction") : t("auth.loginAction"),
  });

  const formFields = [
    el("label", { class: "auth-field" }, [
      el("span", { class: "auth-field-label", text: t("auth.username") }),
      usernameInput,
    ]),
    el("label", { class: "auth-field" }, [
      el("span", { class: "auth-field-label", text: t("auth.password") }),
      passwordInput,
    ]),
  ];
  if (requiresSetup) {
    formFields.push(
      el("label", { class: "auth-field" }, [
        el("span", { class: "auth-field-label", text: t("auth.confirmPassword") }),
        confirmInput,
      ]),
    );
  }

  const form = el(
    "form",
    {
      class: "auth-form",
      onSubmit: async (event) => {
        event.preventDefault();
        errorBox.hidden = true;
        errorBox.textContent = "";
        const payload = {
          username: usernameInput.value.trim(),
          password: passwordInput.value,
        };
        if (requiresSetup && payload.password !== confirmInput.value) {
          errorBox.hidden = false;
          errorBox.textContent = t("auth.passwordMismatch");
          return;
        }
        submitBtn.disabled = true;
        try {
          if (requiresSetup) {
            await api.bootstrapAuth(payload);
          } else {
            await api.login(payload);
          }
          window.location.reload();
        } catch (err) {
          errorBox.hidden = false;
          errorBox.textContent = err?.message || String(err);
        } finally {
          submitBtn.disabled = false;
        }
      },
    },
    [errorBox, ...formFields, submitBtn],
  );

  const card = el("section", { class: "auth-card" }, [
    el("div", { class: "auth-card-head" }, [
      el("div", { class: "auth-card-mark" }, [
        el("img", {
          class: "auth-card-logo",
          src: "/assets/img/brand-logo.png",
          alt: "",
        }),
      ]),
    ]),
    el("div", { class: "auth-card-intro" }, [
      el("div", { class: "auth-card-meta" }, [
        el("div", { class: "auth-card-kicker", text: t("auth.kicker") }),
        el("div", { class: "auth-card-brand", text: t("brand.name") }),
      ]),
      el("h1", {
        class: "auth-card-title",
        text: requiresSetup ? t("auth.setupTitle") : t("auth.loginTitle"),
      }),
      el("p", {
        class: "auth-card-desc",
        text: requiresSetup ? t("auth.setupDesc") : t("auth.loginDesc"),
      }),
    ]),
    form,
    el("div", {
      class: "auth-card-footer",
      text: requiresSetup ? t("auth.setupNote") : t("auth.loginNote"),
    }),
  ]);

  app.appendChild(
    el("div", { class: "auth-stage" }, [
      el("div", { class: "auth-bg-tile", "aria-hidden": "true" }),
      el("div", { class: "auth-orb auth-orb-a", "aria-hidden": "true" }),
      el("div", { class: "auth-orb auth-orb-b", "aria-hidden": "true" }),
      el("div", { class: "auth-orb auth-orb-c", "aria-hidden": "true" }),
      card,
    ]),
  );

  setTimeout(() => {
    if (requiresSetup) usernameInput.focus();
    else if (usernameInput.value) passwordInput.focus();
    else usernameInput.focus();
  }, 0);
}

function roleLabel(role) {
  return role === "admin" ? t("role.admin") : t("role.user");
}

document.addEventListener("DOMContentLoaded", () => {
  main().catch((err) => {
    console.error(err);
    renderAuthScreen({
      authenticated: false,
      requires_setup: false,
      load_error: err?.message || String(err),
    });
  });
});
