"use strict";

const VERDICTS = {
  AC: "Accepted",
  WA: "Wrong answer",
  TLE: "Time limit exceeded",
  MLE: "Memory limit exceeded",
  OLE: "Output limit exceeded",
  RE: "Runtime error",
  CE: "Compilation error",
  SE: "Judge error",
};

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* private mode */ } },
};

const view = document.getElementById("view");
let cleanup = () => {};

// ------------------------------------------------------------------ helpers

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = store.get("token");
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401 && token) {
    store.set("token", null);
    location.hash = "#/login";
  }
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : `request failed (${res.status})`);
  }
  return body;
}

function verdictLabel(sub) {
  if (sub.status !== "done") return h("span", { class: "verdict pending" }, sub.status === "running" ? "Running" : "In queue");
  return h("span", { class: `verdict ${sub.verdict}` }, VERDICTS[sub.verdict] || sub.verdict);
}

// Time and memory mean nothing for a submission that never ran.
function ran(sub) {
  return sub.status === "done" && sub.verdict !== "CE" && sub.verdict !== "SE";
}

function when(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// A tiny Markdown subset for statements: headings, paragraphs, code.
function renderStatement(md) {
  const out = h("div", { class: "statement" });
  const blocks = md.split(/\n{2,}/);
  for (const block of blocks) {
    if (block.startsWith("```")) {
      out.append(h("pre", {}, block.replace(/^```\w*\n?|```$/g, "")));
    } else if (block.startsWith("# ")) {
      continue; // the title is shown as the page heading already
    } else if (block.startsWith("## ")) {
      out.append(h("h2", {}, block.slice(3)));
    } else {
      const p = h("p");
      block.split(/(`[^`]+`)/).forEach((part) => {
        p.append(part.startsWith("`") ? h("code", {}, part.slice(1, -1)) : part);
      });
      out.append(p);
    }
  }
  return out;
}

function show(...nodes) {
  cleanup();
  cleanup = () => {};
  view.replaceChildren(...nodes);
  view.focus({ preventScroll: true });
}

function needLogin() {
  if (store.get("token")) return false;
  show(h("p", { class: "notice" }, "Log in to submit and see your submissions. ", h("a", { href: "#/login" }, "Log in")));
  return true;
}

// ------------------------------------------------------------------ verdict tape

function tape(total) {
  const headline = h("div", { class: "headline" }, "In queue");
  const detail = h("div", { class: "detail" }, "Waiting for a free judge");
  const cells = Array.from({ length: total }, (_, i) => h("div", { class: "cell", title: `Test ${i + 1}` }, i + 1));
  const message = h("div");
  const root = h("div", { class: "tape", "aria-live": "polite" }, headline, h("div", { class: "cells" }, cells), detail, message);

  return {
    root,
    apply(e) {
      if (e.type === "compiling") {
        headline.textContent = "Compiling";
        detail.textContent = "";
      } else if (e.type === "compiled" && e.ok) {
        headline.textContent = "Running tests";
        cells[0]?.classList.add("running");
      } else if (e.type === "retrying") {
        headline.textContent = "Judge error, trying again";
        cells.forEach((c) => (c.className = "cell"));
      } else if (e.type === "test") {
        const cell = cells[e.index - 1];
        if (cell) {
          cell.className = `cell ${e.verdict}`;
          cell.title = `Test ${e.index}: ${VERDICTS[e.verdict]}, ${e.cpu_ms} ms, ${Math.round(e.memory_kb / 1024)} MB`;
        }
        if (e.verdict === "AC") cells[e.index]?.classList.add("running");
        detail.textContent = `Test ${e.index} of ${e.total}: ${e.cpu_ms} ms`;
      } else if (e.type === "done") {
        cells.forEach((c) => c.classList.remove("running"));
        headline.replaceChildren(h("span", { class: `verdict ${e.verdict}` }, VERDICTS[e.verdict] || e.verdict));
        headline.firstChild.style.fontSize = "22px";
        detail.textContent = e.verdict === "CE" ? "" :
          `Slowest test ${e.cpu_ms} ms, peak memory ${Math.round((e.memory_kb || 0) / 1024)} MB`;
        if (e.message && e.verdict !== "AC") message.replaceChildren(h("div", { class: "msg" }, e.message));
      }
    },
    compileError(text) {
      message.replaceChildren(h("div", { class: "msg" }, text || "The compiler gave no message."));
    },
  };
}

