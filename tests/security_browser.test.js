"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const sw = fs.readFileSync(path.join(root, "web/sw.js"), "utf8");
const app = fs.readFileSync(path.join(root, "web/js/app.js"), "utf8");

async function serviceWorkerChecks() {
  const handlers = {};
  const calls = [];
  const deleted = [];
  const cachedRequests = [{url: "https://example.test/api/memory"}, {url: "https://example.test/index.html"}];
  const cache = {
    keys: async () => cachedRequests,
    delete: async (request) => { deleted.push(request.url); },
    match: async () => { calls.push("cache-read"); return {cached: true}; },
    put: async () => { calls.push("cache-write"); },
    addAll: async () => {},
  };
  const context = vm.createContext({URL, console,
    self: {location: {origin: "https://example.test"}, addEventListener: (kind, handler) => { handlers[kind] = handler; },
      clients: {claim: async () => {}, matchAll: async () => []}, skipWaiting: async () => {}},
    caches: {keys: async () => ["garmin-coach-v97", "garmin-coach-v100", "unrelated-cache"],
      delete: async (name) => { deleted.push(name); }, open: async () => cache},
    fetch: async (_, options) => { calls.push(["network", options]); throw new Error("synthetic offline"); },
  });
  vm.runInContext(sw, context);
  for (const pathname of ["/api/memory", "/api/goals", "/api/readiness", "/api/workouts/latest"]) {
    calls.length = 0;
    let result;
    handlers.fetch({request: {url: "https://example.test" + pathname, method: "GET", headers: {has: () => false}},
      respondWith: (promise) => { result = promise; }});
    await assert.rejects(result, /synthetic offline/);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "network");
    assert.equal(calls[0][1].cache, "no-store");
  }
  let activation;
  handlers.activate({waitUntil: (promise) => { activation = promise; }});
  await activation;
  assert(deleted.includes("garmin-coach-v97"));
  assert(deleted.includes("https://example.test/api/memory"));
  assert(!deleted.includes("unrelated-cache"));
  calls.length = 0;
  let staticResult;
  handlers.fetch({request: {url: "https://example.test/index.html", method: "GET", headers: {has: () => false}},
    respondWith: (promise) => { staticResult = promise; }});
  assert.equal((await staticResult).cached, true);
  assert.deepEqual(calls, ["cache-read"]);
}

async function accountChecks() {
  const source = app.slice(app.indexOf("function switchAccount("), app.indexOf('$("#garmin-badge-btn")'));
  let reloads = 0;
  let offline = false;
  const events = [];
  const values = new Map();
  const context = vm.createContext({URL, console,
    window: {location: {reload: () => { reloads++; }}},
    document: {getElementById: () => null},
    localStorage: {setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key)},
    loadAccounts: () => [{email: "second@example.test", token: "synthetic-second-token"}, {email: "first@example.test", token: "synthetic-first-token"}],
    saveAccounts: (accounts) => { context.remainingAccounts = accounts; },
    closeAccountModal: () => {}, showLogin: () => {}, resetReadiness: () => {}, toast: () => {},
    api: async (url) => { events.push(url); if (offline) throw new Error("synthetic offline"); return {}; },
  });
  vm.runInContext('let SESSION={user_email:"first@example.test",token:"synthetic-first-token"}; const SESSION_STORE="session"; let CURRENT_USER_EMAIL="first@example.test"; let _lastSummary={private:true}; let injuries=[]; let tableData=null; let pendingOverride=null; function activeAccountEmail(){return SESSION?.user_email;}', context);
  vm.runInContext(source, context);
  vm.runInContext('switchAccount("second@example.test")', context);
  assert.equal(reloads, 1);
  assert.equal(vm.runInContext("SESSION.user_email", context), "second@example.test");
  assert.equal(events.length, 0); // Fresh document boot handles consent, no stale-page requests.
  await vm.runInContext("logout()", context);
  assert.equal(events[0], "/api/user/logout");
  assert.equal(vm.runInContext("SESSION", context), null);
  assert.equal(context.remainingAccounts.length, 1);
  assert.equal(context.remainingAccounts[0].email, "first@example.test");
  assert.equal(reloads, 2);
  vm.runInContext('SESSION={user_email:"first@example.test",token:"synthetic-first-token"}', context);
  offline = true;
  await vm.runInContext("logout()", context);
  assert.notEqual(vm.runInContext("SESSION", context), null); // Never report server revocation while offline.
  assert.equal(reloads, 2);
}

function goalMetricsChecks() {
  const nodes = new Map(["#goal-threshold-hr", "#goal-threshold-pace", "#goal-max-hr", "#goal-vo2max"].map(id => [id, {textContent: "old-account"}]));
  const context = vm.createContext({$: selector => nodes.get(selector), metricValue: value => Number.isFinite(value) ? String(value) : "—", fmtPace: value => String(value)});
  const start = app.indexOf("function renderGoalMetrics(");
  vm.runInContext(app.slice(start, app.indexOf("function resetReadiness(", start)), context);
  vm.runInContext('renderGoalMetrics({garmin_official_threshold:{heart_rate:169,pace_sec_per_km:303},atleta:{fc_max:191},garmin_vo2max:47})', context);
  assert.equal(nodes.get("#goal-threshold-hr").textContent, "169");
  assert.equal(nodes.get("#goal-threshold-pace").textContent, "303");
  assert.equal(nodes.get("#goal-max-hr").textContent, "191");
  assert.equal(nodes.get("#goal-vo2max").textContent, "47");
  vm.runInContext("renderGoalMetrics({})", context);
  for (const node of nodes.values()) assert.equal(node.textContent, "—");
  const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
  for (const id of nodes.keys()) assert(html.includes('id="' + id.slice(1) + '">—</span>'));
  assert(app.slice(app.indexOf("function resetReadiness("), app.indexOf("async function loadReadiness(")).includes("renderGoalMetrics({})"));
}

(async () => {
  await serviceWorkerChecks();
  await accountChecks();
  goalMetricsChecks();
  console.log("PASS: API offline never reads cache; old API caches removed; static shell preserved; account switch reloads; logout revokes online and reports offline failure; goal metrics use current account and clear on reset.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
