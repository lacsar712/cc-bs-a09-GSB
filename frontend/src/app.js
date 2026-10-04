import m from "mithril";

const TOKEN_KEY = "bridge_strain_token";
const USER_KEY = "bridge_strain_user";

const ACTION_LABELS = {
  register: "登记",
  retire: "退役",
  delete: "删除",
  bind: "绑定",
};

function verdictClass(verdict, status) {
  if (verdict === "合格") return "tag pass";
  if (verdict === "越界") return "tag fail";
  return "tag wait";
}

function displayVerdict(row) {
  if (row.verdict) return row.verdict;
  if (row.status === "pending") return "待处理";
  if (row.status === "processing") return "处理中";
  return "—";
}

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: null,
  page: "submit",
  loginForm: { username: "surveyor", password: "surv123456" },
  submitForm: { span_code: "", microstrain: "", gauge_serial: "" },
  registerForm: { serial: "" },
  rows: [],
  gauges: [],
  history: [],
  error: "",
  msg: "",
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

const activeGauges = () => state.gauges.filter((g) => g.status === "active");
const retiredGauges = () => state.gauges.filter((g) => g.status === "retired");

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

async function loadRoster() {
  if (!state.token) return;
  try {
    const [gauges, history] = await Promise.all([
      api("/api/gauges"),
      api("/api/gauge-history"),
    ]);
    state.gauges = gauges;
    state.history = history;
  } catch (err) {
    state.error = err.message || "加载片号名册失败";
  }
  m.redraw();
}

function startPolling() {
  if (state.timer) clearInterval(state.timer);
  if (!state.token) return;
  state.timer = setInterval(() => {
    loadReadings();
    if (state.page === "roster") loadRoster();
  }, 3000);
}

function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  state.token = "";
  state.user = null;
  state.rows = [];
  state.gauges = [];
  state.history = [];
  if (state.timer) clearInterval(state.timer);
  m.redraw();
}

async function retireGauge(serial) {
  if (!window.confirm(`确认退役片号 ${serial}？\n退役后旧单已冻住的片号不变，但不能再用该片号报送。`)) return;
  try {
    await api(`/api/gauges/${encodeURIComponent(serial)}/retire`, { method: "POST" });
    state.msg = `片号 ${serial} 已退役`;
    await loadRoster();
  } catch (err) {
    state.error = err.message;
  }
  m.redraw();
}

async function deleteGauge(serial) {
  if (!window.confirm(`确认从名册删除片号 ${serial}？删除动作仍会计入履历。`)) return;
  try {
    await api(`/api/gauges/${encodeURIComponent(serial)}`, { method: "DELETE" });
    state.msg = `片号 ${serial} 已删除`;
    await loadRoster();
  } catch (err) {
    state.error = err.message;
  }
  m.redraw();
}

const LoginPage = {
  view: () =>
    m("div.wrap", [
      m("h1", "桥梁应变班交台"),
      m("p.sub", "测量员先登记应变片序列号，报送时点选在役片号绑定跨段；片号随单冻住。"),
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
                await Promise.all([loadReadings(), loadRoster()]);
                startPolling();
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
    ]),
};

function topbar(isWriter) {
  const navBtn = (page, label) =>
    m(
      `button.secondary${state.page === page ? ".active" : ""}`,
      {
        type: "button",
        onclick: () => {
          state.page = page;
          state.error = "";
          state.msg = "";
          if (page === "roster") loadRoster();
        },
      },
      label
    );
  return m("div.topbar", [
    m("div", [
      m("h1", "桥梁应变班交台"),
      m("p.sub", "微应变 80～220 με 为合格，否则为越界。应变片序列号先登记、再绑定、后报送。"),
    ]),
    m("div.topright", [
      m("div.nav", [navBtn("submit", "读数报送"), navBtn("roster", "片号名册")]),
      m("div.userline", [
        `${state.user?.username}（${isWriter ? "测量员" : "复核员"}） `,
        m("button.secondary", { type: "button", onclick: logout }, "退出"),
      ]),
    ]),
  ]);
}