function follow(id, t, onDone) {
  const source = new EventSource(`/api/submissions/${id}/events?token=${encodeURIComponent(store.get("token"))}`);
  source.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    t.apply(e);
    if (e.type === "done") {
      source.close();
      onDone?.(e);
    }
  };
  source.onerror = () => {
    // The stream ends after "done"; on a real drop, fall back to one fetch.
    source.close();
    api(`/api/submissions/${id}`).then((sub) => {
      if (sub.status === "done") t.apply({ type: "done", ...sub });
    }).catch(() => {});
  };
  return () => source.close();
}

// ------------------------------------------------------------------ pages

async function problemsPage() {
  const problems = await api("/api/problems");
  show(
    h("h1", {}, "Problems"),
    h("p", { class: "lede" }, "Pick a problem, write a solution in C++, Python or Java, and watch it run against every test."),
    h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Problem"), h("th", { class: "num" }, "Time limit"), h("th", { class: "num" }, "Memory"))),
      h("tbody", {}, problems.map((p) => h("tr", {},
        h("td", {}, h("a", { href: `#/problems/${p.slug}` }, p.title)),
        h("td", { class: "num" }, `${p.time_limit_ms / 1000} s`),
        h("td", { class: "num" }, `${p.memory_mb} MB`),
      ))),
    ),
  );
}

async function problemPage(slug, contest) {
  const [p, languages] = await Promise.all([api(`/api/problems/${slug}`), api("/api/languages")]);
  const draftKey = (lang) => `draft:${slug}:${lang}`;
  const lang = h("select", { "aria-label": "Language" }, languages.map((l) => h("option", { value: l.key }, l.name)));
  lang.value = store.get("lang") || languages[0].key;
  const code = h("textarea", { class: "code", spellcheck: "false", wrap: "off", "aria-label": "Source code" });
  code.value = store.get(draftKey(lang.value)) || "";
  code.addEventListener("input", () => store.set(draftKey(lang.value), code.value));
  code.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      code.setRangeText("    ", code.selectionStart, code.selectionEnd, "end");
    }
  });
  lang.addEventListener("change", () => {
    store.set("lang", lang.value);
    code.value = store.get(draftKey(lang.value)) || "";
  });

  const result = h("div");
  const error = h("div", { class: "error" });
  const submit = h("button", {
    onclick: async () => {
      if (needLogin()) return;
      submit.disabled = true;
      error.textContent = "";
      try {
        const res = await api("/api/submissions", {
          method: "POST",
          body: JSON.stringify({ problem: slug, language: lang.value, source: code.value, contest: contest || null }),
        });
        const t = tape(p.tests);
        result.replaceChildren(t.root);
        const stop = follow(res.id, t, async (e) => {
          submit.disabled = false;
          if (e.verdict === "CE") t.compileError((await api(`/api/submissions/${res.id}`)).compile_message);
        });
        cleanup = stop;
      } catch (err) {
        error.textContent = err.message;
        submit.disabled = false;
      }
    },
  }, "Submit");

  show(
    h("h1", {}, p.title),
    h("p", { class: "limits" }, `Time limit ${p.time_limit_ms / 1000} s per test, memory ${p.memory_mb} MB, ${p.tests} tests`,
      contest ? ` (contest ${contest})` : ""),
    h("div", { class: "split" },
      renderStatement(p.statement),
      h("div", { class: "editor" },
        h("div", { class: "row" }, lang, h("span", { class: "spacer" }), submit),
        code, error, result),
    ),
  );
}

