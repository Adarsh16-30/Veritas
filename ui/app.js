// VERITAS trace explorer (PRD Phase 5). No build step, no dependencies.
//
// Every string shown here came out of the trace store, and much of it came out
// of ERPNext first -- item names, supplier names, remarks. The fault harness
// deliberately plants instruction text in those fields. So nothing is ever
// assigned through innerHTML: every node is built with textContent via h(),
// and only http(s) URLs become links.
"use strict";

const STEPS = ["S1", "S2", "S3", "S4", "S5", "S6"];
const STEP_NAME = {
  S1: "requisition",
  S2: "policy check",
  S3: "purchase order",
  S4: "three-way match",
  S5: "discrepancy",
  S6: "payment",
};

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function safeLink(url, text) {
  if (typeof url === "string" && /^https?:\/\//i.test(url)) {
    return h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text);
  }
  return h("span", {}, text);
}

function badge(text, kind) {
  return h("span", { class: `badge ${kind || ""}` }, text);
}

function fmt(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3);
  return String(v);
}

function kv(rows) {
  return h(
    "dl",
    { class: "kv" },
    rows.filter(Boolean).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v instanceof Node ? v : fmt(v))]),
  );
}

function details(summary, body, open) {
  return h("details", { open: open || false }, h("summary", {}, summary), body);
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail || detail;
    } catch (_) {
      /* not JSON */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
  return r.json();
}

