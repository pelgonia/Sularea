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
    section TEXT NOT NULL CHECK (section IN ('badges', 'modifiers', 'tickets')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    disabled_reason TEXT,
    PRIMARY KEY (guild_id, section)
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
                NULL::INTEGER AS duration_minutes
            FROM badges
            WHERE guild_id = $1 AND purchasable = TRUE
            UNION ALL
            SELECT
                'modifier' AS item_type, name, name_key, price, shop_section,
                emoji, NULL::BIGINT AS badge_role_id,
                NULL::BIGINT AS color_role_id, id AS modifier_id,
                NULL::BIGINT AS ticket_id, NULL::TEXT AS description,
                NULL::BOOLEAN AS active, trigger_numerator, trigger_denominator,
                cooldown_seconds, duration_minutes
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
                NULL::INTEGER AS duration_minutes
            FROM tickets
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
        trigger_numerator: int,
        trigger_denominator: int,
        cooldown_seconds: int,
        duration_minutes: int,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO modifiers (
                guild_id, name, name_key, purchasable, price,
                shop_section, emoji, messages, trigger_numerator, trigger_denominator,
                cooldown_seconds, duration_minutes
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::TEXT[], $9, $10, $11, $12)
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
                duration_minutes = $13
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
    ) -> dict | None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason
                    )
                    VALUES ($1, 'tickets', TRUE, NULL)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                )
                section = await connection.fetchrow(
                    """
                    SELECT enabled, disabled_reason
                    FROM object_section_settings
                    WHERE guild_id = $1 AND section = 'tickets'
                    FOR SHARE
                    """,
                    guild_id,
                )
                if not section["enabled"]:
                    return {
                        "status": "disabled",
                        "reason": section["disabled_reason"],
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
    ) -> dict:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason
                    )
                    VALUES ($1, 'modifiers', TRUE, NULL)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                )
                section = await connection.fetchrow(
                    """
                    SELECT enabled, disabled_reason
                    FROM object_section_settings
                    WHERE guild_id = $1 AND section = 'modifiers'
                    FOR SHARE
                    """,
                    guild_id,
                )
                if section is not None and not section["enabled"]:
                    return {
                        "status": "disabled",
                        "reason": section["disabled_reason"],
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
                    SELECT i.modifier_id, i.quantity, m.name, m.duration_minutes
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
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason
                    )
                    VALUES ($1, 'modifiers', TRUE, NULL)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                )
                section = await connection.fetchrow(
                    """
                    SELECT enabled, disabled_reason
                    FROM object_section_settings
                    WHERE guild_id = $1 AND section = 'modifiers'
                    FOR SHARE
                    """,
                    guild_id,
                )
                if not section["enabled"]:
                    return {
                        "status": "disabled",
                        "reason": section["disabled_reason"],
                    }
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
              AND NOT EXISTS (
                  SELECT 1
                  FROM object_section_settings AS section
                  WHERE section.guild_id = active.guild_id
                    AND section.section = 'modifiers'
                    AND section.enabled = FALSE
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
            SELECT enabled, disabled_reason
            FROM object_section_settings
            WHERE guild_id = $1 AND section = $2
            """,
            guild_id,
            section,
        )
        if row is None:
            return {"enabled": True, "disabled_reason": None}
        return dict(row)

    async def toggle_object_section(
        self,
        guild_id: int,
        section: str,
        disabled_reason: str,
    ) -> dict:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO object_section_settings (
                        guild_id, section, enabled, disabled_reason
                    )
                    VALUES ($1, $2, TRUE, NULL)
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
                        disabled_reason = CASE WHEN $3 THEN NULL ELSE $4 END
                    WHERE guild_id = $1 AND section = $2
                    """,
                    guild_id,
                    section,
                    enabled,
                    disabled_reason,
                )

                removed = []
                refunds = []
                if section == "modifiers" and not enabled:
                    removed = await connection.fetch(
                        """
                        WITH removed AS (
                            DELETE FROM active_modifiers
                            WHERE guild_id = $1
                            RETURNING user_id, owner_user_id, modifier_id, expires_at
                        )
                        SELECT removed.user_id AS target_user_id,
                               removed.owner_user_id,
                               removed.modifier_id,
                               modifier.name,
                               (
                                   removed.owner_user_id IS NOT NULL
                                   AND removed.expires_at > NOW()
                               ) AS refundable
                        FROM removed
                        JOIN modifiers AS modifier ON modifier.id = removed.modifier_id
                        """,
                        guild_id,
                    )
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
                   cooldown_seconds, duration_minutes
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
        object_section_settings = await self._pool().fetch(
            """
            SELECT section, enabled, disabled_reason
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
            "tickets": [dict(row) for row in tickets],
            "modifier_inventory": [dict(row) for row in modifier_inventory],
            "ticket_inventory": [dict(row) for row in ticket_inventory],
            "active_modifiers": [dict(row) for row in active_modifiers],
            "object_section_settings": [
                dict(row) for row in object_section_settings
            ],
            "settings": dict(settings) if settings else None,
            "ticket_admins": [dict(row) for row in ticket_admins],
            "whitelist_entries": [dict(row) for row in whitelist_entries],
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