function submitCard() {
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "提交读数"),
    m(
      "form",
      {
        onsubmit: async (e) => {
          e.preventDefault();
          state.error = "";
          state.msg = "";
          state.loading = true;
          try {
            const data = await api("/api/readings", {
              method: "POST",
              body: JSON.stringify({
                span_code: state.submitForm.span_code,
                microstrain: parseFloat(state.submitForm.microstrain),
                gauge_serial: state.submitForm.gauge_serial,
              }),
            });
            state.msg = data.message || "已提交";
            state.submitForm = { span_code: "", microstrain: "", gauge_serial: "" };
            await Promise.all([loadReadings(), loadRoster()]);
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
            "应变片片号（必选在役片号）",
            m(
              "select",
              {
                required: true,
                value: state.submitForm.gauge_serial,
                onchange: (e) => {
                  state.submitForm.gauge_serial = e.target.value;
                },
              },
              [
                m("option", { value: "", disabled: true, selected: !state.submitForm.gauge_serial }, "请选择在役片号…"),
                ...activeGauges().map((g) =>
                  m("option", { key: g.serial, value: g.serial }, `${g.serial}（在役）`)
                ),
              ]
            ),
          ]),
          m("label", [
            "跨段编号",
            m("input", {
              required: true,
              placeholder: "例如 跨中S3",
              value: state.submitForm.span_code,
              oninput: (e) => {
                state.submitForm.span_code = e.target.value;
              },
            }),
          ]),
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
        m("p.hint", [
          "片号必须先在 ",
          m(
            "a",
            {
              href: "#",
              onclick: (e) => {
                e.preventDefault();
                state.page = "roster";
                loadRoster();
              },
            },
            "片号名册"
          ),
          " 登记；空选片号或点选已退役片号，整笔退回。片号随单冻住，退役不改旧单。",
        ]),
        state.error ? m("p.err", state.error) : null,
        state.msg ? m("p.ok", state.msg) : null,
      ]
    ),
  ]);
}

function readingsCard() {
  return m("div.card", [
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
                  m("span", { class: verdictClass(r.verdict, r.status) }, displayVerdict(r)),
                ]),
                m("td", r.reason || "—"),
                m("td", r.status),
                m("td", r.created_by),
              ])
            )
          : [m("tr", m("td", { colspan: 8 }, "暂无数据"))]
      ),
    ]),
  ]);
}

function gaugeTables(isWriter) {
  const renderRow = (g, retired) =>
    m("tr", { key: g.serial }, [
      m("td", g.serial),
      m("td", [
        m("span", { class: `gauge-badge ${retired ? "off" : "on"}` }, retired ? "退役" : "在役"),
      ]),
      m("td", g.registered_by),
      m("td", g.registered_by ? new Date(g.registered_at).toLocaleString() : "—"),
      m("td", g.last_span_code || "—"),
      retired
        ? m("td", `${g.retired_by || ""} ${g.retired_at ? new Date(g.retired_at).toLocaleString() : ""}`)
        : m("td", [
            isWriter
              ? [
                  m(
                    "button.mini",
                    { type: "button", onclick: () => retireGauge(g.serial) },
                    "退役"
                  ),
                  !g.last_span_code
                    ? m(
                        "button.mini.danger",
                        { type: "button", style: { marginLeft: "0.4rem" }, onclick: () => deleteGauge(g.serial) },
                        "删除"
                      )
                    : null,
                ]
              : m("span.ro", "只读"),
          ]),
    ]);

  const head = m("thead", [
    m("tr", [
      m("th", "片号"),
      m("th", "状态"),
      m("th", "登记人"),
      m("th", "登记时间"),
      m("th", "最近绑定跨段"),
      m("th", isWriter ? "操作" : "退役信息"),
    ]),
  ]);

  return [
    m("h3", "在役清单"),
    m(
      "table",
      activeGauges().length
        ? [head, m("tbody", activeGauges().map((g) => renderRow(g, false)))]
        : [head, m("tbody", [m("tr", m("td", { colspan: 6 }, "暂无在役片号"))])]
    ),
    m("h3", "退役清单"),
    m(
      "table",
      retiredGauges().length
        ? [head, m("tbody", retiredGauges().map((g) => renderRow(g, true)))]
        : [head, m("tbody", [m("tr", m("td", { colspan: 6 }, "暂无退役片号"))])]
    ),
  ];
}

