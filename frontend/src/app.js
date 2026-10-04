import m from "mithril";

const TOKEN_KEY = "bridge_strain_token";
const USER_KEY = "bridge_strain_user";

function verdictClass(verdict, status) {
  if (verdict === "合格") return "tag pass";
  if (verdict === "越界") return "tag fail";
  if (status === "pending" || status === "processing") return "tag wait";
  return "tag wait";
}

function displayVerdict(row) {
  if (row.verdict) return row.verdict;
  if (row.status === "pending") return "待处理";
  if (row.status === "processing") return "处理中";
  return "—";
}

const ACTION_TEXT = {
  register: "登记",
  bind: "绑定",
  retire: "退役",
  delete: "删除",
};

function actionClass(action) {
  return `gact gact-${action}`;
}

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: null,
  route: location.hash || "#/",
  loginForm: { username: "surveyor", password: "surv123456" },
  submitForm: { gauge_id: "", microstrain: "" },
  registerForm: { serial: "", note: "" },
  bindDrafts: {},
  rows: [],
  gauges: [],
  gaugeLog: [],
  error: "",
  msg: "",
  rosterError: "",
  rosterMsg: "",
  loading: false,
  timer: null,
};

try {
  state.user = JSON.parse(localStorage.getItem(USER_KEY) || "null");
} catch {
  state.user = null;
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { ...opts, headers });
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

async function loadReadings() {
  if (!state.token) return;
  try {
    state.rows = await api("/api/readings");
    state.error = "";
  } catch {
    state.error = "加载列表失败，请重新登录";
  }
  m.redraw();
}

async function loadRoster(silent = false) {
  if (!state.token) return;
  try {
    const [gauges, logs] = await Promise.all([
      api("/api/gauges"),
      api("/api/gauges/log"),
    ]);
    state.gauges = gauges;
    state.gaugeLog = logs;
    if (!silent) state.rosterError = "";
  } catch (err) {
    state.rosterError = err.message || "加载名册失败";
  }
  m.redraw();
}

function startPolling() {
  if (state.timer) clearInterval(state.timer);
  if (!state.token) return;
  state.timer = setInterval(() => {
    if (state.route === "#/roster") {
      loadRoster(true);
    } else {
      loadReadings();
    }
  }, 3000);
}

function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  state.token = "";
  state.user = null;
  state.rows = [];
  state.gauges = [];
  state.gaugeLog = [];
  if (state.timer) clearInterval(state.timer);
}

/* ---------------- 班交台（报送 + 读数列表） ---------------- */

