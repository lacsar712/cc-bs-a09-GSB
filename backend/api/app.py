import os
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import jwt
from passlib.context import CryptContext
from sanic import Sanic
from sanic.response import json as sanic_json

from db import create_pool, ensure_schema, seed_if_empty

SECRET = os.environ.get("JWT_SECRET", "bridge-strain-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = {
    "surveyor": {"role": "writer", "password_hash": pwd.hash("surv123456")},
    "reviewer": {"role": "reader", "password_hash": pwd.hash("rev123456")},
}

app = Sanic("bridge-strain-shift")


def _auth_header(request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _decode_user(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    sub = payload.get("sub")
    if sub not in USERS:
        return None
    return {"username": sub, "role": payload.get("role")}


def _require_user(request) -> dict | None:
    return _decode_user(_auth_header(request))


def _require_writer(request):
    user = _require_user(request)
    if not user:
        return None, sanic_json({"detail": "未登录"}, status=401)
    if user["role"] != "writer":
        return None, sanic_json({"detail": "仅测量员可操作"}, status=403)
    return user, None


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


_GAUGE_SELECT = """
SELECT g.serial, g.status, g.registered_by, g.registered_at,
       g.retired_by, g.retired_at,
       (
           SELECT r.span_code
           FROM strain_readings r
           WHERE r.gauge_serial = g.serial
           ORDER BY r.id DESC
           LIMIT 1
       ) AS last_span_code
FROM strain_gauges g
"""


def _gauge_row(r) -> dict:
    return {
        "serial": r["serial"],
        "status": r["status"],
        "registered_by": r["registered_by"],
        "registered_at": _iso(r["registered_at"]),
        "retired_by": r["retired_by"],
        "retired_at": _iso(r["retired_at"]),
        "last_span_code": r["last_span_code"],
    }


@app.before_server_start
async def setup(_app, _loop):
    pool = await create_pool()
    _app.ctx.pool = pool
    await ensure_schema(pool)
    await seed_if_empty(pool)


@app.after_server_stop
async def teardown(_app, _loop):
    pool = _app.ctx.pool
    if pool:
        await pool.close()


@app.get("/api/health")
async def health(_request):
    return sanic_json({"status": "ok", "service": "bridge-strain-shift"})


@app.post("/api/auth/login")
async def login(request):
    body = request.json or {}
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    user = USERS.get(username)
    if not user or not pwd.verify(password, user["password_hash"]):
        return sanic_json({"detail": "用户名或密码错误"}, status=401)
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": username, "role": user["role"], "exp": exp},
        SECRET,
        algorithm="HS256",
    )
    return sanic_json(
        {"access_token": token, "username": username, "role": user["role"]}
    )


# ---------------- 片号名册 ----------------


@app.get("/api/gauges")
async def list_gauges(request):
    """在役 + 退役全名册；测量员与复核员均可查看。"""
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT g.serial, g.status, g.registered_by, g.registered_at,
                       g.retired_by, g.retired_at,
                       (
                           SELECT r.span_code
                           FROM strain_readings r
                           WHERE r.gauge_serial = g.serial
                           ORDER BY r.id DESC
                           LIMIT 1
                       ) AS last_span_code
                FROM strain_gauges g
                ORDER BY g.status ASC, g.serial ASC
                """
            )
            rows = await cur.fetchall()
    return sanic_json([_gauge_row(r) for r in rows])


@app.post("/api/gauges")
async def register_gauge(request):
    """登记新片号；与登记履历同一事务落库。"""
    user, err = _require_writer(request)
    if err:
        return err
    body = request.json or {}
    serial = str(body.get("serial", "")).strip()
    if not serial:
        return sanic_json({"detail": "片号不能为空"}, status=400)
    if len(serial) > 64:
        return sanic_json({"detail": "片号最长 64 个字符"}, status=400)

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO strain_gauges (serial, status, registered_by)
                        VALUES (%s, 'active', %s)
                        """,
                        (serial, user["username"]),
                    )
                    await cur.execute(
                        """
                        INSERT INTO gauge_history (serial, action, detail, operator)
                        VALUES (%s, 'register', '片号登记入册', %s)
                        """,
                        (serial, user["username"]),
                    )
                    await cur.execute(
                        _GAUGE_SELECT + " WHERE g.serial = %s",
                        (serial,),
                    )
                    row = await cur.fetchone()
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":  # unique_violation
                return sanic_json(
                    {"detail": "片号已存在（在役或退役片号均不可重复登记）"},
                    status=409,
                )
            raise
    return sanic_json(_gauge_row(row), status=201)


@app.post("/api/gauges/<serial>/retire")
async def retire_gauge(request, serial):
    """退役片号；行锁 + 履历同一事务。不影响历史读数已冻住的片号。"""
    serial = unquote(serial)
    user, err = _require_writer(request)
    if err:
        return err
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status FROM strain_gauges WHERE serial = %s FOR UPDATE",
                    (serial,),
                )
                row = await cur.fetchone()
                if not row:
                    return sanic_json({"detail": "片号未登记"}, status=404)
                if row["status"] == "retired":
                    return sanic_json({"detail": "片号已退役，请勿重复操作"}, status=409)
                await cur.execute(
                    """
                    UPDATE strain_gauges
                    SET status = 'retired', retired_by = %s, retired_at = now()
                    WHERE serial = %s
                    """,
                    (user["username"], serial),
                )
                await cur.execute(
                    """
                    INSERT INTO gauge_history (serial, action, detail, operator)
                    VALUES (%s, 'retire', '片号退役', %s)
                    """,
                    (serial, user["username"]),
                )
                await cur.execute(
                    _GAUGE_SELECT + " WHERE g.serial = %s",
                    (serial,),
                )
                updated = await cur.fetchone()
    return sanic_json(_gauge_row(updated))


