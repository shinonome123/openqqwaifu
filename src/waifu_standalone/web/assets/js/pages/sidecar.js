// Sidecar page: OneBot HTTP bridge settings + probe.

import { api } from "../api.js";
import { el, card, fieldRow, textInput, numberInput, jsonView, chip } from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError } from "../ui.js";

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let state = null;
  let other = null;
  let probe = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  async function load() {
    try {
      [state, other] = await Promise.all([
        api.getSidecarPanel(false),
        api.getOtherPanel(),
      ]);
      if (stopped) return;
      render();
    } catch (err) {
      toastError(String(err?.message || err));
    }
  }

  async function save() {
    try {
      const [savedState, savedOther] = await Promise.all([
        api.saveSidecarPanel({
          adapter_name: state?.adapter_name || "napcat",
          outbound_base_url: state?.outbound_base_url || "",
          outbound_timeout_seconds: Number(state?.outbound_timeout_seconds || 10),
          access_token: state?.access_token || "",
          inbound_host: state?.inbound_host || "127.0.0.1",
          inbound_port: Number(state?.inbound_port || 8080),
          webui_base_url: state?.webui_base_url || "",
          webui_api_prefix: state?.webui_api_prefix ?? "/api",
          webui_timeout_seconds: Number(state?.webui_timeout_seconds || 10),
          webui_token: state?.webui_token || "",
          reverse_ws_url: state?.reverse_ws_url || "",
        }),
        api.saveOtherPanel({
          ...(other || {}),
          bot_account_id: other?.bot_account_id || "",
        }),
      ]);
      state = savedState;
      other = savedOther;
      toastOk(t("actions.savedOk"));
      render();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    }
  }

  async function doProbe() {
    try {
      probe = await api.testSidecar();
      toastOk(probe?.mode || "tested");
      render();
    } catch (err) {
      toastError(err?.message || String(err));
    }
  }

  function patch(next) {
    state = { ...state, ...next };
  }

  function patchOther(next) {
    other = { ...(other || {}), ...next };
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(
      el("div", { class: "page-header" }, [
        el("div", { class: "page-header-text" }, [
          el("div", { class: "page-title", text: t("page.sidecar.title") }),
          el("div", { class: "page-desc", text: t("page.sidecar.desc") }),
        ]),
        el("div", { class: "page-actions" }, [
          el("button", {
            type: "button",
            class: "btn",
            text: t("sidecar.test.title"),
            onClick: doProbe,
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

    if (!state) return;

    const modeVariant =
      state.mode === "online" ? "ok" : state.mode === "error" ? "danger" : "outline";

    container.appendChild(
      card({
        title: t("nav.sidecar"),
        actions: [chip({ label: state.mode || "offline", variant: modeVariant })],
        body: [
          fieldRow({
            label: t("sidecar.field.adapter"),
            control: textInput({
              value: state.adapter_name || "napcat",
              onChange: (v) => patch({ adapter_name: v }),
            }),
          }),
          fieldRow({
            label: t("character.field.botId"),
            hint: t("character.field.botId.hint"),
            control: textInput({
              value: other?.bot_account_id || "",
              onChange: (v) => patchOther({ bot_account_id: v.trim() }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.inboundHost"),
            control: textInput({
              value: state.inbound_host || "",
              onChange: (v) => patch({ inbound_host: v }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.inboundPort"),
            control: numberInput({
              value: Number(state.inbound_port || 8080),
              min: 1,
              max: 65535,
              onChange: (v) => patch({ inbound_port: v }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.outboundUrl"),
            control: textInput({
              value: state.outbound_base_url || "",
              placeholder: "http://127.0.0.1:3000",
              onChange: (v) => patch({ outbound_base_url: v }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.outboundTimeout"),
            control: numberInput({
              value: Number(state.outbound_timeout_seconds || 10),
              min: 1,
              max: 120,
              onChange: (v) => patch({ outbound_timeout_seconds: v }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.accessToken"),
            control: textInput({
              type: "password",
              value: state.access_token || "",
              onChange: (v) => patch({ access_token: v }),
            }),
          }),
          fieldRow({
            label: t("sidecar.field.reverseWs"),
            control: textInput({
              value: state.reverse_ws_url || "",
              placeholder: "ws://127.0.0.1:3001/onebot/v11/ws",
              onChange: (v) => patch({ reverse_ws_url: v }),
            }),
          }),
        ],
      }),
    );

    if (probe?.details || probe?.error) {
      container.appendChild(
        card({
          title: t("sidecar.probe.result"),
          body: [jsonView(probe)],
        }),
      );
    }
  }

  load();
  unsubLang = onLangChange(() => render());

  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}
