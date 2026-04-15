import { api } from "../api.js";
import {
  el,
  card,
  fieldRow,
  textInput,
  textarea,
  numberInput,
  tagInput,
  select,
  segmented,
  switchControl,
  chip,
  empty,
} from "../components.js";
import { t, onLangChange } from "../i18n.js";
import { toastOk, toastError, toastInfo, openModal } from "../ui.js";

const PORTRAIT_STYLE_OPTIONS = [
  { value: "neon-pixel", labelKey: "character.portrait.style.neon-pixel" },
  { value: "dream-anime", labelKey: "character.portrait.style.dream-anime" },
  { value: "comic-pop", labelKey: "character.portrait.style.comic-pop" },
];

const VARIANT_SECTIONS = [
  ["profile", "character.field.profile", "character.field.profile.hint", 5],
  ["skills", "character.field.skills", "character.field.skills.hint", 4],
  ["background", "character.field.background", "character.field.background.hint", 5],
  ["rules", "character.field.rules", "character.field.rules.hint", 5],
  ["prologue", "character.field.prologue", "character.field.prologue.hint", 3],
];

export function mount(root) {
  let stopped = false;
  let unsubLang = null;
  let bundle = null;
  let editor = null;
  let other = null;
  let ai = null;
  let cursor = 0;
  let motionDirection = "next";
  let saving = false;
  let loading = true;
  let dragStartX = null;
  let previewing = false;
  let preview = null;

  root.innerHTML = "";
  const container = el("div", { class: "page" });
  root.appendChild(container);

  function cards() {
    return Array.isArray(bundle?.available) ? bundle.available : [];
  }

  function isImageProviderReady() {
    const provider = ai?.image_generation || {};
    return !!(provider.enabled && provider.base_url && provider.api_key && provider.model);
  }

  function applyBundle(nextBundle) {
    bundle = nextBundle;
    editor = bundleToEditor(nextBundle);
    const items = cards();
    const currentName = String(nextBundle?.character || "");
    const nextIndex = items.findIndex((item) => item.character === currentName);
    cursor = nextIndex >= 0 ? nextIndex : 0;
    preview = syncPreviewState(preview, nextBundle);
  }

  async function loadAll(character = "") {
    loading = true;
    render();
    try {
      const [nextBundle, nextOther, nextAi] = await Promise.all([
        api.getCharacterPanel(character),
        api.getOtherPanel(),
        api.getAiPanel(),
      ]);
      if (stopped) return;
      other = nextOther;
      ai = nextAi;
      applyBundle(nextBundle);
    } catch (err) {
      toastError(String(err?.message || err));
    } finally {
      if (!stopped) {
        loading = false;
        render();
      }
    }
  }

  async function selectCharacter(name, { scroll = false } = {}) {
    if (!name) return;
    await loadAll(name);
    if (scroll) {
      scrollToWorkbench();
    }
  }

  async function activateCharacter(name) {
    if (!name || saving) return;
    saving = true;
    render();
    try {
      const selectedBundle = await api.getCharacterPanel(name);
      const nextAssistant = selectedBundle?.shared?.assistant_name || other?.assistant_name || "";
      const [savedBundle, savedOther] = await Promise.all([
        api.saveCharacterPanel({ character: name, set_active: true }),
        api.saveOtherPanel({ ...(other || {}), assistant_name: nextAssistant }),
      ]);
      if (stopped) return;
      other = savedOther;
      applyBundle(savedBundle);
      toastOk(t("character.carousel.activated", { name }));
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    } finally {
      if (!stopped) {
        saving = false;
        render();
      }
    }
  }

  async function savePage({ generatePortrait = null, setActive = false } = {}) {
    if (!editor || !other || saving) return;
    saving = true;
    render();
    try {
      const portrait = {
        style: editor.portrait.style,
        prompt_suffix: editor.portrait.prompt_suffix,
        auto_generate: !!editor.portrait.auto_generate,
        generate: generatePortrait === null ? !!editor.portrait.auto_generate : !!generatePortrait,
      };
      const syncAssistant = setActive || bundle?.current_character === editor.character;
      const otherPayload = {
        ...other,
        assistant_name: syncAssistant ? editor.shared.assistant_name : other.assistant_name,
      };
      const [savedBundle, savedOther] = await Promise.all([
        api.saveCharacterPanel({
          character: editor.character,
          set_active: setActive,
          shared_fields: cloneShared(editor.shared),
          person_fields: normalizeVariantFields(editor.person),
          group_fields: normalizeVariantFields(editor.group),
          portrait,
        }),
        api.saveOtherPanel(otherPayload),
      ]);
      if (stopped) return;
      other = savedOther;
      applyBundle(savedBundle);
      if (savedBundle?.portrait?.error) {
        toastError(savedBundle.portrait.error);
      } else if (savedBundle?.portrait?.notice) {
        toastInfo(t("character.portrait.notConfiguredHint"));
      } else if (portrait.generate) {
        toastOk(t("character.portrait.generated"));
      } else {
        toastOk(t("actions.savedOk"));
      }
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    } finally {
      if (!stopped) {
        saving = false;
        render();
      }
    }
  }

  async function createCharacter() {
    const nameInput = textInput({ placeholder: t("character.create.namePlaceholder") });
    const assistantInput = textInput({ placeholder: t("character.create.assistantPlaceholder") });
    const styleState = { value: PORTRAIT_STYLE_OPTIONS[0].value };
    const styleSelect = select({
      value: styleState.value,
      options: PORTRAIT_STYLE_OPTIONS.map((item) => ({ value: item.value, label: t(item.labelKey) })),
      onChange: (value) => {
        styleState.value = value;
      },
    });
    const body = el("div", { class: "stack" }, [
      fieldRow({
        label: t("character.create.name"),
        hint: t("character.create.nameHint"),
        required: true,
        control: nameInput,
      }),
      fieldRow({
        label: t("character.create.assistant"),
        hint: t("character.create.assistantHint"),
        required: true,
        control: assistantInput,
      }),
      fieldRow({
        label: t("character.portrait.style"),
        control: styleSelect,
      }),
    ]);
    const confirmed = await openModal({
      title: t("character.create.title"),
      body,
      actions: [
        { label: t("common.cancel"), value: false },
        { label: t("character.create.action"), value: true, variant: "primary" },
      ],
    });
    if (!confirmed) return;

    const name = String(nameInput.value || "").trim();
    const assistantName = String(assistantInput.value || "").trim();
    if (!name || !assistantName) {
      toastError(t("character.create.nameRequired"));
      return;
    }
    if (cards().some((item) => item.character === name)) {
      toastError(t("character.create.duplicate"));
      return;
    }

    saving = true;
    render();
    try {
      const shared = {
        assistant_name: assistantName,
        user_name: "主人",
        language: "简体中文",
      };
      const savedBundle = await api.saveCharacterPanel({
        character: name,
        set_active: false,
        shared_fields: shared,
        person_fields: defaultVariantFields("person", assistantName),
        group_fields: defaultVariantFields("group", assistantName),
        portrait: {
          style: styleState.value,
          prompt_suffix: "",
          auto_generate: true,
          generate: false,
        },
      });
      if (stopped) return;
      applyBundle(savedBundle);
      toastOk(t("character.create.created", { name }));
      scrollToWorkbench();
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    } finally {
      if (!stopped) {
        saving = false;
        render();
      }
    }
  }

  function currentAssistantName() {
    return String(editor?.shared?.assistant_name || bundle?.shared?.assistant_name || "Waifu");
  }

  function syncPreviewState(current, nextBundle) {
    const shared = nextBundle?.shared || {};
    const characterName = String(nextBundle?.character || "");
    const defaultUserName = String(shared.user_name || "User");
    if (!current || current.character !== characterName) {
      return {
        character: characterName,
        launcher_type: "person",
        user_name: defaultUserName,
        message: "",
        history: [],
        analysis_hint: "",
        llm_ready: false,
      };
    }
    return {
      ...current,
      character: characterName,
      user_name: current.user_name || defaultUserName,
    };
  }

  function resetPreview({ keepIdentity = true } = {}) {
    if (!preview) {
      preview = syncPreviewState(null, bundle);
      return;
    }
    preview = {
      ...preview,
      user_name: keepIdentity ? preview.user_name : String(editor?.shared?.user_name || "User"),
      message: "",
      history: [],
      analysis_hint: "",
    };
  }

  async function runPreview() {
    if (!editor || previewing) return;
    const message = String(preview?.message || "").trim();
    if (!message) {
      toastError(t("character.preview.messageRequired"));
      return;
    }
    previewing = true;
    render();
    try {
      const result = await api.previewCharacter({
        character: editor.character,
        launcher_type: preview.launcher_type,
        user_name: preview.user_name,
        message,
        history: preview.history,
        shared_fields: cloneShared(editor.shared),
        person_fields: normalizeVariantFields(editor.person),
        group_fields: normalizeVariantFields(editor.group),
      });
      if (stopped) return;
      preview = {
        ...preview,
        user_name: result?.user_name || preview.user_name,
        message: "",
        history: Array.isArray(result?.transcript)
          ? result.transcript.map((item) => ({
              role: String(item?.role || "").trim() || "assistant",
              text: String(item?.text || "").trim(),
            })).filter((item) => item.text)
          : [
              ...preview.history,
              { role: "user", text: message },
              { role: "assistant", text: String(result?.reply_text || "").trim() },
            ],
        analysis_hint: String(result?.analysis_hint || "").trim(),
        llm_ready: !!result?.llm_ready,
      };
    } catch (err) {
      toastError(t("actions.saveFailed", { msg: err?.message || err }));
    } finally {
      if (!stopped) {
        previewing = false;
        render();
      }
    }
  }

  function render() {
    container.innerHTML = "";
    container.appendChild(renderHeader());
    if (loading && !bundle) {
      container.appendChild(empty({ title: t("common.loading") }));
      return;
    }
    container.appendChild(renderCarousel());
    container.appendChild(renderWorkbench());
  }

  function renderHeader() {
    const runtimeName = String(other?.service_name || "");
    const configPath = String(other?.config_path || "");
    const dataRoot = String(other?.data_root || "");
    const activeCharacter = String(bundle?.current_character || "");
    return el("div", { class: "page-header" }, [
      el("div", { class: "page-header-text" }, [
        el("div", { class: "page-title", text: t("page.character.title") }),
        el("div", { class: "page-desc", text: t("page.character.desc") }),
        el("div", { class: "row" }, [
          runtimeName ? chip({ label: runtimeName, variant: "info" }) : null,
          activeCharacter ? chip({ label: `live:${activeCharacter}`, variant: "ok" }) : null,
          dataRoot ? chip({ label: dataRoot, variant: "outline" }) : null,
          configPath ? chip({ label: configPath, variant: "outline" }) : null,
        ]),
      ]),
      el("div", { class: "page-actions" }, [
        el("button", {
          type: "button",
          class: "btn",
          text: t("character.create.action"),
          onClick: createCharacter,
          disabled: saving,
        }),
        el("button", {
          type: "button",
          class: "btn is-primary",
          text: saving ? t("character.editor.saving") : t("common.save"),
          onClick: () => savePage(),
          disabled: saving || !editor,
        }),
      ]),
    ]);
  }

  function renderCarousel() {
    const items = cards();
    if (!items.length) {
      return card({
        title: t("character.carousel.title"),
        subtitle: t("character.carousel.desc"),
        body: [empty({ title: t("character.carousel.empty") })],
      });
    }
    const track = el(
      "div",
      {
        class: "carousel-track",
        style: { transform: `translate3d(${-cursor * 100}%, 0, 0)` },
      },
      items.map((item, index) =>
        el("div", { class: `carousel-slide${index === cursor ? " is-active" : ""}` }, [
          renderPersonaCard(item),
        ]),
      ),
    );
    const viewport = el("div", { class: "carousel-viewport" }, [track]);
    viewport.addEventListener("pointerdown", (event) => {
      dragStartX = event.clientX;
    });
    viewport.addEventListener("pointerup", (event) => {
      if (dragStartX === null) return;
      const delta = event.clientX - dragStartX;
      dragStartX = null;
      if (Math.abs(delta) < 48) return;
      moveCursor(delta < 0 ? 1 : -1);
    });
    viewport.addEventListener("pointerleave", () => {
      dragStartX = null;
    });

    return el("section", { class: `carousel is-moving-${motionDirection}` }, [
      el("button", {
        type: "button",
        class: "carousel-arrow is-prev",
        text: "←",
        disabled: items.length <= 1,
        onClick: () => moveCursor(-1),
        title: t("character.carousel.prev"),
      }),
      viewport,
      el("button", {
        type: "button",
        class: "carousel-arrow is-next",
        text: "→",
        disabled: items.length <= 1,
        onClick: () => moveCursor(1),
        title: t("character.carousel.next"),
      }),
    ]);
  }

  function renderPersonaCard(item) {
    const isActive = item.character === bundle?.current_character;
    const isSelected = item.character === editor?.character;
    const portrait = item.portrait || {};
    const scopeLabel = t(scopeI18nKey(item));
    const summary = item.summary || t("character.carousel.summaryFallback");
    const badges = [];
    if (isActive) {
      badges.push(chip({ label: t("character.carousel.active"), variant: "accent" }));
    }
    if (isSelected && !isActive) {
      badges.push(chip({ label: t("character.carousel.editing"), variant: "outline" }));
    }
    const visual = portrait.available
      ? el("img", {
          class: "carousel-card-portrait-img",
          src: portrait.url,
          alt: item.assistant_name || item.character,
        })
      : el("div", { class: "carousel-card-placeholder" }, [
          el("span", { class: "carousel-card-placeholder-mark", text: portraitLetter(item.assistant_name || item.character) }),
        ]);

    return el("article", { class: `carousel-card${isActive ? " is-current" : ""}${isSelected ? " is-selected" : ""}` }, [
      el("div", { class: "carousel-card-visual" }, [
        visual,
        el("div", { class: "carousel-card-visual-overlay" }, [
          badges.length ? el("div", { class: "carousel-card-badges" }, badges) : null,
          el("div", { class: "carousel-card-copy" }, [
            el("div", { class: "carousel-card-name", text: item.assistant_name || item.character }),
            el("div", { class: "carousel-card-scope", text: scopeLabel }),
          ]),
        ]),
      ]),
      el("div", { class: "carousel-card-summary", text: summary, title: summary }),
      el("div", { class: "carousel-card-meta" }, [
        el("div", { class: "carousel-card-meta-item", text: `${t("character.carousel.meta.person")}: ${item.has_person ? t("character.carousel.meta.personReady") : t("character.carousel.meta.personMissing")}` }),
        el("div", { class: "carousel-card-meta-item", text: `${t("character.carousel.meta.group")}: ${item.has_group ? t("character.carousel.meta.groupReady") : t("character.carousel.meta.groupMissing")}` }),
      ]),
      el("div", { class: "carousel-card-actions" }, [
        el("button", {
          type: "button",
          class: "btn",
          text: isSelected ? t("character.carousel.editing") : t("character.carousel.edit"),
          onClick: () => selectCharacter(item.character, { scroll: true }),
          disabled: saving,
        }),
        el("button", {
          type: "button",
          class: "btn is-primary",
          text: isActive ? t("character.carousel.active") : t("character.carousel.activate"),
          onClick: () => activateCharacter(item.character),
          disabled: saving || isActive,
        }),
      ]),
    ]);
  }

  function renderWorkbench() {
    if (!editor || !other) {
      return empty({ title: t("character.editor.empty") });
    }
    return el("div", { class: "character-workbench", id: "character-workbench" }, [
      el("div", { class: "character-editor-main stack" }, [
        renderEditorCard(),
        renderRuntimeCard(),
      ]),
      el("div", { class: "character-editor-side stack" }, [renderPreviewCard(), renderPortraitCard()]),
    ]);
  }

  function renderEditorCard() {
    return card({
      title: t("character.editor.title"),
      subtitle: t("character.editor.desc"),
      body: [
        el("div", { class: "character-shared-grid" }, [
          fieldRow({ label: t("character.field.assistantName"), hint: t("character.field.assistantName.hint"), control: textInput({ value: editor.shared.assistant_name, onChange: (value) => { editor.shared.assistant_name = value.trim() || editor.shared.assistant_name; } }) }),
          fieldRow({ label: t("character.field.userLabel"), hint: t("character.field.userLabel.hint"), control: textInput({ value: editor.shared.user_name, onChange: (value) => { editor.shared.user_name = value.trim() || "用户"; } }) }),
          fieldRow({ label: t("character.field.language"), hint: t("character.field.language.hint"), control: textInput({ value: editor.shared.language, onChange: (value) => { editor.shared.language = value.trim() || "简体中文"; } }) }),
          fieldRow({ label: t("character.field.character"), hint: t("character.field.character.hint"), control: textInput({ value: editor.character, disabled: true }) }),
        ]),
        renderVariantMatrix(),
      ],
    });
  }

  function renderVariantMatrix() {
    const children = [
      el("div", { class: "variant-matrix-corner" }),
      el("div", { class: "variant-matrix-head-cell" }, [
        el("div", { class: "variant-matrix-title", text: t("character.personPersona") }),
        el("div", { class: "variant-matrix-subtitle", text: t("character.editor.person.desc") }),
      ]),
      el("div", { class: "variant-matrix-head-cell" }, [
        el("div", { class: "variant-matrix-title", text: t("character.groupPersona") }),
        el("div", { class: "variant-matrix-subtitle", text: t("character.editor.group.desc") }),
      ]),
    ];
    VARIANT_SECTIONS.forEach(([key, labelKey, hintKey, rows]) => {
      children.push(
        el("div", { class: "variant-matrix-label" }, [
          el("div", { class: "variant-matrix-label-text", text: t(labelKey) }),
          el("div", { class: "variant-matrix-label-hint", text: t(hintKey) }),
        ]),
        el("div", { class: "variant-matrix-cell" }, [
          el("div", { class: "variant-matrix-cell-label", text: t("character.personPersona") }),
          textarea({
            rows,
            value: joinLines(editor.person[key]),
            onChange: (value) => {
              editor.person[key] = splitLines(value);
            },
          }),
        ]),
        el("div", { class: "variant-matrix-cell" }, [
          el("div", { class: "variant-matrix-cell-label", text: t("character.groupPersona") }),
          textarea({
            rows,
            value: joinLines(editor.group[key]),
            onChange: (value) => {
              editor.group[key] = splitLines(value);
            },
          }),
        ]),
      );
    });
    return el("div", { class: "variant-matrix" }, children);
  }

  function renderPortraitCard() {
    const portrait = editor.portrait;
    const preview = portrait.available && portrait.url
      ? el("img", { class: "character-portrait-image", src: portrait.url, alt: editor.shared.assistant_name })
      : el("div", { class: "character-portrait-placeholder" }, [
          el("span", { class: "character-portrait-initial", text: portraitLetter(editor.shared.assistant_name) }),
          el("div", { class: "character-portrait-caption", text: t("character.portrait.placeholder") }),
        ]);
    return card({
      title: t("character.portrait.title"),
      subtitle: t("character.portrait.desc"),
      body: [
        el("div", { class: "character-portrait-stack" }, [
          el("div", { class: "character-portrait-frame" }, [preview]),
          el("div", { class: "character-portrait-meta" }, [
            chip({ label: portrait.available ? t("character.portrait.ready") : t("character.portrait.waiting"), variant: portrait.available ? "ok" : "outline" }),
            chip({ label: isImageProviderReady() ? t("character.portrait.providerReady") : t("character.portrait.providerMissing"), variant: isImageProviderReady() ? "info" : "warn" }),
            portrait.updated_at ? chip({ label: t("character.portrait.updatedAt", { time: formatTimestamp(portrait.updated_at) }), variant: "outline" }) : null,
          ]),
          fieldRow({ label: t("character.portrait.style"), control: select({ value: portrait.style, options: PORTRAIT_STYLE_OPTIONS.map((item) => ({ value: item.value, label: t(item.labelKey) })), onChange: (value) => { portrait.style = value; } }) }),
          fieldRow({ label: t("character.portrait.prompt"), hint: t("character.portrait.prompt.hint"), control: textarea({ rows: 4, value: portrait.prompt_suffix, onChange: (value) => { portrait.prompt_suffix = value.trim(); } }) }),
          fieldRow({ label: t("character.portrait.autoGenerate"), control: switchControl({ checked: !!portrait.auto_generate, onChange: (value) => { portrait.auto_generate = value; } }) }),
          portrait.last_prompt ? el("div", { class: "character-portrait-prompt", text: portrait.last_prompt }) : null,
          portrait.error ? chip({ label: t("character.portrait.failed"), variant: "danger" }) : null,
          portrait.notice ? chip({ label: t("character.portrait.notice"), variant: "warn" }) : null,
          el("div", { class: "row-tight" }, [
            el("button", { type: "button", class: "btn", text: t("character.portrait.regenerate"), onClick: () => savePage({ generatePortrait: true, setActive: false }), disabled: saving }),
            el("button", { type: "button", class: "btn is-primary", text: t("character.portrait.saveActivate"), onClick: () => savePage({ setActive: true }), disabled: saving }),
          ]),
        ]),
      ],
    });
  }

  function renderPreviewCard() {
    if (!preview) {
      preview = syncPreviewState(null, bundle);
    }
    const assistantName = currentAssistantName();
    const transcript = Array.isArray(preview?.history) ? preview.history : [];
    const transcriptBody = transcript.length
      ? transcript.map((entry) => {
          const role = String(entry?.role || "").trim().toLowerCase();
          const text = String(entry?.text || "").trim();
          if (!text) return null;
          const isAssistant = role === "assistant";
          return el("div", { class: `character-preview-line${isAssistant ? " is-assistant" : " is-user"}` }, [
            el("div", { class: "character-preview-speaker", text: isAssistant ? assistantName : (preview.user_name || editor?.shared?.user_name || "User") }),
            el("div", { class: "character-preview-bubble", text }),
          ]);
        }).filter(Boolean)
      : [el("div", { class: "character-preview-empty", text: t("character.preview.empty") })];
    const composer = textarea({
      rows: 4,
      value: preview.message,
      placeholder: t("character.preview.messagePlaceholder"),
      onInput: (value) => {
        preview.message = value;
      },
    });
    return card({
      title: t("character.preview.title"),
      subtitle: t("character.preview.desc"),
      actions: [
        chip({
          label: preview.llm_ready ? t("character.preview.providerLive") : t("character.preview.providerFallback"),
          variant: preview.llm_ready ? "info" : "outline",
        }),
      ],
      body: [
        fieldRow({
          label: t("character.preview.mode"),
          control: segmented({
            value: preview.launcher_type,
            options: [
              { value: "person", label: t("character.preview.mode.person") },
              { value: "group", label: t("character.preview.mode.group") },
            ],
            onChange: (value) => {
              preview.launcher_type = value;
              preview.history = [];
              preview.analysis_hint = "";
              render();
            },
          }),
        }),
        fieldRow({
          label: t("character.preview.userName"),
          hint: t("character.preview.userName.hint"),
          control: textInput({
            value: preview.user_name,
            onInput: (value) => {
              preview.user_name = value;
            },
          }),
        }),
        el("div", { class: "character-preview-transcript" }, transcriptBody),
        preview.analysis_hint
          ? el("div", { class: "character-preview-analysis" }, [
              el("div", { class: "character-preview-analysis-label", text: t("character.preview.analysis") }),
              el("div", { class: "character-preview-analysis-text", text: preview.analysis_hint }),
            ])
          : null,
        fieldRow({
          label: t("character.preview.message"),
          hint: t("character.preview.message.hint"),
          control: composer,
        }),
        el("div", { class: "character-preview-actions" }, [
          el("button", {
            type: "button",
            class: "btn",
            text: t("character.preview.clear"),
            onClick: () => {
              resetPreview();
              render();
            },
            disabled: previewing || (!preview.history.length && !preview.message),
          }),
          el("button", {
            type: "button",
            class: "btn is-primary",
            text: previewing ? t("character.preview.sending") : t("character.preview.send"),
            onClick: runPreview,
            disabled: previewing,
          }),
        ]),
      ],
    });
  }

  function renderRuntimeCard() {
    return card({
      title: t("character.tab.pipeline"),
      subtitle: t("character.carousel.desc"),
      body: [
        el("div", { class: "stack" }, [
          chip({ label: t("character.runtime.note"), variant: "info" }),
          el("div", { class: "page-desc", text: t("character.runtime.tip") }),
        ]),
        fieldRow({ label: t("character.field.serviceName"), hint: t("character.field.serviceName.hint"), control: textInput({ value: other.service_name || "", onChange: (value) => { other.service_name = value; } }) }),
        fieldRow({
          label: t("character.field.groupReplyRequiresMention"),
          hint: t("character.field.groupReplyRequiresMention.hint"),
          control: switchControl({
            checked: other.group_reply_requires_mention !== false,
            onChange: (value) => {
              other.group_reply_requires_mention = value;
            },
          }),
        }),
        fieldRow({ label: t("character.pipeline.followup"), control: numberInput({ value: Number(other.group_follow_up_window_seconds || 5), min: 0, max: 60, step: 1, onChange: (value) => { other.group_follow_up_window_seconds = value; } }) }),
        fieldRow({
          label: t("character.field.groupReplyDelay"),
          hint: t("character.field.groupReplyDelay.hint"),
          control: numberInput({
            value: Number(other.group_response_delay_seconds || 0),
            min: 0,
            max: 15,
            step: 0.5,
            onChange: (value) => {
              other.group_response_delay_seconds = value;
            },
          }),
        }),
        fieldRow({
          label: t("character.field.repeatTrigger"),
          hint: t("character.field.repeatTrigger.hint"),
          control: numberInput({
            value: Number(other.repeat_trigger_count || 0),
            min: 0,
            max: 10,
            step: 1,
            onChange: (value) => {
              other.repeat_trigger_count = value;
            },
          }),
        }),
        fieldRow({
          label: t("character.field.multimodal"),
          hint: t("character.field.multimodal.hint"),
          control: switchControl({
            checked: other.multimodal_enabled !== false,
            onChange: (value) => {
              other.multimodal_enabled = value;
            },
          }),
        }),
        fieldRow({ label: t("character.pipeline.imageCmd"), control: textInput({ value: other.image_command_prefix || "", onChange: (value) => { other.image_command_prefix = value.trim(); } }) }),
        fieldRow({ label: t("character.pipeline.aliases"), control: tagInput({ values: other.image_command_aliases || [], onChange: (value) => { other.image_command_aliases = value; } }) }),
        fieldRow({ label: t("character.field.ignorePrefixes"), hint: t("character.field.ignorePrefixes.hint"), control: tagInput({ values: other.ignore_prefixes || [], onChange: (value) => { other.ignore_prefixes = value; } }) }),
      ],
    });
  }

  function moveCursor(delta) {
    const items = cards();
    if (!items.length) return;
    const nextIndex = (cursor + delta + items.length) % items.length;
    motionDirection = delta > 0 ? "next" : "prev";
    cursor = nextIndex;
    render();
    void selectCharacter(items[nextIndex].character);
  }

  loadAll();
  unsubLang = onLangChange(() => render());
  return () => {
    stopped = true;
    if (unsubLang) unsubLang();
  };
}

