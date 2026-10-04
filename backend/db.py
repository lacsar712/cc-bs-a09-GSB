import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from rules import judge_microstrain

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:54398/bridgestrain"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gauges (
    id serial PRIMARY KEY,
    serial text NOT NULL UNIQUE,
    bound_span text,
    status text NOT NULL DEFAULT 'active',
    note text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    retired_by text
);

CREATE TABLE IF NOT EXISTS gauge_log (
    id serial PRIMARY KEY,
    gauge_id integer,
    gauge_serial text NOT NULL,
    action text NOT NULL,
    detail text,
    operator text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gauge_log_gauge ON gauge_log (gauge_id, id);

CREATE TABLE IF NOT EXISTS strain_readings (
    id serial PRIMARY KEY,
    span_code text NOT NULL,
    microstrain double precision NOT NULL,
    verdict text,
    reason text,
    status text NOT NULL DEFAULT 'pending',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    gauge_serial text
);
CREATE INDEX IF NOT EXISTS idx_strain_readings_status ON strain_readings (status, id);

ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS gauge_serial text;
"""

# 种子片号：甲在役（绑定跨中S1），乙已退役（旧单仍冻住它的片号）
SEED_GAUGES = [
    {
        "serial": "GP-JIA-001",
        "bound_span": "跨中S1",
        "status": "active",
        "note": "在役样例片",
    },
    {
        "serial": "GP-YI-002",
        "bound_span": "支座S2",
        "status": "retired",
        "note": "退役样例片，旧读单片号仍冻住",
    },
]
SEED_READINGS = [
    ("跨中S1", 150.0, "GP-JIA-001"),
    ("支座S2", 40.0, "GP-YI-002"),
]


async def create_pool() -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        conninfo=DSN,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await pool.open()
    return pool


async def ensure_schema(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(SCHEMA_SQL)
        await conn.commit()


async def seed_if_empty(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) AS n FROM strain_readings")
            row = await cur.fetchone()
            if row["n"] > 0:
                return
            for g in SEED_GAUGES:
                retired = g["status"] == "retired"
                await cur.execute(
                    """
                    INSERT INTO gauges
                        (serial, bound_span, status, note, created_by,
                         retired_at, retired_by)
                    VALUES (%s, %s, %s, %s, 'surveyor',
                            CASE WHEN %s THEN now() END,
                            CASE WHEN %s THEN 'surveyor' END)
                    ON CONFLICT (serial) DO NOTHING
                    """,
                    (
                        g["serial"],
                        g["bound_span"],
                        g["status"],
                        g["note"],
                        retired,
                        retired,
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO gauge_log
                        (gauge_id, gauge_serial, action, detail, operator)
                    SELECT id, serial, 'register',
                           '种子数据登记片号，绑定跨段 ' || COALESCE(bound_span, '—'),
                           'surveyor'
                    FROM gauges WHERE serial = %s
                    """,
                    (g["serial"],),
                )
                if retired:
                    await cur.execute(
                        """
                        INSERT INTO gauge_log
                            (gauge_id, gauge_serial, action, detail, operator)
                        SELECT id, serial, 'retire', '种子数据：片号已退役',
                               'surveyor'
                        FROM gauges WHERE serial = %s
                        """,
                        (g["serial"],),
                    )
            for span_code, microstrain, serial in SEED_READINGS:
                verdict, reason = judge_microstrain(microstrain)
                await cur.execute(
                    """
                    INSERT INTO strain_readings
                        (span_code, microstrain, verdict, reason, status,
                         created_by, processed_at, gauge_serial)
                    VALUES (%s, %s, %s, %s, 'done', 'surveyor', now(), %s)
                    """,
                    (span_code, microstrain, verdict, reason, serial),
                )
        await conn.commit()


def connect_sync():
    import psycopg

    return psycopg.connect(DSN, row_factory=dict_row)


def ensure_schema_sync(conn) -> None:
    conn.execute(SCHEMA_SQL)


def seed_if_empty_sync(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM strain_readings").fetchone()
    if row["n"] > 0:
        return
    for g in SEED_GAUGES:
        retired = g["status"] == "retired"
        conn.execute(
            """
            INSERT INTO gauges
                (serial, bound_span, status, note, created_by,
                 retired_at, retired_by)
            VALUES (%s, %s, %s, %s, 'surveyor',
                    CASE WHEN %s THEN now() END,
                    CASE WHEN %s THEN 'surveyor' END)
            ON CONFLICT (serial) DO NOTHING
            """,
            (
                g["serial"],
                g["bound_span"],
                g["status"],
                g["note"],
                retired,
                retired,
            ),
        )
        conn.execute(
            """
            INSERT INTO gauge_log
                (gauge_id, gauge_serial, action, detail, operator)
            SELECT id, serial, 'register',
                   '种子数据登记片号，绑定跨段 ' || COALESCE(bound_span, '—'),
                   'surveyor'
            FROM gauges WHERE serial = %s
            """,
            (g["serial"],),
        )
        if retired:
            conn.execute(
                """
                INSERT INTO gauge_log
                    (gauge_id, gauge_serial, action, detail, operator)
                SELECT id, serial, 'retire', '种子数据：片号已退役', 'surveyor'
                FROM gauges WHERE serial = %s
                """,
                (g["serial"],),
            )
    for span_code, microstrain, serial in SEED_READINGS:
        verdict, reason = judge_microstrain(microstrain)
        conn.execute(
            """
            INSERT INTO strain_readings
                (span_code, microstrain, verdict, reason, status,
                 created_by, processed_at, gauge_serial)
            VALUES (%s, %s, %s, %s, 'done', 'surveyor', now(), %s)
            """,
            (span_code, microstrain, verdict, reason, serial),
        )
    conn.commit()
