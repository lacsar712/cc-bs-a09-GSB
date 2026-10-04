import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from rules import judge_microstrain

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:54398/bridgestrain"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strain_gauges (
    serial text PRIMARY KEY,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    registered_by text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT now(),
    retired_by text,
    retired_at timestamptz
);

CREATE TABLE IF NOT EXISTS gauge_history (
    id serial PRIMARY KEY,
    serial text NOT NULL,
    action text NOT NULL CHECK (action IN ('register', 'retire', 'delete', 'bind')),
    detail text,
    operator text NOT NULL,
    reading_id integer,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gauge_history_serial ON gauge_history (serial, id);

CREATE TABLE IF NOT EXISTS strain_readings (
    id serial PRIMARY KEY,
    span_code text NOT NULL,
    microstrain double precision NOT NULL,
    verdict text,
    reason text,
    status text NOT NULL DEFAULT 'pending',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_strain_readings_status ON strain_readings (status, id);

ALTER TABLE strain_readings
    ADD COLUMN IF NOT EXISTS gauge_serial text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_readings_gauge'
    ) THEN
        ALTER TABLE strain_readings
            ADD CONSTRAINT fk_readings_gauge
            FOREIGN KEY (gauge_serial) REFERENCES strain_gauges (serial);
    END IF;
END $$;

-- 数据库层兜底：绕过接口直连写库同样拦截
-- 1) 读数落库时片号必须在名册且在役（退役/空号一律拒绝，整笔无法写入）
CREATE OR REPLACE FUNCTION trg_reading_gauge_must_active()
RETURNS trigger AS $fn$
DECLARE
    g_status text;
BEGIN
    IF NEW.gauge_serial IS NULL OR btrim(NEW.gauge_serial) = '' THEN
        RAISE EXCEPTION '必须绑定在役应变片片号，空片号禁止落库';
    END IF;
    SELECT status INTO g_status
    FROM strain_gauges
    WHERE serial = NEW.gauge_serial
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION '片号 % 未登记，禁止落库', NEW.gauge_serial;
    END IF;
    IF g_status <> 'active' THEN
        RAISE EXCEPTION '片号 % 已退役，禁止新报送落库', NEW.gauge_serial
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS t_reading_gauge_must_active ON strain_readings;
CREATE TRIGGER t_reading_gauge_must_active
    BEFORE INSERT ON strain_readings
    FOR EACH ROW EXECUTE FUNCTION trg_reading_gauge_must_active();

-- 2) 片号随单冻住：读数一旦写入，gauge_serial 不可改（退役不影响旧单）
CREATE OR REPLACE FUNCTION trg_reading_gauge_frozen()
RETURNS trigger AS $fn$
BEGIN
    IF NEW.gauge_serial IS DISTINCT FROM OLD.gauge_serial THEN
        RAISE EXCEPTION '片号随单冻住，禁止修改旧单片号（旧=%，新=%）',
            OLD.gauge_serial, NEW.gauge_serial;
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS t_reading_gauge_frozen ON strain_readings;
CREATE TRIGGER t_reading_gauge_frozen
    BEFORE UPDATE OF gauge_serial ON strain_readings
    FOR EACH ROW EXECUTE FUNCTION trg_reading_gauge_frozen();
"""

# (片号, 跨段, 微应变)；种子片号登记为在役并绑定到种子读数
SEED_GAUGES = [
    ("SG-SEED-01", "跨中S1", 150.0),
    ("SG-SEED-02", "支座S2", 40.0),
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
            await cur.execute("SELECT COUNT(*) AS n FROM strain_gauges")
            gauges_empty = (await cur.fetchone())["n"] == 0
            await cur.execute("SELECT COUNT(*) AS n FROM strain_readings")
            readings_empty = (await cur.fetchone())["n"] == 0
            if not gauges_empty and not readings_empty:
                return

            for serial, span_code, microstrain in SEED_GAUGES:
                if gauges_empty:
                    await cur.execute(
                        """
                        INSERT INTO strain_gauges (serial, status, registered_by)
                        VALUES (%s, 'active', 'surveyor')
                        ON CONFLICT (serial) DO NOTHING
                        """,
                        (serial,),
                    )
                    await cur.execute(
                        """
                        INSERT INTO gauge_history (serial, action, detail, operator)
                        VALUES (%s, 'register', '种子片号登记', 'surveyor')
                        """,
                        (serial,),
                    )
                if readings_empty:
                    verdict, reason = judge_microstrain(microstrain)
                    await cur.execute(
                        """
                        INSERT INTO strain_readings
                            (span_code, microstrain, gauge_serial, verdict, reason,
                             status, created_by, processed_at)
                        VALUES (%s, %s, %s, %s, %s, 'done', 'surveyor', now())
                        RETURNING id
                        """,
                        (span_code, microstrain, serial, verdict, reason),
                    )
                    reading = await cur.fetchone()
                    await cur.execute(
                        """
                        INSERT INTO gauge_history
                            (serial, action, detail, operator, reading_id)
                        VALUES (%s, 'bind', %s, 'surveyor', %s)
                        """,
                        (serial, f"绑定跨段 {span_code}", reading["id"]),
                    )
        await conn.commit()


def connect_sync():
    import psycopg

    return psycopg.connect(DSN, row_factory=dict_row)


def ensure_schema_sync(conn) -> None:
    conn.execute(SCHEMA_SQL)


def seed_if_empty_sync(conn) -> None:
    gauges_empty = conn.execute(
        "SELECT COUNT(*) AS n FROM strain_gauges"
    ).fetchone()["n"] == 0
    readings_empty = conn.execute(
        "SELECT COUNT(*) AS n FROM strain_readings"
    ).fetchone()["n"] == 0
    if not gauges_empty and not readings_empty:
        return

    for serial, span_code, microstrain in SEED_GAUGES:
        if gauges_empty:
            conn.execute(
                """
                INSERT INTO strain_gauges (serial, status, registered_by)
                VALUES (%s, 'active', 'surveyor')
                ON CONFLICT (serial) DO NOTHING
                """,
                (serial,),
            )
            conn.execute(
                """
                INSERT INTO gauge_history (serial, action, detail, operator)
                VALUES (%s, 'register', '种子片号登记', 'surveyor')
                """,
                (serial,),
            )
        if readings_empty:
            verdict, reason = judge_microstrain(microstrain)
            reading = conn.execute(
                """
                INSERT INTO strain_readings
                    (span_code, microstrain, gauge_serial, verdict, reason,
                     status, created_by, processed_at)
                VALUES (%s, %s, %s, %s, %s, 'done', 'surveyor', now())
                RETURNING id
                """,
                (span_code, microstrain, serial, verdict, reason),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO gauge_history
                    (serial, action, detail, operator, reading_id)
                VALUES (%s, 'bind', %s, 'surveyor', %s)
                """,
                (serial, f"绑定跨段 {span_code}", reading["id"]),
            )
    conn.commit()
