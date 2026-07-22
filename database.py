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
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    badge_role_id BIGINT NOT NULL,
    color_role_id BIGINT NOT NULL,
    purchasable BOOLEAN NOT NULL DEFAULT FALSE,
    price BIGINT NOT NULL DEFAULT 0 CHECK (price >= 0),
    shop_section TEXT,
    emoji TEXT,
    PRIMARY KEY (guild_id, name_key),
    UNIQUE (guild_id, badge_role_id)
);

ALTER TABLE badges
ADD COLUMN IF NOT EXISTS shop_section TEXT;

ALTER TABLE badges
ADD COLUMN IF NOT EXISTS emoji TEXT;

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
    UNIQUE (guild_id, name_key)
);

CREATE INDEX IF NOT EXISTS modifiers_shop_index
ON modifiers (guild_id, purchasable, price);

CREATE TABLE IF NOT EXISTS modifier_inventory (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    modifier_id BIGINT NOT NULL REFERENCES modifiers(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (guild_id, user_id, modifier_id)
);

CREATE TABLE IF NOT EXISTS active_modifiers (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    modifier_id BIGINT NOT NULL REFERENCES modifiers(id) ON DELETE CASCADE,
    channel_id BIGINT,
    expires_at TIMESTAMPTZ NOT NULL,
    last_trigger_at TIMESTAMPTZ,
    PRIMARY KEY (guild_id, user_id)
);

ALTER TABLE active_modifiers
ADD COLUMN IF NOT EXISTS channel_id BIGINT;

CREATE INDEX IF NOT EXISTS active_modifiers_expiration_index
ON active_modifiers (expires_at);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    log_channel_id BIGINT,
    logs_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    coin_emoji TEXT
);

ALTER TABLE guild_settings
ADD COLUMN IF NOT EXISTS coin_emoji TEXT;

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
    reward BIGINT NOT NULL CHECK (reward > 0),
    expires_at TIMESTAMPTZ NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, channel_id)
);

ALTER TABLE active_question_events
ADD COLUMN IF NOT EXISTS message_id BIGINT;

