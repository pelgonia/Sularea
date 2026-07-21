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
    PRIMARY KEY (guild_id, name_key),
    UNIQUE (guild_id, badge_role_id)
);

CREATE INDEX IF NOT EXISTS badges_shop_index
ON badges (guild_id, purchasable, price);
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
    ) -> int:
        if not user_ids:
            return 0
        result = await self._pool().execute(
            """
            UPDATE balances
            SET balance = balance - $3
            WHERE guild_id = $1
              AND user_id = ANY($2::BIGINT[])
              AND balance >= $3
            """,
            guild_id,
            user_ids,
            amount,
        )
        return int(result.rsplit(" ", 1)[-1])

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
                ORDER BY price, name
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
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO badges (
                guild_id, name, name_key, badge_role_id,
                color_role_id, purchasable, price
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            guild_id,
            name,
            name_key,
            badge_role_id,
            color_role_id,
            purchasable,
            price,
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
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE badges
            SET name = $3,
                name_key = $4,
                badge_role_id = $5,
                color_role_id = $6,
                purchasable = $7,
                price = $8
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
