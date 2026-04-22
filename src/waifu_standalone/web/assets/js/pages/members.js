import { api } from "../api.js";
import { el, card, fieldRow, textInput, textarea, select, chip, empty, numberInput } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, confirmDialog } from "../ui.js";

const ONBOARDING_OPTIONS = [
  { value: "new", labelKey: "user.directory.status.new" },
  { value: "asked_once", labelKey: "user.directory.status.askedOnce" },
  { value: "pending_name", labelKey: "user.directory.status.pending" },
  { value: "ready", labelKey: "user.directory.status.ready" },
];

const MEMBERSHIP_OPTIONS = [
  { value: "active", labelKey: "user.directory.membership.active" },
  { value: "left", labelKey: "user.directory.membership.left" },
  { value: "removed", labelKey: "user.directory.membership.removed" },
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

  async function onResetPersona() {
    if (!memberDraft?.user_id) {
      toastError(t("user.directory.select"));
      return;
    }
    const ok = await confirmDialog({
      title: t("user.directory.resetPersona"),
      message: t("user.directory.resetPersonaConfirm"),
      danger: true,
    });
    if (!ok) return;
    try {
      const result = await api.resetDirectoryMemberPersona(memberDraft.group_id || "", memberDraft.user_id);
      selectMember(result?.member || memberDraft);
      toastOk(t("user.directory.personaReset"));
      await load();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  function render() {
    container.innerHTML = "";
    const members = panel?.members || [];
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.members.title") }),
          el("div", { class: "page-desc", text: t("page.members.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          chip({
            label: `${t("user.directory.title")}: ${panel?.member_count ?? members.length}`,
            variant: "outline",
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

    container.appendChild(
      el("div", { class: "overview-split" }, [
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
    const table = members.length
      ? el(
          "div",
          { class: "member-group-stack" },
          groupMembersByGroup(members).map((section) => renderMemberGroup(section)),
        )
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

  function renderMemberGroup(section) {
    const title = section.group_id
      ? `${t("character.preview.mode.group")} ${section.group_id}`
      : t("character.preview.mode.person");
    const subtitle = `${section.members.length} ${t("user.directory.member")}`;
    return el("div", { class: "member-group-section" }, [
      el("div", { class: "member-group-header" }, [
        el("div", { class: "member-group-title-wrap" }, [
          el("div", { class: "member-group-title", text: title }),
          el("div", { class: "member-group-subtitle", text: subtitle }),
        ]),
        chip({ label: String(section.members.length), variant: "outline" }),
      ]),
      renderMemberTable(section.members),
    ]);
  }

  function renderMemberTable(groupMembers) {
    const tbody = el("tbody");
    groupMembers.forEach((member) => {
      const active = memberKey(member) === selectedKey;
      const displayName =
        member.preferred_name || member.group_card || member.qq_nickname || member.user_id;
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
              el("div", {
                class: "session-id",
                text: `${member.group_id || "person"}:${member.user_id}`,
              }),
            ]),
            el("td", {}, [
              chip({
                label: onboardingLabel(member.onboarding_status),
                variant: member.preferred_name ? "ok" : "outline",
              }),
            ]),
            el("td", {}, [
              chip({
                label: member.membership_status || "active",
                variant:
                  (member.membership_status || "active") === "active" ? "ok" : "outline",
              }),
            ]),
            el("td", {
              class: "session-meta",
              text: `${Number(member.affinity_score || 0).toFixed(2)} / ${bondStage(member.affinity_score)}`,
            }),
            el("td", {
              class: "session-meta",
              text: formatDateTime(member.last_seen_at),
            }),
          ],
        ),
      );
    });
    return el("div", { class: "table-wrap" }, [
      el("table", { class: "table session-table" }, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { text: t("user.directory.member") }),
            el("th", { text: t("user.directory.status") }),
            el("th", { text: t("user.directory.membershipStatus") }),
            el("th", { text: t("user.directory.affinity") }),
            el("th", { text: t("user.directory.lastSeen") }),
          ]),
        ]),
        tbody,
      ]),
    ]);
  }

  function groupMembersByGroup(members) {
    const grouped = new Map();
    members.forEach((member) => {
      const key = String(member?.group_id || "");
      const list = grouped.get(key) || [];
      list.push(member);
      grouped.set(key, list);
    });
    return Array.from(grouped.entries())
      .sort(([leftGroup], [rightGroup]) => {
        if (!leftGroup && rightGroup) return 1;
        if (leftGroup && !rightGroup) return -1;
        return leftGroup.localeCompare(rightGroup, "zh-Hans-CN", { numeric: true });
      })
      .map(([group_id, sectionMembers]) => ({
        group_id,
        members: sectionMembers.sort((left, right) => {
          const leftName =
            left.preferred_name ||
            left.group_card ||
            left.qq_nickname ||
            String(left.user_id || "");
          const rightName =
            right.preferred_name ||
            right.group_card ||
            right.qq_nickname ||
            String(right.user_id || "");
          return leftName.localeCompare(rightName, "zh-Hans-CN", { numeric: true });
        }),
      }));
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
          class: "btn is-danger",
          text: t("user.directory.resetPersona"),
          onClick: onResetPersona,
        }),
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
          label: t("user.directory.assistantAlias"),
          hint: t("user.directory.assistantAliasHint"),
          control: textInput({
            value: memberDraft.assistant_alias || "",
            disabled: true,
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
          label: t("user.directory.membershipStatus"),
          control: select({
            value: memberDraft.membership_status || "active",
            options: MEMBERSHIP_OPTIONS.map((item) => ({ value: item.value, label: t(item.labelKey) })),
            onChange: (value) => {
              memberDraft.membership_status = value;
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
          label: t("user.directory.affinity"),
          hint: t("user.directory.affinityHint"),
          control: numberInput({
            value: Number(memberDraft.affinity_score || 0),
            min: -1,
            max: 1,
            step: 0.01,
            onChange: (value) => {
              memberDraft.affinity_score = value;
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
          chip({ label: `${t("user.directory.bondStage")}: ${bondStage(memberDraft.affinity_score)}`, variant: "ok" }),
          chip({ label: `${t("user.directory.membershipStatus")}: ${memberDraft.membership_status || "active"}`, variant: "outline" }),
          chip({ label: `${t("user.directory.lastSeen")}: ${formatDateTime(memberDraft.last_seen_at)}`, variant: "outline" }),
          chip({ label: `${t("user.directory.lastSync")}: ${formatDateTime(memberDraft.last_sync_at)}`, variant: "outline" }),
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

function bondStage(score) {
  const value = Number(score || 0);
  if (value < 0.12) return "new";
  if (value < 0.3) return "warming";
  if (value < 0.55) return "familiar";
  if (value < 0.8) return "close";
  return "devoted";
}

function onboardingLabel(status) {
  const value = String(status || "new");
  const option = ONBOARDING_OPTIONS.find((item) => item.value === value);
  return option ? t(option.labelKey) : value;
}