function bundleToEditor(bundle) {
  return {
    character: String(bundle?.character || ""),
    shared: cloneShared(bundle?.shared || {}),
    person: cloneVariant(bundle?.person?.fields || {}),
    group: cloneVariant(bundle?.group?.fields || {}),
    portrait: {
      available: !!bundle?.portrait?.available,
      url: String(bundle?.portrait?.url || ""),
      style: String(bundle?.portrait?.style || PORTRAIT_STYLE_OPTIONS[0].value),
      prompt_suffix: String(bundle?.portrait?.prompt_suffix || ""),
      auto_generate: bundle?.portrait?.auto_generate !== false,
      last_prompt: String(bundle?.portrait?.last_prompt || ""),
      updated_at: Number(bundle?.portrait?.updated_at || 0),
      error: String(bundle?.portrait?.error || ""),
      notice: String(bundle?.portrait?.notice || ""),
    },
  };
}

function cloneVariant(source) {
  return {
    profile: [...(source?.profile || [])],
    skills: [...(source?.skills || [])],
    background: [...(source?.background || [])],
    rules: [...(source?.rules || [])],
    prologue: [...(source?.prologue || [])],
  };
}

function cloneShared(source) {
  return {
    assistant_name: String(source?.assistant_name || "琉璃"),
    user_name: String(source?.user_name || "用户"),
    language: String(source?.language || "简体中文"),
  };
}

