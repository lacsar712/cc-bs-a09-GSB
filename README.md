# 桥梁应变班交台

测量员上报跨段编号与微应变读数，后台工人用 `FOR UPDATE SKIP LOCKED` 认领待处理队列，按 **80～220 με** 判定 **合格** 或 **越界**。

## 片号名册规则（强约束，服务端落库前校验）

- **先登记、再绑定、后报送**：应变片序列号必须先在「片号名册」专页登记，并绑定跨段，报送时才能点选。
- 报送必须**点选仍在役且已绑定跨段**的片号；空选、未登记、已退役一律由后端整笔退回（HTTP 400），不写库——直连 API 绕过网页同样拦住。
- 跨段以名册绑定为准由服务端带出，请求里夹带的 `span_code` 一律忽略，防落库前偷改。
- **片号随单冻住**：读数入库时把片号序列号快照写入 `strain_readings.gauge_serial`，片号事后退役/改绑都不影响旧单。
- 名册的登记、绑定、退役、删除均与**履历**（`gauge_log`）在同一事务落库；已冻入历史读数的片号只能退役、不能删除（履历保留）。
- 顶栏「片号名册」专页含在役清单、退役清单、绑定样例与履历；复核员可看名册与履历，不能改册（写接口返回 403）。


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
| surveyor | surv123456 | 测量员，可提交读数、维护片号名册 |
| reviewer | rev123456 | 复核员，只读读数/名册/履历 |

## 启动

```bash
cd projects/19-bridge-strain-shift
docker compose up --build
```

健康检查：`GET http://localhost:8198/api/health` → `{"status":"ok","service":"bridge-strain-shift"}`

## 种子数据

| 片号 | 状态 | 跨段 | 微应变 | 结论 |
|------|------|------|--------|------|
| GP-JIA-001 | 在役 | 跨中S1 | 150 με | 合格 |
| GP-YI-002 | 已退役 | 支座S2 | 40 με | 越界（旧单片号仍冻住） |

## 接口

| 方法/路径 | 权限 | 说明 |
|-----------|------|------|
| `POST /api/auth/login` | 公开 | 登录取 JWT |
| `GET /api/readings` | 登录 | 读数列表（含冻住的 `gauge_serial`） |
| `POST /api/readings` | 测量员 | 入参 `gauge_id`+`microstrain`；事务内 `SELECT … FOR UPDATE` 复检在役/绑定后入候审 |
| `GET /api/gauges` | 登录 | 名册（在役+退役） |
| `GET /api/gauges/log` | 登录 | 片号履历 |
| `POST /api/gauges` | 测量员 | 登记片号（重号 409） |
| `POST /api/gauges/{id}/bind` | 测量员 | 绑定/改绑跨段，写履历 |
| `POST /api/gauges/{id}/retire` | 测量员 | 退役（旧单不变），写履历 |
| `DELETE /api/gauges/{id}` | 测量员 | 删除；已有历史读数的片号拒绝删除 |

## 页面

顶栏两个页签（hash 路由）：**班交台**（`#/`，点选在役片号报送+读数列表）与 **片号名册**（`#/roster`，在役清单/退役清单/绑定样例/履历）。复核员进入名册页为纯只读。

## 本地开发（可选）

```bash
cd backend && pip install -r requirements.txt
python -m sanic api.app --host=0.0.0.0 --port=8000 --single-process
python worker.py
cd frontend && npm install && npm run dev
```

接口进程默认监听容器内 **8000**，对外映射 **8198**。