function submitView() {
  const isWriter = state.user?.role === "writer";
  const activeGauges = state.gauges.filter((g) => g.status === "active");
  const selectable = activeGauges.filter((g) => g.bound_span);
  const selected = state.gauges.find(
    (g) => String(g.id) === String(state.submitForm.gauge_id)
  );

  return [
    isWriter
      ? m("div.card", [
          m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "提交读数"),
          m(
            "form",
            {
              onsubmit: async (e) => {
                e.preventDefault();
                state.error = "";
                state.msg = "";
                if (!state.submitForm.gauge_id) {
                  state.error = "必须点选仍在役的片号，未选片号整笔退回";
                  m.redraw();
                  return;
                }
                state.loading = true;
                try {
                  const data = await api("/api/readings", {
                    method: "POST",
                    body: JSON.stringify({
                      gauge_id: Number(state.submitForm.gauge_id),
                      microstrain: parseFloat(state.submitForm.microstrain),
                    }),
                  });
                  state.msg = data.message || "已提交";
                  state.submitForm = { gauge_id: "", microstrain: "" };
                  await loadReadings();
                } catch (err) {
                  state.error = err.message || "提交失败";
                } finally {
                  state.loading = false;
                  m.redraw();
                }
              },
            },
            [
              m("div.row", [
                m("label", [
                  "片号（点选仍在役片号）",
                  m(
                    "select",
                    {
                      required: true,
                      value: state.submitForm.gauge_id,
                      onchange: (e) => {
                        state.submitForm.gauge_id = e.target.value;
                      },
                    },
                    [
                      m(
                        "option",
                        { value: "", disabled: true },
                        "— 请选择仍在役且已绑定跨段的片号 —"
                      ),
                      ...activeGauges.map((g) =>
                        m(
                          "option",
                          {
                            value: g.id,
                            disabled: !g.bound_span,
                          },
                          g.serial +
                            (g.bound_span ? `（${g.bound_span}）` : "（未绑定跨段）")
                        )
                      ),
                    ]
                  ),
                ]),
                m(
                  "span.bound-hint",
                  selected?.bound_span
                    ? `绑定跨段：${selected.bound_span}（以名册绑定为准）`
                    : "选中片号后自动带出绑定跨段"
                ),
                m("label", [
                  "微应变（με）",
                  m("input", {
                    required: true,
                    type: "number",
                    step: "0.1",
                    value: state.submitForm.microstrain,
                    oninput: (e) => {
                      state.submitForm.microstrain = e.target.value;
                    },
                  }),
                ]),
                m("button", { type: "submit", disabled: state.loading }, "提交"),
              ]),
              selectable.length === 0
                ? m("p.err", "名册里没有“在役且已绑定跨段”的片号，请到片号名册先登记并绑定")
                : null,
              state.error ? m("p.err", state.error) : null,
              state.msg ? m("p.ok", state.msg) : null,
            ]
          ),
        ])
      : null,
    m("div.card", [
      m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "读数列表"),
      m("table", [
        m("thead", [
          m("tr", [
            m("th", "编号"),
            m("th", "片号（随单冻住）"),
            m("th", "跨段"),
            m("th", "微应变"),
            m("th", "结论"),
            m("th", "说明"),
            m("th", "状态"),
            m("th", "提交人"),
          ]),
        ]),
        m(
          "tbody",
          state.rows.length
            ? state.rows.map((r) =>
                m("tr", { key: r.id }, [
                  m("td", r.id),
                  m("td", r.gauge_serial || "—"),
                  m("td", r.span_code),
                  m("td", r.microstrain),
                  m("td", [
                    m(
                      "span",
                      { class: verdictClass(r.verdict, r.status) },
                      displayVerdict(r)
                    ),
                  ]),
                  m("td", r.reason || "—"),
                  m("td", r.status),
                  m("td", r.created_by),
                ])
              )
            : [m("tr", m("td", { colspan: 8 }, "暂无数据"))]
        ),
      ]),
    ]),
  ];
}

/* ---------------- 片号名册专页 ---------------- */

async function rosterAction(fn) {
  state.rosterError = "";
  state.rosterMsg = "";
  try {
    const msg = await fn();
    if (msg) state.rosterMsg = msg;
  } catch (err) {
    state.rosterError = err.message || "操作失败";
  }
  await loadRoster(true);
  m.redraw();
}

