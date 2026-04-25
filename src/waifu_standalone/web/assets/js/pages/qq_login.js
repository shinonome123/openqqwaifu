import { api } from "../api.js";
import { card, chip, el, empty, fieldRow, numberInput, textInput } from "../components.js";
import { onLangChange, t } from "../i18n.js";
import { copyToClipboard, toastError, toastOk } from "../ui.js";

const POLL_MS = 3000;

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let state = null;
  let pollTimer = null;
  let qrStamp = "0";
  let qrFailed = false;
  let lastQrKey = "";
  let lastRenderSignature = "";
  let statusLoading = false;
  let qrRefreshing = false;
  let settingsExpanded = false;

  function panelSignature(panel) {
    const isLogin = !!panel?.status?.is_login;
    return JSON.stringify({
      configured: !!panel?.configured,
      token_configured: !!panel?.token_configured,
      webui_url: panel?.webui_url || "",
      error: isLogin ? "" : panel?.error || "",
      status: {
        is_login: isLogin,
        is_offline: !!panel?.status?.is_offline,
        qrcode_url: isLogin ? "" : panel?.status?.qrcode_url || "",
        login_error: isLogin ? "" : panel?.status?.login_error || "",
      },
      login_info: {
        uin: panel?.login_info?.uin || "",
        nickname: panel?.login_info?.nickname || "",
        online: !!panel?.login_info?.online,
      },
    });
  }

  function syncQrState(panel, { force = false } = {}) {
    const qrKey = String(panel?.status?.qrcode_url || "");
    if (!force && qrKey === lastQrKey) return false;
    lastQrKey = qrKey;
    qrStamp = stableHash(qrKey || String(Date.now()));
    qrFailed = false;
    return true;
  }

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load(refresh = false, { background = false, notify = false } = {}) {
    const showLoading = refresh && !background;
    if (showLoading) {
      statusLoading = true;
      render();
    }
    try {
      const nextState = await api.getQqLoginPanel(refresh);
      const previousSignature = lastRenderSignature;
      const nextSignature = panelSignature(nextState);
      const qrChanged = syncQrState(nextState);
      if (background && state) {
        state = {
          ...state,
          configured: nextState?.configured,
          token_configured: nextState?.token_configured,
          webui_url: nextState?.webui_url,
          error: nextState?.error,
          status: nextState?.status,
          login_info: nextState?.login_info,
        };
      } else {
        state = nextState;
      }
      if (stopped) return;
      lastRenderSignature = nextSignature;
      if (!background || qrChanged || previousSignature !== nextSignature) {
        render();
      }
      if (notify) toastOk(t("qqlogin.status.refreshed"));
      syncPolling();
    } catch (err) {
      if (!stopped && !background) toastError(String(err?.message || err));
    } finally {
      if (showLoading) {
        statusLoading = false;
        if (!stopped) render();
      }
    }
  }

  async function save() {
    try {
      state = await api.saveQqLoginPanel({
        webui_base_url: state?.webui_base_url || "",
        webui_api_prefix: state?.webui_api_prefix ?? "/api",
        webui_timeout_seconds: Number(state?.webui_timeout_seconds || 10),
        webui_token: state?.webui_token || "",
      });
      syncQrState(state, { force: true });
      lastRenderSignature = panelSignature(state);
      toastOk(t("actions.savedOk"));
      render();
      syncPolling();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  async function refreshQr() {
    if (state?.status?.is_login) {
      toastError(t("qqlogin.qr.loggedInNoRefresh"));
      return;
    }
    qrRefreshing = true;
    render();
    try {
      state = await api.refreshQqLoginPanel();
      syncQrState(state, { force: true });
      lastRenderSignature = panelSignature(state);
      toastOk(t("qqlogin.qr.refreshed"));
      render();
      syncPolling();
    } catch (err) {
      toastError(String(err?.message || err));
    } finally {
      qrRefreshing = false;
      if (!stopped) render();
    }
  }

  function refreshStatus() {
    return load(true, { notify: true });
  }

  function notifySwitchAccount() {
    toastOk(t("qqlogin.actions.switchOpened"));
  }

  function patch(next) {
    state = { ...(state || {}), ...next };
  }

  function syncPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    const shouldPoll =
      !!state?.configured &&
      !!state?.token_configured &&
      !state?.status?.is_login &&
      !state?.error;
    if (shouldPoll) {
      pollTimer = setInterval(() => {
        load(true, { background: true });
      }, POLL_MS);
    }
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.qqLogin.title") }),
          el("div", { class: "page-desc", text: t("page.qqLogin.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          el("button", {
            type: "button",
            class: "btn",
            disabled: statusLoading,
            text: statusLoading ? t("qqlogin.actions.refreshing") : t("qqlogin.actions.refreshStatus"),
            onClick: refreshStatus,
          }),
        ]),
      ]),
    );

    container.appendChild(
      el("div", { class: "qq-login-grid" }, [
        renderStatusCard(),
        renderQrCard(),
      ]),
    );

    container.appendChild(renderSettingsCard());
  }

  function renderStatusCard() {
    const status = state?.status || {};
    const info = state?.login_info || {};
    const chips = [
      chip({
        label: state?.configured ? t("qqlogin.state.bridgeReady") : t("qqlogin.state.bridgeMissing"),
        variant: state?.configured ? "ok" : "outline",
      }),
      chip({
        label: state?.token_configured ? t("qqlogin.state.tokenReady") : t("qqlogin.state.tokenMissing"),
        variant: state?.token_configured ? "accent" : "warn",
      }),
      chip({
        label: status?.is_login ? t("qqlogin.state.loggedIn") : t("qqlogin.state.waiting"),
        variant: status?.is_login ? "ok" : "outline",
      }),
    ];

    const body = [];
    if (state?.error && !status?.is_login) {
      body.push(el("div", { class: "qq-login-alert is-danger", text: state.error }));
    }

    if (status?.is_login) {
      body.push(
        el("div", { class: "qq-login-profile-card" }, [
          el("div", { class: "qq-login-profile" }, [
            info?.avatar_url
              ? el("img", {
                  class: "qq-login-avatar",
                  src: info.avatar_url,
                  alt: "",
                })
              : el("div", { class: "qq-login-avatar is-fallback", text: "QQ" }),
            el("div", { class: "qq-login-profile-meta" }, [
              el("div", {
                class: "qq-login-profile-name",
                text: info?.nickname || info?.uin || t("common.none"),
              }),
              el("div", {
                class: "qq-login-profile-sub",
                text: info?.uin ? `QQ ${info.uin}` : t("common.none"),
              }),
              el("div", {
                class: "qq-login-profile-sub",
                text: info?.online ? t("qqlogin.profile.online") : t("qqlogin.profile.offline"),
              }),
            ]),
          ]),
          el("div", { class: "qq-login-status-actions" }, [
            state?.webui_url
              ? el(
                  "a",
                  {
                    class: "btn is-primary",
                    href: state.webui_url,
                    target: "_blank",
                    rel: "noreferrer noopener",
                    onClick: notifySwitchAccount,
                  },
                  [t("qqlogin.actions.switchAccount")],
                )
              : el("button", {
                  type: "button",
                  class: "btn is-primary",
                  disabled: true,
                  text: t("qqlogin.actions.switchAccount"),
                }),
            el("button", {
              type: "button",
              class: "btn",
              disabled: statusLoading,
              text: statusLoading ? t("qqlogin.actions.refreshing") : t("qqlogin.actions.refreshStatus"),
              onClick: refreshStatus,
            }),
          ]),
          el("div", { class: "qq-login-switch-hint", text: t("qqlogin.settings.switchHint") }),
        ]),
      );
    } else {
      body.push(
        el("div", { class: "stack-list" }, [
          renderMetaRow(t("qqlogin.status.offline"), status?.is_offline ? t("common.enabled") : t("common.disabled")),
          renderMetaRow(
            t("qqlogin.status.lastQr"),
            status?.qrcode_url ? t("qqlogin.status.available") : t("qqlogin.status.pending"),
          ),
          renderMetaRow(
            t("qqlogin.status.loginError"),
            status?.login_error || t("qqlogin.status.none"),
          ),
        ]),
      );
    }

    if (state?.webui_url) {
      body.push(
        el("div", { class: "row-tight", style: { marginTop: "12px" } }, [
          el(
            "a",
            {
              class: "btn",
              href: state.webui_url,
              target: "_blank",
              rel: "noreferrer noopener",
            },
            [t("qqlogin.actions.openWebui")],
          ),
        ]),
      );
    }

    return card({
      title: t("qqlogin.status.title"),
      subtitle: t("qqlogin.status.desc"),
      actions: chips,
      body,
      extraClass: "qq-login-card qq-login-status-card",
    });
  }

  function renderQrCard() {
    const status = state?.status || {};
    if (!state?.configured) {
      return card({
        title: t("qqlogin.qr.title"),
        subtitle: t("qqlogin.qr.desc"),
        extraClass: "qq-login-card qq-login-qr-card",
        body: [
          empty({
            title: t("qqlogin.qr.needBridge"),
            message: t("qqlogin.qr.needBridgeDesc"),
          }),
        ],
      });
    }
    if (!state?.token_configured) {
      return card({
        title: t("qqlogin.qr.title"),
        subtitle: t("qqlogin.qr.desc"),
        extraClass: "qq-login-card qq-login-qr-card",
        body: [
          empty({
            title: t("qqlogin.qr.needToken"),
            message: t("qqlogin.qr.needTokenDesc"),
          }),
        ],
      });
    }
    if (status?.is_login) {
      return card({
        title: t("qqlogin.qr.title"),
        subtitle: t("qqlogin.qr.desc"),
        extraClass: "qq-login-card qq-login-qr-card",
        body: [
          el("div", { class: "qq-login-success" }, [
            el("div", { class: "qq-login-success-mark", text: "OK" }),
            el("div", { class: "qq-login-success-title", text: t("qqlogin.qr.loggedIn") }),
            el("div", { class: "qq-login-success-copy", text: t("qqlogin.qr.loggedInDesc") }),
          ]),
        ],
      });
    }
    const qrAvailable = !!status?.qrcode_url && !qrFailed;
    const content = qrAvailable
      ? el("div", { class: "qq-login-qr-shell" }, [
          el("img", {
            class: "qq-login-qr-image",
            src: api.qqLoginQrcodeImageUrl(qrStamp),
            alt: t("qqlogin.qr.alt"),
            onError: () => {
              qrFailed = true;
              lastRenderSignature = "";
              render();
            },
          }),
          el("div", { class: "qq-login-qr-hint", text: t("qqlogin.qr.scanHint") }),
        ])
      : empty({
          title: t("qqlogin.qr.unavailable"),
          message: status?.login_error || t("qqlogin.qr.unavailableDesc"),
        });
    return card({
      title: t("qqlogin.qr.title"),
      subtitle: t("qqlogin.qr.desc"),
      extraClass: "qq-login-card qq-login-qr-card",
      body: [
        status?.login_error
          ? el("div", { class: "qq-login-alert is-danger", text: status.login_error })
          : null,
        content,
        el("div", { class: "row-tight", style: { marginTop: "12px" } }, [
          el("button", {
            type: "button",
            class: "btn",
            disabled: qrRefreshing,
            text: qrRefreshing ? t("qqlogin.qr.refreshing") : t("qqlogin.actions.refreshQr"),
            onClick: refreshQr,
          }),
          el("button", {
            type: "button",
            class: "btn",
            disabled: !status?.qrcode_url,
            text: t("qqlogin.actions.copyPayload"),
            onClick: () => copyToClipboard(status?.qrcode_url || ""),
          }),
        ]),
      ],
    });
  }

  function renderSettingsCard() {
    const body = [
      el("div", { class: "qq-login-settings-summary" }, [
        renderSummaryPill(t("qqlogin.settings.summaryBase"), state?.webui_base_url || state?.webui_url || t("common.none")),
        renderSummaryPill(t("qqlogin.settings.summaryPrefix"), state?.webui_api_prefix ?? "/api"),
        renderSummaryPill(
          t("qqlogin.settings.summaryToken"),
          state?.token_configured ? t("qqlogin.state.tokenReady") : t("qqlogin.state.tokenMissing"),
          state?.token_configured ? "ok" : "warn",
        ),
      ]),
    ];

    if (settingsExpanded) {
      body.push(
        el("div", { class: "qq-login-settings-fields" }, [
          fieldRow({
            label: t("sidecar.field.webuiBaseUrl"),
            hint: t("qqlogin.settings.baseHint"),
            control: textInput({
              value: state?.webui_base_url || "",
              placeholder: "http://127.0.0.1:6099",
              onInput: (value) => patch({ webui_base_url: value }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.webuiApiPrefix"),
            hint: t("qqlogin.settings.prefixHint"),
            control: textInput({
              value: state?.webui_api_prefix ?? "/api",
              placeholder: "/api",
              onInput: (value) => patch({ webui_api_prefix: value }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.webuiTimeout"),
            hint: t("qqlogin.settings.timeoutHint"),
            control: numberInput({
              value: Number(state?.webui_timeout_seconds || 10),
              min: 1,
              max: 120,
              onChange: (value) => patch({ webui_timeout_seconds: value }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.webuiToken"),
            hint: t("qqlogin.settings.tokenHint"),
            control: textInput({
              type: "password",
              value: state?.webui_token || "",
              placeholder: t("qqlogin.settings.tokenPlaceholder"),
              onInput: (value) => patch({ webui_token: value }),
            }),
          }),
        ]),
        el("div", { class: "qq-login-settings-actions" }, [
          el("button", {
            type: "button",
            class: "btn is-primary",
            text: t("qqlogin.settings.save"),
            onClick: save,
          }),
        ]),
      );
    }

    return card({
      title: t("qqlogin.settings.advancedTitle"),
      subtitle: t("qqlogin.settings.advancedDesc"),
      actions: [
        el("button", {
          type: "button",
          class: "btn",
          text: settingsExpanded ? t("qqlogin.settings.collapse") : t("qqlogin.settings.expand"),
          onClick: () => {
            settingsExpanded = !settingsExpanded;
            render();
          },
        }),
      ],
      body,
      extraClass: "qq-login-settings-card",
    });
  }

  function renderSummaryPill(label, value, variant = "") {
    return el("div", { class: `qq-login-summary-pill ${variant ? `is-${variant}` : ""}`.trim() }, [
      el("span", { class: "qq-login-summary-label", text: label }),
      el("span", { class: "qq-login-summary-value", text: value || t("common.none") }),
    ]);
  }

  load(true);
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (pollTimer) clearInterval(pollTimer);
    if (unsubLang) unsubLang();
  };
}

function stableHash(value) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return String(hash >>> 0);
}

function renderMetaRow(label, value) {
  return el("div", { class: "qq-login-meta-row" }, [
    el("div", { class: "qq-login-meta-label", text: label }),
    el("div", { class: "qq-login-meta-value", text: value || t("common.none") }),
  ]);
}
