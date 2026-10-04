import os
from datetime import datetime, timedelta, timezone

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


def _require_user(request) -> dict:
    return _decode_user(_auth_header(request))


def _require_writer(request):
    """返回 (user, error_response)；测量员通过时 error_response 为 None。"""
    user = _require_user(request)
    if not user:
        return None, sanic_json({"detail": "未登录"}, status=401)
    if user["role"] != "writer":
        return None, sanic_json({"detail": "仅测量员可维护片号名册"}, status=403)
    return user, None


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _gauge_dict(r) -> dict:
    return {
        "id": r["id"],
        "serial": r["serial"],
        "bound_span": r["bound_span"],
        "status": r["status"],
        "note": r["note"],
        "created_by": r["created_by"],
        "created_at": _iso(r["created_at"]),
        "retired_at": _iso(r["retired_at"]),
        "retired_by": r["retired_by"],
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


@app.get("/api/readings")
async def list_readings(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, span_code, microstrain, verdict, reason, status,
                       created_by, created_at, processed_at, gauge_serial
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
                "verdict": r["verdict"],
                "reason": r["reason"],
                "status": r["status"],
                "created_by": r["created_by"],
                "created_at": _iso(r["created_at"]),
                "processed_at": _iso(r["processed_at"]),
                "gauge_serial": r["gauge_serial"],
            }
        )
    return sanic_json(out)


@app.post("/api/readings")
async def create_reading(request):
    user = _require_user(request)
    if not user:
        return sanic_json({"detail": "未登录"}, status=401)
    if user["role"] != "writer":
        return sanic_json({"detail": "仅测量员可提交应变读数"}, status=403)
    body = request.json or {}

    # 必须点选片号：空选整笔退回（拒绝发生在服务端，直连 API 同样拦住）
    gauge_id_raw = body.get("gauge_id")
    try:
        gauge_id = int(gauge_id_raw)
    except (TypeError, ValueError):
        return sanic_json(
            {"detail": "必须点选仍在役的片号，未选片号的读数整笔退回"}, status=400
        )
    try:
        microstrain = float(body.get("microstrain"))
    except (TypeError, ValueError):
        return sanic_json({"detail": "微应变必须是数字"}, status=400)

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # 落库前锁行复检：防名册在本事务前被退役/改动，也防前端偷改片号
                await cur.execute(
                    """
                    SELECT id, serial, bound_span, status
                    FROM gauges
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (gauge_id,),
                )
                gauge = await cur.fetchone()
                if not gauge:
                    return sanic_json(
                        {"detail": "片号未在名册登记，请先登记再绑定跨段"},
                        status=400,
                    )
                if gauge["status"] != "active":
                    return sanic_json(
                        {"detail": f"片号 {gauge['serial']} 已退役，整笔退回"},
                        status=400,
                    )
                if not gauge["bound_span"]:
                    return sanic_json(
                        {
                            "detail": (
                                f"片号 {gauge['serial']} 尚未绑定跨段，"
                                "请先在名册专页绑定再报送"
                            )
                        },
                        status=400,
                    )

                # 跨段一律以名册绑定为准，忽略请求里夹带的 span_code（防偷改）
                # gauge_serial 作为快照冻入本单，事后退役不影响旧单。
                await cur.execute(
                    """
                    INSERT INTO strain_readings
                        (span_code, microstrain, gauge_serial,
                         status, created_by, created_at)
                    VALUES (%s, %s, %s, 'pending', %s, now())
                    RETURNING id, span_code, microstrain, verdict, reason, status,
                              created_by, created_at, processed_at, gauge_serial
                    """,
                    (
                        gauge["bound_span"],
                        microstrain,
                        gauge["serial"],
                        user["username"],
                    ),
                )
                row = await cur.fetchone()

    return sanic_json(
        {
            "id": row["id"],
            "span_code": row["span_code"],
            "microstrain": row["microstrain"],
            "verdict": row["verdict"],
            "reason": row["reason"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": _iso(row["created_at"]),
            "processed_at": None,
            "gauge_serial": row["gauge_serial"],
            "message": "已入队，片号随单冻住，后台工人将认领并判定",
        },
        status=201,
    )


@app.get("/api/gauges")
async def list_gauges(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, serial, bound_span, status, note, created_by,
                       created_at, retired_at, retired_by
                FROM gauges
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()
    return sanic_json([_gauge_dict(r) for r in rows])


@app.get("/api/gauges/log")
async def list_gauge_log(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, gauge_id, gauge_serial, action, detail,
                       operator, created_at
                FROM gauge_log
                ORDER BY id DESC
                """
            )
            rows = await cur.fetchall()
    out = [
        {
            "id": r["id"],
            "gauge_id": r["gauge_id"],
            "gauge_serial": r["gauge_serial"],
            "action": r["action"],
            "detail": r["detail"],
            "operator": r["operator"],
            "created_at": _iso(r["created_at"]),
        }
        for r in rows
    ]
    return sanic_json(out)