function bindingSample() {
  const binds = state.history.filter((h) => h.action === "bind").slice(0, 5);
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "绑定样例"),
    m("ol.sample", [
      m("li", "测量员在本页登记片号，例如片号甲，进入在役清单。"),
      m("li", "在「读数报送」页点选片号甲，填写跨段（如跨中甲）与微应变后提交。"),
      m("li", "读数进入候审（pending），片号甲随单冻住；后台工人认领后写入合格/越界结论。"),
      m("li", "事后片号甲退役：旧单片号仍为甲不可改；再用甲报送会整笔退回。"),
    ]),
    m("table", [
      m("thead", [
        m("tr", [m("th", "片号"), m("th", "绑定说明"), m("th", "读数编号"), m("th", "操作人"), m("th", "时间")]),
      ]),
      m(
        "tbody",
        binds.length
          ? binds.map((h) =>
              m("tr", { key: h.id }, [
                m("td", h.serial),
                m("td", h.detail || "—"),
                m("td", h.reading_id || "—"),
                m("td", h.operator),
                m("td", new Date(h.created_at).toLocaleString()),
              ])
            )
          : [m("tr", m("td", { colspan: 5 }, "暂无绑定记录"))]
      ),
    ]),
  ]);
}

function rosterPage(isWriter) {
  return [
    isWriter
      ? m("div.card", [
          m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "登记新片号"),
          m(
            "form.row",
            {
              onsubmit: async (e) => {
                e.preventDefault();
                state.error = "";
                state.msg = "";
                const serial = state.registerForm.serial.trim();
                if (!serial) {
                  state.error = "片号不能为空";
                  m.redraw();
                  return;
                }
                try {
                  await api("/api/gauges", {
                    method: "POST",
                    body: JSON.stringify({ serial }),
                  });
                  state.msg = `片号 ${serial} 已登记入册`;
                  state.registerForm.serial = "";
                  await loadRoster();
                } catch (err) {
                  state.error = err.message || "登记失败";
                }
                m.redraw();
              },
            },
            [
              m("label", [
                "应变片序列号",
                m("input", {
                  placeholder: "例如 SG-2026-甲",
                  value: state.registerForm.serial,
                  oninput: (e) => {
                    state.registerForm.serial = e.target.value;
                  },
                }),
              ]),
              m("button", { type: "submit" }, "登记入册"),
            ]
          ),
          state.error ? m("p.err", state.error) : null,
          state.msg ? m("p.ok", state.msg) : null,
        ])
      : m("div.card.readonlybanner", "复核员只读视图：可查看名册与履历，不能登记、退役或删片。"),
    m("div.card", gaugeTables(isWriter)),
    bindingSample(),
    m("div.card", [
      m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "片号履历（登记 / 退役 / 删除 / 绑定）"),
      m("table", [
        m("thead", [
          m("tr", [
            m("th", "#"),
            m("th", "片号"),
            m("th", "动作"),
            m("th", "说明"),
            m("th", "读数编号"),
            m("th", "操作人"),
            m("th", "时间"),
          ]),
        ]),
        m(
          "tbody",
          state.history.length
            ? state.history.map((h) =>
                m("tr", { key: h.id }, [
                  m("td", h.id),
                  m("td", h.serial),
                  m("td", [m("span", { class: `act act-${h.action}` }, ACTION_LABELS[h.action] || h.action)]),
                  m("td", h.detail || "—"),
                  m("td", h.reading_id || "—"),
                  m("td", h.operator),
                  m("td", new Date(h.created_at).toLocaleString()),
                ])
              )
            : [m("tr", m("td", { colspan: 7 }, "暂无履历"))]
        ),
      ]),
    ]),
  ];
}

const App = {
  oninit() {
    if (state.token) {
      loadReadings();
      loadRoster();
      startPolling();
    }
  },
  onremove() {
    if (state.timer) clearInterval(state.timer);
  },
  view() {
    if (!state.token) return m(LoginPage);

    const isWriter = state.user?.role === "writer";

    return m("div.wrap", [
      topbar(isWriter),
      state.page === "roster"
        ? rosterPage(isWriter)
        : [isWriter ? submitCard() : null, readingsCard()],
    ]);
  },
};

export default App;
