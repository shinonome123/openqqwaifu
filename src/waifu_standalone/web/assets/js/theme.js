// Theme family + dark/light mode with View Transitions when available.

const PREFS_KEY = "waifu:prefs";

function readPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
  } catch (err) {
    return {};
  }
}

function writePrefs(next) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(next));
  } catch (err) {
    /* ignore */
  }
}

function prefersReducedMotion() {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (err) {
    return false;
  }
}

export function getThemeMode() {
  return document.documentElement.getAttribute("data-theme-mode") || "dark";
}

export function getThemeFamily() {
  return document.documentElement.getAttribute("data-theme") || "default";
}

function applyAttributes(mode, family) {
  document.documentElement.setAttribute("data-theme-mode", mode);
  document.documentElement.setAttribute("data-theme", family);
}

function withTransition(change) {
  const canTransition =
    typeof document.startViewTransition === "function" && !prefersReducedMotion();
  if (!canTransition) {
    change();
    return;
  }
  document.startViewTransition(change);
}

export function setThemeMode(mode) {
  if (mode !== "light" && mode !== "dark") return;
  if (getThemeMode() === mode) return;
  withTransition(() => applyAttributes(mode, getThemeFamily()));
  const prefs = readPrefs();
  prefs.themeMode = mode;
  writePrefs(prefs);
}

export function toggleThemeMode() {
  setThemeMode(getThemeMode() === "dark" ? "light" : "dark");
}

export function setThemeFamily(family) {
  if (!family) return;
  if (getThemeFamily() === family) return;
  withTransition(() => applyAttributes(getThemeMode(), family));
  const prefs = readPrefs();
  prefs.themeFamily = family;
  writePrefs(prefs);
}

export function syncThemeToggleUI(button) {
  if (!button) return;
  const mode = getThemeMode();
  const darkIcon = button.querySelector(".theme-icon-dark");
  const lightIcon = button.querySelector(".theme-icon-light");
  if (darkIcon && lightIcon) {
    darkIcon.hidden = mode === "light";
    lightIcon.hidden = mode === "dark";
  }
}