function rosterView() {
  const isWriter = state.user?.role === "writer";
  const active = state.gauges.filter((g) => g.status === "active");
  const retired = state.gauges.filter((g) => g.status === "retired");

  return [
    m("div.card", [
      m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, [
        "片号名册",
        m(
          "span.ro",
          isWriter ? "测量员维护：登记 → 绑定跨段 → 报送" : "复核员只读：可查看名册与履历，不能改册"
        ),
      ]),

      isWriter
        ? m(
            "form.roster-form",
            {
              onsubmit: async (e) => {
                e.preventDefault();
                const serial = state.registerForm.serial.trim();
                if (!serial) return;
                await rosterAction(async () => {
                  await api("/api/gauges", {
                    method: "POST",
                    body: JSON.stringify(state.registerForm),
                  });
                  state.registerForm = { serial: "", note: "" };
                  return `片号 ${serial} 已登记，可在下方在役清单绑定跨段`;
                });
              },
            },
            [
              m("label", [
                "序列号（片号）",
                m("input", {
                  required: true,
                  placeholder: "例如 GP-BING-003",
                  value: state.registerForm.serial,
                  oninput: (e) => {
                    state.registerForm.serial = e.target.value;
                  },
                }),
              ]),
              m("label", [
                "备注",
                m("input", {
                  placeholder: "可选",
                  value: state.registerForm.note,
                  oninput: (e) => {
                    state.registerForm.note = e.target.value;
                  },
                }),
              ]),
              m("button", { type: "submit" }, "登记片号"),
            ]
          )
        : null,

      m("div.example", [
        m("strong", "绑定样例："),
        "先登记片号 ",
        m("code", "GP-JIA-001"),
        " → 在在役清单把它绑定跨段 ",
        m("code", "跨中甲"),
        " → 回班交台在下拉中点选该片号报送，读数进入候审；该片号退役后，再用同片号报送会被整笔退回，但旧单里冻住的片号不变。",
      ]),

      state.rosterError ? m("p.err", state.rosterError) : null,
      state.rosterMsg ? m("p.ok", state.rosterMsg) : null,
    ]),

    m("div.card", [
      m("h2", { style: { marginTop: 0, fontSize: "1.05rem" } }, "在役清单"),
      m("table", [
        m("thead", [
          m("tr", [
            m("th", "片号"),
            m("th", "绑定跨段"),
            m("th", "备注"),
            m("th", "登记人/时间"),
            isWriter ? m("th", "绑定 / 退役 / 删除") : null,
          ]),
        ]),
        m(
          "tbody",
          active.length
            ? active.map((g) =>
                m("tr", { key: g.id }, [
                  m("td", m("code", g.serial)),
                  m("td", g.bound_span || m("span.muted", "未绑定")),
                  m("td", g.note || "—"),
                  m("td", [
                    g.created_by,
                    m("div.muted", new Date(g.created_at).toLocaleString()),
                  ]),
                  isWriter
                    ? m("td", [
                        m("div.inline", [
                          m("input.bind-input", {
                            placeholder: g.bound_span
                              ? `改绑（现：${g.bound_span}）`
                              : "输入跨段，如 跨中甲",
                            value: state.bindDrafts[g.id] || "",
                            oninput: (e) => {
                              state.bindDrafts[g.id] = e.target.value;
                            },
                          }),
                          m(
                            "button.mini",
                            {
                              type: "button",
                              onclick: () =>
                                rosterAction(async () => {
                                  const span = (state.bindDrafts[g.id] || "").trim();
                                  if (!span) throw new Error("请填写要绑定的跨段");
                                  await api(`/api/gauges/${g.id}/bind`, {
                                    method: "POST",
                                    body: JSON.stringify({ bound_span: span }),
                                  });
                                  state.bindDrafts[g.id] = "";
                                  return `片号 ${g.serial} 已绑定跨段 ${span}`;
                                }),
                            },
                            "绑定"
                          ),
                          m(
                            "button.mini.warn",
                            {
                              type: "button",
                              onclick: () => {
                                if (
                                  !confirm(
                                    `确认退役片号 ${g.serial}？退役后不能再用它报送，旧读单片号不变。`
                                  )
                                )
                                  return;
                                rosterAction(async () => {
                                  await api(`/api/gauges/${g.id}/retire`, {
                                    method: "POST",
                                  });
                                  return `片号 ${g.serial} 已退役`;
                                });
                              },
                            },
                            "退役"
                          ),
                          m(
                            "button.mini.danger",
                            {
                              type: "button",
                              onclick: () => {
                                if (
                                  !confirm(
                                    `确认从名册删除片号 ${g.serial}？已随单冻入读数的片号只能退役不能删。`
                                  )
                                )
                                  return;
                                rosterAction(async () => {
                                  const r = await api(`/api/gauges/${g.id}`, {
                                    method: "DELETE",
                                  });
                                  return r.detail;
                                });
                              },
                            },
                            "删除"
                          ),
                        ]),
                      ])
                    : null,
                ])
              )
            : [
                m(
                  "tr",
                  m(
                    "td",
                    { colspan: isWriter ? 5 : 4 },
                    m("span.muted", "暂无在役片号")
                  )
                ),
              ]
        ),
      ]),
    ]),

    m("div.card", [
      m("h2", { style: { marginTop: 0, fontSize: "1.05rem" } }, "退役清单"),
      m("table", [
        m("thead", [
          m("tr", [
            m("th", "片号"),
            m("th", "原绑定跨段"),
            m("th", "退役人"),
            m("th", "退役时间"),
          ]),
        ]),
        m(
          "tbody",
          retired.length
            ? retired.map((g) =>
                m("tr", { key: g.id }, [
                  m("td", m("code", g.serial)),
                  m("td", g.bound_span || "—"),
                  m("td", g.retired_by || "—"),
                  m("td", g.retired_at ? new Date(g.retired_at).toLocaleString() : "—"),
                ])
              )
            : [m("tr", m("td", { colspan: 4 }, m("span.muted", "暂无退役片号")))]
        ),
      ]),
    ]),

    m("div.card", [
      m("h2", { style: { marginTop: 0, fontSize: "1.05rem" } }, "片号履历"),
      m("table", [
        m("thead", [
          m("tr", [
            m("th", "时间"),
            m("th", "片号"),
            m("th", "动作"),
            m("th", "明细"),
            m("th", "操作人"),
          ]),
        ]),
        m(
          "tbody",
          state.gaugeLog.length
            ? state.gaugeLog.map((l) =>
                m("tr", { key: l.id }, [
                  m("td", new Date(l.created_at).toLocaleString()),
                  m("td", m("code", l.gauge_serial)),
                  m("td", [
                    m(
                      `span.taglog ${actionClass(l.action)}`,
                      ACTION_TEXT[l.action] || l.action
                    ),
                  ]),
                  m("td", l.detail || "—"),
                  m("td", l.operator),
                ])
              )
            : [m("tr", m("td", { colspan: 5 }, m("span.muted", "暂无履历")))]
        ),
      ]),
    ]),
  ];
}