async function submissionsPage() {
  if (needLogin()) return;
  const subs = await api("/api/submissions");
  show(
    h("h1", {}, "My submissions"),
    subs.length === 0
      ? h("p", { class: "notice" }, "Nothing yet. Open a problem and submit a solution.")
      : h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Problem"), h("th", {}, "Language"),
          h("th", {}, "Verdict"), h("th", { class: "num" }, "Time"), h("th", { class: "num" }, "Memory"), h("th", {}, "Sent"))),
        h("tbody", {}, subs.map((s) => h("tr", {},
          h("td", {}, h("a", { href: `#/submissions/${s.id}` }, s.id)),
          h("td", {}, h("a", { href: `#/problems/${s.problem}` }, s.problem)),
          h("td", {}, s.language),
          h("td", {}, verdictLabel(s)),
          h("td", { class: "num" }, ran(s) ? `${s.cpu_ms} ms` : ""),
          h("td", { class: "num" }, ran(s) ? `${Math.round(s.memory_kb / 1024)} MB` : ""),
          h("td", {}, when(s.created_at)),
        )))),
  );
}

async function submissionPage(id) {
  if (needLogin()) return;
  const s = await api(`/api/submissions/${id}`);
  show(
    h("h1", {}, `Submission ${s.id}`),
    h("p", { class: "lede" }, h("a", { href: `#/problems/${s.problem}` }, s.problem), `, ${s.language}, `, verdictLabel(s)),
    s.compile_message ? h("div", { class: "msg" }, s.compile_message) : null,
    s.message && s.verdict !== "CE" ? h("div", { class: "msg" }, s.message) : null,
    s.tests.length ? h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Test"), h("th", {}, "Verdict"), h("th", { class: "num" }, "Time"), h("th", { class: "num" }, "Memory"))),
      h("tbody", {}, s.tests.map((t) => h("tr", {},
        h("td", {}, t.name), h("td", {}, h("span", { class: `verdict ${t.verdict}` }, VERDICTS[t.verdict])),
        h("td", { class: "num" }, `${t.cpu_ms} ms`), h("td", { class: "num" }, `${Math.round(t.memory_kb / 1024)} MB`))))) : null,
    h("h2", {}, "Source"),
    h("pre", { class: "msg", style: "border-left-color: var(--line)" }, s.source),
  );
}

async function contestsPage() {
  const contests = await api("/api/contests");
  show(
    h("h1", {}, "Contests"),
    contests.length === 0
      ? h("p", { class: "notice" }, "No contests are scheduled.")
      : h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Contest"), h("th", {}, "Starts"), h("th", {}, "Ends"))),
        h("tbody", {}, contests.map((c) => h("tr", {},
          h("td", {}, h("a", { href: `#/contests/${c.slug}` }, c.title)),
          h("td", {}, when(c.starts_at)), h("td", {}, when(c.ends_at)))))),
  );
}

async function contestPage(slug) {
  const c = await api(`/api/contests/${slug}`);
  const board = h("div");
  const joinError = h("span", { class: "error" });
  const join = h("button", {
    onclick: async () => {
      if (needLogin()) return;
      try {
        await api(`/api/contests/${slug}/join`, { method: "POST" });
        join.textContent = "Joined";
        join.disabled = true;
        refresh();
      } catch (err) { joinError.textContent = err.message; }
    },
  }, "Join contest");

  async function refresh() {
    const token = store.get("token");
    const data = await api(`/api/contests/${slug}/scoreboard${token ? `?token=${encodeURIComponent(token)}` : ""}`);
    const notice = data.frozen
      ? [h("p", { class: "frozen" }, "The scoreboard is frozen. Other contestants' last submissions show as pending.")]
      : [];
    board.replaceChildren(
      ...notice,
      h("table", { class: "board" },
        h("thead", {}, h("tr", {}, h("th", { class: "num" }, "Rank"), h("th", {}, "Contestant"),
          h("th", { class: "num" }, "Solved"), h("th", { class: "num" }, "Penalty"),
          data.problems.map((p) => h("th", { class: "num", style: "text-align:center" }, p.label)))),
        h("tbody", {}, data.rows.map((r) => h("tr", {},
          h("td", { class: "num" }, r.rank), h("td", {}, r.username),
          h("td", { class: "num" }, r.solved), h("td", { class: "num" }, r.penalty),
          data.problems.map((p) => {
            const cell = r.cells[p.label];
            if (cell.solved) {
              return h("td", { class: "cell solved", title: `Solved at minute ${cell.minute} after ${cell.rejected} rejected tries` },
                h("div", {}, cell.rejected ? `+${cell.rejected}` : "+"), h("small", {}, `${cell.minute}'`));
            }
            if (cell.pending) return h("td", { class: "cell pend", title: "Waiting for the verdict or frozen" }, `?${cell.rejected + cell.pending}`);
            if (cell.rejected) return h("td", { class: "cell tried" }, `-${cell.rejected}`);
            return h("td", { class: "cell" }, "");
          }),
        )))),
    );
  }

  show(
    h("h1", {}, c.title),
    h("p", { class: "lede" }, `${when(c.starts_at)} to ${when(c.ends_at)}`),
    h("div", { class: "row", style: "display:flex;gap:12px;align-items:center" }, join, joinError),
    c.problems.length ? h("h2", {}, "Problems") : h("p", { class: "notice" }, "Problems appear when the contest starts."),
    c.problems.length ? h("table", {}, h("tbody", {}, c.problems.map((p) => h("tr", {},
      h("td", { style: "width:3em" }, p.label),
      h("td", {}, h("a", { href: `#/problems/${p.slug}?contest=${slug}` }, p.title)))))) : null,
    h("h2", {}, "Scoreboard"),
    board,
  );
  await refresh();
  const timer = setInterval(refresh, 15000);
  cleanup = () => clearInterval(timer);
}

