# 桥梁应变班交台

测量员上报跨段编号与微应变读数，后台工人用 `FOR UPDATE SKIP LOCKED` 认领待处理队列，按 **80～220 με** 判定 **合格** 或 **越界**。

## 应变片序列号（片号）规则

- **先登记、再绑定、后报送**：片号由测量员在顶栏「片号名册」专页登记；报送读数时必须从下拉中点选仍在役的片号，空选或点到已退役片号整笔退回（不进候审队列）。
- **片号随单冻住**：读数落库时把片号快照写入 `strain_readings.gauge_serial`；事后片号退役不改变任何旧单，且数据库触发器禁止修改旧单片号。
- **退役不改旧单，旧片不能再报**：退役后用同片号再报送会被退回；历史读数的片号仍在。
- **增删退役与履历一并落库**：登记 / 退役 / 删除 / 绑定均在同一数据库事务内写 `gauge_history`；已有绑定读数的片号不允许删除，只能退役。
- **三层防线**：前端必选下拉 → 接口单事务 `SELECT … FOR UPDATE` 校验在役后再插入并写绑定履历 → 数据库触发器兜底（直连数据库写 SQL 插空号/退役号/未登记号、偷改旧单片号，一律拒绝）。
- **复核员只读**：复核侧可看名册、在役/退役清单与履历，但不能登记、退役、删片或报送。
- 名册专页含：在役清单、退役清单、绑定样例（含最近绑定记录）、片号履历。

## 技术栈

| 层 | 选型 |
|----|------|
| 接口 | Python Sanic + psycopg（异步连接池） |
| 工人 | `worker.py`（psycopg 同步，`FOR UPDATE SKIP LOCKED`） |
| 页面 | Mithril.js + Vite，nginx 反代 `/api` |
| 数据库 | PostgreSQL 16 |

## 端口

| 服务 | 地址 |
|------|------|
| 页面 | http://localhost:3198 |
| 接口 | http://localhost:8198 |
| PostgreSQL | localhost:54398（库名 `bridgestrain`） |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| surveyor | surv123456 | 测量员，可提交读数 |
| reviewer | rev123456 | 复核员，只读列表 |

## 启动

```bash
cd projects/19-bridge-strain-shift
docker compose up --build
```

健康检查：`GET http://localhost:8198/api/health` → `{"status":"ok","service":"bridge-strain-shift"}`

## 种子数据

| 片号 | 跨段 | 微应变 | 结论 |
|------|------|--------|------|
| SG-SEED-01 | 跨中S1 | 150 με | 合格 |
| SG-SEED-02 | 支座S2 | 40 με | 越界 |

两片号均为在役名册片号，绑定履历已写入。

## 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/gauges` | 名册（在役 + 退役），测量员/复核员可看 |
| POST | `/api/gauges` | 登记片号（测量员，与登记履历同事务） |
| POST | `/api/gauges/<serial>/retire` | 退役片号（测量员，行锁 + 履历同事务） |
| DELETE | `/api/gauges/<serial>` | 删除无绑定读数的片号（测量员，履历留存） |
| GET | `/api/gauge-history?serial=` | 片号履历（复核员只读可看） |
| POST | `/api/readings` | 报送读数，必须带在役 `gauge_serial`，否则整笔 400 退回 |

## 本地开发（可选）

```bash
cd backend && pip install -r requirements.txt
python -m sanic api.app --host=0.0.0.0 --port=8000 --single-process
python worker.py
cd frontend && npm install && npm run dev
```

接口进程默认监听容器内 **8000**，对外映射 **8198**。
