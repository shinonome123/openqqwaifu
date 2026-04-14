// User page: current account, role and password management.

import { api } from "../api.js";
import { el, card, fieldRow, textInput, chip, empty } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let panel = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      panel = await api.getUserPanel();
      if (stopped) return;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  async function onLogout() {
    const ok = await confirmDialog({
      title: t("user.logout"),
      message: t("user.logoutConfirm"),
      danger: false,
    });
    if (!ok) return;
    try {
      await api.logout();
      window.location.reload();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  async function onChangePassword(currentInput, nextInput, confirmInput) {
    const currentPassword = currentInput.value;
    const newPassword = nextInput.value;
    const confirmPassword = confirmInput.value;
    if (!newPassword || newPassword !== confirmPassword) {
      toastError(t("user.passwordMismatch"));
      return;
    }
    try {
      await api.changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      currentInput.value = "";
      nextInput.value = "";
      confirmInput.value = "";
      toastOk(t("user.passwordUpdated"));
      await load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  function render() {
    container.innerHTML = "";
    const currentUser = panel?.current_user || null;
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.user.title") }),
          el("div", { class: "page-desc", text: t("page.user.desc") }),
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
            class: "btn is-danger",
            text: t("user.logout"),
            onClick: onLogout,
          }),
        ]),
      ]),
    );

    if (!currentUser) {
      container.appendChild(empty({ title: t("user.empty") }));
      return;
    }

    container.appendChild(
      el("div", { class: "user-grid" }, [
        renderProfileCard(currentUser, panel?.users || []),
        renderPasswordCard(onChangePassword),
      ]),
    );
  }

  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}

function renderProfileCard(currentUser, users) {
  const initial = String(currentUser.username || "U").slice(0, 1).toUpperCase();
  const rows = [
    fieldRow({
      label: t("user.username"),
      control: el("div", { class: "muted text-sm", text: currentUser.username }),
    }),
    fieldRow({
      label: t("user.role"),
      control: chip({
        label: roleLabel(currentUser.role),
        variant: currentUser.role === "admin" ? "accent" : "outline",
      }),
    }),
    fieldRow({
      label: t("user.createdAt"),
      control: el("div", { class: "muted text-sm", text: formatDateTime(currentUser.created_at) }),
    }),
    fieldRow({
      label: t("user.lastLoginAt"),
      control: el("div", { class: "muted text-sm", text: formatDateTime(currentUser.last_login_at) }),
    }),
  ];

  if (Array.isArray(users) && users.length) {
    rows.push(
      fieldRow({
        label: t("user.knownUsers"),
        control: el(
          "div",
          { class: "user-meta" },
          users.map((item) =>
            chip({
              label: `${item.username} · ${roleLabel(item.role)}`,
              variant: item.role === "admin" ? "accent" : "outline",
            }),
          ),
        ),
      }),
    );
  }

  return card({
    title: t("user.profile"),
    subtitle: t("user.profileDesc"),
    body: [
      el("div", { class: "user-identity" }, [
        el("div", { class: "user-avatar", text: initial }),
        el("div", {}, [
          el("div", { class: "user-name", text: currentUser.username }),
          el("div", { class: "muted text-sm", text: t("user.profileHint") }),
        ]),
      ]),
      ...rows,
    ],
  });
}

function renderPasswordCard(onChangePassword) {
  const currentInput = textInput({ type: "password", value: "" });
  const nextInput = textInput({ type: "password", value: "" });
  const confirmInput = textInput({ type: "password", value: "" });

  return card({
    title: t("user.passwordTitle"),
    subtitle: t("user.passwordDesc"),
    body: [
      fieldRow({
        label: t("user.currentPassword"),
        control: currentInput,
      }),
      fieldRow({
        label: t("user.newPassword"),
        control: nextInput,
      }),
      fieldRow({
        label: t("user.confirmPassword"),
        control: confirmInput,
      }),
      el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "center" } }, [
        el("div", { class: "muted text-sm", text: t("user.passwordHint") }),
        el("button", {
          type: "button",
          class: "btn is-primary",
          text: t("user.updatePassword"),
          onClick: () => onChangePassword(currentInput, nextInput, confirmInput),
        }),
      ]),
    ],
  });
}

function roleLabel(role) {
  return role === "admin" ? t("role.admin") : t("role.user");
}

function formatDateTime(value) {
  const seconds = Number(value || 0);
  if (!seconds) return t("common.none");
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return t("common.none");
  return date.toLocaleString();
}