CREATE INDEX IF NOT EXISTS active_question_events_expiration_index
ON active_question_events (expires_at);
"""


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
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO badges (
                guild_id, name, name_key, badge_role_id,
                color_role_id, purchasable, price, shop_section, emoji
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
                emoji = $10
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
                emoji, badge_role_id, color_role_id, NULL::BIGINT AS modifier_id
            FROM badges
            WHERE guild_id = $1 AND purchasable = TRUE
            UNION ALL
            SELECT
                'modifier' AS item_type, name, name_key, price, shop_section,
                emoji, NULL::BIGINT AS badge_role_id,
                NULL::BIGINT AS color_role_id, id AS modifier_id
            FROM modifiers
            WHERE guild_id = $1 AND purchasable = TRUE
            ORDER BY shop_section NULLS FIRST, price, name
            """,
            guild_id,
        )

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
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO modifiers (
                guild_id, name, name_key, purchasable, price,
                shop_section, emoji, messages
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::TEXT[])
            """,
            guild_id,
            name,
            name_key,
            purchasable,
            price,
            shop_section,
            emoji,
            messages,
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
                messages = $9::TEXT[]
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
        user_id: int,
        name_key: str,
        channel_id: int,
        duration_minutes: int = 5,
    ) -> dict:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                active = await connection.fetchrow(
                    """
                    SELECT m.name, a.expires_at
                    FROM active_modifiers a
                    JOIN modifiers m ON m.id = a.modifier_id
                    WHERE a.guild_id = $1 AND a.user_id = $2
                    FOR UPDATE OF a
                    """,
                    guild_id,
                    user_id,
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
                        user_id,
                    )
                inventory = await connection.fetchrow(
                    """
                    SELECT i.modifier_id, i.quantity, m.name
                    FROM modifier_inventory i
                    JOIN modifiers m ON m.id = i.modifier_id
                    WHERE i.guild_id = $1 AND i.user_id = $2
                      AND m.guild_id = $1 AND m.name_key = $3
                    FOR UPDATE OF i
                    """,
                    guild_id,
                    user_id,
                    name_key,
                )
                if inventory is None or inventory["quantity"] <= 0:
                    return {"status": "missing"}
                quantity = await connection.fetchval(
                    """
                    UPDATE modifier_inventory
                    SET quantity = quantity - 1
                    WHERE guild_id = $1 AND user_id = $2 AND modifier_id = $3
                    RETURNING quantity
                    """,
                    guild_id,
                    user_id,
                    inventory["modifier_id"],
                )
                expires_at = await connection.fetchval(
                    """
                    INSERT INTO active_modifiers (
                        guild_id, user_id, modifier_id, channel_id, expires_at
                    )
                    VALUES (
                        $1, $2, $3, $4,
                        NOW() + ($5::INTEGER * INTERVAL '1 minute')
                    )
                    RETURNING expires_at
                    """,
                    guild_id,
                    user_id,
                    inventory["modifier_id"],
                    channel_id,
                    duration_minutes,
                )
                return {
                    "status": "activated",
                    "name": inventory["name"],
                    "quantity": quantity,
                    "expires_at": expires_at,
                }

    async def force_activate_modifier(
        self,
        guild_id: int,
        user_id: int,
        modifier_id: int,
        channel_id: int,
        duration_minutes: int = 5,
    ):
        return await self._pool().fetchrow(
            """
            INSERT INTO active_modifiers (
                guild_id, user_id, modifier_id, channel_id, expires_at,
                last_trigger_at
            )
            VALUES (
                $1, $2, $3, $4,
                NOW() + ($5::INTEGER * INTERVAL '1 minute'), NULL
            )
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
                modifier_id = EXCLUDED.modifier_id,
                channel_id = EXCLUDED.channel_id,
                expires_at = EXCLUDED.expires_at,
                last_trigger_at = NULL
            RETURNING expires_at
            """,
            guild_id,
            user_id,
            modifier_id,
            channel_id,
            duration_minutes,
        )

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

    async def try_trigger_modifier(
        self,
        guild_id: int,
        user_id: int,
        cooldown_seconds: int = 10,
    ):
        return await self._pool().fetchrow(
            """
            UPDATE active_modifiers AS active
            SET last_trigger_at = NOW()
            FROM modifiers AS modifier
            WHERE active.guild_id = $1
              AND active.user_id = $2
              AND modifier.id = active.modifier_id
              AND active.expires_at > NOW()
              AND (
                  active.last_trigger_at IS NULL
                  OR active.last_trigger_at <= NOW() - ($3::INTEGER * INTERVAL '1 second')
              )
              AND RANDOM() < 0.1
            RETURNING modifier.name, modifier.messages
            """,
            guild_id,
            user_id,
            cooldown_seconds,
        )

    async def pop_expired_modifiers(self):
        return await self._pool().fetch(
            """
            WITH expired AS (
                DELETE FROM active_modifiers
                WHERE expires_at <= NOW()
                RETURNING guild_id, user_id, modifier_id, channel_id
            )
            SELECT expired.guild_id, expired.user_id, expired.channel_id, modifier.name
            FROM expired
            JOIN modifiers AS modifier ON modifier.id = expired.modifier_id
            """
        )

    async def get_log_settings(self, guild_id: int):
        return await self._pool().fetchrow(
            """
            SELECT log_channel_id, logs_enabled, coin_emoji
            FROM guild_settings
            WHERE guild_id = $1
            """,
            guild_id,
        )

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
                (SELECT COUNT(*) FROM movements WHERE guild_id = $1) AS movements,
                (SELECT COUNT(*) FROM movements WHERE guild_id = $1 AND action IN ('purchase', 'modifier_purchase')) AS purchases,
                (SELECT COUNT(*) FROM movements WHERE guild_id = $1 AND action = 'event_reward') AS event_wins,
                (SELECT COUNT(*) FROM active_question_events WHERE guild_id = $1) AS active_events,
                (SELECT COUNT(*) FROM active_modifiers WHERE guild_id = $1) AS active_modifiers
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
            SELECT name, name_key, badge_role_id, color_role_id,
                   purchasable, price, shop_section, emoji
            FROM badges WHERE guild_id = $1 ORDER BY name
            """,
            guild_id,
        )
        modifiers = await self._pool().fetch(
            """
            SELECT id, name, name_key, purchasable, price,
                   shop_section, emoji, messages
            FROM modifiers WHERE guild_id = $1 ORDER BY name
            """,
            guild_id,
        )
        modifier_inventory = await self._pool().fetch(
            """
            SELECT user_id, modifier_id, quantity
            FROM modifier_inventory WHERE guild_id = $1
            ORDER BY user_id, modifier_id
            """,
            guild_id,
        )
        active_modifiers = await self._pool().fetch(
            """
            SELECT user_id, modifier_id, channel_id, expires_at, last_trigger_at
            FROM active_modifiers WHERE guild_id = $1
            ORDER BY user_id
            """,
            guild_id,
        )
        settings = await self.get_log_settings(guild_id)
        movements = await self._pool().fetch(
            """
            SELECT user_id, actor_id, action, amount, description, created_at
            FROM movements WHERE guild_id = $1 ORDER BY created_at
            """,
            guild_id,
        )
        active_events = await self._pool().fetch(
            """
            SELECT channel_id, message_id, question, reward, expires_at, created_by, created_at
            FROM active_question_events
            WHERE guild_id = $1
            ORDER BY created_at
            """,
            guild_id,
        )
        return {
            "guild_id": guild_id,
            "balances": [dict(row) for row in balances],
            "badges": [dict(row) for row in badges],
            "modifiers": [dict(row) for row in modifiers],
            "modifier_inventory": [dict(row) for row in modifier_inventory],
            "active_modifiers": [dict(row) for row in active_modifiers],
            "settings": dict(settings) if settings else None,
            "movements": [dict(row) for row in movements],
            "active_question_events": [dict(row) for row in active_events],
        }

    async def create_question_event(
        self,
        guild_id: int,
        channel_id: int,
        question: str,
        answer_hash: str,
        reward: int,
        duration_minutes: int,
        created_by: int,
    ) -> bool:
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
                        guild_id, channel_id, question, answer_hash,
                        reward, expires_at, created_by
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        NOW() + ($6::INTEGER * INTERVAL '1 minute'), $7
                    )
                    ON CONFLICT (guild_id, channel_id) DO NOTHING
                    RETURNING expires_at
                    """,
                    guild_id,
                    channel_id,
                    question,
                    answer_hash,
                    reward,
                    duration_minutes,
                    created_by,
                )
                return row["expires_at"] if row is not None else None

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
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                event = await connection.fetchrow(
                    """
                    DELETE FROM active_question_events
                    WHERE guild_id = $1
                      AND channel_id = $2
                      AND answer_hash = $3
                      AND message_id = $4
                      AND expires_at > NOW()
                    RETURNING question, reward, created_by, message_id
                    """,
                    guild_id,
                    channel_id,
                    answer_hash,
                    message_id,
                )
                if event is None:
                    return None
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
                await connection.execute(
                    """
                    INSERT INTO movements (
                        guild_id, user_id, actor_id, action, amount, description
                    )
                    VALUES ($1, $2, $3, 'event_reward', $4, $5)
                    """,
                    guild_id,
                    winner_id,
                    event["created_by"],
                    event["reward"],
                    description,
                )
                return {
                    "question": event["question"],
                    "reward": event["reward"],
                    "created_by": event["created_by"],
                    "message_id": event["message_id"],
                    "new_balance": new_balance,
                }

    async def cancel_question_event(self, guild_id: int, channel_id: int):
        return await self._pool().fetchrow(
            """
            DELETE FROM active_question_events
            WHERE guild_id = $1 AND channel_id = $2
            RETURNING question, reward, created_by, message_id
            """,
            guild_id,
            channel_id,
        )

    async def pop_expired_question_events(self):
        return await self._pool().fetch(
            """
            DELETE FROM active_question_events
            WHERE expires_at <= NOW()
            RETURNING guild_id, channel_id, message_id, question, reward, created_by
            """
        )
