// Hash-based router with a listener pattern.

export function createRouter(routes, defaultRoute) {
  const handlers = new Set();
  const table = new Map();
  routes.forEach((route) => table.set(route.id, route));

  function current() {
    const hash = (location.hash || "").replace(/^#\/?/, "");
    const id = hash.split("/")[0] || defaultRoute;
    return table.has(id) ? id : defaultRoute;
  }

  function navigate(id) {
    if (!table.has(id)) return;
    if (location.hash === `#/${id}`) {
      emit(id);
      return;
    }
    location.hash = `#/${id}`;
  }

  function onChange(fn) {
    handlers.add(fn);
    return () => handlers.delete(fn);
  }

  function emit(id) {
    const route = table.get(id) || table.get(defaultRoute);
    handlers.forEach((fn) => {
      try {
        fn(route);
      } catch (err) {
        console.error(err);
      }
    });
  }

  window.addEventListener("hashchange", () => emit(current()));

  return {
    current,
    navigate,
    onChange,
    route: (id) => table.get(id),
    routes,
    start() {
      emit(current());
    },
  };
}