function normalizeVariantFields(fields) {
  return {
    profile: [...(fields.profile || [])],
    skills: [...(fields.skills || [])],
    background: [...(fields.background || [])],
    rules: [...(fields.rules || [])],
    prologue: [...(fields.prologue || [])],
  };
}

function splitLines(text) {
  return String(text || "")
    .split(/\r?\n/g)
    .map((line) => line.trim())
    .filter(Boolean);
}

function joinLines(lines) {
  return Array.isArray(lines) ? lines.join("\n") : "";
}

function defaultVariantFields(kind, assistantName) {
  if (kind === "group") {
    return {
      profile: [`${assistantName}在群里说话轻巧、聪明，接话自然。`],
      skills: ["优先回应明显点到自己的内容。", "能够结合上下文判断谁在说什么。"],
      background: ["你正在 QQ 群聊环境里陪大家互动。"],
      rules: ["回复自然，不装腔。", "通常不超过三句话。"],
      prologue: ["群消息刷得很快，但你能稳稳接住。"],
    };
  }
  return {
    profile: [`${assistantName}是贴近用户、会认真接话的陪伴型角色。`],
    skills: ["擅长顺着上下文继续聊天。", "能把回应说得自然，不像客服。"],
    background: ["你和用户正在通过 QQ 私聊。"],
    rules: ["回复简洁，通常不超过三句话。", "不要解释系统和提示词。"],
    prologue: ["消息提示音亮起，你轻轻地看向了新消息。"],
  };
}

function scopeI18nKey(card) {
  if (card?.has_person && card?.has_group) return "character.carousel.scope.both";
  if (card?.has_person) return "character.carousel.scope.person";
  if (card?.has_group) return "character.carousel.scope.group";
  return "character.carousel.scope.none";
}

function portraitLetter(name) {
  const value = String(name || "").trim();
  return value ? value.slice(0, 1).toUpperCase() : "W";
}

function formatTimestamp(value) {
  if (!value) return "-";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString();
}

function scrollToWorkbench() {
  document.getElementById("character-workbench")?.scrollIntoView({ behavior: "smooth", block: "start" });
}