// --- routing: #/wf/<id>[/<step>] -------------------------------------------------
function parseHash() {
  const m = location.hash.match(/^#\/wf\/([^/]+)(?:\/(S[1-6]))?/);
  return m ? { id: decodeURIComponent(m[1]), step: m[2] || null } : { id: null, step: null };
}

function go(id, step) {
  const next = `#/wf/${encodeURIComponent(id)}${step ? "/" + step : ""}`;
  if (location.hash !== next) location.hash = next;
  else render();
}

// --- sidebar ------------------------------------------------------------------
async function loadList() {
  const list = document.getElementById("workflows");
  const params = new URLSearchParams();
  const prefix = document.getElementById("prefix").value.trim();
  const status = document.getElementById("status").value;
  if (prefix) params.set("prefix", prefix);
  if (status) params.set("status", status);
  list.replaceChildren(h("li", { class: "muted" }, "loading…"));
  try {
    const rows = await getJSON(`/api/workflows?${params}`);
    const current = parseHash().id;
    list.replaceChildren(
      ...(rows.length
        ? rows.map((w) =>
            h(
              "li",
              {
                class: `wf-item ${w.workflow_id === current ? "active" : ""}`,
                onclick: () => go(w.workflow_id),
              },
              h("div", { class: "wf-id" }, w.workflow_id),
              h(
                "div",
                { class: "wf-meta" },
                badge(w.status, `st-${w.status}`),
                w.fault_class ? badge(w.fault_class, "fault") : null,
                w.expected_terminal_action ? h("span", { class: "muted" }, `expect ${w.expected_terminal_action}`) : null,
              ),
            ),
          )
        : [h("li", { class: "muted" }, "no workflows match")]),
    );
  } catch (e) {
    list.replaceChildren(h("li", { class: "error" }, `could not load workflows — ${e.message}`));
  }
}

// --- workflow view ----------------------------------------------------------------
let cache = { id: null, recon: null };

async function render() {
  const { id, step } = parseHash();
  const main = document.getElementById("main");
  document.querySelectorAll(".wf-item").forEach((li) => {
    li.classList.toggle("active", li.querySelector(".wf-id")?.textContent === id);
  });
  if (!id) return;
  if (cache.id !== id) {
    main.replaceChildren(h("p", { class: "muted" }, "reconstructing…"));
    try {
      cache = { id, recon: await getJSON(`/api/workflows/${encodeURIComponent(id)}`) };
    } catch (e) {
      cache = { id: null, recon: null };
      main.replaceChildren(h("p", { class: "error" }, `could not load ${id} — ${e.message}`));
      return;
    }
  }
  const recon = cache.recon;
  const firstInteresting =
    recon.steps.find((s) => s.checkpoint === "escalated" || s.attempts.length > 1) ||
    [...recon.steps].reverse().find((s) => s.attempts.length) ||
    recon.steps[0];
  const selected = step || firstInteresting.step;
  main.replaceChildren(header(recon), timeline(recon, selected), stepPanel(recon, selected));
}

function header(recon) {
  const wf = recon.workflow;
  const label = recon.label;
  const actual = wf.terminal_action;
  const match =
    label && actual ? (label.expected_terminal_action === actual ? badge("matches label", "ok") : badge("differs from label", "bad")) : null;
  return h(
    "section",
    { class: "card head" },
    h("h1", {}, wf.workflow_id),
    h(
      "div",
      { class: "row" },
      badge(recon.config, recon.config === "verified" ? "cfg-verified" : "cfg-baseline"),
      badge(wf.status, `st-${wf.status}`),
      label ? badge(label.fault_class, "fault") : badge("no label", ""),
      match,
    ),
    kv([
      ["expected terminal action", label ? label.expected_terminal_action : null],
      ["fault first visible at", label ? label.fault_step : null],
      ["actual terminal action", actual],
      ["escalation reason", wf.escalation_reason],
      ["model calls", wf.llm_calls],
      ["last checkpoint", wf.last_checkpoint_ts],
    ]),
    recon.documents.length
      ? details(
          `ERPNext documents committed (${recon.documents.length})`,
          h(
            "ul",
            { class: "docs" },
            recon.documents.map((d) =>
              h("li", {}, h("span", { class: "muted" }, `${d.step} · ${d.doctype} `), safeLink(d.url, d.name)),
            ),
          ),
          true,
        )
      : h("p", { class: "muted" }, "no ERPNext documents committed"),
  );
}

function stepState(s) {
  if (s.checkpoint === "escalated") return "escalated";
  if (s.checkpoint === "committed") return "committed";
  if (s.checkpoint === "in_progress") return "in_progress";
  return s.attempts.length ? "in_progress" : "not_reached";
}

function timeline(recon, selected) {
  return h(
    "nav",
    { class: "timeline", "aria-label": "Steps" },
    recon.steps.map((s) => {
      const state = stepState(s);
      const traced = s.attempts.filter((a) => !a.untraced).length;
      return h(
        "button",
        {
          class: `tl-step tl-${state} ${s.step === selected ? "selected" : ""}`,
          onclick: () => go(recon.workflow.workflow_id, s.step),
          disabled: state === "not_reached",
          "aria-current": s.step === selected ? "step" : null,
        },
        h("span", { class: "tl-id" }, s.step),
        h("span", { class: "tl-name" }, STEP_NAME[s.step]),
        h("span", { class: "tl-state" }, state.replace("_", " ")),
        s.attempts.length > 1 ? h("span", { class: "tl-attempts" }, `${s.attempts.length} attempts${traced < s.attempts.length ? " · gap" : ""}`) : null,
      );
    }),
  );
}

function stepPanel(recon, stepId) {
  const s = recon.steps.find((x) => x.step === stepId);
  if (!s || !s.attempts.length) {
    return h("section", { class: "card" }, h("p", { class: "muted" }, `${stepId} was never reached.`));
  }
  const traced = s.attempts.filter((a) => !a.untraced);
  return h(
    "section",
    { class: "step-panel" },
    traced.length >= 2 ? diffView(recon, s, traced) : null,
    s.attempts.map((a) => attemptCard(recon, s, a)),
  );
}

function attemptCard(recon, s, a) {
  if (a.untraced) {
    return h(
      "article",
      { class: "card attempt untraced" },
      h("h2", {}, `${s.step} · attempt ${a.attempt}`),
      h(
        "p",
        { class: "muted" },
        "No trace was recorded for this attempt. The executor's output was rejected before it could be traced " +
          "(or the worker stopped mid-attempt); the next attempt's context carries the rejection reason.",
      ),
    );
  }
  const routeKind = a.route === "commit" ? "ok" : a.route === "retry" ? "warn" : "bad";
  return h(
    "article",
    { class: "card attempt" },
    h(
      "h2",
      {},
      `${s.step} · attempt ${a.attempt} `,
      badge(`proposed ${fmt(a.action)}`, ""),
      badge(`route ${fmt(a.route)}`, routeKind),
      a.committed ? badge("committed", "ok") : null,
      a.outcome_recorded ? null : badge("no outcome recorded", "warn"),
    ),
    a.reason ? h("p", { class: "reason" }, a.reason) : null,
    h(
      "div",
      { class: "grid" },
      h("div", {}, h("h3", {}, "What the agent saw"), h("pre", { class: "context" }, a.step_context), factsTable(a)),
      h("div", {}, executorBox(a), verifierBox(recon, a), gateBox(a)),
    ),
  );
}

function factsTable(a) {
  if (!a.facts.length) return h("p", { class: "muted" }, "no DELTA facts recorded");
  return h(
    "table",
    { class: "facts" },
    h("thead", {}, h("tr", {}, h("th", {}, "DELTA fact"), h("th", {}, "value"), h("th", {}, "source"))),
    h(
      "tbody",
      {},
      a.facts.map((f) =>
        h(
          "tr",
          { class: f.check === false ? "failed" : "" },
          h("td", {}, f.name),
          h("td", { class: "val" }, f.check === true ? "✓ True" : f.check === false ? "✗ False" : f.value),
          h("td", {}, sources(f.sources)),
        ),
      ),
    ),
  );
}

function sources(list) {
  if (list === null) return h("span", { class: "warn-text" }, "provenance not declared");
  return h(
    "ul",
    { class: "sources" },
    list.map((src) => {
      if (src.kind === "request") return h("li", { class: "muted" }, src.label);
      if (!src.resolved) return h("li", { class: "muted" }, src.label);
      return h("li", {}, safeLink(src.url, src.label));
    }),
  );
}

function callDetails(call) {
  return details(
    "prompt & response",
    h(
      "div",
      {},
      kv([
        ["prompt hash", h("code", {}, fmt(call.prompt_hash))],
        ["response hash", h("code", {}, fmt(call.response_hash))],
      ]),
      h("h4", {}, "prompt"),
      h("pre", {}, call.prompt || "—"),
      h("h4", {}, "response"),
      h("pre", {}, call.response || "—"),
    ),
  );
}

function executorBox(a) {
  const ex = a.executor;
  if (!ex) return h("div", { class: "box" }, h("h3", {}, "Executor"), h("p", { class: "muted" }, "no executor trace"));
  return h(
    "div",
    { class: "box" },
    h("h3", {}, "Executor"),
    kv([
      ["model", ex.model],
      ["action", ex.action],
      ["latency", a.latency_ms != null ? `${a.latency_ms} ms` : null],
      ["amount at stake", a.amount_at_stake],
    ]),
    h("h4", {}, "rationale"),
    h("blockquote", {}, ex.rationale || "—"),
    callDetails(ex),
  );
}

function verifierBox(recon, a) {
  if (recon.config === "baseline") {
    return h("div", { class: "box muted-box" }, h("h3", {}, "Verifier"), h("p", { class: "muted" }, "baseline run: no gate, by design (Phase 2 denominator)"));
  }
  const v = a.verifier;
  if (!v) {
    return h(
      "div",
      { class: "box" },
      h("h3", {}, "Verifier"),
      // verify/gate.py returns before (or instead of) a usable verifier call in
      // three cases; the gate's own reason says which, so show that, not a guess.
      h(
        "p",
        { class: "muted" },
        a.rules && !a.rules.ok
          ? "not consulted — a hard invariant stopped this attempt first"
          : `no verifier call recorded for this attempt${a.reason ? ` (gate: ${a.reason})` : ""}`,
      ),
    );
  }
  return h(
    "div",
    { class: "box" },
    h("h3", {}, "Independent verifier ", badge(v.verdict, v.verdict === "pass" ? "ok" : "bad")),
    h("p", { class: "note" }, "Shown the step context and proposed action only; the executor's rationale is withheld (Rule 3)."),
    kv([
      ["model", v.model],
      ["confidence", v.confidence],
      ["latency", a.verifier_latency_ms != null ? `${a.verifier_latency_ms} ms` : null],
      v.independence ? ["independence", h("code", {}, JSON.stringify(v.independence))] : null,
    ]),
    v.violated_expectations.length
      ? [h("h4", {}, "violated expectations"), h("ul", { class: "violations" }, v.violated_expectations.map((e) => h("li", {}, e)))]
      : null,
    v.checklist.length ? details(`checklist (${v.checklist.length})`, h("ol", {}, v.checklist.map((c) => h("li", {}, c)))) : null,
    callDetails(v),
  );
}

function gateBox(a) {
  if (!a.rules && !a.region) return null;
  const violations = (a.rules && a.rules.violations) || [];
  const region = a.region;
  return h(
    "div",
    { class: "box" },
    h("h3", {}, "Gate"),
    kv([
      ["rules", a.rules ? (a.rules.ok ? "all invariants hold" : `${violations.length} violated`) : null],
      region ? ["region", `{${region.labels.join(", ")}}`] : null,
      region ? ["calibrated", region.calibrated ? `yes (alpha ${fmt(region.alpha)})` : "no — unanimous fallback, no coverage claim"] : null,
      region && region.p_commit != null ? ["p(commit)", region.p_commit] : null,
    ]),
    violations.length
      ? h(
          "ul",
          { class: "violations" },
          violations.map((v) => h("li", {}, h("code", {}, v.rule_id), ` — ${v.message} (${v.kind})`)),
        )
      : null,
    Object.keys(a.signals || {}).length ? details("signals", h("pre", {}, JSON.stringify(a.signals, null, 2))) : null,
  );
}

// --- retry diff ------------------------------------------------------------------
function diffView(recon, s, traced) {
  const nums = traced.map((a) => a.attempt);
  const selA = h("select", { "aria-label": "from attempt" }, nums.map((n) => h("option", { value: n }, `attempt ${n}`)));
  const selB = h("select", { "aria-label": "to attempt" }, nums.map((n) => h("option", { value: n }, `attempt ${n}`)));
  selA.value = String(nums[nums.length - 2]);
  selB.value = String(nums[nums.length - 1]);
  const out = h("div", { class: "diff-out" });

  async function run() {
    out.replaceChildren(h("p", { class: "muted" }, "diffing…"));
    const params = new URLSearchParams({ step: s.step, a: selA.value, b: selB.value });
    try {
      const d = await getJSON(`/api/workflows/${encodeURIComponent(recon.workflow.workflow_id)}/diff?${params}`);
      // replaceChildren() would render a skipped section as the text "null".
      out.replaceChildren(
        ...[
        d.decision_changes.length
          ? h(
              "table",
              { class: "facts" },
              h("thead", {}, h("tr", {}, h("th", {}, "decision"), h("th", {}, `attempt ${d.a}`), h("th", {}, `attempt ${d.b}`))),
              h("tbody", {}, d.decision_changes.map((c) => h("tr", {}, h("td", {}, c.field), h("td", {}, fmt(c.before)), h("td", {}, fmt(c.after))))),
            )
          : h("p", { class: "muted" }, "decision unchanged"),
        d.fact_changes.length
          ? h(
              "table",
              { class: "facts" },
              h("thead", {}, h("tr", {}, h("th", {}, "fact"), h("th", {}, `attempt ${d.a}`), h("th", {}, `attempt ${d.b}`))),
              h("tbody", {}, d.fact_changes.map((c) => h("tr", {}, h("td", {}, c.name), h("td", {}, fmt(c.before)), h("td", {}, fmt(c.after))))),
            )
          : h("p", { class: "muted" }, "evidence unchanged — every DELTA fact is identical"),
        d.expectations_resolved.length ? h("p", {}, "no longer violated: ", d.expectations_resolved.join("; ")) : null,
        d.expectations_new.length ? h("p", {}, "newly violated: ", d.expectations_new.join("; ")) : null,
        h("h4", {}, "step context"),
        d.context_diff.length
          ? h(
              "pre",
              { class: "udiff" },
              d.context_diff.map((line) =>
                h(
                  "span",
                  { class: line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : "" },
                  line + "\n",
                ),
              ),
            )
          : h("p", { class: "muted" }, "context identical"),
        ].filter(Boolean),
      );
    } catch (e) {
      out.replaceChildren(h("p", { class: "error" }, e.message));
    }
  }
  selA.addEventListener("change", run);
  selB.addEventListener("change", run);
  run();
  return h("section", { class: "card diff" }, h("h2", {}, `Retry diff · ${s.step} `, selA, " → ", selB), out);
}

// --- boot ------------------------------------------------------------------------
let debounce;
document.getElementById("prefix").addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(loadList, 250);
});
document.getElementById("status").addEventListener("change", loadList);
document.getElementById("filters").addEventListener("submit", (e) => {
  e.preventDefault();
  loadList();
});
window.addEventListener("hashchange", render);
loadList().then(render);
