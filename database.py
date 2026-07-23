import asyncio

import asyncpg


SCHEMA = """
CREATE TABLE IF NOT EXISTS balances (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS badges (
    id BIGSERIAL,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    badge_role_id BIGINT NOT NULL,
    color_role_id BIGINT NOT NULL,
    purchasable BOOLEAN NOT NULL DEFAULT FALSE,
    price BIGINT NOT NULL DEFAULT 0 CHECK (price >= 0),
    shop_section TEXT,
    emoji TEXT,
    whitelist_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (guild_id, name_key),
    UNIQUE (guild_id, badge_role_id)
);

ALTER TABLE badges
ADD COLUMN IF NOT EXISTS shop_section TEXT;

ALTER TABLE badges
ADD COLUMN IF NOT EXISTS emoji TEXT;

ALTER TABLE badges
ADD COLUMN IF NOT EXISTS whitelist_enabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE badges
ADD COLUMN IF NOT EXISTS id BIGINT;

CREATE SEQUENCE IF NOT EXISTS badges_id_seq;

ALTER SEQUENCE badges_id_seq
OWNED BY badges.id;

ALTER TABLE badges
ALTER COLUMN id SET DEFAULT nextval('badges_id_seq');

UPDATE badges
SET id = nextval('badges_id_seq')
WHERE id IS NULL;

ALTER TABLE badges
ALTER COLUMN id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS badges_id_unique
ON badges (id);

UPDATE badges
SET shop_section = 'General'
WHERE purchasable = TRUE
  AND (shop_section IS NULL OR BTRIM(shop_section) = '');

CREATE INDEX IF NOT EXISTS badges_shop_index
ON badges (guild_id, purchasable, price);

CREATE TABLE IF NOT EXISTS modifiers (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    purchasable BOOLEAN NOT NULL DEFAULT FALSE,
    price BIGINT NOT NULL DEFAULT 0 CHECK (price >= 0),
    shop_section TEXT,
    emoji TEXT,
    messages TEXT[] NOT NULL CHECK (CARDINALITY(messages) > 0),
    trigger_numerator INTEGER NOT NULL DEFAULT 1 CHECK (trigger_numerator >= 0),
    trigger_denominator INTEGER NOT NULL DEFAULT 10 CHECK (trigger_denominator >= 1),
    cooldown_seconds INTEGER NOT NULL DEFAULT 10 CHECK (cooldown_seconds >= 0),
    duration_minutes INTEGER NOT NULL DEFAULT 5 CHECK (duration_minutes >= 1),
    effect_scope TEXT NOT NULL DEFAULT 'individual'
        CHECK (effect_scope IN ('individual', 'channel')),
    UNIQUE (guild_id, name_key)
);

ALTER TABLE modifiers
ADD COLUMN IF NOT EXISTS trigger_numerator INTEGER NOT NULL DEFAULT 1;

ALTER TABLE modifiers
ADD COLUMN IF NOT EXISTS trigger_denominator INTEGER NOT NULL DEFAULT 10;

ALTER TABLE modifiers
ADD COLUMN IF NOT EXISTS cooldown_seconds INTEGER NOT NULL DEFAULT 10;

ALTER TABLE modifiers
ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NOT NULL DEFAULT 5;

ALTER TABLE modifiers
ADD COLUMN IF NOT EXISTS effect_scope TEXT NOT NULL DEFAULT 'individual';

CREATE INDEX IF NOT EXISTS modifiers_shop_index
ON modifiers (guild_id, purchasable, price);

CREATE TABLE IF NOT EXISTS tickets (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    purchasable BOOLEAN NOT NULL DEFAULT FALSE,
    price BIGINT NOT NULL DEFAULT 0 CHECK (price >= 0),
    shop_section TEXT,
    emoji TEXT,
    description TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (guild_id, name_key)
);

ALTER TABLE tickets
ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS tickets_shop_index
ON tickets (guild_id, purchasable, price);

CREATE TABLE IF NOT EXISTS shop_categories (
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (guild_id, name_key)
);

WITH existing_categories AS (
    SELECT guild_id, BTRIM(shop_section) AS name
    FROM badges
    WHERE shop_section IS NOT NULL AND BTRIM(shop_section) <> ''
    UNION
    SELECT guild_id, BTRIM(shop_section) AS name
    FROM modifiers
    WHERE shop_section IS NOT NULL AND BTRIM(shop_section) <> ''
    UNION
    SELECT guild_id, BTRIM(shop_section) AS name
    FROM tickets
    WHERE shop_section IS NOT NULL AND BTRIM(shop_section) <> ''
),
deduplicated_categories AS (
    SELECT DISTINCT ON (guild_id, LOWER(name))
           guild_id, name, LOWER(name) AS name_key
    FROM existing_categories
    ORDER BY guild_id, LOWER(name), name
)
INSERT INTO shop_categories (guild_id, name, name_key, description)
SELECT guild_id, name, name_key, ''
FROM deduplicated_categories
ON CONFLICT (guild_id, name_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS modifier_inventory (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    modifier_id BIGINT NOT NULL REFERENCES modifiers(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (guild_id, user_id, modifier_id)
);

CREATE TABLE IF NOT EXISTS ticket_inventory (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (guild_id, user_id, ticket_id)
);

CREATE TABLE IF NOT EXISTS active_modifiers (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    owner_user_id BIGINT,
    modifier_id BIGINT NOT NULL REFERENCES modifiers(id) ON DELETE CASCADE,
    channel_id BIGINT,
    expires_at TIMESTAMPTZ NOT NULL,
    last_trigger_at TIMESTAMPTZ,
    duration_minutes INTEGER NOT NULL DEFAULT 5,
    PRIMARY KEY (guild_id, user_id)
);

ALTER TABLE active_modifiers
ADD COLUMN IF NOT EXISTS channel_id BIGINT;

ALTER TABLE active_modifiers
ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NOT NULL DEFAULT 5;

ALTER TABLE active_modifiers
ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;

CREATE INDEX IF NOT EXISTS active_modifiers_expiration_index
ON active_modifiers (expires_at);

CREATE TABLE IF NOT EXISTS active_channel_modifiers (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    owner_user_id BIGINT,
    modifier_id BIGINT NOT NULL REFERENCES modifiers(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    last_trigger_at TIMESTAMPTZ,
    duration_minutes INTEGER NOT NULL DEFAULT 5,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE INDEX IF NOT EXISTS active_channel_modifiers_expiration_index
ON active_channel_modifiers (expires_at);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    log_channel_id BIGINT,
    logs_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    coin_emoji TEXT,
    whitelist_emoji TEXT
);

ALTER TABLE guild_settings
ADD COLUMN IF NOT EXISTS coin_emoji TEXT;

ALTER TABLE guild_settings
ADD COLUMN IF NOT EXISTS whitelist_emoji TEXT;

CREATE TABLE IF NOT EXISTS object_section_settings (
    guild_id BIGINT NOT NULL,
    section TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    disabled_reason TEXT,
    admins_bypass BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (guild_id, section)
);

ALTER TABLE object_section_settings
ADD COLUMN IF NOT EXISTS admins_bypass BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE object_section_settings
DROP CONSTRAINT IF EXISTS object_section_settings_section_check;

ALTER TABLE object_section_settings
ADD CONSTRAINT object_section_settings_section_check
CHECK (
    section IN (
        'badges', 'modifiers', 'tickets', 'shop',
        'all_objects', 'all_commands'
    )
);

CREATE TABLE IF NOT EXISTS ticket_admins (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS whitelist_entries (
    guild_id BIGINT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('member', 'role')),
    target_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS movements (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT,
    actor_id BIGINT,
    action TEXT NOT NULL,
    amount BIGINT,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS movements_user_history_index
ON movements (guild_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS movements_action_index
ON movements (guild_id, action);

UPDATE movements
SET description = REPLACE(description, ' monedas', '')
WHERE description LIKE '%🪙% monedas%';

CREATE TABLE IF NOT EXISTS active_question_events (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT,
    question TEXT NOT NULL,
    answer_hash TEXT NOT NULL,
    answer_text TEXT,
    reward BIGINT NOT NULL DEFAULT 0,
    reward_object_count SMALLINT NOT NULL DEFAULT 0,
    reward_type TEXT NOT NULL DEFAULT 'coins',
    reward_object_id BIGINT,
    reward_quantity INTEGER NOT NULL DEFAULT 1,
    reward_name TEXT,
    reward_emoji TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, channel_id)
);

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS message_id BIGINT;

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS reward_type TEXT NOT NULL DEFAULT 'coins';

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS reward_object_id BIGINT;

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS reward_quantity INTEGER NOT NULL DEFAULT 1;

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS reward_name TEXT;

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS reward_emoji TEXT;

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS answer_text TEXT;

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS reward_object_count SMALLINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS active_question_event_rewards (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    position SMALLINT NOT NULL CHECK (position BETWEEN 1 AND 3),
    item_type TEXT NOT NULL CHECK (item_type IN ('badge', 'modifier', 'ticket')),
    item_id BIGINT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    name TEXT NOT NULL,
    emoji TEXT,
    PRIMARY KEY (guild_id, channel_id, position),
    UNIQUE (guild_id, channel_id, item_type, item_id),
    FOREIGN KEY (guild_id, channel_id)
        REFERENCES active_question_events (guild_id, channel_id)
        ON DELETE CASCADE
);

INSERT INTO active_question_event_rewards (
    guild_id, channel_id, position, item_type, item_id, quantity, name, emoji
)
SELECT guild_id, channel_id, 1, reward_type, reward_object_id,
       reward_quantity, reward_name, reward_emoji
FROM active_question_events
WHERE reward_type IN ('badge', 'modifier', 'ticket')
  AND reward_object_id IS NOT NULL
  AND reward_name IS NOT NULL
ON CONFLICT (guild_id, channel_id, position) DO NOTHING;

ALTER TABLE active_question_events
DROP CONSTRAINT IF EXISTS active_question_events_reward_check;

UPDATE active_question_events
SET reward_object_count = CASE
        WHEN reward_type IN ('badge', 'modifier', 'ticket')
             AND reward_object_id IS NOT NULL
        THEN GREATEST(reward_object_count, 1)
        ELSE reward_object_count
    END,
    reward_type = 'coins',
    reward_object_id = NULL,
    reward_quantity = 1,
    reward_name = NULL,
    reward_emoji = NULL
WHERE reward_type <> 'coins' OR reward_object_id IS NOT NULL;

ALTER TABLE active_question_events
ALTER COLUMN reward SET DEFAULT 0;

ALTER TABLE active_question_events
ADD CONSTRAINT active_question_events_reward_check
CHECK (
    reward >= 0
    AND reward_object_count BETWEEN 0 AND 3
    AND (reward > 0 OR reward_object_count > 0)
    AND reward_type = 'coins'
    AND reward_object_id IS NULL
    AND reward_quantity = 1
    AND reward_name IS NULL
    AND reward_emoji IS NULL
);

CREATE INDEX IF NOT EXISTS active_question_events_expiration_index
ON active_question_events (expires_at);

DROP INDEX IF EXISTS active_question_events_reward_object_index;

CREATE INDEX IF NOT EXISTS active_question_event_rewards_object_index
ON active_question_event_rewards (guild_id, item_type, item_id);
"""