@app.post("/api/gauges")
async def register_gauge(request):
    user, err = _require_writer(request)
    if err:
        return err
    body = request.json or {}
    serial = str(body.get("serial", "")).strip()
    if not serial:
        return sanic_json({"detail": "片号（序列号）不能为空"}, status=400)
    note = str(body.get("note", "")).strip() or None

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM gauges WHERE serial = %s FOR UPDATE",
                    (serial,),
                )
                if await cur.fetchone():
                    return sanic_json(
                        {"detail": f"片号 {serial} 已登记，不能重复登记"},
                        status=409,
                    )
                await cur.execute(
                    """
                    INSERT INTO gauges (serial, note, status, created_by)
                    VALUES (%s, %s, 'active', %s)
                    RETURNING id, serial, bound_span, status, note, created_by,
                              created_at, retired_at, retired_by
                    """,
                    (serial, note, user["username"]),
                )
                row = await cur.fetchone()
                await cur.execute(
                    """
                    INSERT INTO gauge_log
                        (gauge_id, gauge_serial, action, detail, operator)
                    VALUES (%s, %s, 'register', %s, %s)
                    """,
                    (
                        row["id"],
                        row["serial"],
                        note or "登记片号，尚未绑定跨段",
                        user["username"],
                    ),
                )
    return sanic_json(_gauge_dict(row), status=201)


@app.post("/api/gauges/<gid:int>/bind")
async def bind_gauge(request, gid: int):
    user, err = _require_writer(request)
    if err:
        return err
    body = request.json or {}
    span = str(body.get("bound_span", "")).strip()
    if not span:
        return sanic_json({"detail": "绑定跨段不能为空"}, status=400)

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, serial, bound_span, status
                    FROM gauges WHERE id = %s FOR UPDATE
                    """,
                    (gid,),
                )
                row = await cur.fetchone()
                if not row:
                    return sanic_json({"detail": "片号不存在"}, status=404)
                if row["status"] != "active":
                    return sanic_json(
                        {"detail": "已退役片号不能再绑定跨段"}, status=400
                    )
                old = row["bound_span"]
                await cur.execute(
                    "UPDATE gauges SET bound_span = %s WHERE id = %s",
                    (span, gid),
                )
                detail = (
                    f"绑定跨段 {span}"
                    if not old
                    else f"改绑跨段：{old} → {span}"
                )
                await cur.execute(
                    """
                    INSERT INTO gauge_log
                        (gauge_id, gauge_serial, action, detail, operator)
                    VALUES (%s, %s, 'bind', %s, %s)
                    """,
                    (gid, row["serial"], detail, user["username"]),
                )
                await cur.execute(
                    """
                    SELECT id, serial, bound_span, status, note, created_by,
                           created_at, retired_at, retired_by
                    FROM gauges WHERE id = %s
                    """,
                    (gid,),
                )
                updated = await cur.fetchone()
    return sanic_json(_gauge_dict(updated))


@app.post("/api/gauges/<gid:int>/retire")
async def retire_gauge(request, gid: int):
    user, err = _require_writer(request)
    if err:
        return err

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, serial, status FROM gauges
                    WHERE id = %s FOR UPDATE
                    """,
                    (gid,),
                )
                row = await cur.fetchone()
                if not row:
                    return sanic_json({"detail": "片号不存在"}, status=404)
                if row["status"] == "retired":
                    return sanic_json(
                        {"detail": "该片号已是退役状态"}, status=400
                    )
                await cur.execute(
                    """
                    UPDATE gauges
                    SET status = 'retired', retired_at = now(), retired_by = %s
                    WHERE id = %s
                    """,
                    (user["username"], gid),
                )
                await cur.execute(
                    """
                    INSERT INTO gauge_log
                        (gauge_id, gauge_serial, action, detail, operator)
                    VALUES (%s, %s, 'retire', '片号退役；旧读单片号随单冻住不变', %s)
                    """,
                    (gid, row["serial"], user["username"]),
                )
                await cur.execute(
                    """
                    SELECT id, serial, bound_span, status, note, created_by,
                           created_at, retired_at, retired_by
                    FROM gauges WHERE id = %s
                    """,
                    (gid,),
                )
                updated = await cur.fetchone()
    return sanic_json(_gauge_dict(updated))


@app.delete("/api/gauges/<gid:int>")
async def delete_gauge(request, gid: int):
    user, err = _require_writer(request)
    if err:
        return err
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, serial, status FROM gauges WHERE id = %s FOR UPDATE",
                    (gid,),
                )
                row = await cur.fetchone()
                if not row:
                    return sanic_json({"detail": "片号不存在"}, status=404)
                # 已冻入旧单的片号只允许退役，不允许删除（保履历/旧单可追溯）
                await cur.execute(
                    "SELECT 1 FROM strain_readings WHERE gauge_serial = %s LIMIT 1",
                    (row["serial"],),
                )
                if await cur.fetchone():
                    return sanic_json(
                        {
                            "detail": (
                                "该片号已随单冻入历史读数，不能删除；"
                                "请改为退役，旧单片号仍保留"
                            )
                        },
                        status=400,
                    )
                await cur.execute("DELETE FROM gauges WHERE id = %s", (gid,))
                # 履历不带外键，删名册后仍保留；gauge_id 留存、serial 冗余可查
                await cur.execute(
                    """
                    INSERT INTO gauge_log
                        (gauge_id, gauge_serial, action, detail, operator)
                    VALUES (%s, %s, 'delete', '从名册删除片号（未绑定任何读数）', %s)
                    """,
                    (gid, row["serial"], user["username"]),
                )
    return sanic_json({"detail": f"片号 {row['serial']} 已从名册删除"})
