// DOM-building helpers used by page modules.

import { t } from "./i18n.js";

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class" || key === "className") {
      node.className = value;
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else if (key === "style" && typeof value === "object") {
      Object.assign(node.style, value);
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "html") {
      node.innerHTML = value;
    } else if (key === "text") {
      node.textContent = value;
    } else if (key === "i18n") {
      node.dataset.i18n = value;
      node.textContent = t(value);
    } else if (key in node) {
      try {
        node[key] = value;
      } catch (err) {
        node.setAttribute(key, value);
      }
    } else {
      node.setAttribute(key, value);
    }
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  if (children === null || children === undefined) return;
  if (Array.isArray(children)) {
    children.forEach((child) => appendChildren(node, child));
    return;
  }
  if (children instanceof Node) {
    node.appendChild(children);
    return;
  }
  if (typeof children === "string" || typeof children === "number") {
    node.appendChild(document.createTextNode(String(children)));
  }
}

export function card({ title, subtitle, actions, body, extraClass = "" }) {
  const header =
    title || actions
      ? el("div", { class: "card-header" }, [
          el("div", {}, [
            title ? el("div", { class: "card-title", text: title }) : null,
            subtitle ? el("div", { class: "card-subtitle", text: subtitle }) : null,
          ]),
          actions ? el("div", { class: "row-tight" }, actions) : null,
        ])
      : null;
  return el("section", { class: `card ${extraClass}`.trim() }, [
    header,
    el("div", { class: "card-body" }, body),
  ]);
}

export function fieldRow({ label, hint, control, required = false }) {
  return el("div", { class: "field-row" }, [
    el("div", { class: "field-label" }, [
      el("span", {}, [label, required ? el("span", { class: "muted", text: " *" }) : null]),
      hint ? el("span", { class: "field-hint", text: hint }) : null,
    ]),
    el("div", { class: "field-control" }, [control]),
  ]);
}

export function textInput({ value = "", onChange, placeholder = "", type = "text", onInput, disabled = false }) {
  const input = el("input", {
    type,
    value: value ?? "",
    placeholder,
    disabled,
    autocomplete: "off",
    spellcheck: "false",
  });
  if (typeof onChange === "function") {
    input.addEventListener("change", () => onChange(input.value));
  }
  if (typeof onInput === "function") {
    input.addEventListener("input", () => onInput(input.value));
  }
  return input;
}

export function textarea({ value = "", onChange, onInput, rows = 4, mono = false, placeholder = "" }) {
  const node = el("textarea", {
    rows,
    class: mono ? "mono" : "",
    placeholder,
  });
  node.value = value ?? "";
  if (typeof onChange === "function") {
    node.addEventListener("change", () => onChange(node.value));
  }
  if (typeof onInput === "function") {
    node.addEventListener("input", () => onInput(node.value));
  }
  return node;
}

export function numberInput({ value = 0, min, max, step = 1, onChange }) {
  const node = el("input", { type: "number", value });
  if (min !== undefined) node.min = String(min);
  if (max !== undefined) node.max = String(max);
  if (step !== undefined) node.step = String(step);
  if (typeof onChange === "function") {
    node.addEventListener("change", () => {
      const num = Number(node.value);
      onChange(Number.isNaN(num) ? 0 : num);
    });
  }
  return node;
}

export function switchControl({ checked = false, onChange, label = "" }) {
  const input = el("input", { type: "checkbox" });
  input.checked = !!checked;
  if (typeof onChange === "function") {
    input.addEventListener("change", () => onChange(input.checked));
  }
  const track = el("span", { class: "switch-track", "aria-hidden": "true" });
  return el("label", { class: "switch" }, [
    input,
    track,
    label ? el("span", { text: label }) : null,
  ]);
}

export function select({ value, options, onChange }) {
  const node = el("select", {});
  options.forEach((opt) => {
    const o = el("option", { value: opt.value, text: opt.label });
    if (opt.value === value) o.selected = true;
    node.appendChild(o);
  });
  if (typeof onChange === "function") {
    node.addEventListener("change", () => onChange(node.value));
  }
  return node;
}