def _question_events_with_rewards(event_rows, reward_rows) -> list[dict]:
    rewards_by_event: dict[tuple[int, int], list[dict]] = {}
    for row in reward_rows:
        reward = dict(row)
        key = (reward.pop("guild_id"), reward.pop("channel_id"))
        rewards_by_event.setdefault(key, []).append(reward)

    events = []
    for row in event_rows:
        event = dict(row)
        key = (event["guild_id"], event["channel_id"])
        event["reward_objects"] = sorted(
            rewards_by_event.get(key, []),
            key=lambda reward: reward["position"],
        )
        events.append(event)
    return events


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=5)
                async with self.pool.acquire() as connection:
                    await connection.execute(SCHEMA)
                return
            except (OSError, asyncpg.PostgresError) as error:
                last_error = error
                if attempt < 5:
                    await asyncio.sleep(2 * attempt)
        raise RuntimeError("No fue posible conectar con PostgreSQL.") from last_error

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("La base de datos no está conectada.")
        return self.pool

    async def get_balance(self, guild_id: int, user_id: int) -> int:
        value = await self._pool().fetchval(
            "SELECT balance FROM balances WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
        return value or 0

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        return await self._pool().fetchval(
            """
            INSERT INTO balances (guild_id, user_id, balance)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET balance = balances.balance + EXCLUDED.balance
            RETURNING balance
            """,
            guild_id,
            user_id,
            amount,
        )

    async def add_balance_many(
        self,
        guild_id: int,
        user_ids: list[int],
        amount: int,
    ) -> int:
        if not user_ids:
            return 0
        await self._pool().execute(
            """
            INSERT INTO balances (guild_id, user_id, balance)
            SELECT $1, user_id, $3
            FROM UNNEST($2::BIGINT[]) AS users(user_id)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET balance = balances.balance + EXCLUDED.balance
            """,
            guild_id,
            user_ids,
            amount,
        )
        return len(user_ids)

    async def remove_balance(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
    ) -> int | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO balances (guild_id, user_id, balance)
                    VALUES ($1, $2, 0)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                    user_id,
                )
                return await connection.fetchval(
                    """
                    UPDATE balances
                    SET balance = balance - $3
                    WHERE guild_id = $1 AND user_id = $2 AND balance >= $3
                    RETURNING balance
                    """,
                    guild_id,
                    user_id,
                    amount,
                )

    async def spend_balance(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
    ) -> int | None:
        return await self.remove_balance(guild_id, user_id, amount)

    async def remove_balance_many(
        self,
        guild_id: int,
        user_ids: list[int],
        amount: int,
    ) -> list[int]:
        if not user_ids:
            return []
        rows = await self._pool().fetch(
            """
            UPDATE balances
            SET balance = balance - $3
            WHERE guild_id = $1
              AND user_id = ANY($2::BIGINT[])
              AND balance >= $3
            RETURNING user_id
            """,
            guild_id,
            user_ids,
            amount,
        )
        return [row["user_id"] for row in rows]

    async def set_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        return await self._pool().fetchval(
            """
            INSERT INTO balances (guild_id, user_id, balance)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET balance = EXCLUDED.balance
            RETURNING balance
            """,
            guild_id,
            user_id,
            amount,
        )

    async def set_balance_many(
        self,
        guild_id: int,
        user_ids: list[int],
        amount: int,
    ) -> int:
        if not user_ids:
            return 0
        await self._pool().execute(
            """
            INSERT INTO balances (guild_id, user_id, balance)
            SELECT $1, user_id, $3
            FROM UNNEST($2::BIGINT[]) AS users(user_id)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET balance = EXCLUDED.balance
            """,
            guild_id,
            user_ids,
            amount,
        )
        return len(user_ids)

    async def get_badge(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            "SELECT * FROM badges WHERE guild_id = $1 AND name_key = $2",
            guild_id,
            name_key,
        )

    async def get_badge_by_id(self, guild_id: int, badge_id: int):
        return await self._pool().fetchrow(
            "SELECT * FROM badges WHERE guild_id = $1 AND id = $2",
            guild_id,
            badge_id,
        )

    async def list_badges(self, guild_id: int, purchasable_only: bool = False):
        if purchasable_only:
            return await self._pool().fetch(
                """
                SELECT * FROM badges
                WHERE guild_id = $1 AND purchasable = TRUE
                ORDER BY COALESCE(shop_section, 'General'), price, name
                """,
                guild_id,
            )
        return await self._pool().fetch(
            "SELECT * FROM badges WHERE guild_id = $1 ORDER BY name",
            guild_id,
        )

    async def create_badge(
        self,
        guild_id: int,
        name: str,
        name_key: str,
        badge_role_id: int,
        color_role_id: int,
        purchasable: bool,
        price: int,
        shop_section: str | None,
        emoji: str | None,
        whitelist_enabled: bool,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO badges (
                guild_id, name, name_key, badge_role_id,
                color_role_id, purchasable, price, shop_section, emoji,
                whitelist_enabled
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            guild_id,
            name,
            name_key,
            badge_role_id,
            color_role_id,
            purchasable,
            price,
            shop_section,
            emoji,
            whitelist_enabled,
        )

    async def update_badge(
        self,
        guild_id: int,
        old_name_key: str,
        name: str,
        name_key: str,
        badge_role_id: int,
        color_role_id: int,
        purchasable: bool,
        price: int,
        shop_section: str | None,
        emoji: str | None,
        whitelist_enabled: bool,
    ):
        result = await self._pool().execute(
            """
            UPDATE badges
            SET name = $3,
                name_key = $4,
                badge_role_id = $5,
                color_role_id = $6,
                purchasable = $7,
                price = $8,
                shop_section = $9,
                emoji = $10,
                whitelist_enabled = $11
            WHERE guild_id = $1 AND name_key = $2
            """,
            guild_id,
            old_name_key,
            name,
            name_key,
            badge_role_id,
            color_role_id,
            purchasable,
            price,
            shop_section,
            emoji,
            whitelist_enabled,
        )
        return result == "UPDATE 1"

    async def delete_badge(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            """
            DELETE FROM badges
            WHERE guild_id = $1 AND name_key = $2
            RETURNING *
            """,
            guild_id,
            name_key,
        )

    async def get_modifier(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            "SELECT * FROM modifiers WHERE guild_id = $1 AND name_key = $2",
            guild_id,
            name_key,
        )

    async def list_modifiers(self, guild_id: int, purchasable_only: bool = False):
        if purchasable_only:
            return await self._pool().fetch(
                """
                SELECT * FROM modifiers
                WHERE guild_id = $1 AND purchasable = TRUE
                ORDER BY COALESCE(shop_section, 'General'), price, name
                """,
                guild_id,
            )
        return await self._pool().fetch(
            "SELECT * FROM modifiers WHERE guild_id = $1 ORDER BY name",
            guild_id,
        )

    async def list_shop_items(self, guild_id: int):
        return await self._pool().fetch(
            """
            SELECT
                'badge' AS item_type, name, name_key, price, shop_section,
                emoji, badge_role_id, color_role_id, NULL::BIGINT AS modifier_id,
                NULL::BIGINT AS ticket_id, NULL::TEXT AS description,
                NULL::BOOLEAN AS active,
                NULL::INTEGER AS trigger_numerator,
                NULL::INTEGER AS trigger_denominator,
                NULL::INTEGER AS cooldown_seconds,
                NULL::INTEGER AS duration_minutes,
                NULL::TEXT AS effect_scope
            FROM badges
            WHERE guild_id = $1 AND purchasable = TRUE
            UNION ALL
            SELECT
                'modifier' AS item_type, name, name_key, price, shop_section,
                emoji, NULL::BIGINT AS badge_role_id,
                NULL::BIGINT AS color_role_id, id AS modifier_id,
                NULL::BIGINT AS ticket_id, NULL::TEXT AS description,
                NULL::BOOLEAN AS active, trigger_numerator, trigger_denominator,
                cooldown_seconds, duration_minutes, effect_scope
            FROM modifiers
            WHERE guild_id = $1 AND purchasable = TRUE
            UNION ALL
            SELECT
                'ticket' AS item_type, name, name_key, price, shop_section,
                emoji, NULL::BIGINT AS badge_role_id,
                NULL::BIGINT AS color_role_id, NULL::BIGINT AS modifier_id,
                id AS ticket_id, description, active,
                NULL::INTEGER AS trigger_numerator,
                NULL::INTEGER AS trigger_denominator,
                NULL::INTEGER AS cooldown_seconds,
                NULL::INTEGER AS duration_minutes,
                NULL::TEXT AS effect_scope
            FROM tickets
            WHERE guild_id = $1 AND purchasable = TRUE
            ORDER BY shop_section NULLS FIRST, price, name
            """,
            guild_id,
        )

    async def search_named_items(
        self,
        guild_id: int,
        item_type: str,
        search: str,
        limit: int = 25,
    ):
        tables = {
            "badge": "badges",
            "modifier": "modifiers",
            "ticket": "tickets",
            "category": "shop_categories",
        }
        table = tables.get(item_type)
        if table is None:
            raise ValueError("Tipo de búsqueda no válido.")
        return await self._pool().fetch(
            f"""
            SELECT name, name_key
            FROM {table}
            WHERE guild_id = $1
              AND POSITION($2 IN name_key) > 0
            ORDER BY name
            LIMIT $3
            """,
            guild_id,
            search,
            limit,
        )

    async def search_configured_objects(
        self,
        guild_id: int,
        search: str,
        include_categories: bool = True,
        limit: int = 25,
    ):
        return await self._pool().fetch(
            """
            SELECT item_type, name, name_key
            FROM (
                SELECT 'badge'::TEXT AS item_type, name, name_key
                FROM badges WHERE guild_id = $1
                UNION ALL
                SELECT 'modifier'::TEXT AS item_type, name, name_key
                FROM modifiers WHERE guild_id = $1
                UNION ALL
                SELECT 'ticket'::TEXT AS item_type, name, name_key
                FROM tickets WHERE guild_id = $1
                UNION ALL
                SELECT 'category'::TEXT AS item_type, name, name_key
                FROM shop_categories
                WHERE guild_id = $1 AND $3::BOOLEAN
            ) AS configured
            WHERE POSITION($2 IN name_key) > 0
            ORDER BY
                CASE item_type
                    WHEN 'badge' THEN 0
                    WHEN 'modifier' THEN 1
                    WHEN 'ticket' THEN 2
                    ELSE 3
                END,
                name
            LIMIT $4
            """,
            guild_id,
            search,
            include_categories,
            limit,
        )

    async def get_configured_object(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            """
            SELECT item_type, id, name, name_key, emoji, badge_role_id
            FROM (
                SELECT 'badge'::TEXT AS item_type, id, name, name_key, emoji,
                       badge_role_id
                FROM badges
                WHERE guild_id = $1 AND name_key = $2
                UNION ALL
                SELECT 'modifier'::TEXT AS item_type, id, name, name_key, emoji,
                       NULL::BIGINT AS badge_role_id
                FROM modifiers
                WHERE guild_id = $1 AND name_key = $2
                UNION ALL
                SELECT 'ticket'::TEXT AS item_type, id, name, name_key, emoji,
                       NULL::BIGINT AS badge_role_id
                FROM tickets
                WHERE guild_id = $1 AND name_key = $2
            ) AS configured
            LIMIT 1
            """,
            guild_id,
            name_key,
        )

    async def search_shop_items(
        self,
        guild_id: int,
        search: str,
        owned_role_ids: list[int],
        limit: int = 25,
    ):
        return await self._pool().fetch(
            """
            SELECT item_type, name, name_key, badge_role_id
            FROM (
                SELECT 'badge'::TEXT AS item_type, name, name_key,
                       badge_role_id
                FROM badges
                WHERE guild_id = $1 AND purchasable = TRUE
                  AND NOT (badge_role_id = ANY($3::BIGINT[]))
                UNION ALL
                SELECT 'modifier'::TEXT AS item_type, name, name_key,
                       NULL::BIGINT AS badge_role_id
                FROM modifiers
                WHERE guild_id = $1 AND purchasable = TRUE
                UNION ALL
                SELECT 'ticket'::TEXT AS item_type, name, name_key,
                       NULL::BIGINT AS badge_role_id
                FROM tickets
                WHERE guild_id = $1 AND purchasable = TRUE
            ) AS available
            WHERE POSITION($2 IN name_key) > 0
            ORDER BY name
            LIMIT $4
            """,
            guild_id,
            search,
            owned_role_ids,
            limit,
        )

    async def search_owned_objects(
        self,
        guild_id: int,
        user_id: int,
        role_ids: list[int],
        search: str,
        limit: int = 25,
    ):
        return await self._pool().fetch(
            """
            WITH whitelist_access AS (
                SELECT EXISTS (
                    SELECT 1
                    FROM whitelist_entries
                    WHERE guild_id = $1
                      AND (
                          (target_type = 'member' AND target_id = $2)
                          OR
                          (target_type = 'role' AND target_id = ANY($3::BIGINT[]))
                      )
                ) AS allowed
            ),
            owned AS (
                SELECT
                    'badge'::TEXT AS item_type,
                    badge.name,
                    badge.name_key,
                    1::INTEGER AS quantity,
                    TRUE AS active,
                    (
                        NOT (badge.badge_role_id = ANY($3::BIGINT[]))
                        AND badge.whitelist_enabled
                        AND whitelist_access.allowed
                    ) AS via_whitelist
                FROM badges AS badge
                CROSS JOIN whitelist_access
                WHERE badge.guild_id = $1
                  AND (
                      badge.badge_role_id = ANY($3::BIGINT[])
                      OR (
                          badge.whitelist_enabled
                          AND whitelist_access.allowed
                      )
                  )
                UNION ALL
                SELECT
                    'modifier'::TEXT,
                    modifier.name,
                    modifier.name_key,
                    inventory.quantity,
                    TRUE,
                    FALSE
                FROM modifier_inventory AS inventory
                JOIN modifiers AS modifier ON modifier.id = inventory.modifier_id
                WHERE inventory.guild_id = $1
                  AND inventory.user_id = $2
                  AND inventory.quantity > 0
                UNION ALL
                SELECT
                    'ticket'::TEXT,
                    ticket.name,
                    ticket.name_key,
                    inventory.quantity,
                    ticket.active,
                    FALSE
                FROM ticket_inventory AS inventory
                JOIN tickets AS ticket ON ticket.id = inventory.ticket_id
                WHERE inventory.guild_id = $1
                  AND inventory.user_id = $2
                  AND inventory.quantity > 0
            )
            SELECT *
            FROM owned
            WHERE POSITION($4 IN name_key) > 0
            ORDER BY
                CASE item_type
                    WHEN 'badge' THEN 0
                    WHEN 'modifier' THEN 1
                    ELSE 2
                END,
                name
            LIMIT $5
            """,
            guild_id,
            user_id,
            role_ids,
            search,
            limit,
        )

    async def search_removable_objects(
        self,
        guild_id: int,
        user_id: int,
        role_ids: list[int],
        search: str,
        limit: int = 25,
    ):
        return await self._pool().fetch(
            """
            SELECT *
            FROM (
                SELECT 'badge'::TEXT AS item_type, name, name_key,
                       1::INTEGER AS quantity
                FROM badges
                WHERE guild_id = $1
                  AND badge_role_id = ANY($3::BIGINT[])
                UNION ALL
                SELECT 'modifier'::TEXT, modifier.name, modifier.name_key,
                       inventory.quantity
                FROM modifier_inventory AS inventory
                JOIN modifiers AS modifier ON modifier.id = inventory.modifier_id
                WHERE inventory.guild_id = $1
                  AND inventory.user_id = $2
                  AND inventory.quantity > 0
                UNION ALL
                SELECT 'ticket'::TEXT, ticket.name, ticket.name_key,
                       inventory.quantity
                FROM ticket_inventory AS inventory
                JOIN tickets AS ticket ON ticket.id = inventory.ticket_id
                WHERE inventory.guild_id = $1
                  AND inventory.user_id = $2
                  AND inventory.quantity > 0
            ) AS removable
            WHERE POSITION($4 IN name_key) > 0
            ORDER BY
                CASE item_type
                    WHEN 'badge' THEN 0
                    WHEN 'modifier' THEN 1
                    ELSE 2
                END,
                name
            LIMIT $5
            """,
            guild_id,
            user_id,
            role_ids,
            search,
            limit,
        )

    async def get_shop_category(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            """
            SELECT name, name_key, description
            FROM shop_categories
            WHERE guild_id = $1 AND name_key = $2
            """,
            guild_id,
            name_key,
        )

    async def list_shop_categories(self, guild_id: int):
        return await self._pool().fetch(
            """
            SELECT name, name_key, description
            FROM shop_categories
            WHERE guild_id = $1
            ORDER BY
                CASE WHEN name_key = 'general' THEN 0 ELSE 1 END,
                name
            """,
            guild_id,
        )

    async def create_shop_category(
        self,
        guild_id: int,
        name: str,
        name_key: str,
        description: str,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO shop_categories (
                guild_id, name, name_key, description
            )
            VALUES ($1, $2, $3, $4)
            """,
            guild_id,
            name,
            name_key,
            description,
        )

    async def update_shop_category(
        self,
        guild_id: int,
        old_name_key: str,
        name: str,
        name_key: str,
        description: str,
    ) -> bool:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    """
                    SELECT name
                    FROM shop_categories
                    WHERE guild_id = $1 AND name_key = $2
                    FOR UPDATE
                    """,
                    guild_id,
                    old_name_key,
                )
                if current is None:
                    return False
                await connection.execute(
                    """
                    UPDATE shop_categories
                    SET name = $3, name_key = $4, description = $5
                    WHERE guild_id = $1 AND name_key = $2
                    """,
                    guild_id,
                    old_name_key,
                    name,
                    name_key,
                    description,
                )
                for table in ("badges", "modifiers", "tickets"):
                    await connection.execute(
                        f"""
                        UPDATE {table}
                        SET shop_section = $3
                        WHERE guild_id = $1 AND LOWER(shop_section) = LOWER($2)
                        """,
                        guild_id,
                        current["name"],
                        name,
                    )
                return True

    async def delete_shop_category(
        self,
        guild_id: int,
        name_key: str,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                category = await connection.fetchrow(
                    """
                    DELETE FROM shop_categories
                    WHERE guild_id = $1 AND name_key = $2
                    RETURNING name, description
                    """,
                    guild_id,
                    name_key,
                )
                if category is None:
                    return None
                affected = 0
                for table in ("badges", "modifiers", "tickets"):
                    result = await connection.execute(
                        f"""
                        UPDATE {table}
                        SET shop_section = NULL
                        WHERE guild_id = $1 AND LOWER(shop_section) = LOWER($2)
                        """,
                        guild_id,
                        category["name"],
                    )
                    affected += int(result.rsplit(" ", 1)[-1])
                return {
                    "name": category["name"],
                    "description": category["description"],
                    "affected_items": affected,
                }

    async def create_modifier(
        self,
        guild_id: int,
        name: str,
        name_key: str,
        purchasable: bool,
        price: int,
        shop_section: str | None,
        emoji: str | None,
        messages: list[str],
        trigger_numerator: int,
        trigger_denominator: int,
        cooldown_seconds: int,
        duration_minutes: int,
        effect_scope: str,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO modifiers (
                guild_id, name, name_key, purchasable, price,
                shop_section, emoji, messages, trigger_numerator, trigger_denominator,
                cooldown_seconds, duration_minutes, effect_scope
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8::TEXT[],
                $9, $10, $11, $12, $13
            )
            """,
            guild_id,
            name,
            name_key,
            purchasable,
            price,
            shop_section,
            emoji,
            messages,
            trigger_numerator,
            trigger_denominator,
            cooldown_seconds,
            duration_minutes,
            effect_scope,
        )

    async def update_modifier(
        self,
        guild_id: int,
        old_name_key: str,
        name: str,
        name_key: str,
        purchasable: bool,
        price: int,
        shop_section: str | None,
        emoji: str | None,
        messages: list[str],
        trigger_numerator: int,
        trigger_denominator: int,
        cooldown_seconds: int,
        duration_minutes: int,
        effect_scope: str,
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE modifiers
            SET name = $3,
                name_key = $4,
                purchasable = $5,
                price = $6,
                shop_section = $7,
                emoji = $8,
                messages = $9::TEXT[],
                trigger_numerator = $10,
                trigger_denominator = $11,
                cooldown_seconds = $12,
                duration_minutes = $13,
                effect_scope = $14
            WHERE guild_id = $1 AND name_key = $2
            """,
            guild_id,
            old_name_key,
            name,
            name_key,
            purchasable,
            price,
            shop_section,
            emoji,
            messages,
            trigger_numerator,
            trigger_denominator,
            cooldown_seconds,
            duration_minutes,
            effect_scope,
        )
        return result == "UPDATE 1"

    async def delete_modifier(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            """
            DELETE FROM modifiers
            WHERE guild_id = $1 AND name_key = $2
            RETURNING *
            """,
            guild_id,
            name_key,
        )

    async def get_ticket(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            "SELECT * FROM tickets WHERE guild_id = $1 AND name_key = $2",
            guild_id,
            name_key,
        )

    async def list_tickets(self, guild_id: int, purchasable_only: bool = False):
        if purchasable_only:
            return await self._pool().fetch(
                """
                SELECT * FROM tickets
                WHERE guild_id = $1 AND purchasable = TRUE
                ORDER BY COALESCE(shop_section, 'General'), price, name
                """,
                guild_id,
            )
        return await self._pool().fetch(
            "SELECT * FROM tickets WHERE guild_id = $1 ORDER BY name",
            guild_id,
        )

    async def create_ticket(
        self,
        guild_id: int,
        name: str,
        name_key: str,
        purchasable: bool,
        price: int,
        shop_section: str | None,
        emoji: str | None,
        description: str,
        active: bool,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO tickets (
                guild_id, name, name_key, purchasable, price,
                shop_section, emoji, description, active
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            guild_id,
            name,
            name_key,
            purchasable,
            price,
            shop_section,
            emoji,
            description,
            active,
        )

    async def update_ticket(
        self,
        guild_id: int,
        old_name_key: str,
        name: str,
        name_key: str,
        purchasable: bool,
        price: int,
        shop_section: str | None,
        emoji: str | None,
        description: str,
        active: bool,
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE tickets
            SET name = $3,
                name_key = $4,
                purchasable = $5,
                price = $6,
                shop_section = $7,
                emoji = $8,
                description = $9,
                active = $10
            WHERE guild_id = $1 AND name_key = $2
            """,
            guild_id,
            old_name_key,
            name,
            name_key,
            purchasable,
            price,
            shop_section,
            emoji,
            description,
            active,
        )
        return result == "UPDATE 1"

    async def delete_ticket(self, guild_id: int, name_key: str):
        return await self._pool().fetchrow(
            """
            DELETE FROM tickets
            WHERE guild_id = $1 AND name_key = $2
            RETURNING *
            """,
            guild_id,
            name_key,
        )

    async def delete_object_with_balance_refunds(
        self,
        guild_id: int,
        item_type: str,
        name_key: str,
        badge_user_ids: list[int],
        refund_enabled: bool,
        actor_id: int,
        max_balance: int,
    ) -> dict | None:
        table_by_type = {
            "badge": "badges",
            "modifier": "modifiers",
            "ticket": "tickets",
        }
        table = table_by_type.get(item_type)
        if table is None:
            raise ValueError("Tipo de objeto no válido.")

        async with self._pool().acquire() as connection:
            async with connection.transaction():
                item = await connection.fetchrow(
                    f"""
                    SELECT *
                    FROM {table}
                    WHERE guild_id = $1 AND name_key = $2
                    FOR UPDATE
                    """,
                    guild_id,
                    name_key,
                )
                if item is None:
                    return None

                cancelled_event_rows = await connection.fetch(
                    """
                    SELECT event.guild_id, event.channel_id, event.message_id,
                           event.question, event.answer_text, event.reward,
                           event.expires_at, event.created_by
                    FROM active_question_events AS event
                    WHERE event.guild_id = $1
                      AND EXISTS (
                          SELECT 1
                          FROM active_question_event_rewards AS reward
                          WHERE reward.guild_id = event.guild_id
                            AND reward.channel_id = event.channel_id
                            AND reward.item_type = $2
                            AND reward.item_id = $3
                      )
                    FOR UPDATE
                    """,
                    guild_id,
                    item_type,
                    item["id"],
                )
                cancelled_reward_rows = []
                if cancelled_event_rows:
                    cancelled_reward_rows = await connection.fetch(
                        """
                        SELECT reward.guild_id, reward.channel_id, reward.position,
                               reward.item_type, reward.item_id, reward.quantity,
                               reward.name, reward.emoji
                        FROM active_question_event_rewards AS reward
                        JOIN active_question_events AS event
                          ON event.guild_id = reward.guild_id
                         AND event.channel_id = reward.channel_id
                        WHERE event.guild_id = $1
                          AND EXISTS (
                              SELECT 1
                              FROM active_question_event_rewards AS selected
                              WHERE selected.guild_id = event.guild_id
                                AND selected.channel_id = event.channel_id
                                AND selected.item_type = $2
                                AND selected.item_id = $3
                          )
                        ORDER BY reward.guild_id, reward.channel_id, reward.position
                        """,
                        guild_id,
                        item_type,
                        item["id"],
                    )
                    await connection.execute(
                        """
                        DELETE FROM active_question_events AS event
                        WHERE event.guild_id = $1
                          AND EXISTS (
                              SELECT 1
                              FROM active_question_event_rewards AS reward
                              WHERE reward.guild_id = event.guild_id
                                AND reward.channel_id = event.channel_id
                                AND reward.item_type = $2
                                AND reward.item_id = $3
                          )
                        """,
                        guild_id,
                        item_type,
                        item["id"],
                    )
                quantities: dict[int, int] = {}
                active_user_ids: list[int] = []
                active_channel_ids: list[int] = []
                direct_admin_activations = 0
                if item_type == "badge":
                    for user_id in set(badge_user_ids):
                        quantities[user_id] = 1
                elif item_type == "modifier":
                    inventory_rows = await connection.fetch(
                        """
                        SELECT user_id, quantity
                        FROM modifier_inventory
                        WHERE guild_id = $1 AND modifier_id = $2 AND quantity > 0
                        FOR UPDATE
                        """,
                        guild_id,
                        item["id"],
                    )
                    for row in inventory_rows:
                        quantities[row["user_id"]] = (
                            quantities.get(row["user_id"], 0) + row["quantity"]
                        )
                    active_users = await connection.fetch(
                        """
                        SELECT user_id, owner_user_id
                        FROM active_modifiers
                        WHERE guild_id = $1 AND modifier_id = $2
                          AND expires_at > NOW()
                        FOR UPDATE
                        """,
                        guild_id,
                        item["id"],
                    )
                    active_channels = await connection.fetch(
                        """
                        SELECT channel_id, owner_user_id
                        FROM active_channel_modifiers
                        WHERE guild_id = $1 AND modifier_id = $2
                          AND expires_at > NOW()
                        FOR UPDATE
                        """,
                        guild_id,
                        item["id"],
                    )
                    active_user_ids = [row["user_id"] for row in active_users]
                    active_channel_ids = [
                        row["channel_id"] for row in active_channels
                    ]
                    for row in [*active_users, *active_channels]:
                        owner_user_id = row["owner_user_id"]
                        if owner_user_id is None:
                            direct_admin_activations += 1
                            continue
                        quantities[owner_user_id] = (
                            quantities.get(owner_user_id, 0) + 1
                        )
                else:
                    inventory_rows = await connection.fetch(
                        """
                        SELECT user_id, quantity
                        FROM ticket_inventory
                        WHERE guild_id = $1 AND ticket_id = $2 AND quantity > 0
                        FOR UPDATE
                        """,
                        guild_id,
                        item["id"],
                    )
                    for row in inventory_rows:
                        quantities[row["user_id"]] = row["quantity"]

                price = int(item["price"])
                refunds = []
                if refund_enabled and quantities:
                    user_ids = sorted(quantities)
                    await connection.executemany(
                        """
                        INSERT INTO balances (guild_id, user_id, balance)
                        VALUES ($1, $2, 0)
                        ON CONFLICT DO NOTHING
                        """,
                        [(guild_id, user_id) for user_id in user_ids],
                    )
                    balance_rows = await connection.fetch(
                        """
                        SELECT user_id, balance
                        FROM balances
                        WHERE guild_id = $1 AND user_id = ANY($2::BIGINT[])
                        ORDER BY user_id
                        FOR UPDATE
                        """,
                        guild_id,
                        user_ids,
                    )
                    balances = {
                        row["user_id"]: int(row["balance"])
                        for row in balance_rows
                    }
                    updates = []
                    movements = []
                    for user_id, quantity in quantities.items():
                        old_balance = balances.get(user_id, 0)
                        requested = price * quantity
                        credited = min(requested, max(0, max_balance - old_balance))
                        new_balance = old_balance + credited
                        updates.append((guild_id, user_id, new_balance))
                        movements.append(
                            (
                                guild_id,
                                user_id,
                                actor_id,
                                "object_delete_refund",
                                credited,
                                (
                                    f"Recibió un reembolso de {credited} por "
                                    f"{quantity} unidad(es) de {item['name']} "
                                    "al eliminarse su configuración."
                                ),
                            )
                        )
                        refunds.append(
                            {
                                "user_id": user_id,
                                "quantity": quantity,
                                "requested": requested,
                                "credited": credited,
                                "new_balance": new_balance,
                            }
                        )
                    await connection.executemany(
                        """
                        UPDATE balances
                        SET balance = $3
                        WHERE guild_id = $1 AND user_id = $2
                        """,
                        updates,
                    )
                    await connection.executemany(
                        """
                        INSERT INTO movements (
                            guild_id, user_id, actor_id, action, amount, description
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        movements,
                    )

                deleted = await connection.fetchrow(
                    f"""
                    DELETE FROM {table}
                    WHERE guild_id = $1 AND name_key = $2
                    RETURNING *
                    """,
                    guild_id,
                    name_key,
                )
                return {
                    "item": dict(deleted),
                    "item_type": item_type,
                    "price": price,
                    "eligible_units": sum(quantities.values()),
                    "eligible_users": len(quantities),
                    "direct_admin_activations": direct_admin_activations,
                    "active_user_ids": active_user_ids,
                    "active_channel_ids": active_channel_ids,
                    "cancelled_events": _question_events_with_rewards(
                        cancelled_event_rows,
                        cancelled_reward_rows,
                    ),
                    "refunds": refunds,
                    "total_requested": sum(
                        row["requested"] for row in refunds
                    ),
                    "total_credited": sum(row["credited"] for row in refunds),
                }

    async def list_modifier_inventory(self, guild_id: int, user_id: int):
        return await self._pool().fetch(
            """
            SELECT m.*, i.quantity
            FROM modifier_inventory i
            JOIN modifiers m ON m.id = i.modifier_id
            WHERE i.guild_id = $1 AND i.user_id = $2 AND i.quantity > 0
            ORDER BY m.name
            """,
            guild_id,
            user_id,
        )

    async def add_modifier_inventory(
        self,
        guild_id: int,
        user_id: int,
        modifier_id: int,
        quantity: int,
    ) -> int:
        return await self._pool().fetchval(
            """
            INSERT INTO modifier_inventory (guild_id, user_id, modifier_id, quantity)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, user_id, modifier_id)
            DO UPDATE SET quantity = modifier_inventory.quantity + EXCLUDED.quantity
            RETURNING quantity
            """,
            guild_id,
            user_id,
            modifier_id,
            quantity,
        )

    async def remove_modifier_inventory(
        self,
        guild_id: int,
        user_id: int,
        modifier_id: int,
        quantity: int,
    ) -> int:
        value = await self._pool().fetchval(
            """
            UPDATE modifier_inventory
            SET quantity = GREATEST(quantity - $4, 0)
            WHERE guild_id = $1 AND user_id = $2 AND modifier_id = $3
            RETURNING quantity
            """,
            guild_id,
            user_id,
            modifier_id,
            quantity,
        )
        return int(value or 0)

    async def list_ticket_inventory(self, guild_id: int, user_id: int):
        return await self._pool().fetch(
            """
            SELECT ticket.*, inventory.quantity
            FROM ticket_inventory AS inventory
            JOIN tickets AS ticket ON ticket.id = inventory.ticket_id
            WHERE inventory.guild_id = $1 AND inventory.user_id = $2
              AND inventory.quantity > 0
            ORDER BY ticket.name
            """,
            guild_id,
            user_id,
        )

    async def add_ticket_inventory(
        self,
        guild_id: int,
        user_id: int,
        ticket_id: int,
        quantity: int,
    ) -> int:
        return await self._pool().fetchval(
            """
            INSERT INTO ticket_inventory (guild_id, user_id, ticket_id, quantity)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, user_id, ticket_id)
            DO UPDATE SET quantity = ticket_inventory.quantity + EXCLUDED.quantity
            RETURNING quantity
            """,
            guild_id,
            user_id,
            ticket_id,
            quantity,
        )

    async def remove_ticket_inventory(
        self,
        guild_id: int,
        user_id: int,
        ticket_id: int,
        quantity: int,
    ) -> int:
        value = await self._pool().fetchval(
            """
            UPDATE ticket_inventory
            SET quantity = GREATEST(quantity - $4, 0)
            WHERE guild_id = $1 AND user_id = $2 AND ticket_id = $3
            RETURNING quantity
            """,
            guild_id,
            user_id,
            ticket_id,
            quantity,
        )
        return int(value or 0)

    async def consume_ticket(
        self,
        guild_id: int,
        user_id: int,
        name_key: str,
        user_is_admin: bool = False,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason, admins_bypass
                    )
                    SELECT $1, section, TRUE, NULL, FALSE
                    FROM UNNEST(ARRAY['all_objects', 'tickets']) AS sections(section)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                )
                sections = await connection.fetch(
                    """
                    SELECT section, enabled, disabled_reason, admins_bypass
                    FROM object_section_settings
                    WHERE guild_id = $1
                      AND section IN ('all_objects', 'tickets')
                    ORDER BY CASE WHEN section = 'all_objects' THEN 0 ELSE 1 END
                    FOR SHARE
                    """,
                    guild_id,
                )
                blocker = next(
                    (
                        row
                        for row in sections
                        if not row["enabled"]
                        and not (user_is_admin and row["admins_bypass"])
                    ),
                    None,
                )
                if blocker is not None:
                    return {
                        "status": "disabled",
                        "section": blocker["section"],
                        "reason": blocker["disabled_reason"],
                    }
                row = await connection.fetchrow(
                    """
                    UPDATE ticket_inventory AS inventory
                    SET quantity = inventory.quantity - 1
                    FROM tickets AS ticket
                    WHERE inventory.guild_id = $1
                      AND inventory.user_id = $2
                      AND inventory.quantity > 0
                      AND ticket.id = inventory.ticket_id
                      AND ticket.guild_id = $1
                      AND ticket.name_key = $3
                      AND ticket.active = TRUE
                    RETURNING ticket.id, ticket.name, ticket.description,
                              inventory.quantity
                    """,
                    guild_id,
                    user_id,
                    name_key,
                )
                if row is None:
                    return None
                result = dict(row)
                result["status"] = "consumed"
                return result

    async def purchase_ticket(
        self,
        guild_id: int,
        user_id: int,
        name_key: str,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                ticket = await connection.fetchrow(
                    """
                    SELECT * FROM tickets
                    WHERE guild_id = $1 AND name_key = $2 AND purchasable = TRUE
                    FOR UPDATE
                    """,
                    guild_id,
                    name_key,
                )
                if ticket is None:
                    return None
                await connection.execute(
                    """
                    INSERT INTO balances (guild_id, user_id, balance)
                    VALUES ($1, $2, 0)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                    user_id,
                )
                new_balance = await connection.fetchval(
                    """
                    UPDATE balances
                    SET balance = balance - $3
                    WHERE guild_id = $1 AND user_id = $2 AND balance >= $3
                    RETURNING balance
                    """,
                    guild_id,
                    user_id,
                    ticket["price"],
                )
                if new_balance is None:
                    return {"status": "insufficient"}
                quantity = await connection.fetchval(
                    """
                    INSERT INTO ticket_inventory (
                        guild_id, user_id, ticket_id, quantity
                    )
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (guild_id, user_id, ticket_id)
                    DO UPDATE SET quantity = ticket_inventory.quantity + 1
                    RETURNING quantity
                    """,
                    guild_id,
                    user_id,
                    ticket["id"],
                )
                return {
                    "status": "purchased",
                    "name": ticket["name"],
                    "price": ticket["price"],
                    "new_balance": new_balance,
                    "quantity": quantity,
                    "active": ticket["active"],
                }

    async def purchase_modifier(
        self,
        guild_id: int,
        user_id: int,
        name_key: str,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                modifier = await connection.fetchrow(
                    """
                    SELECT * FROM modifiers
                    WHERE guild_id = $1 AND name_key = $2 AND purchasable = TRUE
                    FOR UPDATE
                    """,
                    guild_id,
                    name_key,
                )
                if modifier is None:
                    return None
                await connection.execute(
                    """
                    INSERT INTO balances (guild_id, user_id, balance)
                    VALUES ($1, $2, 0)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                    user_id,
                )
                new_balance = await connection.fetchval(
                    """
                    UPDATE balances
                    SET balance = balance - $3
                    WHERE guild_id = $1 AND user_id = $2 AND balance >= $3
                    RETURNING balance
                    """,
                    guild_id,
                    user_id,
                    modifier["price"],
                )
                if new_balance is None:
                    return {"status": "insufficient"}
                quantity = await connection.fetchval(
                    """
                    INSERT INTO modifier_inventory (
                        guild_id, user_id, modifier_id, quantity
                    )
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (guild_id, user_id, modifier_id)
                    DO UPDATE SET quantity = modifier_inventory.quantity + 1
                    RETURNING quantity
                    """,
                    guild_id,
                    user_id,
                    modifier["id"],
                )
                return {
                    "status": "purchased",
                    "name": modifier["name"],
                    "price": modifier["price"],
                    "new_balance": new_balance,
                    "quantity": quantity,
                }

    async def activate_modifier(
        self,
        guild_id: int,
        owner_user_id: int,
        target_user_id: int,
        name_key: str,
        channel_id: int,
        owner_is_admin: bool = False,
    ) -> dict:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason, admins_bypass
                    )
                    SELECT $1, section, TRUE, NULL, FALSE
                    FROM UNNEST(ARRAY['all_objects', 'modifiers']) AS sections(section)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                )
                sections = await connection.fetch(
                    """
                    SELECT section, enabled, disabled_reason, admins_bypass
                    FROM object_section_settings
                    WHERE guild_id = $1
                      AND section IN ('all_objects', 'modifiers')
                    ORDER BY CASE WHEN section = 'all_objects' THEN 0 ELSE 1 END
                    FOR SHARE
                    """,
                    guild_id,
                )
                blocker = next(
                    (
                        row
                        for row in sections
                        if not row["enabled"]
                        and not (owner_is_admin and row["admins_bypass"])
                    ),
                    None,
                )
                if blocker is not None:
                    return {
                        "status": "disabled",
                        "section": blocker["section"],
                        "reason": blocker["disabled_reason"],
                    }
                active = await connection.fetchrow(
                    """
                    SELECT m.name, a.expires_at
                    FROM active_modifiers a
                    JOIN modifiers m ON m.id = a.modifier_id
                    WHERE a.guild_id = $1 AND a.user_id = $2
                    FOR UPDATE OF a
                    """,
                    guild_id,
                    target_user_id,
                )
                if active is not None and active["expires_at"] > await connection.fetchval("SELECT NOW()"):
                    return {
                        "status": "already_active",
                        "name": active["name"],
                        "expires_at": active["expires_at"],
                    }
                if active is not None:
                    await connection.execute(
                        "DELETE FROM active_modifiers WHERE guild_id = $1 AND user_id = $2",
                        guild_id,
                        target_user_id,
                    )
                inventory = await connection.fetchrow(
                    """
                    SELECT i.modifier_id, i.quantity, m.name, m.duration_minutes,
                           m.effect_scope
                    FROM modifier_inventory i
                    JOIN modifiers m ON m.id = i.modifier_id
                    WHERE i.guild_id = $1 AND i.user_id = $2
                      AND m.guild_id = $1 AND m.name_key = $3
                    FOR UPDATE OF i
                    """,
                    guild_id,
                    owner_user_id,
                    name_key,
                )
                if inventory is None or inventory["quantity"] <= 0:
                    return {"status": "missing"}
                if inventory["effect_scope"] != "individual":
                    return {"status": "wrong_scope", "scope": inventory["effect_scope"]}
                quantity = await connection.fetchval(
                    """
                    UPDATE modifier_inventory
                    SET quantity = quantity - 1
                    WHERE guild_id = $1 AND user_id = $2 AND modifier_id = $3
                    RETURNING quantity
                    """,
                    guild_id,
                    owner_user_id,
                    inventory["modifier_id"],
                )
                expires_at = await connection.fetchval(
                    """
                    INSERT INTO active_modifiers (
                        guild_id, user_id, owner_user_id, modifier_id, channel_id,
                        expires_at, duration_minutes
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        NOW() + ($6::INTEGER * INTERVAL '1 minute'), $6
                    )
                    RETURNING expires_at
                    """,
                    guild_id,
                    target_user_id,
                    owner_user_id,
                    inventory["modifier_id"],
                    channel_id,
                    inventory["duration_minutes"],
                )
                return {
                    "status": "activated",
                    "name": inventory["name"],
                    "quantity": quantity,
                    "expires_at": expires_at,
                    "duration_minutes": inventory["duration_minutes"],
                }

    async def activate_channel_modifier(
        self,
        guild_id: int,
        owner_user_id: int,
        target_channel_id: int,
        name_key: str,
        owner_is_admin: bool = False,
    ) -> dict:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason, admins_bypass
                    )
                    SELECT $1, section, TRUE, NULL, FALSE
                    FROM UNNEST(ARRAY['all_objects', 'modifiers']) AS sections(section)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                )
                sections = await connection.fetch(
                    """
                    SELECT section, enabled, disabled_reason, admins_bypass
                    FROM object_section_settings
                    WHERE guild_id = $1
                      AND section IN ('all_objects', 'modifiers')
                    ORDER BY CASE WHEN section = 'all_objects' THEN 0 ELSE 1 END
                    FOR SHARE
                    """,
                    guild_id,
                )
                blocker = next(
                    (
                        row
                        for row in sections
                        if not row["enabled"]
                        and not (owner_is_admin and row["admins_bypass"])
                    ),
                    None,
                )
                if blocker is not None:
                    return {
                        "status": "disabled",
                        "section": blocker["section"],
                        "reason": blocker["disabled_reason"],
                    }
                active = await connection.fetchrow(
                    """
                    SELECT m.name, a.expires_at
                    FROM active_channel_modifiers a
                    JOIN modifiers m ON m.id = a.modifier_id
                    WHERE a.guild_id = $1 AND a.channel_id = $2
                    FOR UPDATE OF a
                    """,
                    guild_id,
                    target_channel_id,
                )
                now = await connection.fetchval("SELECT NOW()")
                if active is not None and active["expires_at"] > now:
                    return {
                        "status": "already_active",
                        "name": active["name"],
                        "expires_at": active["expires_at"],
                    }
                if active is not None:
                    await connection.execute(
                        """
                        DELETE FROM active_channel_modifiers
                        WHERE guild_id = $1 AND channel_id = $2
                        """,
                        guild_id,
                        target_channel_id,
                    )
                inventory = await connection.fetchrow(
                    """
                    SELECT i.modifier_id, i.quantity, m.name, m.duration_minutes,
                           m.effect_scope
                    FROM modifier_inventory i
                    JOIN modifiers m ON m.id = i.modifier_id
                    WHERE i.guild_id = $1 AND i.user_id = $2
                      AND m.guild_id = $1 AND m.name_key = $3
                    FOR UPDATE OF i
                    """,
                    guild_id,
                    owner_user_id,
                    name_key,
                )
                if inventory is None or inventory["quantity"] <= 0:
                    return {"status": "missing"}
                if inventory["effect_scope"] != "channel":
                    return {"status": "wrong_scope", "scope": inventory["effect_scope"]}
                quantity = await connection.fetchval(
                    """
                    UPDATE modifier_inventory
                    SET quantity = quantity - 1
                    WHERE guild_id = $1 AND user_id = $2 AND modifier_id = $3
                    RETURNING quantity
                    """,
                    guild_id,
                    owner_user_id,
                    inventory["modifier_id"],
                )
                expires_at = await connection.fetchval(
                    """
                    INSERT INTO active_channel_modifiers (
                        guild_id, channel_id, owner_user_id, modifier_id,
                        expires_at, duration_minutes
                    )
                    VALUES (
                        $1, $2, $3, $4,
                        NOW() + ($5::INTEGER * INTERVAL '1 minute'), $5
                    )
                    RETURNING expires_at
                    """,
                    guild_id,
                    target_channel_id,
                    owner_user_id,
                    inventory["modifier_id"],
                    inventory["duration_minutes"],
                )
                return {
                    "status": "activated",
                    "name": inventory["name"],
                    "quantity": quantity,
                    "expires_at": expires_at,
                    "duration_minutes": inventory["duration_minutes"],
                }

    async def force_activate_modifier(
        self,
        guild_id: int,
        user_id: int,
        modifier_id: int,
        channel_id: int,
        duration_minutes: int = 5,
    ):
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                activation = await connection.fetchrow(
                    """
                    INSERT INTO active_modifiers (
                        guild_id, user_id, owner_user_id, modifier_id, channel_id,
                        expires_at, last_trigger_at, duration_minutes
                    )
                    VALUES (
                        $1, $2, NULL, $3, $4,
                        NOW() + ($5::INTEGER * INTERVAL '1 minute'), NULL, $5
                    )
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET
                        modifier_id = EXCLUDED.modifier_id,
                        owner_user_id = NULL,
                        channel_id = EXCLUDED.channel_id,
                        expires_at = EXCLUDED.expires_at,
                        last_trigger_at = NULL,
                        duration_minutes = EXCLUDED.duration_minutes
                    RETURNING expires_at
                    """,
                    guild_id,
                    user_id,
                    modifier_id,
                    channel_id,
                    duration_minutes,
                )
                return {
                    "status": "activated",
                    "expires_at": activation["expires_at"],
                }

    async def force_activate_channel_modifier(
        self,
        guild_id: int,
        channel_id: int,
        modifier_id: int,
        duration_minutes: int = 5,
    ):
        activation = await self._pool().fetchrow(
            """
            INSERT INTO active_channel_modifiers (
                guild_id, channel_id, owner_user_id, modifier_id,
                expires_at, last_trigger_at, duration_minutes
            )
            VALUES (
                $1, $2, NULL, $3,
                NOW() + ($4::INTEGER * INTERVAL '1 minute'), NULL, $4
            )
            ON CONFLICT (guild_id, channel_id)
            DO UPDATE SET
                modifier_id = EXCLUDED.modifier_id,
                owner_user_id = NULL,
                expires_at = EXCLUDED.expires_at,
                last_trigger_at = NULL,
                duration_minutes = EXCLUDED.duration_minutes
            RETURNING expires_at
            """,
            guild_id,
            channel_id,
            modifier_id,
            duration_minutes,
        )
        return {
            "status": "activated",
            "expires_at": activation["expires_at"],
        }

    async def deactivate_modifier(self, guild_id: int, user_id: int):
        return await self._pool().fetchrow(
            """
            WITH removed AS (
                DELETE FROM active_modifiers
                WHERE guild_id = $1 AND user_id = $2
                RETURNING modifier_id
            )
            SELECT modifier.name
            FROM removed
            JOIN modifiers AS modifier ON modifier.id = removed.modifier_id
            """,
            guild_id,
            user_id,
        )

    async def deactivate_channel_modifier(self, guild_id: int, channel_id: int):
        return await self._pool().fetchrow(
            """
            WITH removed AS (
                DELETE FROM active_channel_modifiers
                WHERE guild_id = $1 AND channel_id = $2
                RETURNING modifier_id
            )
            SELECT modifier.name
            FROM removed
            JOIN modifiers AS modifier ON modifier.id = removed.modifier_id
            """,
            guild_id,
            channel_id,
        )

    async def deactivate_and_refund_modifier(
        self,
        guild_id: int,
        target_user_id: int,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                active = await connection.fetchrow(
                    """
                    SELECT active.owner_user_id, active.modifier_id,
                           active.expires_at > NOW() AS is_active,
                           modifier.name
                    FROM active_modifiers AS active
                    JOIN modifiers AS modifier ON modifier.id = active.modifier_id
                    WHERE active.guild_id = $1 AND active.user_id = $2
                    FOR UPDATE OF active
                    """,
                    guild_id,
                    target_user_id,
                )
                if active is None:
                    return None
                await connection.execute(
                    """
                    DELETE FROM active_modifiers
                    WHERE guild_id = $1 AND user_id = $2
                    """,
                    guild_id,
                    target_user_id,
                )
                if not active["is_active"]:
                    return {
                        "status": "expired",
                        "name": active["name"],
                    }
                if active["owner_user_id"] is None:
                    return {
                        "status": "deactivated_without_refund",
                        "name": active["name"],
                    }
                quantity = await connection.fetchval(
                    """
                    INSERT INTO modifier_inventory (
                        guild_id, user_id, modifier_id, quantity
                    )
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (guild_id, user_id, modifier_id)
                    DO UPDATE SET quantity = modifier_inventory.quantity + 1
                    RETURNING quantity
                    """,
                    guild_id,
                    active["owner_user_id"],
                    active["modifier_id"],
                )
                return {
                    "status": "refunded",
                    "name": active["name"],
                    "owner_user_id": active["owner_user_id"],
                    "quantity": quantity,
                }

    async def deactivate_and_refund_channel_modifier(
        self,
        guild_id: int,
        channel_id: int,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                active = await connection.fetchrow(
                    """
                    SELECT active.owner_user_id, active.modifier_id,
                           active.expires_at > NOW() AS is_active,
                           modifier.name
                    FROM active_channel_modifiers AS active
                    JOIN modifiers AS modifier ON modifier.id = active.modifier_id
                    WHERE active.guild_id = $1 AND active.channel_id = $2
                    FOR UPDATE OF active
                    """,
                    guild_id,
                    channel_id,
                )
                if active is None:
                    return None
                await connection.execute(
                    """
                    DELETE FROM active_channel_modifiers
                    WHERE guild_id = $1 AND channel_id = $2
                    """,
                    guild_id,
                    channel_id,
                )
                if not active["is_active"]:
                    return {
                        "status": "expired",
                        "name": active["name"],
                    }
                if active["owner_user_id"] is None:
                    return {
                        "status": "deactivated_without_refund",
                        "name": active["name"],
                    }
                quantity = await connection.fetchval(
                    """
                    INSERT INTO modifier_inventory (
                        guild_id, user_id, modifier_id, quantity
                    )
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (guild_id, user_id, modifier_id)
                    DO UPDATE SET quantity = modifier_inventory.quantity + 1
                    RETURNING quantity
                    """,
                    guild_id,
                    active["owner_user_id"],
                    active["modifier_id"],
                )
                return {
                    "status": "refunded",
                    "name": active["name"],
                    "owner_user_id": active["owner_user_id"],
                    "quantity": quantity,
                }

    async def get_active_modifier(self, guild_id: int, user_id: int):
        return await self._pool().fetchrow(
            """
            SELECT modifier.name, active.expires_at
            FROM active_modifiers AS active
            JOIN modifiers AS modifier ON modifier.id = active.modifier_id
            WHERE active.guild_id = $1
              AND active.user_id = $2
              AND active.expires_at > NOW()
            """,
            guild_id,
            user_id,
        )

    async def get_active_channel_modifier(self, guild_id: int, channel_id: int):
        return await self._pool().fetchrow(
            """
            SELECT modifier.name, active.expires_at
            FROM active_channel_modifiers AS active
            JOIN modifiers AS modifier ON modifier.id = active.modifier_id
            WHERE active.guild_id = $1
              AND active.channel_id = $2
              AND active.expires_at > NOW()
            """,
            guild_id,
            channel_id,
        )

    async def list_active_channel_modifiers(self, guild_id: int):
        return await self._pool().fetch(
            """
            SELECT active.channel_id, modifier.name, active.expires_at
            FROM active_channel_modifiers AS active
            JOIN modifiers AS modifier ON modifier.id = active.modifier_id
            WHERE active.guild_id = $1
              AND active.expires_at > NOW()
            ORDER BY active.expires_at, active.channel_id
            """,
            guild_id,
        )

    async def list_active_modifier_targets(self):
        return await self._pool().fetch(
            """
            SELECT 'individual'::TEXT AS target_kind,
                   guild_id, user_id AS target_id
            FROM active_modifiers
            WHERE expires_at > NOW()
            UNION ALL
            SELECT 'channel'::TEXT AS target_kind,
                   guild_id, channel_id AS target_id
            FROM active_channel_modifiers
            WHERE expires_at > NOW()
            """
        )

    async def try_trigger_message_modifier(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ):
        return await self._pool().fetchrow(
            """
            WITH channel_state AS (
                SELECT EXISTS (
                    SELECT 1
                    FROM active_channel_modifiers AS active
                    WHERE active.guild_id = $1
                      AND active.channel_id = $2
                      AND active.expires_at > NOW()
                ) AS is_active
            ),
            channel_triggered AS (
                UPDATE active_channel_modifiers AS active
                SET last_trigger_at = NOW()
                FROM modifiers AS modifier, channel_state
                WHERE channel_state.is_active
                  AND active.guild_id = $1
                  AND active.channel_id = $2
                  AND (
                      active.owner_user_id IS NULL
                      OR NOT EXISTS (
                          SELECT 1
                          FROM object_section_settings AS section
                          WHERE section.guild_id = active.guild_id
                            AND section.section IN ('all_objects', 'modifiers')
                            AND section.enabled = FALSE
                            AND section.admins_bypass = FALSE
                      )
                  )
                  AND modifier.id = active.modifier_id
                  AND active.expires_at > NOW()
                  AND (
                      active.last_trigger_at IS NULL
                      OR active.last_trigger_at <= NOW() - (
                          modifier.cooldown_seconds * INTERVAL '1 second'
                      )
                  )
                  AND RANDOM() < (
                      modifier.trigger_numerator::DOUBLE PRECISION
                      / modifier.trigger_denominator
                  )
                RETURNING modifier.name, modifier.messages
            ),
            individual_state AS (
                SELECT (
                    NOT channel_state.is_active
                    AND EXISTS (
                        SELECT 1
                        FROM active_modifiers AS active
                        WHERE active.guild_id = $1
                          AND active.user_id = $3
                          AND active.expires_at > NOW()
                    )
                ) AS is_active
                FROM channel_state
            ),
            individual_triggered AS (
                UPDATE active_modifiers AS active
                SET last_trigger_at = NOW()
                FROM modifiers AS modifier, channel_state
                WHERE NOT channel_state.is_active
                  AND active.guild_id = $1
                  AND active.user_id = $3
                  AND (
                      active.owner_user_id IS NULL
                      OR NOT EXISTS (
                          SELECT 1
                          FROM object_section_settings AS section
                          WHERE section.guild_id = active.guild_id
                            AND section.section IN ('all_objects', 'modifiers')
                            AND section.enabled = FALSE
                            AND section.admins_bypass = FALSE
                      )
                  )
                  AND modifier.id = active.modifier_id
                  AND active.expires_at > NOW()
                  AND (
                      active.last_trigger_at IS NULL
                      OR active.last_trigger_at <= NOW() - (
                          modifier.cooldown_seconds * INTERVAL '1 second'
                      )
                  )
                  AND RANDOM() < (
                      modifier.trigger_numerator::DOUBLE PRECISION
                      / modifier.trigger_denominator
                  )
                RETURNING modifier.name, modifier.messages
            )
            SELECT
                channel_state.is_active AS channel_active,
                individual_state.is_active AS individual_active,
                CASE
                    WHEN channel_triggered.name IS NOT NULL THEN 'channel'
                    WHEN individual_triggered.name IS NOT NULL THEN 'individual'
                    ELSE NULL
                END AS triggered_kind,
                COALESCE(
                    channel_triggered.name,
                    individual_triggered.name
                ) AS name,
                COALESCE(
                    channel_triggered.messages,
                    individual_triggered.messages
                ) AS messages
            FROM channel_state
            CROSS JOIN individual_state
            LEFT JOIN channel_triggered ON TRUE
            LEFT JOIN individual_triggered ON TRUE
            """,
            guild_id,
            channel_id,
            user_id,
        )

    async def try_trigger_modifier(
        self,
        guild_id: int,
        user_id: int,
    ):
        return await self._pool().fetchrow(
            """
            UPDATE active_modifiers AS active
            SET last_trigger_at = NOW()
            FROM modifiers AS modifier
            WHERE active.guild_id = $1
              AND active.user_id = $2
              AND (
                  active.owner_user_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1
                      FROM object_section_settings AS section
                      WHERE section.guild_id = active.guild_id
                        AND section.section IN ('all_objects', 'modifiers')
                        AND section.enabled = FALSE
                        AND section.admins_bypass = FALSE
                  )
              )
              AND modifier.id = active.modifier_id
              AND active.expires_at > NOW()
              AND (
                  active.last_trigger_at IS NULL
                  OR active.last_trigger_at <= NOW() - (
                      modifier.cooldown_seconds * INTERVAL '1 second'
                  )
              )
              AND RANDOM() < (
                  modifier.trigger_numerator::DOUBLE PRECISION
                  / modifier.trigger_denominator
              )
            RETURNING modifier.name, modifier.messages
            """,
            guild_id,
            user_id,
        )

    async def try_trigger_channel_modifier(
        self,
        guild_id: int,
        channel_id: int,
    ):
        return await self._pool().fetchrow(
            """
            WITH channel_state AS (
                SELECT EXISTS (
                    SELECT 1
                    FROM active_channel_modifiers AS active
                    WHERE active.guild_id = $1
                      AND active.channel_id = $2
                      AND active.expires_at > NOW()
                ) AS is_active
            ),
            triggered AS (
                UPDATE active_channel_modifiers AS active
                SET last_trigger_at = NOW()
                FROM modifiers AS modifier
                WHERE active.guild_id = $1
                  AND active.channel_id = $2
                  AND (
                      active.owner_user_id IS NULL
                      OR NOT EXISTS (
                          SELECT 1
                          FROM object_section_settings AS section
                          WHERE section.guild_id = active.guild_id
                            AND section.section IN ('all_objects', 'modifiers')
                            AND section.enabled = FALSE
                            AND section.admins_bypass = FALSE
                      )
                  )
                  AND modifier.id = active.modifier_id
                  AND active.expires_at > NOW()
                  AND (
                      active.last_trigger_at IS NULL
                      OR active.last_trigger_at <= NOW() - (
                          modifier.cooldown_seconds * INTERVAL '1 second'
                      )
                  )
                  AND RANDOM() < (
                      modifier.trigger_numerator::DOUBLE PRECISION
                      / modifier.trigger_denominator
                  )
                RETURNING modifier.name, modifier.messages
            )
            SELECT channel_state.is_active, triggered.name, triggered.messages
            FROM channel_state
            LEFT JOIN triggered ON TRUE
            """,
            guild_id,
            channel_id,
        )

    async def pop_expired_modifiers(self):
        return await self._pool().fetch(
            """
            WITH expired AS (
                DELETE FROM active_modifiers
                WHERE expires_at <= NOW()
                RETURNING guild_id, user_id, modifier_id, channel_id, duration_minutes
            )
            SELECT expired.guild_id, expired.user_id, expired.channel_id,
                   expired.duration_minutes, modifier.name
            FROM expired
            JOIN modifiers AS modifier ON modifier.id = expired.modifier_id
            """
        )

    async def pop_expired_channel_modifiers(self):
        return await self._pool().fetch(
            """
            WITH expired AS (
                DELETE FROM active_channel_modifiers
                WHERE expires_at <= NOW()
                RETURNING guild_id, channel_id, owner_user_id, modifier_id,
                          duration_minutes
            )
            SELECT expired.guild_id, expired.channel_id, expired.owner_user_id,
                   expired.duration_minutes, modifier.name
            FROM expired
            JOIN modifiers AS modifier ON modifier.id = expired.modifier_id
            """
        )

    async def add_ticket_admin(self, guild_id: int, user_id: int) -> bool:
        result = await self._pool().execute(
            """
            INSERT INTO ticket_admins (guild_id, user_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            guild_id,
            user_id,
        )
        return result == "INSERT 0 1"

    async def remove_ticket_admin(self, guild_id: int, user_id: int) -> bool:
        result = await self._pool().execute(
            "DELETE FROM ticket_admins WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
        return result == "DELETE 1"

    async def list_ticket_admins(self, guild_id: int):
        return await self._pool().fetch(
            "SELECT user_id FROM ticket_admins WHERE guild_id = $1 ORDER BY user_id",
            guild_id,
        )

    async def add_whitelist_entry(
        self,
        guild_id: int,
        target_type: str,
        target_id: int,
    ) -> bool:
        result = await self._pool().execute(
            """
            INSERT INTO whitelist_entries (guild_id, target_type, target_id)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            guild_id,
            target_type,
            target_id,
        )
        return result == "INSERT 0 1"

    async def remove_whitelist_entry(
        self,
        guild_id: int,
        target_type: str,
        target_id: int,
    ) -> bool:
        result = await self._pool().execute(
            """
            DELETE FROM whitelist_entries
            WHERE guild_id = $1 AND target_type = $2 AND target_id = $3
            """,
            guild_id,
            target_type,
            target_id,
        )
        return result == "DELETE 1"

    async def list_whitelist_entries(self, guild_id: int):
        return await self._pool().fetch(
            """
            SELECT target_type, target_id
            FROM whitelist_entries
            WHERE guild_id = $1
            ORDER BY target_type, target_id
            """,
            guild_id,
        )

    async def is_whitelisted(
        self,
        guild_id: int,
        user_id: int,
        role_ids: list[int],
    ) -> bool:
        return bool(
            await self._pool().fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM whitelist_entries
                    WHERE guild_id = $1
                      AND (
                          (target_type = 'member' AND target_id = $2)
                          OR (target_type = 'role' AND target_id = ANY($3::BIGINT[]))
                      )
                )
                """,
                guild_id,
                user_id,
                role_ids,
            )
        )

    async def get_log_settings(self, guild_id: int):
        return await self._pool().fetchrow(
            """
            SELECT log_channel_id, logs_enabled, coin_emoji, whitelist_emoji
            FROM guild_settings
            WHERE guild_id = $1
            """,
            guild_id,
        )

    async def get_object_section_setting(
        self,
        guild_id: int,
        section: str,
    ) -> dict:
        row = await self._pool().fetchrow(
            """
            SELECT enabled, disabled_reason, admins_bypass
            FROM object_section_settings
            WHERE guild_id = $1 AND section = $2
            """,
            guild_id,
            section,
        )
        if row is None:
            return {
                "enabled": True,
                "disabled_reason": None,
                "admins_bypass": False,
            }
        return dict(row)

    async def get_maintenance_block(
        self,
        guild_id: int,
        section: str,
        is_admin: bool,
    ) -> dict | None:
        sections = [section]
        if section in {"badges", "modifiers", "tickets", "shop"}:
            sections.insert(0, "all_objects")
        rows = await self._pool().fetch(
            """
            SELECT section, disabled_reason, admins_bypass
            FROM object_section_settings
            WHERE guild_id = $1
              AND section = ANY($2::TEXT[])
              AND enabled = FALSE
            ORDER BY ARRAY_POSITION($2::TEXT[], section)
            """,
            guild_id,
            sections,
        )
        for row in rows:
            if is_admin and row["admins_bypass"]:
                continue
            return dict(row)
        return None

    async def toggle_object_section(
        self,
        guild_id: int,
        section: str,
        disabled_reason: str,
        admins_bypass: bool,
    ) -> dict:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason, admins_bypass
                    )
                    VALUES ($1, $2, TRUE, NULL, FALSE)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                    section,
                )
                current = await connection.fetchrow(
                    """
                    SELECT enabled
                    FROM object_section_settings
                    WHERE guild_id = $1 AND section = $2
                    FOR UPDATE
                    """,
                    guild_id,
                    section,
                )
                enabled = not current["enabled"]
                await connection.execute(
                    """
                    UPDATE object_section_settings
                    SET enabled = $3,
                        disabled_reason = CASE WHEN $3 THEN NULL ELSE $4 END,
                        admins_bypass = CASE WHEN $3 THEN FALSE ELSE $5 END
                    WHERE guild_id = $1 AND section = $2
                    """,
                    guild_id,
                    section,
                    enabled,
                    disabled_reason,
                    admins_bypass,
                )

                removed = []
                refunds = []
                if section in {"modifiers", "all_objects", "all_commands"} and not enabled:
                    individual_removed = await connection.fetch(
                        """
                        WITH removed AS (
                            DELETE FROM active_modifiers
                            WHERE guild_id = $1
                            RETURNING user_id, owner_user_id, modifier_id, expires_at
                        )
                        SELECT 'individual'::TEXT AS target_kind,
                               removed.user_id AS target_id,
                               removed.owner_user_id,
                               removed.modifier_id,
                               modifier.name,
                               removed.expires_at > NOW() AS was_active,
                               (
                                   removed.owner_user_id IS NOT NULL
                                   AND removed.expires_at > NOW()
                               ) AS refundable
                        FROM removed
                        JOIN modifiers AS modifier ON modifier.id = removed.modifier_id
                        """,
                        guild_id,
                    )
                    channel_removed = await connection.fetch(
                        """
                        WITH removed AS (
                            DELETE FROM active_channel_modifiers
                            WHERE guild_id = $1
                            RETURNING channel_id, owner_user_id, modifier_id, expires_at
                        )
                        SELECT 'channel'::TEXT AS target_kind,
                               removed.channel_id AS target_id,
                               removed.owner_user_id,
                               removed.modifier_id,
                               modifier.name,
                               removed.expires_at > NOW() AS was_active,
                               (
                                   removed.owner_user_id IS NOT NULL
                                   AND removed.expires_at > NOW()
                               ) AS refundable
                        FROM removed
                        JOIN modifiers AS modifier ON modifier.id = removed.modifier_id
                        """,
                        guild_id,
                    )
                    removed = [*individual_removed, *channel_removed]
                    refunds = [row for row in removed if row["refundable"]]
                    if refunds:
                        await connection.executemany(
                            """
                            INSERT INTO modifier_inventory (
                                guild_id, user_id, modifier_id, quantity
                            )
                            VALUES ($1, $2, $3, 1)
                            ON CONFLICT (guild_id, user_id, modifier_id)
                            DO UPDATE SET quantity = modifier_inventory.quantity + 1
                            """,
                            [
                                (
                                    guild_id,
                                    row["owner_user_id"],
                                    row["modifier_id"],
                                )
                                for row in refunds
                            ],
                        )
                return {
                    "enabled": enabled,
                    "disabled_reason": None if enabled else disabled_reason,
                    "admins_bypass": False if enabled else admins_bypass,
                    "removed": [dict(row) for row in removed],
                    "refunds": [dict(row) for row in refunds],
                }

    async def set_log_settings(
        self,
        guild_id: int,
        channel_id: int | None,
        enabled: bool,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO guild_settings (guild_id, log_channel_id, logs_enabled)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id)
            DO UPDATE SET
                log_channel_id = COALESCE(EXCLUDED.log_channel_id, guild_settings.log_channel_id),
                logs_enabled = EXCLUDED.logs_enabled
            """,
            guild_id,
            channel_id,
            enabled,
        )

    async def set_coin_emoji(self, guild_id: int, emoji: str | None) -> None:
        await self._pool().execute(
            """
            INSERT INTO guild_settings (guild_id, coin_emoji)
            VALUES ($1, $2)
            ON CONFLICT (guild_id)
            DO UPDATE SET coin_emoji = EXCLUDED.coin_emoji
            """,
            guild_id,
            emoji,
        )

    async def list_coin_emojis(self):
        return await self._pool().fetch(
            """
            SELECT guild_id, coin_emoji
            FROM guild_settings
            WHERE coin_emoji IS NOT NULL
            """
        )

    async def set_whitelist_emoji(self, guild_id: int, emoji: str | None) -> None:
        await self._pool().execute(
            """
            INSERT INTO guild_settings (guild_id, whitelist_emoji)
            VALUES ($1, $2)
            ON CONFLICT (guild_id)
            DO UPDATE SET whitelist_emoji = EXCLUDED.whitelist_emoji
            """,
            guild_id,
            emoji,
        )

    async def list_whitelist_emojis(self):
        return await self._pool().fetch(
            """
            SELECT guild_id, whitelist_emoji
            FROM guild_settings
            WHERE whitelist_emoji IS NOT NULL
            """
        )

    async def replace_coin_emoji_in_movements(
        self,
        guild_id: int,
        old_emoji: str,
        new_emoji: str,
    ) -> None:
        if old_emoji == new_emoji:
            return
        await self._pool().execute(
            """
            UPDATE movements
            SET description = REPLACE(description, $2, $3)
            WHERE guild_id = $1 AND POSITION($2 IN description) > 0
            """,
            guild_id,
            old_emoji,
            new_emoji,
        )

    async def record_movement(
        self,
        guild_id: int,
        user_id: int | None,
        actor_id: int | None,
        action: str,
        amount: int | None,
        description: str,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO movements (
                guild_id, user_id, actor_id, action, amount, description
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            guild_id,
            user_id,
            actor_id,
            action,
            amount,
            description,
        )

    async def record_movements(
        self,
        guild_id: int,
        user_ids: list[int],
        actor_id: int | None,
        action: str,
        amount: int | None,
        description: str,
    ) -> None:
        if not user_ids:
            return
        await self._pool().executemany(
            """
            INSERT INTO movements (
                guild_id, user_id, actor_id, action, amount, description
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            [
                (guild_id, user_id, actor_id, action, amount, description)
                for user_id in user_ids
            ],
        )

    async def get_history(self, guild_id: int, user_id: int, limit: int = 10):
        return await self._pool().fetch(
            """
            SELECT action, amount, description, actor_id, created_at
            FROM movements
            WHERE guild_id = $1 AND user_id = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            guild_id,
            user_id,
            limit,
        )

    async def get_ranking(self, guild_id: int, limit: int = 10):
        return await self._pool().fetch(
            """
            SELECT user_id, balance
            FROM balances
            WHERE guild_id = $1 AND balance > 0
            ORDER BY balance DESC, user_id
            LIMIT $2
            """,
            guild_id,
            limit,
        )

    async def get_statistics(self, guild_id: int) -> dict[str, int]:
        row = await self._pool().fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM balances WHERE guild_id = $1) AS users,
                (SELECT COALESCE(SUM(balance), 0) FROM balances WHERE guild_id = $1) AS total_money,
                (SELECT COUNT(*) FROM badges WHERE guild_id = $1) AS badges,
                (SELECT COUNT(*) FROM modifiers WHERE guild_id = $1) AS modifiers,
                (SELECT COUNT(*) FROM tickets WHERE guild_id = $1) AS tickets,
                (SELECT COUNT(*) FROM ticket_admins WHERE guild_id = $1) AS ticket_admins,
                (SELECT COUNT(*) FROM whitelist_entries WHERE guild_id = $1) AS whitelist_entries,
                (SELECT COUNT(*) FROM movements WHERE guild_id = $1) AS movements,
                (SELECT COUNT(*) FROM movements WHERE guild_id = $1 AND action IN ('purchase', 'modifier_purchase', 'ticket_purchase')) AS purchases,
                (
                    SELECT COUNT(*)
                    FROM movements
                    WHERE guild_id = $1
                      AND action IN ('event_reward', 'event_object_reward')
                ) AS event_wins,
                (SELECT COUNT(*) FROM active_question_events WHERE guild_id = $1) AS active_events,
                (
                    (SELECT COUNT(*) FROM active_modifiers WHERE guild_id = $1)
                    +
                    (SELECT COUNT(*) FROM active_channel_modifiers WHERE guild_id = $1)
                ) AS active_modifiers
            """,
            guild_id,
        )
        return {key: int(row[key]) for key in row.keys()}

    async def export_guild_data(self, guild_id: int) -> dict:
        balances = await self._pool().fetch(
            "SELECT user_id, balance FROM balances WHERE guild_id = $1 ORDER BY user_id",
            guild_id,
        )
        badges = await self._pool().fetch(
            """
            SELECT id, name, name_key, badge_role_id, color_role_id,
                   purchasable, price, shop_section, emoji, whitelist_enabled
            FROM badges WHERE guild_id = $1 ORDER BY name
            """,
            guild_id,
        )
        modifiers = await self._pool().fetch(
            """
            SELECT id, name, name_key, purchasable, price,
                   shop_section, emoji, messages, trigger_numerator,
                   trigger_denominator,
                   cooldown_seconds, duration_minutes, effect_scope
            FROM modifiers WHERE guild_id = $1 ORDER BY name
            """,
            guild_id,
        )
        tickets = await self._pool().fetch(
            """
            SELECT id, name, name_key, purchasable, price,
                   shop_section, emoji, description, active
            FROM tickets WHERE guild_id = $1 ORDER BY name
            """,
            guild_id,
        )
        shop_categories = await self.list_shop_categories(guild_id)
        modifier_inventory = await self._pool().fetch(
            """
            SELECT user_id, modifier_id, quantity
            FROM modifier_inventory WHERE guild_id = $1
            ORDER BY user_id, modifier_id
            """,
            guild_id,
        )
        ticket_inventory = await self._pool().fetch(
            """
            SELECT user_id, ticket_id, quantity
            FROM ticket_inventory WHERE guild_id = $1
            ORDER BY user_id, ticket_id
            """,
            guild_id,
        )
        active_modifiers = await self._pool().fetch(
            """
            SELECT user_id, owner_user_id, modifier_id, channel_id, expires_at,
                   last_trigger_at, duration_minutes
            FROM active_modifiers WHERE guild_id = $1
            ORDER BY user_id
            """,
            guild_id,
        )
        active_channel_modifiers = await self._pool().fetch(
            """
            SELECT channel_id, owner_user_id, modifier_id, expires_at,
                   last_trigger_at, duration_minutes
            FROM active_channel_modifiers WHERE guild_id = $1
            ORDER BY channel_id
            """,
            guild_id,
        )
        object_section_settings = await self._pool().fetch(
            """
            SELECT section, enabled, disabled_reason, admins_bypass
            FROM object_section_settings
            WHERE guild_id = $1
            ORDER BY section
            """,
            guild_id,
        )
        settings = await self.get_log_settings(guild_id)
        ticket_admins = await self.list_ticket_admins(guild_id)
        whitelist_entries = await self.list_whitelist_entries(guild_id)
        movements = await self._pool().fetch(
            """
            SELECT user_id, actor_id, action, amount, description, created_at
            FROM movements WHERE guild_id = $1 ORDER BY created_at
            """,
            guild_id,
        )
        active_events = await self._pool().fetch(
            """
            SELECT channel_id, message_id, question, answer_text, reward,
                   reward_object_count, expires_at, created_by, created_at
            FROM active_question_events
            WHERE guild_id = $1
            ORDER BY created_at
            """,
            guild_id,
        )
        active_event_rewards = await self._pool().fetch(
            """
            SELECT channel_id, position, item_type, item_id, quantity, name, emoji
            FROM active_question_event_rewards
            WHERE guild_id = $1
            ORDER BY channel_id, position
            """,
            guild_id,
        )
        return {
            "guild_id": guild_id,
            "balances": [dict(row) for row in balances],
            "badges": [dict(row) for row in badges],
            "modifiers": [dict(row) for row in modifiers],
            "tickets": [dict(row) for row in tickets],
            "shop_categories": [dict(row) for row in shop_categories],
            "modifier_inventory": [dict(row) for row in modifier_inventory],
            "ticket_inventory": [dict(row) for row in ticket_inventory],
            "active_modifiers": [dict(row) for row in active_modifiers],
            "active_channel_modifiers": [
                dict(row) for row in active_channel_modifiers
            ],
            "object_section_settings": [
                dict(row) for row in object_section_settings
            ],
            "settings": dict(settings) if settings else None,
            "ticket_admins": [dict(row) for row in ticket_admins],
            "whitelist_entries": [dict(row) for row in whitelist_entries],
            "movements": [dict(row) for row in movements],
            "active_question_events": [dict(row) for row in active_events],
            "active_question_event_rewards": [
                dict(row) for row in active_event_rewards
            ],
        }

    async def create_question_event(
        self,
        guild_id: int,
        channel_id: int,
        question: str,
        answer_hash: str,
        answer_text: str,
        reward: int,
        reward_objects: list[dict],
        duration_minutes: int,
        created_by: int,
    ):
        if len(reward_objects) > 3:
            raise ValueError("Un evento admite un máximo de tres objetos.")
        if reward <= 0 and not reward_objects:
            raise ValueError("El evento debe tener al menos una recompensa.")

        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    DELETE FROM active_question_events
                    WHERE guild_id = $1 AND channel_id = $2 AND expires_at <= NOW()
                    """,
                    guild_id,
                    channel_id,
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO active_question_events (
                        guild_id, channel_id, question, answer_hash, answer_text,
                        reward, reward_object_count, expires_at, created_by
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7,
                        NOW() + ($8::INTEGER * INTERVAL '1 minute'), $9
                    )
                    ON CONFLICT (guild_id, channel_id) DO NOTHING
                    RETURNING expires_at
                    """,
                    guild_id,
                    channel_id,
                    question,
                    answer_hash,
                    answer_text,
                    reward,
                    len(reward_objects),
                    duration_minutes,
                    created_by,
                )
                if row is None:
                    return None
                if reward_objects:
                    await connection.executemany(
                        """
                        INSERT INTO active_question_event_rewards (
                            guild_id, channel_id, position, item_type, item_id,
                            quantity, name, emoji
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        [
                            (
                                guild_id,
                                channel_id,
                                position,
                                reward_object["item_type"],
                                reward_object["item_id"],
                                reward_object["quantity"],
                                reward_object["name"],
                                reward_object.get("emoji"),
                            )
                            for position, reward_object in enumerate(
                                reward_objects,
                                start=1,
                            )
                        ],
                    )
                return row["expires_at"]

    async def set_question_event_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE active_question_events
            SET message_id = $3
            WHERE guild_id = $1 AND channel_id = $2
            """,
            guild_id,
            channel_id,
            message_id,
        )
        return result == "UPDATE 1"

    async def claim_question_event(
        self,
        guild_id: int,
        channel_id: int,
        answer_hash: str,
        message_id: int,
        winner_id: int,
        expected_badge_role_ids: dict[int, int] | None = None,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                event = await connection.fetchrow(
                    """
                    SELECT guild_id, channel_id, question, answer_text, reward,
                           created_by, message_id
                    FROM active_question_events
                    WHERE guild_id = $1
                      AND channel_id = $2
                      AND answer_hash = $3
                      AND message_id = $4
                      AND expires_at > NOW()
                    FOR UPDATE
                    """,
                    guild_id,
                    channel_id,
                    answer_hash,
                    message_id,
                )
                if event is None:
                    return None

                reward_rows = await connection.fetch(
                    """
                    SELECT position, item_type, item_id, quantity, name, emoji
                    FROM active_question_event_rewards
                    WHERE guild_id = $1 AND channel_id = $2
                    ORDER BY position
                    """,
                    guild_id,
                    channel_id,
                )
                reward_objects = [dict(row) for row in reward_rows]
                configured_by_key: dict[tuple[str, int], dict] = {}
                if reward_objects:
                    configured_rows = await connection.fetch(
                        """
                        SELECT configured.item_type, configured.id,
                               configured.badge_role_id
                        FROM (
                            SELECT 'badge'::TEXT AS item_type, id, badge_role_id
                            FROM badges WHERE guild_id = $1
                            UNION ALL
                            SELECT 'modifier'::TEXT AS item_type, id,
                                   NULL::BIGINT AS badge_role_id
                            FROM modifiers WHERE guild_id = $1
                            UNION ALL
                            SELECT 'ticket'::TEXT AS item_type, id,
                                   NULL::BIGINT AS badge_role_id
                            FROM tickets WHERE guild_id = $1
                        ) AS configured
                        JOIN UNNEST($2::TEXT[], $3::BIGINT[])
                             AS wanted(item_type, item_id)
                          ON wanted.item_type = configured.item_type
                         AND wanted.item_id = configured.id
                        """,
                        guild_id,
                        [row["item_type"] for row in reward_objects],
                        [row["item_id"] for row in reward_objects],
                    )
                    configured_by_key = {
                        (row["item_type"], row["id"]): dict(row)
                        for row in configured_rows
                    }
                    for reward_object in reward_objects:
                        key = (
                            reward_object["item_type"],
                            reward_object["item_id"],
                        )
                        configured = configured_by_key.get(key)
                        if configured is None:
                            return {
                                "status": "reward_unavailable",
                                **dict(event),
                                "reward_objects": reward_objects,
                            }
                        if reward_object["item_type"] == "badge":
                            expected_role_id = (expected_badge_role_ids or {}).get(
                                reward_object["item_id"]
                            )
                            if (
                                expected_role_id is None
                                or configured["badge_role_id"] != expected_role_id
                            ):
                                return {
                                    "status": "reward_unavailable",
                                    **dict(event),
                                    "reward_objects": reward_objects,
                                }

                new_balance = None
                if event["reward"] > 0:
                    new_balance = await connection.fetchval(
                        """
                        INSERT INTO balances (guild_id, user_id, balance)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET balance = balances.balance + EXCLUDED.balance
                        RETURNING balance
                        """,
                        guild_id,
                        winner_id,
                        event["reward"],
                    )

                new_quantities = []
                for reward_object in reward_objects:
                    item_type = reward_object["item_type"]
                    if item_type == "badge":
                        continue
                    inventory_table = (
                        "modifier_inventory"
                        if item_type == "modifier"
                        else "ticket_inventory"
                    )
                    item_column = (
                        "modifier_id" if item_type == "modifier" else "ticket_id"
                    )
                    new_quantity = await connection.fetchval(
                        f"""
                        INSERT INTO {inventory_table} (
                            guild_id, user_id, {item_column}, quantity
                        )
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (guild_id, user_id, {item_column})
                        DO UPDATE SET quantity = (
                            {inventory_table}.quantity + EXCLUDED.quantity
                        )
                        RETURNING quantity
                        """,
                        guild_id,
                        winner_id,
                        reward_object["item_id"],
                        reward_object["quantity"],
                    )
                    new_quantities.append(
                        {
                            "item_type": item_type,
                            "item_id": reward_object["item_id"],
                            "new_quantity": new_quantity,
                        }
                    )

                await connection.execute(
                    """
                    DELETE FROM active_question_events
                    WHERE guild_id = $1 AND channel_id = $2
                    """,
                    guild_id,
                    channel_id,
                )

                description_parts = []
                if event["reward"] > 0:
                    formatted_reward = f"{event['reward']:,}".replace(",", ".")
                    coin_emoji = await connection.fetchval(
                        "SELECT coin_emoji FROM guild_settings WHERE guild_id = $1",
                        guild_id,
                    ) or "🪙"
                    description_parts.append(f"{coin_emoji} {formatted_reward}")
                description_parts.extend(
                    (
                        f"{reward_object['quantity']} unidad(es) de "
                        f"{reward_object['name']}"
                    )
                    for reward_object in reward_objects
                )
                await connection.execute(
                    """
                    INSERT INTO movements (
                        guild_id, user_id, actor_id, action, amount, description
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    guild_id,
                    winner_id,
                    event["created_by"],
                    "event_object_reward" if reward_objects else "event_reward",
                    (
                        event["reward"]
                        if event["reward"] > 0
                        else sum(row["quantity"] for row in reward_objects)
                    ),
                    (
                        "Ganó un evento de pregunta y recibió "
                        f"{', '.join(description_parts)}: {event['question']}"
                    ),
                )
                return {
                    "status": "claimed",
                    **dict(event),
                    "reward_objects": reward_objects,
                    "new_balance": new_balance,
                    "new_quantities": new_quantities,
                }

    async def _claim_question_event_legacy(
        self,
        guild_id: int,
        channel_id: int,
        answer_hash: str,
        message_id: int,
        winner_id: int,
        expected_badge_role_id: int | None = None,
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                event = await connection.fetchrow(
                    """
                    SELECT question, reward, reward_type, reward_object_id,
                           reward_quantity, reward_name, reward_emoji,
                           created_by, message_id
                    FROM active_question_events
                    WHERE guild_id = $1
                      AND channel_id = $2
                      AND answer_hash = $3
                      AND message_id = $4
                      AND expires_at > NOW()
                    FOR UPDATE
                    """,
                    guild_id,
                    channel_id,
                    answer_hash,
                    message_id,
                )
                if event is None:
                    return None
                reward_type = event["reward_type"]
                new_balance = None
                new_quantity = None
                badge_role_id = None
                if reward_type == "coins":
                    new_balance = await connection.fetchval(
                        """
                        INSERT INTO balances (guild_id, user_id, balance)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET balance = balances.balance + EXCLUDED.balance
                        RETURNING balance
                        """,
                        guild_id,
                        winner_id,
                        event["reward"],
                    )
                    formatted_reward = f"{event['reward']:,}".replace(",", ".")
                    coin_emoji = await connection.fetchval(
                        "SELECT coin_emoji FROM guild_settings WHERE guild_id = $1",
                        guild_id,
                    ) or "🪙"
                    description = (
                        f"Ganó un evento de pregunta y recibió "
                        f"{coin_emoji} {formatted_reward}: {event['question']}"
                    )
                    movement_action = "event_reward"
                    movement_amount = event["reward"]
                elif reward_type == "modifier":
                    modifier = await connection.fetchrow(
                        """
                        SELECT id, name
                        FROM modifiers
                        WHERE guild_id = $1 AND id = $2
                        FOR SHARE
                        """,
                        guild_id,
                        event["reward_object_id"],
                    )
                    if modifier is None:
                        return {"status": "reward_unavailable", **dict(event)}
                    new_quantity = await connection.fetchval(
                        """
                        INSERT INTO modifier_inventory (
                            guild_id, user_id, modifier_id, quantity
                        )
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (guild_id, user_id, modifier_id)
                        DO UPDATE SET quantity = (
                            modifier_inventory.quantity + EXCLUDED.quantity
                        )
                        RETURNING quantity
                        """,
                        guild_id,
                        winner_id,
                        modifier["id"],
                        event["reward_quantity"],
                    )
                    description = (
                        f"Ganó {event['reward_quantity']} unidad(es) de "
                        f"{event['reward_name']} en un evento: {event['question']}"
                    )
                    movement_action = "event_object_reward"
                    movement_amount = event["reward_quantity"]
                elif reward_type == "ticket":
                    ticket = await connection.fetchrow(
                        """
                        SELECT id, name
                        FROM tickets
                        WHERE guild_id = $1 AND id = $2
                        FOR SHARE
                        """,
                        guild_id,
                        event["reward_object_id"],
                    )
                    if ticket is None:
                        return {"status": "reward_unavailable", **dict(event)}
                    new_quantity = await connection.fetchval(
                        """
                        INSERT INTO ticket_inventory (
                            guild_id, user_id, ticket_id, quantity
                        )
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (guild_id, user_id, ticket_id)
                        DO UPDATE SET quantity = (
                            ticket_inventory.quantity + EXCLUDED.quantity
                        )
                        RETURNING quantity
                        """,
                        guild_id,
                        winner_id,
                        ticket["id"],
                        event["reward_quantity"],
                    )
                    description = (
                        f"Ganó {event['reward_quantity']} unidad(es) de "
                        f"{event['reward_name']} en un evento: {event['question']}"
                    )
                    movement_action = "event_object_reward"
                    movement_amount = event["reward_quantity"]
                elif reward_type == "badge":
                    badge = await connection.fetchrow(
                        """
                        SELECT id, name, badge_role_id
                        FROM badges
                        WHERE guild_id = $1 AND id = $2
                        FOR SHARE
                        """,
                        guild_id,
                        event["reward_object_id"],
                    )
                    if (
                        badge is None
                        or expected_badge_role_id is None
                        or badge["badge_role_id"] != expected_badge_role_id
                    ):
                        return {"status": "reward_unavailable", **dict(event)}
                    badge_role_id = badge["badge_role_id"]
                    description = (
                        f"Ganó la insignia {event['reward_name']} en un evento: "
                        f"{event['question']}"
                    )
                    movement_action = "event_object_reward"
                    movement_amount = 1
                else:
                    return {"status": "reward_unavailable", **dict(event)}

                await connection.execute(
                    """
                    DELETE FROM active_question_events
                    WHERE guild_id = $1 AND channel_id = $2
                    """,
                    guild_id,
                    channel_id,
                )
                await connection.execute(
                    """
                    INSERT INTO movements (
                        guild_id, user_id, actor_id, action, amount, description
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    guild_id,
                    winner_id,
                    event["created_by"],
                    movement_action,
                    movement_amount,
                    description,
                )
                return {
                    "status": "claimed",
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "question": event["question"],
                    "reward": event["reward"],
                    "reward_type": reward_type,
                    "reward_object_id": event["reward_object_id"],
                    "reward_quantity": event["reward_quantity"],
                    "reward_name": event["reward_name"],
                    "reward_emoji": event["reward_emoji"],
                    "created_by": event["created_by"],
                    "message_id": event["message_id"],
                    "new_balance": new_balance,
                    "new_quantity": new_quantity,
                    "badge_role_id": badge_role_id,
                }

    async def cancel_question_event(self, guild_id: int, channel_id: int):
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                event = await connection.fetchrow(
                    """
                    SELECT guild_id, channel_id, message_id, question, answer_text,
                           reward, expires_at, created_by
                    FROM active_question_events
                    WHERE guild_id = $1 AND channel_id = $2
                    FOR UPDATE
                    """,
                    guild_id,
                    channel_id,
                )
                if event is None:
                    return None
                reward_rows = await connection.fetch(
                    """
                    SELECT guild_id, channel_id, position, item_type, item_id,
                           quantity, name, emoji
                    FROM active_question_event_rewards
                    WHERE guild_id = $1 AND channel_id = $2
                    ORDER BY position
                    """,
                    guild_id,
                    channel_id,
                )
                await connection.execute(
                    """
                    DELETE FROM active_question_events
                    WHERE guild_id = $1 AND channel_id = $2
                    """,
                    guild_id,
                    channel_id,
                )
                return _question_events_with_rewards([event], reward_rows)[0]

    async def pop_expired_question_events(self):
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                events = await connection.fetch(
                    """
                    SELECT guild_id, channel_id, message_id, question, answer_text,
                           reward, expires_at, created_by
                    FROM active_question_events
                    WHERE expires_at <= NOW()
                    FOR UPDATE SKIP LOCKED
                    """
                )
                if not events:
                    return []
                guild_ids = [row["guild_id"] for row in events]
                channel_ids = [row["channel_id"] for row in events]
                reward_rows = await connection.fetch(
                    """
                    SELECT reward.guild_id, reward.channel_id, reward.position,
                           reward.item_type, reward.item_id, reward.quantity,
                           reward.name, reward.emoji
                    FROM active_question_event_rewards AS reward
                    JOIN UNNEST($1::BIGINT[], $2::BIGINT[])
                         AS selected(guild_id, channel_id)
                      ON selected.guild_id = reward.guild_id
                     AND selected.channel_id = reward.channel_id
                    ORDER BY reward.guild_id, reward.channel_id, reward.position
                    """,
                    guild_ids,
                    channel_ids,
                )
                await connection.execute(
                    """
                    DELETE FROM active_question_events AS event
                    USING UNNEST($1::BIGINT[], $2::BIGINT[])
                          AS selected(guild_id, channel_id)
                    WHERE event.guild_id = selected.guild_id
                      AND event.channel_id = selected.channel_id
                    """,
                    guild_ids,
                    channel_ids,
                )
                return _question_events_with_rewards(events, reward_rows)

    async def list_active_question_events(self):
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                events = await connection.fetch(
                    """
                    SELECT guild_id, channel_id, message_id, question, answer_hash,
                           answer_text, reward, expires_at, created_by
                    FROM active_question_events
                    WHERE expires_at > NOW() AND message_id IS NOT NULL
                    """
                )
                reward_rows = await connection.fetch(
                    """
                    SELECT reward.guild_id, reward.channel_id, reward.position,
                           reward.item_type, reward.item_id, reward.quantity,
                           reward.name, reward.emoji
                    FROM active_question_event_rewards AS reward
                    JOIN active_question_events AS event
                      ON event.guild_id = reward.guild_id
                     AND event.channel_id = reward.channel_id
                    WHERE event.expires_at > NOW()
                      AND event.message_id IS NOT NULL
                    ORDER BY reward.guild_id, reward.channel_id, reward.position
                    """
                )
                return _question_events_with_rewards(events, reward_rows)
