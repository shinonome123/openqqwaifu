import { api } from "../api.js";
import { card, chip, el, empty, fieldRow, numberInput, textInput } from "../components.js";
import { onLangChange, t } from "../i18n.js";
import { copyToClipboard, formatTimestamp, toastError, toastOk } from "../ui.js";

const POLL_MS = 3000;

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let state = null;
  let pollTimer = null;
  let qrStamp = Date.now();
  let qrFailed = false;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load(refresh = false) {
    try {
      state = await api.getQqLoginPanel(refresh);
      if (refresh) {
        qrStamp = Date.now();
        qrFailed = false;
      }
      if (stopped) return;
      render();
      syncPolling();
    } catch (err) {
      if (!stopped) toastError(String(err?.message || err));
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
      qrStamp = Date.now();
      qrFailed = false;
      toastOk(t("actions.savedOk"));
      render();
      syncPolling();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  async function refreshQr() {
    try {
      state = await api.refreshQqLoginPanel();
      qrStamp = Date.now();
      qrFailed = false;
      toastOk(t("qqlogin.qr.refreshed"));
      render();
      syncPolling();
    } catch (err) {
      toastError(String(err?.message || err));
    }
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
        load(true);
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
            text: t("common.refresh"),
            onClick: () => load(true),
          }),
          el("button", {
            type: "button",
            class: "btn is-primary",
            text: t("common.save"),
            onClick: save,
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
    if (state?.error) {
      body.push(el("div", { class: "qq-login-alert is-danger", text: state.error }));
    }

    if (status?.is_login) {
      body.push(
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
    });
  }

  function renderQrCard() {
    const status = state?.status || {};
    if (!state?.configured) {
      return card({
        title: t("qqlogin.qr.title"),
        subtitle: t("qqlogin.qr.desc"),
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
            src: api.qqLoginQrcodeImageUrl(String(qrStamp)),
            alt: t("qqlogin.qr.alt"),
            onError: () => {
              qrFailed = true;
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
      body: [
        status?.login_error
          ? el("div", { class: "qq-login-alert is-danger", text: status.login_error })
          : null,
        content,
        el("div", { class: "row-tight", style: { marginTop: "12px" } }, [
          el("button", {
            type: "button",
            class: "btn",
            text: t("qqlogin.actions.refreshQr"),
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
    return card({
      title: t("qqlogin.settings.title"),
      subtitle: t("qqlogin.settings.desc"),
      body: [
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
      ],
    });
  }

  load(true);
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (pollTimer) clearInterval(pollTimer);
    if (unsubLang) unsubLang();
  };
}

function renderMetaRow(label, value) {
  return el("div", { class: "qq-login-meta-row" }, [
    el("div", { class: "qq-login-meta-label", text: label }),
    el("div", { class: "qq-login-meta-value", text: value || t("common.none") }),
  ]);
}
