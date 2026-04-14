import { api } from "../api.js";
import { el, card, fieldRow, textInput, textarea, select, chip, empty, numberInput } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog } from "../ui.js";

const ONBOARDING_OPTIONS = [
  { value: "new", labelKey: "user.directory.status.new" },
  { value: "pending_name", labelKey: "user.directory.status.pending" },
  { value: "ready", labelKey: "user.directory.status.ready" },
];

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let panel = null;
  let selectedKey = "";
  let memberDraft = null;
  let syncGroupId = "";

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      panel = await api.getUserPanel();
      const members = panel?.members || [];
      if (!selectedKey && members.length) {
        selectMember(members[0]);
      } else if (selectedKey) {
        const next = members.find((item) => memberKey(item) === selectedKey);
        if (next) {
          selectMember(next);
        } else if (members.length) {
          selectMember(members[0]);
        } else {
          memberDraft = null;
          selectedKey = "";
        }
      }
      if (!stopped) render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  function selectMember(member) {
    selectedKey = memberKey(member);
    memberDraft = member ? { ...member } : null;
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

  async function onSaveMember() {
    if (!memberDraft?.user_id) {
      toastError(t("user.directory.select"));
      return;
    }
    try {
      const result = await api.saveDirectoryMember(memberDraft);
      selectMember(result?.member || memberDraft);
      toastOk(t("user.directory.saved"));
      await load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  async function onSyncDirectory() {
    if (!syncGroupId.trim()) {
      toastError(t("user.directory.groupIdRequired"));
      return;
    }
    try {
      const result = await api.syncDirectoryGroup(syncGroupId.trim());
      toastOk(t("user.directory.synced", { count: result?.count ?? 0 }));
      await load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  function render() {
    container.innerHTML = "";
    const currentUser = panel?.current_user || null;
    const members = panel?.members || [];
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
    container.appendChild(
      el("div", { class: "overview-split", style: { marginTop: "16px" } }, [
        renderDirectoryList(members),
        renderMemberEditor(),
      ]),
    );
  }

  function renderDirectoryList(members) {
    const syncInput = textInput({
      value: syncGroupId,
      placeholder: t("user.directory.groupIdPlaceholder"),
      onInput: (value) => {
        syncGroupId = value;
      },
    });
    const tbody = el("tbody");
    members.forEach((member) => {
      const active = memberKey(member) === selectedKey;
      const displayName = member.preferred_name || member.group_card || member.qq_nickname || member.user_id;
      tbody.appendChild(
        el(
          "tr",
          {
            style: {
              cursor: "pointer",
              background: active ? "var(--accent-subtle)" : "transparent",
            },
            onClick: () => {
              selectMember(member);
              render();
            },
          },
          [
            el("td", {}, [
              el("div", { text: displayName }),
              el("div", { class: "session-id", text: `${member.group_id || "person"}:${member.user_id}` }),
            ]),
            el("td", {}, [chip({ label: member.onboarding_status || "new", variant: member.preferred_name ? "ok" : "outline" })]),
            el("td", { class: "session-meta", text: formatDateTime(member.last_seen_at) }),
          ],
        ),
      );
    });
    const table = members.length
      ? el("div", { class: "table-wrap" }, [
          el("table", { class: "table session-table" }, [
            el("thead", {}, [
              el("tr", {}, [
                el("th", { text: t("user.directory.member") }),
                el("th", { text: t("user.directory.status") }),
                el("th", { text: t("user.directory.lastSeen") }),
              ]),
            ]),
            tbody,
          ]),
        ])
      : empty({ title: t("user.directory.empty") });

    return card({
      title: `${t("user.directory.title")} (${panel?.member_count ?? members.length})`,
      subtitle: t("user.directory.desc"),
      body: [
        el("div", { class: "row" }, [
          syncInput,
          el("button", {
            type: "button",
            class: "btn",
            text: t("user.directory.sync"),
            onClick: onSyncDirectory,
          }),
        ]),
        table,
      ],
    });
  }

  function renderMemberEditor() {
    if (!memberDraft) {
      return card({
        title: t("user.directory.editor"),
        body: [empty({ title: t("user.directory.select") })],
      });
    }
    return card({
      title: t("user.directory.editor"),
      subtitle: memberDraft.preferred_name || memberDraft.group_card || memberDraft.qq_nickname || memberDraft.user_id,
      actions: [
        el("button", {
          type: "button",
          class: "btn is-primary",
          text: t("common.save"),
          onClick: onSaveMember,
        }),
      ],
      body: [
        fieldRow({
          label: t("user.directory.groupId"),
          control: textInput({
            value: memberDraft.group_id || "",
            onChange: (value) => {
              memberDraft.group_id = value.trim();
            },
          }),
        }),
        fieldRow({
          label: t("user.directory.userId"),
          control: textInput({
            value: memberDraft.user_id || "",
            onChange: (value) => {
              memberDraft.user_id = value.trim();
            },
          }),
        }),
        fieldRow({
          label: t("user.directory.qqNickname"),
          control: textInput({
            value: memberDraft.qq_nickname || "",
            onChange: (value) => {
              memberDraft.qq_nickname = value.trim();
            },
          }),
        }),
        fieldRow({
          label: t("user.directory.groupCard"),
          control: textInput({
            value: memberDraft.group_card || "",
            onChange: (value) => {
              memberDraft.group_card = value.trim();
            },
          }),
        }),
        fieldRow({
          label: t("user.directory.preferredName"),
          hint: t("user.directory.preferredNameHint"),
          control: textInput({
            value: memberDraft.preferred_name || "",
            onChange: (value) => {
              memberDraft.preferred_name = value.trim();
            },
          }),
        }),
        fieldRow({
          label: t("user.directory.status"),
          control: select({
            value: memberDraft.onboarding_status || "new",
            options: ONBOARDING_OPTIONS.map((item) => ({ value: item.value, label: t(item.labelKey) })),
            onChange: (value) => {
              memberDraft.onboarding_status = value;
            },
          }),
        }),
        fieldRow({
          label: t("user.directory.profileSummary"),
          control: textarea({
            rows: 4,
            value: memberDraft.profile_summary || "",
            onChange: (value) => {
              memberDraft.profile_summary = value.trim();
            },
          }),
        }),
        fieldRow({
          label: t("user.directory.notesCount"),
          control: numberInput({
            value: Number(memberDraft.notes_count || 0),
            min: 0,
            step: 1,
            onChange: (value) => {
              memberDraft.notes_count = value;
            },
          }),
        }),
        el("div", { class: "user-meta" }, [
          chip({ label: `${t("user.directory.lastSeen")}: ${formatDateTime(memberDraft.last_seen_at)}`, variant: "outline" }),
          chip({ label: `${t("user.directory.lastAddressed")}: ${formatDateTime(memberDraft.last_addressed_at)}`, variant: "outline" }),
        ]),
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

function memberKey(member) {
  return `${member?.group_id || ""}:${member?.user_id || ""}`;
}