export function segmented({ value, options, onChange }) {
  const wrap = el("div", { class: "segmented" });
  options.forEach((opt) => {
    const btn = el("button", {
      type: "button",
      class: `segmented-btn${opt.value === value ? " is-active" : ""}`,
      text: opt.label,
      onClick: () => {
        wrap.querySelectorAll(".segmented-btn").forEach((b) => b.classList.remove("is-active"));
        btn.classList.add("is-active");
        if (typeof onChange === "function") onChange(opt.value);
      },
    });
    wrap.appendChild(btn);
  });
  return wrap;
}

export function tagInput({ values = [], onChange, placeholder = "" }) {
  const items = [...values];
  const wrap = el("div", { class: "tag-input" });
  const input = el("input", { type: "text", placeholder });

  function render() {
    wrap.innerHTML = "";
    items.forEach((value, index) => {
      const chip = el("span", { class: "chip is-outline" }, [
        document.createTextNode(value),
        el("button", {
          type: "button",
          "aria-label": t("common.remove"),
          onClick: () => {
            items.splice(index, 1);
            if (typeof onChange === "function") onChange([...items]);
            render();
            input.focus();
          },
          text: "\u00d7",
        }),
      ]);
      wrap.appendChild(chip);
    });
    wrap.appendChild(input);
  }

  function commitInput() {
    const text = input.value.trim();
    if (!text) return;
    if (!items.includes(text)) {
      items.push(text);
      if (typeof onChange === "function") onChange([...items]);
    }
    input.value = "";
    render();
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commitInput();
    } else if (e.key === "Backspace" && !input.value && items.length) {
      items.pop();
      if (typeof onChange === "function") onChange([...items]);
      render();
    }
  });
  input.addEventListener("blur", commitInput);
  wrap.addEventListener("click", (e) => {
    if (e.target === wrap) input.focus();
  });

  render();
  return wrap;
}

export function slider({ value = 0, min = 0, max = 1, step = 0.01, onChange, format }) {
  const range = el("input", { type: "range", min, max, step, value });
  const display = el("span", { class: "slider-value", text: format ? format(value) : String(value) });
  range.addEventListener("input", () => {
    const num = Number(range.value);
    display.textContent = format ? format(num) : range.value;
    if (typeof onChange === "function") onChange(num);
  });
  return el("div", { class: "slider" }, [range, display]);
}

export function chip({ label, variant = "" }) {
  const cls = variant ? `chip is-${variant}` : "chip";
  return el("span", { class: cls, text: label });
}

export function statusChip(value, labels) {
  if (value === true || value === "ok") return chip({ label: labels.ok, variant: "ok" });
  if (value === false || value === "off") return chip({ label: labels.off, variant: "outline" });
  if (value === "warn") return chip({ label: labels.warn, variant: "warn" });
  if (value === "danger") return chip({ label: labels.danger, variant: "danger" });
  return chip({ label: labels.neutral || "-" });
}

export function empty({ title, message, icon }) {
  return el("div", { class: "empty" }, [
    icon ? el("div", { class: "empty-icon", html: icon }) : null,
    title ? el("div", { class: "empty-title", text: title }) : null,
    message ? el("div", { text: message }) : null,
  ]);
}

export function statCard({ label, value, meta, metaVariant = "" }) {
  return el("div", { class: "card is-compact" }, [
    el("div", { class: "stat" }, [
      el("div", { class: "stat-label", text: label }),
      el("div", { class: "stat-value", text: value }),
      meta ? el("div", { class: `stat-meta ${metaVariant ? `is-${metaVariant}` : ""}`.trim(), text: meta }) : null,
    ]),
  ]);
}

export function jsonView(value) {
  const node = el("pre", { class: "json-view" });
  node.textContent = JSON.stringify(value, null, 2);
  return node;
}

export function iconSvg(path, size = 18) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", path);
  svg.appendChild(p);
  return svg;
}

export function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}
