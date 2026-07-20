/* Hash routing (delivery plan P1 / §5).
 *
 * The router owns exactly two things: turning a location.hash into a route
 * object, and turning a route object back into a hash. It holds no view
 * state and never renders — app.js subscribes and decides what to draw.
 *
 * Target route shapes are the plan's (§5): `#/home`, `#/goals/{id}`,
 * `#/runs`, `#/runs/{id}`, `#/inbox`, `#/ops`. Legacy run links keep their
 * exact shape.
 */

const KNOWN_VIEWS = [
  "home", "goals", "workflows", "runs", "inbox", "artifacts",
  "agents", "ops", "settings",
];
const RUN_TABS = ["overview", "timeline", "plan", "graph", "data", "errors"];

export function readRoute(hash = location.hash) {
  const parts = hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "runs" && parts[1]) {
    const route = { view: "run", runId: decodeURIComponent(parts[1]) };
    // Keep the canonical deep link compact: overview is the default and is
    // therefore represented by its legacy two-segment shape. Only explicit
    // secondary tabs become part of the route object.
    if (RUN_TABS.includes(parts[2]) && parts[2] !== "overview") route.tab = parts[2];
    return route;
  }
  if (parts[0] === "goals" && parts[1]) {
    return { view: "goal", runId: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "workflows" && parts[1] && parts[2] === "edit" && parts[3]) {
    return {
      view: "workflowEdit",
      workflowId: decodeURIComponent(parts[1]),
      draftId: decodeURIComponent(parts[3]),
      runId: null,
    };
  }
  if (parts[0] === "artifacts" && parts[1]) {
    return { view: "artifact", artifactId: decodeURIComponent(parts[1]), runId: null };
  }
  if (KNOWN_VIEWS.includes(parts[0])) return { view: parts[0], runId: null };
  return { view: "home", runId: null };
}

export function routeHash(route) {
  if (route.view === "run") {
    const base = `#/runs/${encodeURIComponent(route.runId)}`;
    return !route.tab || route.tab === "overview" ? base : `${base}/${route.tab}`;
  }
  if (route.view === "goal") return `#/goals/${encodeURIComponent(route.runId)}`;
  if (route.view === "workflowEdit") {
    return `#/workflows/${encodeURIComponent(route.workflowId)}/edit/${encodeURIComponent(route.draftId)}`;
  }
  if (route.view === "artifact") {
    return `#/artifacts/${encodeURIComponent(route.artifactId)}`;
  }
  return `#/${route.view}`;
}

export class Router {
  constructor(onChange) {
    this.route = readRoute();
    this.onChange = onChange;
    window.addEventListener("hashchange", () => {
      this.route = readRoute();
      this.onChange(this.route);
    });
  }

  /** Navigate to `next`; renders via the hashchange handler, or directly
   *  when the hash would not change (a same-page refresh). */
  navigate(next) {
    this.route = next;
    const hash = routeHash(next);
    if (location.hash !== hash) location.hash = hash;
    else this.onChange(this.route);
  }
}