/* ---------------- 根组件 ---------------- */

const App = {
  oninit() {
    window.addEventListener("hashchange", this.handleHash);
    if (state.token) this.bootAfterLogin();
  },
  onremove() {
    window.removeEventListener("hashchange", this.handleHash);
    if (state.timer) clearInterval(state.timer);
  },
  handleHash() {
    state.route = location.hash || "#/";
    if (state.token) {
      if (state.route === "#/roster") loadRoster();
      else loadReadings();
    }
  },
  bootAfterLogin() {
    state.route = location.hash || "#/";
    loadReadings();
    loadRoster(true);
    startPolling();
  },
  view() {
    if (!state.token) {
      return m(
        "div.wrap",
        [
          m("h1", "桥梁应变班交台"),
          m(
            "p.sub",
            "测量员提交跨段编号与微应变读数；片号必须先登记、再绑定跨段，报送时点选仍在役片号。"
          ),
          m("div.card", [
            m(
              "form",
              {
                onsubmit: async (e) => {
                  e.preventDefault();
                  state.error = "";
                  state.loading = true;
                  try {
                    const data = await api("/api/auth/login", {
                      method: "POST",
                      body: JSON.stringify(state.loginForm),
                    });
                    state.token = data.access_token;
                    state.user = { username: data.username, role: data.role };
                    localStorage.setItem(TOKEN_KEY, state.token);
                    localStorage.setItem(USER_KEY, JSON.stringify(state.user));
                    this.bootAfterLogin();
                  } catch {
                    state.error = "用户名或密码错误";
                  } finally {
                    state.loading = false;
                    m.redraw();
                  }
                },
              },
              [
                m("div.row", [
                  m("label", [
                    "用户名",
                    m("input", {
                      value: state.loginForm.username,
                      oninput: (e) => {
                        state.loginForm.username = e.target.value;
                      },
                    }),
                  ]),
                  m("label", [
                    "密码",
                    m("input", {
                      type: "password",
                      value: state.loginForm.password,
                      oninput: (e) => {
                        state.loginForm.password = e.target.value;
                      },
                    }),
                  ]),
                  m("button", { type: "submit", disabled: state.loading }, "登录"),
                ]),
                state.error ? m("p.err", state.error) : null,
              ]
            ),
            m(
              "p.sub",
              { style: { marginBottom: 0 } },
              "测量员 surveyor / surv123456 · 复核员 reviewer / rev123456"
            ),
          ]),
        ]
      );
    }

    const isWriter = state.user?.role === "writer";
    const onRoster = state.route === "#/roster";

    return m("div.wrap", [
      m("div.topbar", [
        m("div", [
          m("h1", "桥梁应变班交台"),
          m("p.sub", "微应变 80～220 με 为合格，否则为越界；片号先登记绑定、报送点选在役片号。"),
        ]),
        m("div.topright", [
          m("nav.tabs", [
            m(
              `a.tab${onRoster ? "" : ".active"}`,
              { href: "#/" },
              "班交台"
            ),
            m(
              `a.tab${onRoster ? ".active" : ""}`,
              { href: "#/roster" },
              "片号名册"
            ),
          ]),
          m("div.userline", [
            `${state.user?.username}（${isWriter ? "测量员" : "复核员"}） `,
            m(
              "button.secondary",
              {
                type: "button",
                onclick: () => {
                  logout();
                  m.redraw();
                },
              },
              "退出"
            ),
          ]),
        ]),
      ]),
      onRoster ? rosterView() : submitView(),
    ]);
  },
};

export default App;
