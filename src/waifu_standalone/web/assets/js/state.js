// Tiny observable store. Keeps a single authoritative draft of the config.

export function createStore(initial) {
  let state = deepClone(initial);
  const listeners = new Set();

  function get() {
    return state;
  }

  function set(next) {
    state = deepClone(next);
    listeners.forEach((fn) => fn(state));
  }

  function patch(updater) {
    const draft = deepClone(state);
    const result = updater(draft);
    set(result === undefined ? draft : result);
  }

  function subscribe(fn) {
    listeners.add(fn);
    fn(state);
    return () => listeners.delete(fn);
  }

  return { get, set, patch, subscribe };
}

export function deepClone(value) {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(deepClone);
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = deepClone(v);
  }
  return out;
}

export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a && b && typeof a === "object") {
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    if (Array.isArray(a)) {
      if (a.length !== b.length) return false;
      for (let i = 0; i < a.length; i += 1) {
        if (!deepEqual(a[i], b[i])) return false;
      }
      return true;
    }
    const keysA = Object.keys(a);
    const keysB = Object.keys(b);
    if (keysA.length !== keysB.length) return false;
    for (const key of keysA) {
      if (!Object.prototype.hasOwnProperty.call(b, key)) return false;
      if (!deepEqual(a[key], b[key])) return false;
    }
    return true;
  }
  return false;
}