function loginPage() {
  const user = h("input", { id: "u", autocomplete: "username", required: true });
  const pass = h("input", { id: "p", type: "password", autocomplete: "current-password", required: true });
  const error = h("div", { class: "error", role: "alert" });

  async function go(register) {
    error.textContent = "";
    const body = JSON.stringify({ username: user.value.trim(), password: pass.value });
    try {
      if (register) await api("/api/auth/register", { method: "POST", body });
      const res = await api("/api/auth/login", { method: "POST", body });
      store.set("token", res.token);
      store.set("username", res.username);
      renderWho();
      location.hash = "#/problems";
    } catch (err) {
      error.textContent = err.message.startsWith("request failed")
        ? "Usernames are 3 to 32 letters, digits, dots, dashes or underscores; passwords need 8 characters."
        : err.message;
    }
  }

  show(
    h("h1", {}, "Log in"),
    h("form", { class: "auth", onsubmit: (e) => { e.preventDefault(); go(false); } },
      h("label", { for: "u" }, "Username"), user,
      h("label", { for: "p" }, "Password"), pass,
      h("div", { class: "actions" },
        h("button", { type: "submit" }, "Log in"),
        h("button", { type: "button", class: "quiet", onclick: () => go(true) }, "Create account")),
      error),
  );
  user.focus();
}

// ------------------------------------------------------------------ router

function renderWho() {
  const who = document.getElementById("who");
  const name = store.get("username");
  who.replaceChildren(...(store.get("token") && name
    ? [name, h("button", { class: "quiet", onclick: () => { store.set("token", null); store.set("username", null); renderWho(); location.hash = "#/login"; } }, "Log out")]
    : [h("a", { href: "#/login" }, "Log in")]));
}

async function route() {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query || "");
  document.querySelectorAll("[data-nav]").forEach((a) => {
    a.toggleAttribute("aria-current", a.dataset.nav === (parts[0] || "problems"));
    if (a.hasAttribute("aria-current")) a.setAttribute("aria-current", "page");
  });
  try {
    if (parts[0] === "login") return loginPage();
    if (parts[0] === "problems" && parts[1]) return await problemPage(parts[1], params.get("contest"));
    if (parts[0] === "submissions" && parts[1]) return await submissionPage(parts[1]);
    if (parts[0] === "submissions") return await submissionsPage();
    if (parts[0] === "contests" && parts[1]) return await contestPage(parts[1]);
    if (parts[0] === "contests") return await contestsPage();
    return await problemsPage();
  } catch (err) {
    show(h("p", { class: "notice" }, err.message));
  }
}

window.addEventListener("hashchange", route);
renderWho();
route();