@app.delete("/api/gauges/<serial>")
async def delete_gauge(request, serial):
    """删片：仅允许无绑定读数的片号；删除动作写入履历后同事务落库。"""
    serial = unquote(serial)
    user, err = _require_writer(request)
    if err:
        return err
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status FROM strain_gauges WHERE serial = %s FOR UPDATE",
                    (serial,),
                )
                row = await cur.fetchone()
                if not row:
                    return sanic_json({"detail": "片号未登记"}, status=404)
                await cur.execute(
                    "SELECT COUNT(*) AS n FROM strain_readings WHERE gauge_serial = %s",
                    (serial,),
                )
                bound = (await cur.fetchone())["n"]
                if bound > 0:
                    return sanic_json(
                        {"detail": f"该片号已有 {bound} 笔绑定读数，不能删除，可退役"},
                        status=409,
                    )
                await cur.execute(
                    """
                    INSERT INTO gauge_history (serial, action, detail, operator)
                    VALUES (%s, 'delete', '片号从名册删除', %s)
                    """,
                    (serial, user["username"]),
                )
                await cur.execute(
                    "DELETE FROM strain_gauges WHERE serial = %s",
                    (serial,),
                )
    return sanic_json({"deleted": serial})


@app.get("/api/gauge-history")
async def list_gauge_history(request):
    """片号履历（登记/退役/删除/绑定）；复核员只读可见。"""
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    serial = str(request.args.get("serial", "")).strip()
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if serial:
                await cur.execute(
                    """
                    SELECT id, serial, action, detail, operator, reading_id, created_at
                    FROM gauge_history
                    WHERE serial = %s
                    ORDER BY id DESC
                    LIMIT 200
                    """,
                    (serial,),
                )
            else:
                await cur.execute(
                    """
                    SELECT id, serial, action, detail, operator, reading_id, created_at
                    FROM gauge_history
                    ORDER BY id DESC
                    LIMIT 200
                    """
                )
            rows = await cur.fetchall()
    out = [
        {
            "id": r["id"],
            "serial": r["serial"],
            "action": r["action"],
            "detail": r["detail"],
            "operator": r["operator"],
            "reading_id": r["reading_id"],
            "created_at": _iso(r["created_at"]),
        }
        for r in rows
    ]
    return sanic_json(out)


# ---------------- 应变读数 ----------------


@app.get("/api/readings")
async def list_readings(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, span_code, microstrain, gauge_serial, verdict, reason,
                       status, created_by, created_at, processed_at
                FROM strain_readings
                ORDER BY id DESC
                """
            )
            rows = await cur.fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "span_code": r["span_code"],
                "microstrain": r["microstrain"],
                "gauge_serial": r["gauge_serial"],
                "verdict": r["verdict"],
                "reason": r["reason"],
                "status": r["status"],
                "created_by": r["created_by"],
                "created_at": _iso(r["created_at"]),
                "processed_at": _iso(r["processed_at"]),
            }
        )
    return sanic_json(out)


@app.post("/api/readings")
async def create_reading(request):
    user, err = _require_writer(request)
    if err:
        return err
    body = request.json or {}
    span_code = str(body.get("span_code", "")).strip()
    if not span_code:
        return sanic_json({"detail": "跨段编号不能为空"}, status=400)
    try:
        microstrain = float(body.get("microstrain"))
    except (TypeError, ValueError):
        return sanic_json({"detail": "微应变必须是数字"}, status=400)
    gauge_serial = str(body.get("gauge_serial", "")).strip()
    if not gauge_serial:
        # 空选片号：整笔退回，不允许入队
        return sanic_json({"detail": "必须点选在役应变片片号，未选片号整笔退回"}, status=400)

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        # 单事务：行锁锁住片号 → 校验在役 → 插入读数（片号随单冻住）→ 写绑定履历。
        # 直连 API 伪造/空片号在此拦截；并发退役会被行锁挡住，杜绝落库前偷换。
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status FROM strain_gauges WHERE serial = %s FOR UPDATE",
                    (gauge_serial,),
                )
                gauge = await cur.fetchone()
                if not gauge:
                    return sanic_json(
                        {"detail": f"片号 {gauge_serial} 未登记，请先在名册登记后再绑定报送"},
                        status=400,
                    )
                if gauge["status"] != "active":
                    return sanic_json(
                        {"detail": f"片号 {gauge_serial} 已退役，整笔退回；旧单已冻住的片号不变"},
                        status=400,
                    )
                await cur.execute(
                    """
                    INSERT INTO strain_readings
                        (span_code, microstrain, gauge_serial, status, created_by, created_at)
                    VALUES (%s, %s, %s, 'pending', %s, now())
                    RETURNING id, span_code, microstrain, gauge_serial, verdict, reason,
                              status, created_by, created_at, processed_at
                    """,
                    (span_code, microstrain, gauge_serial, user["username"]),
                )
                row = await cur.fetchone()
                await cur.execute(
                    """
                    INSERT INTO gauge_history
                        (serial, action, detail, operator, reading_id)
                    VALUES (%s, 'bind', %s, %s, %s)
                    """,
                    (
                        gauge_serial,
                        f"绑定跨段 {span_code} 并报送，读数进入候审",
                        user["username"],
                        row["id"],
                    ),
                )

    return sanic_json(
        {
            "id": row["id"],
            "span_code": row["span_code"],
            "microstrain": row["microstrain"],
            "gauge_serial": row["gauge_serial"],
            "verdict": row["verdict"],
            "reason": row["reason"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": _iso(row["created_at"]),
            "processed_at": None,
            "message": "已绑定在役片号并入队候审，后台工人将认领并判定",
        },
        status=201,
    )
