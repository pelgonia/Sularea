import asyncio
import hashlib
import io
import json
import os
import random
import time
import traceback
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fractions import Fraction

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database


load_dotenv()
MAX_MONEY = 1_000_000_000_000
DEFAULT_SHOP_SECTION = "General"
MAX_SHOP_SECTIONS = 25
MAX_TICKET_ADMINS = 50
MODIFIER_DURATION_MINUTES = 5
MODIFIER_COOLDOWN_SECONDS = 10
MODIFIER_PROBABILITY = "10"
MAX_MODIFIER_DURATION_MINUTES = 1440
MAX_MODIFIER_COOLDOWN_SECONDS = 3600
MAX_MODIFIER_PROBABILITY_PART = 1_000_000
MAX_AUTOMATIC_MESSAGES = 50
MAX_AUTOMATIC_INTERVAL_MINUTES = 10_080
CACHE_TTL_SECONDS = 10
DEFAULT_COIN_EMOJI = "🪙"
DEFAULT_WHITELIST_EMOJI = "⭐"
MODIFIER_SCOPE_LABELS = {
    "individual": "Individual",
    "channel": "Canal",
}
OBJECT_SECTION_LABELS = {
    "badges": "insignias",
    "modifiers": "modificadores",
    "tickets": "tickets",
    "shop": "tienda",
    "all_objects": "todos los objetos y la tienda",
    "all_commands": "todos los comandos del bot",
}
OBJECT_SECTION_DISABLED_MESSAGES = {
    "badges": "Las insignias no se pueden usar temporalmente.",
    "modifiers": "Los modificadores no se pueden usar temporalmente.",
    "tickets": "Los tickets no se pueden usar temporalmente.",
    "shop": "La tienda no está disponible temporalmente.",
    "all_objects": "Los objetos y la tienda no están disponibles temporalmente.",
    "all_commands": "Los comandos del bot están en mantenimiento temporalmente.",
}


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def shop_section_name(value: str | None) -> str:
    cleaned = (value or "").strip()
    return cleaned or DEFAULT_SHOP_SECTION


def resolve_guild_emoji(
    guild: discord.Guild,
    value: str | None,
) -> tuple[str | None, str | None]:
    if not value or not value.strip():
        return None, None
    cleaned = value.strip()
    application_emojis = list(
        getattr(globals().get("bot"), "application_emojis", {}).values()
    )

    if cleaned.isdigit():
        emoji_id = int(cleaned)
        emoji = guild.get_emoji(emoji_id) or next(
            (item for item in application_emojis if item.id == emoji_id),
            None,
        )
        if emoji is None:
            return None, f"No encontré ningún emoji disponible con el ID `{cleaned}`."
        return str(emoji), None

    partial = discord.PartialEmoji.from_str(cleaned)
    if partial.id is not None:
        emoji = guild.get_emoji(partial.id) or next(
            (item for item in application_emojis if item.id == partial.id),
            None,
        )
        if emoji is None:
            return None, "Ese emoji no pertenece al servidor ni a la aplicación del bot."
        return str(emoji), None

    emoji_name: str | None = None
    if cleaned.startswith(":") and cleaned.endswith(":") and len(cleaned) > 2:
        emoji_name = cleaned[1:-1]
    else:
        direct_match = next(
            (
                emoji
                for emoji in [*guild.emojis, *application_emojis]
                if normalize_name(emoji.name) == normalize_name(cleaned)
            ),
            None,
        )
        if direct_match is not None:
            return str(direct_match), None

    if emoji_name is not None:
        named_match = next(
            (
                emoji
                for emoji in [*guild.emojis, *application_emojis]
                if normalize_name(emoji.name) == normalize_name(emoji_name)
            ),
            None,
        )
        if named_match is None:
            return None, (
                f"No encontré el emoji `:{emoji_name}:` en el servidor "
                "ni entre los emojis exclusivos del bot."
            )
        return str(named_match), None

    return cleaned, None


async def resolve_configured_emoji(
    guild: discord.Guild,
    value: str | None,
) -> tuple[str | None, str | None]:
    resolved, error = resolve_guild_emoji(guild, value)
    if error is None:
        return resolved, None
    try:
        application_emojis = await bot.fetch_application_emojis()
        bot.application_emojis = {emoji.id: emoji for emoji in application_emojis}
    except (discord.HTTPException, discord.MissingApplicationID):
        return resolved, error
    return resolve_guild_emoji(guild, value)


def badge_emoji(value: str | None, guild: discord.Guild) -> str:
    resolved, _ = resolve_guild_emoji(guild, value)
    shown = resolved or (value.strip() if value and value.strip() else "")
    return f"{shown} " if shown else ""


def edited_badge_emoji(value: str | None, current: str | None = None) -> str | None:
    if value is None:
        return current
    cleaned = value.strip()
    if normalize_name(cleaned) in {"quitar", "ninguno", "sin emoji"}:
        return None
    return cleaned or None


def parse_modifier_messages(value: str) -> tuple[list[str] | None, str | None]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("|", "\n")
    messages = [part.strip() for part in normalized.split("\n") if part.strip()]
    if not messages:
        return None, "Debes configurar al menos un mensaje para el modificador."
    if len(messages) > 20:
        return None, "Puedes configurar un máximo de 20 mensajes por modificador."
    if any(len(message) > 1900 for message in messages):
        return None, "Cada mensaje del modificador puede tener hasta 1.900 caracteres."
    return messages, None


def parse_modifier_probability(
    value: str,
) -> tuple[tuple[int, int] | None, str | None]:
    cleaned = value.strip().replace(" ", "").replace(",", ".")
    if not cleaned:
        return None, "Debes indicar una probabilidad, por ejemplo `25` o `1/4`."
    is_percentage = "/" not in cleaned
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
        is_percentage = True
    try:
        probability = Fraction(cleaned)
    except (ValueError, ZeroDivisionError):
        return None, "La probabilidad debe escribirse como porcentaje (`25`) o fracción (`1/4`)."
    if is_percentage:
        probability /= 100
    if probability < 0 or probability > 1:
        return None, "La probabilidad debe estar entre 0% y 100%."
    if (
        probability.numerator > MAX_MODIFIER_PROBABILITY_PART
        or probability.denominator > MAX_MODIFIER_PROBABILITY_PART
    ):
        return None, "La fracción es demasiado grande; usa números de hasta 1.000.000."
    return (probability.numerator, probability.denominator), None


def modifier_probability_label(numerator: int, denominator: int) -> str:
    percentage = numerator / denominator * 100
    shown_percentage = f"{percentage:.4f}".rstrip("0").rstrip(".")
    return f"{numerator}/{denominator} ({shown_percentage}%)"


def modifier_scope_label(scope: str) -> str:
    return MODIFIER_SCOPE_LABELS.get(scope, "Individual")


def answer_hash(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    normalized = " ".join(without_accents.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def question_reward_text(
    guild: discord.Guild,
    reward: int,
    reward_objects: list[dict],
) -> str:
    rewards = []
    if reward > 0:
        rewards.append(money(reward, guild.id))
    for reward_object in reward_objects:
        item_type = reward_object["item_type"]
        shown_name = reward_object.get("name") or "Objeto no disponible"
        emoji = badge_emoji(reward_object.get("emoji"), guild)
        type_label = {
            "badge": "insignia",
            "modifier": "modificador",
            "ticket": "ticket",
        }.get(item_type, "objeto")
        quantity = (
            ""
            if item_type == "badge"
            else f" × **{reward_object['quantity']}**"
        )
        rewards.append(f"{emoji}**{shown_name}**{quantity} ({type_label})")
    return "\n".join(rewards)


def question_event_reward_text(
    guild: discord.Guild,
    event,
) -> str:
    return question_reward_text(
        guild,
        event["reward"],
        event["reward_objects"],
    )


def question_event_movement_amount(event) -> int:
    if event["reward"] > 0:
        return event["reward"]
    return sum(
        reward_object["quantity"]
        for reward_object in event["reward_objects"]
    )


def money(value: int, guild_id: int | None = None) -> str:
    formatted = f"{value:,}".replace(",", ".")
    emoji = DEFAULT_COIN_EMOJI
    if guild_id is not None:
        emoji = getattr(
            globals().get("bot"),
            "coin_emojis",
            {},
        ).get(guild_id, DEFAULT_COIN_EMOJI)
    return f"{emoji} {formatted}"


def discounted_price(price: int, discount_percent: int) -> int:
    discount_percent = max(0, min(100, discount_percent))
    return price * (100 - discount_percent) // 100


def shop_price_text(
    price: int,
    discount_percent: int,
    guild_id: int,
) -> str:
    effective_price = discounted_price(price, discount_percent)
    if discount_percent <= 0 or effective_price == price:
        return money(price, guild_id)
    return (
        f"~~{money(price, guild_id)}~~ "
        f"**{money(effective_price, guild_id)}**"
    )


def format_multiplier(multiplier_percent: int) -> str:
    whole, decimals = divmod(multiplier_percent, 100)
    if decimals == 0:
        return f"{whole}×"
    if decimals % 10 == 0:
        return f"{whole}.{decimals // 10}×"
    return f"{whole}.{decimals:02d}×"


def parse_whitelist_multiplier(
    value: str,
) -> tuple[int | None, str | None]:
    cleaned = value.strip().casefold().replace(" ", "").replace(",", ".")
    if cleaned.endswith(("x", "×")):
        cleaned = cleaned[:-1]
    is_percentage = cleaned.endswith("%")
    if is_percentage:
        cleaned = cleaned[:-1]
    if not cleaned:
        return None, "Escribe un multiplicador, por ejemplo `1.5` o `1,5`."
    try:
        multiplier = Fraction(cleaned)
    except (ValueError, ZeroDivisionError):
        return (
            None,
            "Usa un valor como `1.5`, `1,5`, `1.5x`, `3/2` o `150%`.",
        )
    if is_percentage:
        multiplier /= 100
    if multiplier < 1 or multiplier > 10:
        return None, "El multiplicador debe estar entre **1×** y **10×**."
    multiplier_percent = int(multiplier * 100 + Fraction(1, 2))
    return multiplier_percent, None


def whitelist_marker(guild_id: int | None = None) -> str:
    if guild_id is None:
        return DEFAULT_WHITELIST_EMOJI
    return getattr(
        globals().get("bot"),
        "whitelist_emojis",
        {},
    ).get(guild_id, DEFAULT_WHITELIST_EMOJI)


async def answer(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = False,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, embed=embed, ephemeral=ephemeral)


class SulareaCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        if command is not None and command.name == "estadoobjetos":
            return True
        if interaction.guild_id is None or not hasattr(self.client, "db"):
            return True
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        is_admin = bool(
            member is not None and member.guild_permissions.administrator
        )
        blocker = await self.client.get_maintenance_block_cached(
            interaction.guild_id,
            "all_commands",
            is_admin,
        )
        if blocker is None:
            return True
        await answer(
            interaction,
            disabled_object_section_text(
                blocker["section"],
                blocker["disabled_reason"],
            ),
        )
        return False


class SulareaBot(commands.Bot):
    db: Database

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=SulareaCommandTree,
        )
        self.purchase_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.purchase_lock_counts: dict[tuple[int, int], int] = {}
        self.modifier_webhooks: dict[int, discord.Webhook] = {}
        self.application_emojis: dict[int, discord.Emoji] = {}
        self.coin_emojis: dict[int, str] = {}
        self.whitelist_emojis: dict[int, str] = {}
        self.log_settings_cache: dict[int, tuple[float, dict | None]] = {}
        self.maintenance_cache: dict[
            tuple[int, str, bool],
            tuple[float, dict | None],
        ] = {}
        self.active_modifier_users: set[tuple[int, int]] = set()
        self.active_modifier_channels: set[tuple[int, int]] = set()
        self.question_events: dict[tuple[int, int, int], dict] = {}
        self.question_event_locks: dict[
            tuple[int, int, int],
            asyncio.Lock,
        ] = {}

    async def setup_hook(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "Falta DATABASE_URL. Añade PostgreSQL en Railway y conecta su "
                "variable DATABASE_URL al servicio del bot."
            )
        self.db = Database(database_url)
        await self.db.connect()
        (
            coin_rows,
            whitelist_rows,
            active_modifier_targets,
            active_question_events,
        ) = await asyncio.gather(
            self.db.list_coin_emojis(),
            self.db.list_whitelist_emojis(),
            self.db.list_active_modifier_targets(),
            self.db.list_active_question_events(),
        )
        self.coin_emojis = {
            row["guild_id"]: row["coin_emoji"] for row in coin_rows
        }
        self.whitelist_emojis = {
            row["guild_id"]: row["whitelist_emoji"] for row in whitelist_rows
        }
        self.active_modifier_users = {
            (row["guild_id"], row["target_id"])
            for row in active_modifier_targets
            if row["target_kind"] == "individual"
        }
        self.active_modifier_channels = {
            (row["guild_id"], row["target_id"])
            for row in active_modifier_targets
            if row["target_kind"] == "channel"
        }
        self.question_events = {
            (row["guild_id"], row["channel_id"], row["message_id"]): dict(row)
            for row in active_question_events
        }
        try:
            application_emojis = await self.fetch_application_emojis()
            self.application_emojis = {
                emoji.id: emoji for emoji in application_emojis
            }
        except (discord.HTTPException, discord.MissingApplicationID):
            traceback.print_exc()
        await self.tree.sync()
        if not event_expiration_loop.is_running():
            event_expiration_loop.start()
        if not modifier_expiration_loop.is_running():
            modifier_expiration_loop.start()
        if not state_cache_refresh_loop.is_running():
            state_cache_refresh_loop.start()
        if not automatic_message_loop.is_running():
            automatic_message_loop.start()

    async def get_log_settings_cached(self, guild_id: int) -> dict | None:
        now = time.monotonic()
        cached = self.log_settings_cache.get(guild_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        row = await self.db.get_log_settings(guild_id)
        value = dict(row) if row is not None else None
        self.log_settings_cache[guild_id] = (
            now + CACHE_TTL_SECONDS,
            value,
        )
        return value

    def invalidate_log_settings(self, guild_id: int) -> None:
        self.log_settings_cache.pop(guild_id, None)

    async def get_maintenance_block_cached(
        self,
        guild_id: int,
        section: str,
        is_admin: bool,
    ) -> dict | None:
        now = time.monotonic()
        key = (guild_id, section, is_admin)
        cached = self.maintenance_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        row = await self.db.get_maintenance_block(
            guild_id,
            section,
            is_admin,
        )
        value = dict(row) if row is not None else None
        self.maintenance_cache[key] = (
            now + CACHE_TTL_SECONDS,
            value,
        )
        return value

    def invalidate_maintenance(self, guild_id: int) -> None:
        stale_keys = [
            key for key in self.maintenance_cache if key[0] == guild_id
        ]
        for key in stale_keys:
            self.maintenance_cache.pop(key, None)

    def cache_question_event(self, event: dict) -> None:
        message_id = event.get("message_id")
        if message_id is None:
            return
        stale_keys = [
            key
            for key in self.question_events
            if key[0] == event["guild_id"] and key[1] == event["channel_id"]
        ]
        for stale_key in stale_keys:
            self.question_events.pop(stale_key, None)
            self.question_event_locks.pop(stale_key, None)
        key = (event["guild_id"], event["channel_id"], message_id)
        self.question_events[key] = event

    def remove_question_event(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int | None,
    ) -> None:
        if message_id is None:
            return
        key = (guild_id, channel_id, message_id)
        self.question_events.pop(key, None)
        self.question_event_locks.pop(key, None)

    async def close(self) -> None:
        if event_expiration_loop.is_running():
            event_expiration_loop.cancel()
        if modifier_expiration_loop.is_running():
            modifier_expiration_loop.cancel()
        if state_cache_refresh_loop.is_running():
            state_cache_refresh_loop.cancel()
        if automatic_message_loop.is_running():
            automatic_message_loop.cancel()
        if hasattr(self, "db"):
            await self.db.close()
        await super().close()


bot = SulareaBot()


@asynccontextmanager
async def purchase_lock(guild_id: int, user_id: int):
    key = (guild_id, user_id)
    lock = bot.purchase_locks.setdefault(key, asyncio.Lock())
    bot.purchase_lock_counts[key] = bot.purchase_lock_counts.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = bot.purchase_lock_counts.get(key, 1) - 1
        if remaining <= 0:
            bot.purchase_lock_counts.pop(key, None)
            if bot.purchase_locks.get(key) is lock:
                bot.purchase_locks.pop(key, None)
        else:
            bot.purchase_lock_counts[key] = remaining


def guild_member(interaction: discord.Interaction) -> discord.Member | None:
    return interaction.user if isinstance(interaction.user, discord.Member) else None


def role_can_be_managed(role: discord.Role) -> bool:
    return not role.is_default() and not role.managed and role.is_assignable()


def members_from_target(
    target: discord.Member | discord.Role,
) -> list[discord.Member]:
    if isinstance(target, discord.Member):
        return [target]
    return list(target.members)


async def send_audit_log(
    guild: discord.Guild,
    title: str,
    description: str,
    *,
    color: int = 0x5865F2,
) -> None:
    settings = await bot.get_log_settings_cached(guild.id)
    if not settings or not settings["logs_enabled"] or not settings["log_channel_id"]:
        return
    channel = guild.get_channel(settings["log_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Servidor: {guild.name}")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        traceback.print_exc()


def make_question_embed(
    guild: discord.Guild,
    question: str,
    reward_text: str,
    *,
    expires_at: datetime | None = None,
    final_status: str | None = None,
    correct_answer: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="❓ Evento de pregunta",
        description=question,
        color=0x6B7280 if final_status else 0xEC4899,
    )
    embed.add_field(name="Recompensa", value=reward_text)
    if final_status is not None:
        embed.add_field(name="Finalizado", value=final_status)
        embed.add_field(
            name="Respuesta correcta",
            value=(
                correct_answer[:1024]
                if correct_answer
                else "No disponible para este evento anterior."
            ),
            inline=False,
        )
        embed.set_footer(text="Este evento ya terminó.")
    elif expires_at is not None:
        embed.add_field(
            name="Finaliza",
            value=f"<t:{int(expires_at.timestamp())}:R>",
        )
        embed.set_footer(
            text="Responde directamente a este mensaje. La primera respuesta correcta gana."
        )
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


async def finalize_question_embed(
    guild: discord.Guild,
    event,
    status: str,
) -> None:
    channel_id = event["channel_id"]
    message_id = event["message_id"]
    if message_id is None:
        return
    channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if channel is None or not hasattr(channel, "get_partial_message"):
        return
    try:
        message = channel.get_partial_message(message_id)
        await message.edit(
            embed=make_question_embed(
                guild,
                event["question"],
                question_event_reward_text(guild, event),
                final_status=status,
                correct_answer=event["answer_text"],
            )
        )
    except discord.HTTPException:
        traceback.print_exc()


@tasks.loop(seconds=15)
async def event_expiration_loop() -> None:
    if not hasattr(bot, "db"):
        return
    try:
        expired_events = await bot.db.pop_expired_question_events()
    except (OSError, asyncpg.PostgresError):
        traceback.print_exc()
        return
    for event in expired_events:
        bot.remove_question_event(
            event["guild_id"],
            event["channel_id"],
            event["message_id"],
        )
        movement_amount = question_event_movement_amount(event)
        await bot.db.record_movement(
            event["guild_id"],
            None,
            event["created_by"],
            "event_expire",
            movement_amount,
            f"Caducó el evento de pregunta: {event['question']}",
        )
        guild = bot.get_guild(event["guild_id"])
        if guild is None:
            continue
        await finalize_question_embed(
            guild,
            event,
            "⌛ Terminó sin ganador.",
        )
        channel = guild.get_channel(event["channel_id"]) or guild.get_thread(
            event["channel_id"]
        )
        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(
                    f"⌛ El evento **{event['question']}** caducó sin ganador.\n"
                    f"**Respuesta correcta:** {event['answer_text'] or 'No disponible'}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                traceback.print_exc()
        await send_audit_log(
            guild,
            "Evento de pregunta caducado",
            f"**Pregunta:** {event['question']}\n"
            f"**Respuesta correcta:** {event['answer_text'] or 'No disponible'}\n"
            f"**Recompensa:** {question_event_reward_text(guild, event)}\n"
            "Terminó sin ganador.",
            color=0x6B7280,
        )


@event_expiration_loop.before_loop
async def before_event_expiration_loop() -> None:
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def state_cache_refresh_loop() -> None:
    if not hasattr(bot, "db"):
        return
    try:
        active_targets, active_events = await asyncio.gather(
            bot.db.list_active_modifier_targets(),
            bot.db.list_active_question_events(),
        )
    except (OSError, asyncpg.PostgresError):
        traceback.print_exc()
        return
    for row in active_targets:
        key = (row["guild_id"], row["target_id"])
        if row["target_kind"] == "individual":
            bot.active_modifier_users.add(key)
        else:
            bot.active_modifier_channels.add(key)
    now = datetime.now(timezone.utc)
    for key, event in list(bot.question_events.items()):
        if event["expires_at"] <= now:
            bot.remove_question_event(*key)
    for row in active_events:
        key = (row["guild_id"], row["channel_id"], row["message_id"])
        same_channel_exists = any(
            cached_key[0] == row["guild_id"]
            and cached_key[1] == row["channel_id"]
            for cached_key in bot.question_events
        )
        if key in bot.question_events or not same_channel_exists:
            bot.question_events[key] = dict(row)


@state_cache_refresh_loop.before_loop
async def before_state_cache_refresh_loop() -> None:
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def automatic_message_loop() -> None:
    if not hasattr(bot, "db"):
        return
    try:
        due_messages = await bot.db.claim_due_automatic_messages()
    except (OSError, asyncpg.PostgresError):
        traceback.print_exc()
        return
    for due in due_messages:
        guild = bot.get_guild(due["guild_id"])
        if guild is None:
            continue
        channel = guild.get_channel(due["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            await channel.send(
                due["content"],
                allowed_mentions=discord.AllowedMentions.all(),
            )
        except discord.HTTPException:
            traceback.print_exc()


@automatic_message_loop.before_loop
async def before_automatic_message_loop() -> None:
    await bot.wait_until_ready()


@tasks.loop(seconds=5)
async def modifier_expiration_loop() -> None:
    if not hasattr(bot, "db"):
        return
    try:
        expired_modifiers, expired_channel_modifiers = await asyncio.gather(
            bot.db.pop_expired_modifiers(),
            bot.db.pop_expired_channel_modifiers(),
        )
    except (OSError, asyncpg.PostgresError):
        traceback.print_exc()
        return
    for expired in expired_modifiers:
        guild = bot.get_guild(expired["guild_id"])
        bot.active_modifier_users.discard(
            (expired["guild_id"], expired["user_id"])
        )
        if guild is not None and expired["channel_id"] is not None:
            channel = guild.get_channel(expired["channel_id"]) or guild.get_thread(
                expired["channel_id"]
            )
            if channel is not None and hasattr(channel, "send"):
                member = guild.get_member(expired["user_id"])
                member_name = (
                    discord.utils.escape_mentions(
                        discord.utils.escape_markdown(member.display_name)
                    )
                    if member is not None
                    else f"Usuario {expired['user_id']}"
                )
                try:
                    await channel.send(
                        f"El modificador **{expired['name']}** de "
                        f"**{member_name}** terminó.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        await bot.db.record_movement(
            expired["guild_id"],
            expired["user_id"],
            expired["user_id"],
            "modifier_expire",
            None,
            f"Terminó el modificador {expired['name']}.",
        )
        if guild is not None:
            await send_audit_log(
                guild,
                "Modificador finalizado",
                f"**Miembro:** <@{expired['user_id']}>\n"
                f"**Modificador:** {expired['name']}",
                color=0x6B7280,
            )
    for expired in expired_channel_modifiers:
        guild = bot.get_guild(expired["guild_id"])
        bot.active_modifier_channels.discard(
            (expired["guild_id"], expired["channel_id"])
        )
        channel = (
            guild.get_channel(expired["channel_id"])
            if guild is not None
            else None
        )
        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(
                    f"El modificador de canal **{expired['name']}** terminó.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
        await bot.db.record_movement(
            expired["guild_id"],
            expired["owner_user_id"],
            expired["owner_user_id"],
            "channel_modifier_expire",
            None,
            f"Terminó el modificador de canal {expired['name']} "
            f"en el canal {expired['channel_id']}.",
        )
        if guild is not None:
            await send_audit_log(
                guild,
                "Modificador de canal finalizado",
                f"**Canal:** <#{expired['channel_id']}>\n"
                f"**Modificador:** {expired['name']}",
                color=0x6B7280,
            )


@modifier_expiration_loop.before_loop
async def before_modifier_expiration_loop() -> None:
    await bot.wait_until_ready()


async def get_modifier_webhook(
    channel: discord.abc.GuildChannel | discord.Thread,
) -> tuple[discord.Webhook | None, discord.Thread | None]:
    thread = channel if isinstance(channel, discord.Thread) else None
    base_channel = channel.parent if thread is not None else channel
    if not isinstance(base_channel, (discord.TextChannel, discord.ForumChannel)):
        return None, thread
    cached = bot.modifier_webhooks.get(base_channel.id)
    if cached is not None:
        return cached, thread
    try:
        webhooks = await base_channel.webhooks()
        owned_webhooks = [
            item
            for item in webhooks
            if item.name == "Sularea Modificadores"
            and item.user is not None
            and bot.user is not None
            and item.user.id == bot.user.id
        ]
        webhook = next(
            (item for item in owned_webhooks if item.token is not None),
            None,
        )
        if webhook is None:
            for stale_webhook in owned_webhooks:
                try:
                    await stale_webhook.delete(
                        reason="Renovación del webhook de modificadores",
                    )
                except discord.HTTPException:
                    pass
            webhook = await base_channel.create_webhook(
                name="Sularea Modificadores",
                reason="Mensajes de modificadores de Sularea",
            )
        bot.modifier_webhooks[base_channel.id] = webhook
        return webhook, thread
    except discord.HTTPException:
        traceback.print_exc()
        return None, thread


async def send_modifier_webhook_message(
    message: discord.Message,
    content: str,
) -> None:
    webhook, thread = await get_modifier_webhook(message.channel)
    if webhook is None:
        return
    kwargs = {
        "content": content,
        "username": message.author.display_name[:80],
        "avatar_url": message.author.display_avatar.url,
        "allowed_mentions": discord.AllowedMentions.none(),
        "wait": True,
    }
    if thread is not None:
        kwargs["thread"] = thread
    try:
        await webhook.send(**kwargs)
    except discord.NotFound:
        base_channel = message.channel.parent if thread is not None else message.channel
        bot.modifier_webhooks.pop(base_channel.id, None)
    except discord.HTTPException:
        traceback.print_exc()


async def apply_balance_change(
    guild: discord.Guild,
    members: list[discord.Member],
    target_mention: str,
    action: str,
    amount: int,
    actor: discord.Member | discord.User,
) -> str:
    user_ids = [member.id for member in members]
    if action == "add":
        await bot.db.add_balance_many(guild.id, user_ids, amount)
        affected_ids = user_ids
        movement_action = "balance_add"
        movement_amount = amount
        verb = "Añadió"
        history_text = f"Un administrador añadió {money(amount, guild.id)}."
    elif action == "remove":
        affected_ids = await bot.db.remove_balance_many(guild.id, user_ids, amount)
        movement_action = "balance_remove"
        movement_amount = -amount
        verb = "Restó"
        history_text = f"Un administrador restó {money(amount, guild.id)}."
    else:
        await bot.db.set_balance_many(guild.id, user_ids, amount)
        affected_ids = user_ids
        movement_action = "balance_set"
        movement_amount = amount
        verb = "Estableció"
        history_text = f"Un administrador estableció el balance en {money(amount, guild.id)}."

    await bot.db.record_movements(
        guild.id,
        affected_ids,
        actor.id,
        movement_action,
        movement_amount,
        history_text,
    )
    affected = len(affected_ids)
    skipped = len(members) - affected

    if len(members) == 1:
        current_balance = await bot.db.get_balance(guild.id, members[0].id)
        if action == "remove" and affected == 0:
            result = (
                f"{members[0].mention} no tiene saldo suficiente. "
                f"Su balance es **{money(current_balance, guild.id)}**."
            )
        elif action == "set":
            result = (
                f"El balance de {members[0].mention} ahora es "
                f"**{money(current_balance, guild.id)}**. Se aplicó a **1 miembro**."
            )
        else:
            action_result = "Añadiste" if action == "add" else "Restaste"
            result = (
                f"{action_result} **{money(amount, guild.id)}** a {members[0].mention}. "
                f"Nuevo balance: **{money(current_balance, guild.id)}**."
            )
    elif action == "set":
        result = (
            f"Establecí el balance de {target_mention} en **{money(amount, guild.id)}**. "
            f"Se aplicó a **{affected} {'miembro' if affected == 1 else 'miembros'}**."
        )
    else:
        result = (
            f"{verb} **{money(amount, guild.id)}** a **{affected} de "
            f"{len(members)} {'miembro' if len(members) == 1 else 'miembros'}** "
            f"de {target_mention}."
        )
        if skipped:
            result += f" Se omitieron **{skipped}** por saldo insuficiente."

    await send_audit_log(
        guild,
        "Movimiento de balance",
        f"**Administrador:** {actor.mention}\n"
        f"**Objetivo:** {target_mention}\n"
        f"**Acción:** {verb} {money(amount, guild.id)}\n"
        f"**Aplicado a:** {affected} miembros"
        + (f"\n**Omitidos:** {skipped}" if skipped else ""),
        color=0x22C55E if action == "add" else 0xEF4444 if action == "remove" else 0x3B82F6,
    )
    return result


class MassBalanceConfirmView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        guild: discord.Guild,
        role: discord.Role,
        action: str,
        amount: int,
    ) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.guild = guild
        self.role = role
        self.action = action
        self.amount = amount
        self.message: discord.InteractionMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo el administrador que inició esta operación puede confirmarla.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="La confirmación expiró sin realizar cambios.", view=self)
            except discord.HTTPException:
                pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        await answer(interaction, "Ocurrió un error al aplicar la operación masiva.")

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Aplicando cambios...", view=self)
        members = list(self.role.members)
        result = await apply_balance_change(
            self.guild,
            members,
            self.role.mention,
            self.action,
            self.amount,
            interaction.user,
        )
        await interaction.edit_original_response(content=result, view=self)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Operación cancelada. No se modificó ningún balance.",
            view=self,
        )
        self.stop()


async def handle_balance_command(
    interaction: discord.Interaction,
    target: discord.Member | discord.Role,
    amount: int,
    action: str,
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    members = members_from_target(target)
    if not members:
        await answer(interaction, f"El rol {target.mention} no tiene miembros.")
        return
    if isinstance(target, discord.Role):
        action_text = {
            "add": "añadir",
            "remove": "restar",
            "set": "establecer",
        }[action]
        view = MassBalanceConfirmView(
            interaction.user.id,
            guild,
            target,
            action,
            amount,
        )
        await interaction.response.send_message(
            f"⚠️ Vas a **{action_text} {money(amount, guild.id)}** para "
            f"**{len(members)} miembros** con el rol {target.mention}. ¿Confirmar?",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
        return
    await interaction.response.defer()
    result = await apply_balance_change(
        guild,
        members,
        target.mention,
        action,
        amount,
        interaction.user,
    )
    await answer(interaction, result)


async def find_badge(interaction: discord.Interaction, name: str):
    if interaction.guild_id is None:
        return None
    return await bot.db.get_badge(interaction.guild_id, normalize_name(name))


async def find_modifier(interaction: discord.Interaction, name: str):
    if interaction.guild_id is None:
        return None
    return await bot.db.get_modifier(interaction.guild_id, normalize_name(name))


async def find_ticket(interaction: discord.Interaction, name: str):
    if interaction.guild_id is None:
        return None
    return await bot.db.get_ticket(interaction.guild_id, normalize_name(name))


def duplicate_object_name(source_name: str, copy_number: int = 1) -> str:
    suffix = (
        " (DUPLICADO)"
        if copy_number == 1
        else f" {copy_number} (DUPLICADO)"
    )
    base = source_name.strip()[: 100 - len(suffix)].rstrip()
    return f"{base or 'Objeto'}{suffix}"


async def next_duplicate_object_name(
    guild_id: int,
    source_name: str,
) -> str | None:
    for copy_number in range(1, 101):
        candidate = duplicate_object_name(source_name, copy_number)
        existing = await bot.db.get_configured_object(
            guild_id,
            normalize_name(candidate),
        )
        if existing is None:
            return candidate
    return None


async def find_shop_category(interaction: discord.Interaction, name: str):
    if interaction.guild_id is None:
        return None
    cleaned = name.removeprefix("categoria:").strip()
    return await bot.db.get_shop_category(
        interaction.guild_id,
        normalize_name(cleaned),
    )


def disabled_object_section_text(section: str, reason: str | None) -> str:
    detail = (reason or "Mantenimiento temporal.").strip()
    return f"{OBJECT_SECTION_DISABLED_MESSAGES[section]}\n**Razón:** {detail}"


async def object_section_disabled_message(
    interaction: discord.Interaction,
    section: str,
) -> str | None:
    if interaction.guild_id is None:
        return None
    member = guild_member(interaction)
    is_admin = bool(
        member is not None and member.guild_permissions.administrator
    )
    blocker = await bot.get_maintenance_block_cached(
        interaction.guild_id,
        section,
        is_admin,
    )
    if blocker is None:
        return None
    return disabled_object_section_text(
        blocker["section"],
        blocker["disabled_reason"],
    )


async def member_is_whitelisted(member: discord.Member) -> bool:
    return await bot.db.is_whitelisted(
        member.guild.id,
        member.id,
        [role.id for role in member.roles],
    )


async def whitelist_access_source(member: discord.Member) -> tuple[str, str]:
    entries = await bot.db.list_whitelist_entries(member.guild.id)
    whitelisted_role_ids = {
        row["target_id"]
        for row in entries
        if row["target_type"] == "role"
    }
    matching_role = next(
        (role for role in member.roles if role.id in whitelisted_role_ids),
        None,
    )
    if matching_role is not None:
        return (
            f"Con tu rol {matching_role.mention}",
            f"por el rol {matching_role.name}",
        )
    if any(
        row["target_type"] == "member" and row["target_id"] == member.id
        for row in entries
    ):
        return (
            "Por estar añadido directamente a la whitelist",
            "por acceso directo a la whitelist",
        )
    return ("Gracias a la whitelist", "por la whitelist")


async def badge_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    search = normalize_name(current)
    rows = await bot.db.search_named_items(
        interaction.guild_id,
        "badge",
        search,
    )
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
    ]


async def owned_badge_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    member = guild_member(interaction)
    if interaction.guild_id is None or member is None or not hasattr(bot, "db"):
        return []
    rows = await bot.db.list_badges(interaction.guild_id)
    owned_role_ids = {role.id for role in member.roles}
    search = normalize_name(current)
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
        if row["badge_role_id"] in owned_role_ids and search in row["name_key"]
    ][:25]


async def owned_object_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    member = guild_member(interaction)
    if interaction.guild_id is None or member is None or not hasattr(bot, "db"):
        return []
    search = normalize_name(current)
    rows = await bot.db.search_owned_objects(
        interaction.guild_id,
        member.id,
        [role.id for role in member.roles],
        search,
    )
    choices = []
    for row in rows:
        if row["item_type"] == "badge":
            marker = (
                f"{whitelist_marker(interaction.guild_id)} "
                if row["via_whitelist"]
                else ""
            )
            choices.append(
                app_commands.Choice(
                    name=f"{marker}Insignia · {row['name']}"[:100],
                    value=row["name"][:100],
                )
            )
        elif row["item_type"] == "modifier":
            choices.append(
                app_commands.Choice(
                    name=(
                        f"Modificador · {row['name']} "
                        f"(x{row['quantity']})"
                    )[:100],
                    value=row["name"][:100],
                )
            )
        else:
            choices.append(
                app_commands.Choice(
                    name=(
                        f"Ticket · {row['name']} (x{row['quantity']})"
                        f"{' · Inactivo' if not row['active'] else ''}"
                    )[:100],
                    value=row["name"][:100],
                )
            )
    return choices


async def removable_object_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    guild = interaction.guild
    if interaction.guild_id is None or guild is None or not hasattr(bot, "db"):
        return []
    selected_member = getattr(interaction.namespace, "miembro", None)
    member_id = getattr(selected_member, "id", None)
    if member_id is None:
        return []
    member = (
        selected_member
        if isinstance(selected_member, discord.Member)
        else guild.get_member(member_id)
    )
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except (discord.HTTPException, discord.NotFound):
            return []

    search = normalize_name(current)
    rows = await bot.db.search_removable_objects(
        interaction.guild_id,
        member.id,
        [role.id for role in member.roles],
        search,
    )
    labels = {
        "badge": "Insignia",
        "modifier": "Modificador",
        "ticket": "Ticket",
    }
    return [
        app_commands.Choice(
            name=(
                f"{labels[row['item_type']]} · {row['name']}"
                + (
                    ""
                    if row["item_type"] == "badge"
                    else f" (x{row['quantity']})"
                )
            )[:100],
            value=row["name"][:100],
        )
        for row in rows
    ]


async def modifier_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    search = normalize_name(current)
    rows = await bot.db.search_named_items(
        interaction.guild_id,
        "modifier",
        search,
    )
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
    ]


async def modifier_message_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    modifier_name = getattr(interaction.namespace, "modificador", None)
    action = getattr(interaction.namespace, "accion", None)
    if not isinstance(modifier_name, str) or action == "add":
        return []
    modifier = await bot.db.get_modifier(
        interaction.guild_id,
        normalize_name(modifier_name),
    )
    if modifier is None:
        return []
    search = normalize_name(current)
    choices = []
    for index, message in enumerate(modifier["messages"]):
        preview = " ".join(message.split())
        label = f"{index + 1}. {preview}"
        if search and search not in normalize_name(label):
            continue
        choices.append(
            app_commands.Choice(
                name=label[:100],
                value=f"#{index + 1}",
            )
        )
    return choices[:25]


def modifier_message_index(messages: list[str], selected: str) -> int | None:
    cleaned = selected.strip()
    if cleaned.startswith("#") and cleaned[1:].isdigit():
        index = int(cleaned[1:]) - 1
        return index if 0 <= index < len(messages) else None
    normalized = normalize_name(cleaned)
    return next(
        (
            index
            for index, message in enumerate(messages)
            if normalize_name(message) == normalized
        ),
        None,
    )


async def automatic_message_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    action = getattr(interaction.namespace, "accion", None)
    if action == "add":
        return []
    rows = await bot.db.list_automatic_messages(interaction.guild_id)
    search = normalize_name(current)
    choices = []
    for index, row in enumerate(rows, start=1):
        preview = " ".join(row["content"].split())
        label = f"{index}. {preview}"
        searchable = f"{label} #{row['id']}"
        if search and search not in normalize_name(searchable):
            continue
        choices.append(
            app_commands.Choice(
                name=label[:100],
                value=f"#{row['id']}",
            )
        )
    return choices[:25]


def selected_automatic_message(rows, selected: str):
    cleaned = selected.strip()
    if cleaned.startswith("#") and cleaned[1:].isdigit():
        message_id = int(cleaned[1:])
        return next((row for row in rows if row["id"] == message_id), None)
    normalized = normalize_name(cleaned)
    return next(
        (
            row
            for row in rows
            if normalize_name(row["content"]) == normalized
        ),
        None,
    )


async def ticket_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    search = normalize_name(current)
    rows = await bot.db.search_named_items(
        interaction.guild_id,
        "ticket",
        search,
    )
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
    ]


async def editable_object_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    search = normalize_name(current)
    rows = await bot.db.search_configured_objects(
        interaction.guild_id,
        search,
        True,
    )
    labels = {
        "badge": "Insignia",
        "modifier": "Modificador",
        "ticket": "Ticket",
        "category": "Categoría",
    }
    return [
        app_commands.Choice(
            name=f"{labels[row['item_type']]} · {row['name']}"[:100],
            value=(
                f"categoria:{row['name']}"[:100]
                if row["item_type"] == "category"
                else row["name"][:100]
            ),
        )
        for row in rows
    ]


async def deletable_object_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    rows = await bot.db.search_configured_objects(
        interaction.guild_id,
        normalize_name(current),
        False,
    )
    labels = {
        "badge": "Insignia",
        "modifier": "Modificador",
        "ticket": "Ticket",
    }
    return [
        app_commands.Choice(
            name=f"{labels[row['item_type']]} · {row['name']}"[:100],
            value=row["name"][:100],
        )
        for row in rows
    ]


async def shop_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    member = guild_member(interaction)
    search = normalize_name(current)
    rows = await bot.db.search_shop_items(
        interaction.guild_id,
        search,
        [role.id for role in member.roles] if member is not None else [],
    )
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
    ]


async def shop_section_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    search = normalize_name(current)
    rows = await bot.db.search_named_items(
        interaction.guild_id,
        "category",
        search,
    )
    sections = {
        row["name_key"]: row["name"]
        for row in rows
    }
    sections.setdefault(normalize_name(DEFAULT_SHOP_SECTION), DEFAULT_SHOP_SECTION)
    return [
        app_commands.Choice(name=section[:100], value=section[:100])
        for key, section in sorted(sections.items(), key=lambda item: item[0])
        if search in key
    ][:25]


async def category_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    search = normalize_name(current)
    rows = await bot.db.search_named_items(
        interaction.guild_id,
        "category",
        search,
    )
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
    ]


async def resolve_shop_section(
    guild_id: int,
    section_name: str,
) -> str | None:
    if normalize_name(section_name) == normalize_name(DEFAULT_SHOP_SECTION):
        return DEFAULT_SHOP_SECTION
    category = await bot.db.get_shop_category(
        guild_id,
        normalize_name(section_name),
    )
    return category["name"] if category is not None else None


def group_shop_badges(
    rows: list,
    categories: list,
) -> list[tuple[str, str, list]]:
    grouped: dict[str, tuple[str, str, list]] = {
        row["name_key"]: (row["name"], row["description"], [])
        for row in categories
    }
    for row in rows:
        section_name = shop_section_name(row["shop_section"])
        section_key = normalize_name(section_name)
        if section_key not in grouped:
            grouped[section_key] = (section_name, "", [])
        grouped[section_key][2].append(row)
    return sorted(
        grouped.values(),
        key=lambda section: (
            normalize_name(section[0]) != normalize_name(DEFAULT_SHOP_SECTION),
            normalize_name(section[0]),
        ),
    )


def make_shop_embed(
    guild: discord.Guild,
    sections: list[tuple[str, str, list]],
    selected_index: int,
    discount_percent: int = 0,
) -> discord.Embed:
    section_name, section_description, rows = sections[selected_index]
    item_lines = []
    for row in rows:
        prefix = f"• {badge_emoji(row['emoji'], guild)}**{row['name']}**"
        if row["item_type"] == "badge":
            item_lines.append(
                f"{prefix} (<@&{row['color_role_id']}>) — "
                f"{shop_price_text(row['price'], discount_percent, guild.id)}"
            )
        elif row["item_type"] == "modifier":
            item_lines.append(
                f"{prefix} (Consumible "
                f"{modifier_scope_label(row['effect_scope']).lower()} · "
                f"{row['duration_minutes']} min) — "
                f"{shop_price_text(row['price'], discount_percent, guild.id)}"
            )
        else:
            ticket_status = "activo" if row["active"] else "inactivo"
            item_lines.append(
                f"{prefix} (ticket consumible · {ticket_status}) — "
                f"{shop_price_text(row['price'], discount_percent, guild.id)}\n"
                f"  {row['description']}"
            )
    items = "\n".join(item_lines) if item_lines else "*No hay objetos en esta categoría.*"
    description = (
        f"__**{section_name}**__\n{section_description}\n\n{items}"
        if section_description
        else f"__**{section_name}**__\n\n{items}"
    )
    embed = discord.Embed(
        title="Mercado de Sularea",
        description=description[:4000],
        color=0xF59E0B,
    )
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(
        text=(
            f"Categoría {selected_index + 1} de {len(sections)} · "
            "Usa el menú para cambiar y /comprar para obtener un objeto."
            + (
                f" · Descuento whitelist aplicado: {discount_percent}%"
                if discount_percent > 0
                else ""
            )
        )
    )
    return embed


class ShopSectionSelect(discord.ui.Select):
    def __init__(self, shop_view: "ShopView") -> None:
        self.shop_view = shop_view
        options = [
            discord.SelectOption(
                label=section_name[:100],
                value=str(index),
                description=(
                    section_description
                    or (
                        f"{len(rows)} "
                        f"{'objeto disponible' if len(rows) == 1 else 'objetos disponibles'}"
                    )
                )[:100],
                default=index == 0,
            )
            for index, (section_name, section_description, rows) in enumerate(
                shop_view.sections
            )
        ]
        super().__init__(
            placeholder=f"Categoría: {shop_view.sections[0][0]}"[:150],
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_index = int(self.values[0])
        self.shop_view.selected_index = selected_index
        for option in self.options:
            option.default = option.value == str(selected_index)
        self.placeholder = f"Categoría: {self.shop_view.sections[selected_index][0]}"[:150]
        await interaction.response.edit_message(
            embed=make_shop_embed(
                self.shop_view.guild,
                self.shop_view.sections,
                selected_index,
                self.shop_view.discount_percent,
            ),
            view=self.shop_view,
        )


class ShopView(discord.ui.View):
    def __init__(
        self,
        guild: discord.Guild,
        sections: list[tuple[str, str, list]],
        author_id: int,
        discount_percent: int = 0,
    ) -> None:
        super().__init__(timeout=300)
        self.guild = guild
        self.sections = sections
        self.author_id = author_id
        self.discount_percent = discount_percent
        self.selected_index = 0
        self.message: discord.InteractionMessage | None = None
        self.add_item(ShopSectionSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo la persona que usó /tienda puede controlar este menú.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Cerrar tienda",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        row=1,
    )
    async def close_shop(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        try:
            if interaction.message is not None:
                await interaction.message.delete()
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        await answer(interaction, "No pude cambiar la categoría de la tienda.", ephemeral=True)


class InactiveTicketPurchaseConfirmView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        ticket_name: str,
        source_interaction: discord.Interaction,
    ) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.ticket_name = ticket_name
        self.source_interaction = source_interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo la persona que inició esta compra puede confirmarla.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.source_interaction.edit_original_response(
                content="La confirmación expiró. No se compró ni se cobró el ticket.",
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        await answer(
            interaction,
            "Ocurrió un error al confirmar la compra.",
            ephemeral=True,
        )

    @discord.ui.button(label="Comprar de todos modos", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Procesando la compra de **{self.ticket_name}**...",
            view=self,
        )
        await process_purchase(
            interaction,
            self.ticket_name,
            confirmed_inactive_ticket=True,
        )
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Compra cancelada. No se cobró ni se entregó el ticket.",
            view=None,
        )
        self.stop()


async def process_purchase(
    interaction: discord.Interaction,
    item_name: str,
    *,
    confirmed_inactive_ticket: bool = False,
) -> None:
    member = guild_member(interaction)
    guild = interaction.guild
    if member is None or guild is None:
        return
    disabled_message = await object_section_disabled_message(
        interaction,
        "shop",
    )
    if disabled_message is not None:
        await answer(interaction, disabled_message)
        return
    discount_percent = await bot.db.get_whitelist_discount(
        guild.id,
        member.id,
        [role.id for role in member.roles],
    )
    badge = await find_badge(interaction, item_name)
    modifier = None if badge is not None else await find_modifier(interaction, item_name)
    ticket = (
        None
        if badge is not None or modifier is not None
        else await find_ticket(interaction, item_name)
    )
    if modifier is not None and modifier["purchasable"]:
        if not interaction.response.is_done():
            await interaction.response.defer()
        async with purchase_lock(guild.id, member.id):
            result = await bot.db.purchase_modifier(
                guild.id,
                member.id,
                modifier["name_key"],
                discount_percent,
            )
        if result is None:
            await answer(interaction, "Ese modificador ya no está disponible.")
            return
        if result["status"] == "insufficient":
            current = await bot.db.get_balance(guild.id, member.id)
            await answer(
                interaction,
                f"No tienes suficiente dinero. Tienes **{money(current, guild.id)}**.",
            )
            return
        await bot.db.record_movement(
            guild.id,
            member.id,
            member.id,
            "modifier_purchase",
            -result["price"],
            f"Compró el modificador {result['name']} por "
            f"{money(result['price'], guild.id)}.",
        )
        await send_audit_log(
            guild,
            "Compra de modificador",
            f"{member.mention} compró **{result['name']}** por "
            f"**{money(result['price'], guild.id)}**.\n"
            + (
                f"**Descuento whitelist:** {result['discount_percent']}%\n"
                if result["price"] < result["original_price"]
                else ""
            )
            +
            f"**Cantidad:** {result['quantity']}\n"
            f"**Nuevo balance:** {money(result['new_balance'], guild.id)}",
            color=0xA855F7,
        )
        await answer(
            interaction,
            f"{member.mention} compró **{result['name']}**. Ahora tiene "
            f"**{result['quantity']}**. Su balance es "
            f"**{money(result['new_balance'], guild.id)}**."
            + (
                f" Se aplicó un descuento de whitelist del "
                f"**{result['discount_percent']}%**."
                if result["price"] < result["original_price"]
                else ""
            ),
        )
        return

    if ticket is not None and ticket["purchasable"]:
        if not ticket["active"] and not confirmed_inactive_ticket:
            view = InactiveTicketPurchaseConfirmView(
                member.id,
                ticket["name"],
                interaction,
            )
            await interaction.response.send_message(
                f"⚠️ El ticket **{ticket['name']}** está **inactivo actualmente**. "
                "Puedes comprarlo y conservarlo, pero no podrás usarlo hasta que "
                "un administrador lo reactive. ¿Quieres comprarlo de todos modos?",
                view=view,
                ephemeral=True,
            )
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        async with purchase_lock(guild.id, member.id):
            result = await bot.db.purchase_ticket(
                guild.id,
                member.id,
                ticket["name_key"],
                discount_percent,
            )
        if result is None:
            await answer(interaction, "Ese ticket ya no está disponible.")
            return
        if result["status"] == "insufficient":
            current = await bot.db.get_balance(guild.id, member.id)
            await answer(
                interaction,
                f"No tienes suficiente dinero. Tienes **{money(current, guild.id)}**.",
            )
            return
        await bot.db.record_movement(
            guild.id,
            member.id,
            member.id,
            "ticket_purchase",
            -result["price"],
            f"Compró el ticket {result['name']} por "
            f"{money(result['price'], guild.id)}.",
        )
        await send_audit_log(
            guild,
            "Compra de ticket",
            f"{member.mention} compró **{result['name']}** por "
            f"**{money(result['price'], guild.id)}**.\n"
            + (
                f"**Descuento whitelist:** {result['discount_percent']}%\n"
                if result["price"] < result["original_price"]
                else ""
            )
            +
            f"**Cantidad:** {result['quantity']}\n"
            f"**Estado:** {'Activo' if result['active'] else 'Inactivo'}\n"
            f"**Nuevo balance:** {money(result['new_balance'], guild.id)}",
            color=0x06B6D4,
        )
        warning = (
            "⚠️ Este ticket está **inactivo actualmente**: puedes conservarlo, "
            "pero no podrás usarlo hasta que un administrador lo reactive."
            if not result["active"]
            else "⚠️ Los tickets pueden quedar inactivos temporalmente; si eso "
            "ocurre, conservarás el ticket hasta que vuelva a activarse."
        )
        await answer(
            interaction,
            f"{member.mention} compró **{result['name']}**. Ahora tiene "
            f"**{result['quantity']}**. Su balance es "
            f"**{money(result['new_balance'], guild.id)}**."
            + (
                f" Se aplicó un descuento de whitelist del "
                f"**{result['discount_percent']}%**."
                if result["price"] < result["original_price"]
                else ""
            )
            + f"\n{warning}",
        )
        return

    if badge is None or not badge["purchasable"]:
        await answer(interaction, "Ese objeto no está disponible en la tienda.")
        return
    role = guild.get_role(badge["badge_role_id"])
    if role is None:
        await answer(interaction, "El rol de esa insignia ya no existe.")
        return
    if role in member.roles:
        await answer(interaction, "Ya tienes esa insignia.")
        return
    if not role_can_be_managed(role):
        await answer(
            interaction,
            "No puedo entregar esa insignia. Coloca su rol debajo del rol del bot.",
        )
        return

    if not interaction.response.is_done():
        await interaction.response.defer()
    effective_badge_price = discounted_price(
        badge["price"],
        discount_percent,
    )
    async with purchase_lock(guild.id, member.id):
        if role in member.roles:
            await answer(interaction, "Ya tienes esa insignia.")
            return
        new_balance = await bot.db.spend_balance(
            guild.id,
            member.id,
            effective_badge_price,
        )
        if new_balance is None:
            current = await bot.db.get_balance(guild.id, member.id)
            await answer(
                interaction,
                f"No tienes suficiente dinero. Tienes **{money(current, guild.id)}**.",
            )
            return
        try:
            await member.add_roles(role, reason=f"Compra: {badge['name']}")
        except Exception:
            await bot.db.add_balance(
                guild.id,
                member.id,
                effective_badge_price,
            )
            raise

    await bot.db.record_movement(
        guild.id,
        member.id,
        member.id,
        "purchase",
        -effective_badge_price,
        f"Compró la insignia {badge['name']} por "
        f"{money(effective_badge_price, guild.id)}.",
    )
    await send_audit_log(
        guild,
        "Compra en el Mercado de Sularea",
        f"{member.mention} compró **{badge['name']}** (<@&{badge['color_role_id']}>) "
        f"por **{money(effective_badge_price, guild.id)}**.\n"
        + (
            f"**Descuento whitelist:** {discount_percent}%\n"
            if effective_badge_price < badge["price"]
            else ""
        )
        + f"**Nuevo balance:** {money(new_balance, guild.id)}",
        color=0xF59E0B,
    )
    await answer(
        interaction,
        f"{member.mention} compró **{badge['name']}** por "
        f"**{money(effective_badge_price, guild.id)}**. Su nuevo balance es "
        f"**{money(new_balance, guild.id)}**."
        + (
            f" Se aplicó un descuento de whitelist del **{discount_percent}%**."
            if effective_badge_price < badge["price"]
            else ""
        ),
    )


async def process_question_reply(message: discord.Message) -> None:
    if (
        message.guild is None
        or message.reference is None
        or message.reference.message_id is None
    ):
        return
    key = (
        message.guild.id,
        message.channel.id,
        message.reference.message_id,
    )
    cached_event = bot.question_events.get(key)
    if (
        cached_event is None
        or answer_hash(message.content) != cached_event["answer_hash"]
    ):
        return

    lock = bot.question_event_locks.setdefault(key, asyncio.Lock())
    async with lock:
        event = bot.question_events.get(key)
        if event is None or answer_hash(message.content) != event["answer_hash"]:
            return
        winner = (
            message.author
            if isinstance(message.author, discord.Member)
            else message.guild.get_member(message.author.id)
        )
        if winner is None:
            try:
                winner = await message.guild.fetch_member(message.author.id)
            except (discord.HTTPException, discord.NotFound):
                return
        winner_role_ids_before_reward = [role.id for role in winner.roles]

        badge_rewards = [
            reward_object
            for reward_object in event["reward_objects"]
            if reward_object["item_type"] == "badge"
        ]
        badge_configs = await asyncio.gather(
            *(
                bot.db.get_badge_by_id(
                    message.guild.id,
                    reward_object["item_id"],
                )
                for reward_object in badge_rewards
            )
        )
        expected_badge_role_ids: dict[int, int] = {}
        badge_roles_to_add: list[discord.Role] = []
        owned_badge_count = 0
        for reward_object, badge in zip(badge_rewards, badge_configs):
            role = (
                message.guild.get_role(badge["badge_role_id"])
                if badge is not None
                else None
            )
            if role is None:
                await message.reply(
                    "La respuesta es correcta, pero el rol de una insignia del premio "
                    "ya no existe. El evento sigue activo; avisa a un administrador.",
                    mention_author=False,
                )
                return
            expected_badge_role_ids[reward_object["item_id"]] = role.id
            if role in winner.roles:
                owned_badge_count += 1
            elif not role_can_be_managed(role):
                await message.reply(
                    "La respuesta es correcta, pero el bot no puede asignar una "
                    "insignia del premio. El evento sigue activo; avisa a un "
                    "administrador.",
                    mention_author=False,
                )
                return
            else:
                badge_roles_to_add.append(role)

        added_badge_roles: list[discord.Role] = []
        for role in badge_roles_to_add:
            try:
                await winner.add_roles(
                    role,
                    reason=f"Premio del evento: {event['question']}",
                )
                added_badge_roles.append(role)
            except discord.HTTPException:
                for added_role in added_badge_roles:
                    try:
                        await winner.remove_roles(
                            added_role,
                            reason="No se pudieron entregar todos los premios del evento",
                        )
                    except discord.HTTPException:
                        traceback.print_exc()
                await message.reply(
                    "La respuesta es correcta, pero no pude entregar todas las "
                    "insignias. El evento sigue activo; avisa a un administrador.",
                    mention_author=False,
                )
                return

        try:
            result = await bot.db.claim_question_event(
                message.guild.id,
                message.channel.id,
                event["answer_hash"],
                event["message_id"],
                winner.id,
                expected_badge_role_ids,
                winner_role_ids_before_reward,
            )
        except Exception:
            for added_badge_role in added_badge_roles:
                try:
                    await winner.remove_roles(
                        added_badge_role,
                        reason="La entrega del premio del evento no pudo confirmarse",
                    )
                except discord.HTTPException:
                    traceback.print_exc()
            raise

        if result is None or result.get("status") != "claimed":
            for added_badge_role in added_badge_roles:
                try:
                    await winner.remove_roles(
                        added_badge_role,
                        reason="El evento ya no estaba disponible",
                    )
                except discord.HTTPException:
                    traceback.print_exc()
            if result is None:
                bot.remove_question_event(*key)
            else:
                await message.reply(
                    "La respuesta es correcta, pero el objeto configurado como premio "
                    "ya no está disponible. El evento sigue activo; avisa a un "
                    "administrador.",
                    mention_author=False,
                )
            return

        bot.remove_question_event(*key)
        await finalize_question_embed(
            message.guild,
            result,
            f"✅ Ganado por {message.author.mention}.",
        )
        reward_text = question_reward_text(
            message.guild,
            result["awarded_reward"],
            result["reward_objects"],
        )
        result_details = []
        bonus_detail = None
        if result["awarded_reward"] > result["reward"]:
            multiplier_label = format_multiplier(
                result["whitelist_multiplier_percent"]
            )
            if result["whitelist_source_type"] == "role":
                source_role = message.guild.get_role(
                    result["whitelist_source_id"]
                )
                source_text = (
                    f"por tener el rol {source_role.mention} en la whitelist"
                    if source_role is not None
                    else "por un rol configurado en la whitelist"
                )
            else:
                source_text = "por estar añadido directamente a la whitelist"
            bonus_detail = (
                f"✨ Recibió **{money(result['awarded_reward'], message.guild.id)}** "
                f"en vez de **{money(result['reward'], message.guild.id)}** gracias "
                f"al multiplicador **{multiplier_label}** {source_text}."
            )
            result_details.append(bonus_detail)
        if result["new_balance"] is not None:
            result_details.append(
                "Su nuevo balance es "
                f"**{money(result['new_balance'], message.guild.id)}**."
            )
        if result["new_quantities"]:
            result_details.append(
                "Los modificadores o tickets ya están en su inventario."
            )
        if added_badge_roles:
            result_details.append(
                "Las insignias nuevas ya fueron añadidas a sus roles."
            )
        if owned_badge_count:
            result_details.append(
                "Las insignias que ya tenía se conservaron sin duplicarse."
            )
        result_detail = " ".join(result_details)
        await message.channel.send(
            f"🎉 {message.author.mention} respondió correctamente y ganó:\n"
            f"{reward_text}\n{result_detail}",
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=True,
                roles=False,
            ),
        )
        await send_audit_log(
            message.guild,
            "Evento de pregunta ganado",
            f"**Ganador:** {message.author.mention}\n"
            f"**Pregunta:** {result['question']}\n"
            f"**Respuesta correcta:** {result['answer_text']}\n"
            f"**Recompensa:** {reward_text}\n"
            + (
                f"**Bono de whitelist:** {bonus_detail}\n"
                if bonus_detail is not None
                else ""
            )
            + f"**Canal:** {message.channel.mention}",
            color=0x22C55E,
        )


@bot.event
async def on_ready() -> None:
    if bot.user:
        print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None or not hasattr(bot, "db"):
        return
    try:
        await process_question_reply(message)
    except (OSError, asyncpg.PostgresError, discord.HTTPException):
        traceback.print_exc()

    channel_key = (message.guild.id, message.channel.id)
    user_key = (message.guild.id, message.author.id)
    if (
        channel_key not in bot.active_modifier_channels
        and user_key not in bot.active_modifier_users
    ):
        await bot.process_commands(message)
        return
    try:
        modifier_result = await bot.db.try_trigger_message_modifier(
            message.guild.id,
            message.channel.id,
            message.author.id,
        )
        if modifier_result["channel_active"]:
            bot.active_modifier_channels.add(channel_key)
        else:
            bot.active_modifier_channels.discard(channel_key)
            if modifier_result["individual_active"]:
                bot.active_modifier_users.add(user_key)
            else:
                bot.active_modifier_users.discard(user_key)
        if modifier_result["messages"]:
            await send_modifier_webhook_message(
                message,
                random.choice(list(modifier_result["messages"])),
            )
    except (OSError, asyncpg.PostgresError):
        traceback.print_exc()
    await bot.process_commands(message)


@bot.tree.command(name="say", description="Envía como bot un mensaje escrito por un administrador.")
@app_commands.describe(mensaje="El mensaje que quieres que diga el bot")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def say(interaction: discord.Interaction, mensaje: str) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.channel is None:
        await interaction.edit_original_response(
            content="No pude encontrar el canal donde enviar el mensaje."
        )
        return
    await interaction.channel.send(
        mensaje,
        allowed_mentions=discord.AllowedMentions.all(),
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Mensaje enviado con /say",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Canal:** {interaction.channel.mention}\n"
            f"**Mensaje:** {mensaje[:1000]}",
        )
    await interaction.delete_original_response()


@bot.tree.command(
    name="eventopregunta",
    description="Crea una pregunta con monedas y hasta tres objetos de recompensa.",
)
@app_commands.describe(
    pregunta="Pregunta o texto que se publicará",
    respuesta="Respuesta correcta; se mostrará cuando termine el evento",
    minutos="Minutos antes de que el evento caduque",
    recompensa="Monedas para el ganador; se pueden combinar con objetos",
    objeto="Primer objeto opcional",
    cantidad="Unidades del primer objeto; las insignias siempre usan 1",
    objeto2="Segundo objeto opcional",
    cantidad2="Unidades del segundo objeto; las insignias siempre usan 1",
    objeto3="Tercer objeto opcional",
    cantidad3="Unidades del tercer objeto; las insignias siempre usan 1",
)
@app_commands.autocomplete(
    objeto=deletable_object_autocomplete,
    objeto2=deletable_object_autocomplete,
    objeto3=deletable_object_autocomplete,
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def eventopregunta(
    interaction: discord.Interaction,
    pregunta: app_commands.Range[str, 1, 1000],
    respuesta: app_commands.Range[str, 1, 200],
    minutos: app_commands.Range[int, 1, 1440],
    recompensa: app_commands.Range[int, 1, MAX_MONEY] | None = None,
    objeto: str | None = None,
    cantidad: app_commands.Range[int, 1, 1000] = 1,
    objeto2: str | None = None,
    cantidad2: app_commands.Range[int, 1, 1000] = 1,
    objeto3: str | None = None,
    cantidad3: app_commands.Range[int, 1, 1000] = 1,
) -> None:
    assert interaction.guild_id is not None and interaction.channel_id is not None
    guild = interaction.guild
    if guild is None:
        return
    clean_answer = " ".join(respuesta.split())
    if not clean_answer:
        await answer(interaction, "La respuesta no puede estar vacía.", ephemeral=True)
        return
    object_slots = [
        (objeto.strip() if objeto and objeto.strip() else None, cantidad),
        (objeto2.strip() if objeto2 and objeto2.strip() else None, cantidad2),
        (objeto3.strip() if objeto3 and objeto3.strip() else None, cantidad3),
    ]
    if recompensa is None and not any(name for name, _ in object_slots):
        await answer(
            interaction,
            "Añade al menos una recompensa: monedas, un objeto o ambos.",
            ephemeral=True,
        )
        return
    if object_slots[1][0] and not object_slots[0][0]:
        await answer(
            interaction,
            "Para usar `objeto2`, primero completa `objeto`.",
            ephemeral=True,
        )
        return
    if object_slots[2][0] and not object_slots[1][0]:
        await answer(
            interaction,
            "Para usar `objeto3`, primero completa `objeto2`.",
            ephemeral=True,
        )
        return
    for position, (object_name, quantity) in enumerate(object_slots, start=1):
        if object_name is None and quantity != 1:
            await answer(
                interaction,
                f"`cantidad{'' if position == 1 else position}` solo se usa si "
                f"también eliges `objeto{'' if position == 1 else position}`.",
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True)
    reward_amount = recompensa or 0
    selected_slots = [
        (name, quantity)
        for name, quantity in object_slots
        if name is not None
    ]
    configured_objects = await asyncio.gather(
        *(
            bot.db.get_configured_object(
                interaction.guild_id,
                normalize_name(name),
            )
            for name, _ in selected_slots
        )
    )
    reward_objects = []
    seen_objects: set[tuple[str, int]] = set()
    for (requested_name, quantity), configured_object in zip(
        selected_slots,
        configured_objects,
    ):
        if configured_object is None:
            await answer(
                interaction,
                f"El objeto **{requested_name}** no existe.",
                ephemeral=True,
            )
            return
        item_type = configured_object["item_type"]
        object_key = (item_type, configured_object["id"])
        if object_key in seen_objects:
            await answer(
                interaction,
                f"El objeto **{configured_object['name']}** está repetido.",
                ephemeral=True,
            )
            return
        seen_objects.add(object_key)
        if item_type == "badge":
            if quantity != 1:
                await answer(
                    interaction,
                    f"La insignia **{configured_object['name']}** es única; "
                    "usa cantidad **1**.",
                    ephemeral=True,
                )
                return
            badge_role = guild.get_role(configured_object["badge_role_id"])
            if badge_role is None or not role_can_be_managed(badge_role):
                await answer(
                    interaction,
                    f"No puedo usar la insignia **{configured_object['name']}** "
                    "como premio. Comprueba que su rol exista y esté debajo del "
                    "rol del bot.",
                    ephemeral=True,
                )
                return
        reward_objects.append(
            {
                "item_type": item_type,
                "item_id": configured_object["id"],
                "quantity": quantity,
                "name": configured_object["name"],
                "emoji": configured_object["emoji"],
            }
        )

    expires_at = await bot.db.create_question_event(
        interaction.guild_id,
        interaction.channel_id,
        pregunta.strip(),
        answer_hash(clean_answer),
        clean_answer,
        reward_amount,
        reward_objects,
        minutos,
        interaction.user.id,
    )
    if expires_at is None:
        await interaction.edit_original_response(
            content=(
            "Ya hay un evento de pregunta activo en este canal. "
            "Usa `/cancelarevento` antes de crear otro."
            ),
        )
        return

    reward_text = question_reward_text(
        guild,
        reward_amount,
        reward_objects,
    )
    embed = make_question_embed(
        guild,
        pregunta.strip(),
        reward_text,
        expires_at=expires_at,
    )
    if interaction.channel is None:
        await interaction.edit_original_response(
            content="No pude encontrar el canal donde publicar el evento."
        )
        await bot.db.cancel_question_event(interaction.guild_id, interaction.channel_id)
        return
    try:
        event_message = await interaction.channel.send(embed=embed)
    except discord.HTTPException:
        await bot.db.cancel_question_event(interaction.guild_id, interaction.channel_id)
        await interaction.edit_original_response(
            content="No pude publicar el evento en este canal. Revisa mis permisos."
        )
        return
    saved = await bot.db.set_question_event_message(
        interaction.guild_id,
        interaction.channel_id,
        event_message.id,
    )
    if not saved:
        await event_message.delete()
        await bot.db.cancel_question_event(
            interaction.guild_id,
            interaction.channel_id,
        )
        await interaction.edit_original_response(
            content="No pude guardar el mensaje del evento. Intenta nuevamente."
        )
        return
    bot.cache_question_event(
        {
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "message_id": event_message.id,
            "question": pregunta.strip(),
            "answer_hash": answer_hash(clean_answer),
            "answer_text": clean_answer,
            "reward": reward_amount,
            "reward_objects": [
                {"position": position, **reward_object}
                for position, reward_object in enumerate(reward_objects, start=1)
            ],
            "expires_at": expires_at,
            "created_by": interaction.user.id,
        }
    )
    await interaction.delete_original_response()

    movement_amount = (
        reward_amount
        if reward_amount > 0
        else sum(reward_object["quantity"] for reward_object in reward_objects)
    )
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "event_create",
        movement_amount,
        f"Creó un evento de pregunta con premio {reward_text}: "
        f"{pregunta.strip()}",
    )
    await send_audit_log(
        guild,
        "Evento de pregunta creado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Pregunta:** {pregunta.strip()}\n"
        f"**Recompensa:** {reward_text}\n"
        f"**Duración:** {minutos} minutos\n"
        f"**Canal:** <#{interaction.channel_id}>",
        color=0xEC4899,
    )


@bot.tree.command(name="cancelarevento", description="Cancela la pregunta activa de este canal.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cancelarevento(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None and interaction.channel_id is not None
    event = await bot.db.cancel_question_event(
        interaction.guild_id,
        interaction.channel_id,
    )
    if event is None:
        await answer(interaction, "No hay un evento de pregunta activo en este canal.")
        return
    bot.remove_question_event(
        interaction.guild_id,
        interaction.channel_id,
        event["message_id"],
    )
    if interaction.guild is not None:
        await finalize_question_embed(
            interaction.guild,
            {
                **dict(event),
                "guild_id": interaction.guild_id,
                "channel_id": interaction.channel_id,
            },
            "🚫 Cancelado por un administrador.",
        )
    await answer(
        interaction,
        f"Cancelé el evento **{event['question']}**.\n"
        "**Respuesta correcta:** "
        f"{discord.utils.escape_mentions(event['answer_text'] or 'No disponible')}",
    )
    movement_amount = question_event_movement_amount(event)
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "event_cancel",
        movement_amount,
        f"Canceló el evento de pregunta: {event['question']}",
    )
    if interaction.guild is not None:
        reward_text = question_event_reward_text(interaction.guild, event)
        await send_audit_log(
            interaction.guild,
            "Evento de pregunta cancelado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Pregunta:** {event['question']}\n"
            f"**Respuesta correcta:** {event['answer_text'] or 'No disponible'}\n"
            f"**Recompensa:** {reward_text}\n"
            f"**Canal:** <#{interaction.channel_id}>",
            color=0xEF4444,
        )


async def activate_owned_modifier(
    interaction: discord.Interaction,
    modifier_name_key: str,
    target_user_id: int,
) -> tuple[bool, str]:
    owner = guild_member(interaction)
    guild = interaction.guild
    if owner is None or guild is None or interaction.channel_id is None:
        return False, "No pude identificar el miembro, servidor o canal actual."
    target = guild.get_member(target_user_id)
    if target is None:
        try:
            target = await guild.fetch_member(target_user_id)
        except (discord.HTTPException, discord.NotFound):
            return False, "El miembro elegido ya no está disponible en el servidor."
    if target.bot:
        return False, "No puedes aplicar un modificador a un bot."
    result = await bot.db.activate_modifier(
        guild.id,
        owner.id,
        target.id,
        modifier_name_key,
        interaction.channel_id,
        owner.guild_permissions.administrator,
    )
    if result["status"] == "disabled":
        return False, disabled_object_section_text(
            result.get("section", "modifiers"),
            result.get("reason"),
        )
    if result["status"] == "missing":
        return False, "No tienes ese modificador en tu inventario."
    if result["status"] == "wrong_scope":
        return False, (
            "La configuración de ese modificador cambió. Vuelve a ejecutar `/usar`."
        )
    if result["status"] == "already_active":
        subject = "Ya tienes" if target.id == owner.id else f"{target.mention} ya tiene"
        return False, (
            f"{subject} activo **{result['name']}**. Termina "
            f"<t:{int(result['expires_at'].timestamp())}:R>. No se consumió otra unidad."
        )
    bot.active_modifier_users.add((guild.id, target.id))
    await bot.db.record_movement(
        guild.id,
        owner.id,
        owner.id,
        "modifier_use",
        None,
        f"Usó el modificador {result['name']} sobre {target} durante "
        f"{result['duration_minutes']} minutos.",
    )
    await send_audit_log(
        guild,
        "Modificador activado",
        f"**Propietario:** {owner.mention}\n"
        f"**Objetivo:** {target.mention}\n"
        f"**Modificador:** {result['name']}\n"
        f"**Restantes:** {result['quantity']}\n"
        f"**Duración:** {result['duration_minutes']} minutos\n"
        f"**Finaliza:** <t:{int(result['expires_at'].timestamp())}:R>",
        color=0xA855F7,
    )
    if target.id == owner.id:
        public_message = (
            f"{owner.mention} activó **{result['name']}** sobre sí mismo durante "
            f"**{result['duration_minutes']} minutos**. Le quedan **{result['quantity']}**."
        )
    else:
        public_message = (
            f"{owner.mention} usó **{result['name']}** sobre {target.mention} durante "
            f"**{result['duration_minutes']} minutos**. {owner.mention} tiene "
            f"**{result['quantity']}** unidades restantes."
        )
    return True, public_message


async def activate_owned_channel_modifier(
    interaction: discord.Interaction,
    modifier_name_key: str,
    target_channel_id: int,
) -> tuple[bool, str]:
    owner = guild_member(interaction)
    guild = interaction.guild
    if owner is None or guild is None:
        return False, "No pude identificar el miembro o servidor."
    target_channel = guild.get_channel(target_channel_id)
    if not isinstance(target_channel, discord.TextChannel):
        return False, "El canal elegido ya no está disponible."
    result = await bot.db.activate_channel_modifier(
        guild.id,
        owner.id,
        target_channel.id,
        modifier_name_key,
        owner.guild_permissions.administrator,
    )
    if result["status"] == "disabled":
        return False, disabled_object_section_text(
            result.get("section", "modifiers"),
            result.get("reason"),
        )
    if result["status"] == "missing":
        return False, "No tienes ese modificador en tu inventario."
    if result["status"] == "wrong_scope":
        return False, (
            "La configuración de ese modificador cambió. Vuelve a ejecutar `/usar`."
        )
    if result["status"] == "already_active":
        return False, (
            f"{target_channel.mention} ya tiene activo **{result['name']}**. Termina "
            f"<t:{int(result['expires_at'].timestamp())}:R>. No se consumió otra unidad."
        )
    bot.active_modifier_channels.add((guild.id, target_channel.id))
    await bot.db.record_movement(
        guild.id,
        owner.id,
        owner.id,
        "channel_modifier_use",
        None,
        f"Usó el modificador {result['name']} en {target_channel} durante "
        f"{result['duration_minutes']} minutos.",
    )
    await send_audit_log(
        guild,
        "Modificador de canal activado",
        f"**Propietario:** {owner.mention}\n"
        f"**Canal:** {target_channel.mention}\n"
        f"**Modificador:** {result['name']}\n"
        f"**Restantes:** {result['quantity']}\n"
        f"**Duración:** {result['duration_minutes']} minutos\n"
        f"**Finaliza:** <t:{int(result['expires_at'].timestamp())}:R>",
        color=0xA855F7,
    )
    return True, (
        f"{owner.mention} activó **{result['name']}** en {target_channel.mention} "
        f"durante **{result['duration_minutes']} minutos**. Afectará a cualquiera "
        f"que escriba allí y quedan **{result['quantity']}** unidades."
    )


class ModifierUseConfirmView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        modifier_name: str,
        modifier_name_key: str,
        target_kind: str,
        target_id: int,
        source_interaction: discord.Interaction,
    ) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.modifier_name = modifier_name
        self.modifier_name_key = modifier_name_key
        self.target_kind = target_kind
        self.target_id = target_id
        self.source_interaction = source_interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo la persona que abrió esta confirmación puede responder.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.source_interaction.edit_original_response(
                content="La confirmación expiró. No se consumió el modificador.",
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        await answer(
            interaction,
            "Ocurrió un error al activar el modificador.",
            ephemeral=True,
        )

    @discord.ui.button(label="Usar modificador", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Activando **{self.modifier_name}**...",
            view=self,
        )
        if self.target_kind == "channel":
            activated, result = await activate_owned_channel_modifier(
                interaction,
                self.modifier_name_key,
                self.target_id,
            )
        else:
            activated, result = await activate_owned_modifier(
                interaction,
                self.modifier_name_key,
                self.target_id,
            )
        if not activated:
            await interaction.edit_original_response(content=result, view=None)
            self.stop()
            return
        announcement_channel = interaction.channel
        if self.target_kind == "channel" and interaction.guild is not None:
            announcement_channel = interaction.guild.get_channel(self.target_id)
        if announcement_channel is None:
            await interaction.edit_original_response(
                content=(
                    f"{result}\nEl modificador sí se activó, pero no pude encontrar "
                    "el canal donde debía publicar la confirmación."
                ),
                view=None,
            )
        else:
            try:
                await announcement_channel.send(
                    result,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=True,
                        roles=False,
                    ),
                )
            except discord.HTTPException:
                await interaction.edit_original_response(
                    content=(
                        f"{result}\nEl modificador sí se activó, pero no pude publicar "
                        "la confirmación en el canal objetivo."
                    ),
                    view=None,
                )
            else:
                try:
                    await interaction.delete_original_response()
                except discord.HTTPException:
                    await interaction.edit_original_response(
                        content="Modificador activado y anunciado en el canal.",
                        view=None,
                    )
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Uso cancelado. No se consumió el modificador.",
            view=None,
        )
        self.stop()


@bot.tree.command(name="usar", description="Activa una insignia o consume un modificador o ticket.")
@app_commands.describe(
    objeto="Nombre de la insignia, modificador o ticket que quieres usar",
    miembro="Objetivo de un modificador Individual; por defecto eres tú",
    canal="Canal donde activarás un modificador de tipo Canal",
)
@app_commands.autocomplete(objeto=owned_object_autocomplete)
@app_commands.guild_only()
async def usar(
    interaction: discord.Interaction,
    objeto: str,
    miembro: discord.Member | None = None,
    canal: discord.TextChannel | None = None,
) -> None:
    member = guild_member(interaction)
    guild = interaction.guild
    if member is None or guild is None:
        return
    badge = await find_badge(interaction, objeto)
    if badge is None:
        modifier = await find_modifier(interaction, objeto)
        if modifier is None:
            if miembro is not None or canal is not None:
                await answer(
                    interaction,
                    "Las opciones miembro y canal solo se pueden usar con modificadores.",
                )
                return
            ticket = await find_ticket(interaction, objeto)
            if ticket is None:
                await answer(interaction, "Ese objeto no existe.")
                return
            disabled_message = await object_section_disabled_message(
                interaction,
                "tickets",
            )
            if disabled_message is not None:
                await answer(interaction, disabled_message)
                return
            if not ticket["active"]:
                await answer(
                    interaction,
                    f"El ticket **{ticket['name']}** está inactivo actualmente. "
                    "Sigue en tu inventario y podrás usarlo cuando vuelva a activarse.",
                )
                return
            settings = await bot.get_log_settings_cached(guild.id)
            if (
                settings is None
                or not settings["logs_enabled"]
                or settings["log_channel_id"] is None
            ):
                await answer(
                    interaction,
                    "No puedo usar ese ticket porque el canal de registros no está "
                    "configurado o está desactivado. Avísale a un administrador.",
                )
                return
            log_channel = guild.get_channel(settings["log_channel_id"])
            if not isinstance(log_channel, discord.TextChannel):
                await answer(
                    interaction,
                    "No puedo encontrar el canal de registros. Avísale a un administrador.",
                )
                return
            bot_member = guild.me
            if bot_member is None:
                await answer(interaction, "No pude comprobar mis permisos en el canal de registros.")
                return
            permissions = log_channel.permissions_for(bot_member)
            if not permissions.send_messages or not permissions.embed_links:
                await answer(
                    interaction,
                    "No puedo usar ese ticket porque me falta **Enviar mensajes** o "
                    "**Insertar enlaces** en el canal de registros.",
                )
                return
            admin_rows = await bot.db.list_ticket_admins(guild.id)
            ticket_admins = [
                admin
                for row in admin_rows
                if (admin := guild.get_member(row["user_id"])) is not None
                and not admin.bot
            ]
            if not ticket_admins:
                await answer(
                    interaction,
                    "No puedo usar ese ticket porque no hay miembros activos en la "
                    "lista de administradores de tickets. Avísale a un administrador.",
                )
                return

            await interaction.response.defer()
            result = await bot.db.consume_ticket(
                guild.id,
                member.id,
                ticket["name_key"],
                member.guild_permissions.administrator,
            )
            if result is not None and result.get("status") == "disabled":
                await answer(
                    interaction,
                    disabled_object_section_text(
                        result.get("section", "tickets"),
                        result.get("reason"),
                    ),
                )
                return
            if result is None:
                disabled_message = await object_section_disabled_message(
                    interaction,
                    "tickets",
                )
                if disabled_message is not None:
                    await answer(interaction, disabled_message)
                    return
                latest_ticket = await find_ticket(interaction, objeto)
                if latest_ticket is not None and not latest_ticket["active"]:
                    await answer(
                        interaction,
                        f"El ticket **{latest_ticket['name']}** acaba de ser desactivado. "
                        "No se consumió ninguna unidad.",
                    )
                else:
                    await answer(interaction, "No tienes ese ticket en tu inventario.")
                return
            alert_embed = discord.Embed(
                title="🎟️ Ticket utilizado",
                description=result["description"],
                color=0x06B6D4,
                timestamp=datetime.now(timezone.utc),
            )
            alert_embed.add_field(name="Miembro", value=member.mention)
            alert_embed.add_field(name="Ticket", value=result["name"])
            alert_embed.add_field(
                name="Canal donde se usó",
                value=f"<#{interaction.channel_id}>",
                inline=False,
            )
            alert_embed.add_field(name="Unidades restantes", value=str(result["quantity"]))
            alert_embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await log_channel.send(
                    " ".join(admin.mention for admin in ticket_admins),
                    embed=alert_embed,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=True,
                        roles=False,
                    ),
                )
            except discord.HTTPException:
                await bot.db.add_ticket_inventory(
                    guild.id,
                    member.id,
                    result["id"],
                    1,
                )
                await answer(
                    interaction,
                    "No pude avisar al equipo administrativo, así que devolví el ticket "
                    "a tu inventario. Avísale a un administrador.",
                )
                return
            await bot.db.record_movement(
                guild.id,
                member.id,
                member.id,
                "ticket_use",
                None,
                f"Usó el ticket {result['name']}. Restantes: {result['quantity']}.",
            )
            await answer(
                interaction,
                f"Usaste **{result['name']}**. El equipo de administración ya fue "
                f"contactado. Te quedan **{result['quantity']}**.",
            )
            return
        disabled_message = await object_section_disabled_message(
            interaction,
            "modifiers",
        )
        if disabled_message is not None:
            await answer(interaction, disabled_message)
            return
        channel_warning = ""
        if modifier["effect_scope"] == "channel":
            if miembro is not None:
                await answer(
                    interaction,
                    "Este modificador es de **Canal**; usa la opción **canal**, no miembro.",
                )
                return
            if canal is None:
                await answer(
                    interaction,
                    "Este modificador es de **Canal**. Debes seleccionar dónde activarlo.",
                )
                return
            bot_member = guild.me
            if bot_member is None:
                await answer(interaction, "No pude comprobar mis permisos en ese canal.")
                return
            permissions = canal.permissions_for(bot_member)
            if not permissions.send_messages or not permissions.manage_webhooks:
                await answer(
                    interaction,
                    f"Necesito **Enviar mensajes** y **Gestionar webhooks** en "
                    f"{canal.mention}.",
                )
                return
            target_kind = "channel"
            target_id = canal.id
            target_text = f"en {canal.mention}"
        else:
            if canal is not None:
                await answer(
                    interaction,
                    "Este modificador es **Individual**; usa miembro o déjalo vacío "
                    "para aplicártelo a ti.",
                )
                return
            target = miembro or member
            if target.bot:
                await answer(interaction, "No puedes aplicar un modificador a un bot.")
                return
            target_kind = "individual"
            target_id = target.id
            target_text = (
                "sobre ti" if target.id == member.id else f"sobre {target.mention}"
            )
            active_channel_modifiers = await bot.db.list_active_channel_modifiers(
                guild.id
            )
            channel_mentions = [
                f"<#{row['channel_id']}>"
                for row in active_channel_modifiers[:10]
            ]
            if channel_mentions:
                remaining = len(active_channel_modifiers) - len(channel_mentions)
                extra = f" y **{remaining} más**" if remaining > 0 else ""
                channel_warning = (
                    "\n\n⚠️ **Aviso:** hay modificadores de Canal activos en "
                    f"{', '.join(channel_mentions)}{extra}. Mientras sigan activos, "
                    "el modificador Individual no funcionará cuando su objetivo "
                    "escriba en esos canales."
                )
        view = ModifierUseConfirmView(
            member.id,
            modifier["name"],
            modifier["name_key"],
            target_kind,
            target_id,
            interaction,
        )
        await interaction.response.send_message(
            f"¿Seguro que quieres usar **{modifier['name']}** {target_text}? Consumirá "
            f"**1 unidad** y permanecerá activo durante "
            f"**{modifier['duration_minutes']} minutos**."
            f"{channel_warning if target_kind == 'individual' else ''}",
            view=view,
            ephemeral=True,
        )
        return
    disabled_message = await object_section_disabled_message(
        interaction,
        "badges",
    )
    if disabled_message is not None:
        await answer(interaction, disabled_message)
        return
    if miembro is not None or canal is not None:
        await answer(
            interaction,
            "Las opciones miembro y canal solo se pueden usar con modificadores.",
        )
        return
    badge_role = guild.get_role(badge["badge_role_id"])
    color_role = guild.get_role(badge["color_role_id"])
    if badge_role is None or color_role is None:
        await answer(interaction, "La configuración de esa insignia tiene un rol eliminado.")
        return
    has_badge = badge_role in member.roles
    whitelist_access = (
        badge["whitelist_enabled"] and await member_is_whitelisted(member)
    )
    if not has_badge and not whitelist_access:
        await answer(
            interaction,
            "No tienes esa insignia ni acceso para usarla mediante la whitelist.",
        )
        return
    whitelist_use_prefix = None
    if not has_badge and whitelist_access:
        whitelist_use_prefix, _ = await whitelist_access_source(member)
    if not role_can_be_managed(color_role):
        await answer(interaction, "No puedo asignar ese color. Coloca su rol debajo del rol del bot.")
        return

    await interaction.response.defer()
    rows = await bot.db.list_badges(guild.id)
    color_ids = {row["color_role_id"] for row in rows}
    old_colors = [
        role for role in member.roles
        if role.id in color_ids and role.id != color_role.id
    ]
    if old_colors:
        await member.remove_roles(*old_colors, reason="Cambio de color de insignia")
    if color_role not in member.roles:
        await member.add_roles(color_role, reason=f"Insignia activada: {badge['name']}")
    await send_audit_log(
        guild,
        "Color de insignia activado",
        f"{member.mention} activó **{badge['name']}** ({color_role.mention})."
        + (
            f"\n**Acceso:** {whitelist_use_prefix}."
            if whitelist_use_prefix is not None
            else ""
        ),
        color=0x8B5CF6,
    )
    if whitelist_use_prefix is not None:
        await answer(
            interaction,
            f"{whitelist_use_prefix}, activaste el color de **{badge['name']}**.",
        )
    else:
        await answer(interaction, f"Activaste el color de **{badge['name']}**.")


@bot.tree.command(name="quitar", description="Quita tu color activo sin borrar tus insignias.")
@app_commands.guild_only()
async def quitar(interaction: discord.Interaction) -> None:
    member = guild_member(interaction)
    if member is None or interaction.guild_id is None:
        return
    disabled_message = await object_section_disabled_message(
        interaction,
        "badges",
    )
    if disabled_message is not None:
        await answer(interaction, disabled_message)
        return
    rows = await bot.db.list_badges(interaction.guild_id)
    color_ids = {row["color_role_id"] for row in rows}
    roles = [role for role in member.roles if role.id in color_ids]
    if not roles:
        await answer(interaction, "No tienes ningún rol de color activo.")
        return
    await interaction.response.defer()
    await member.remove_roles(*roles, reason="El usuario quitó su color")
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Rol de color retirado",
            f"{member.mention} retiró sus roles de color: "
            + ", ".join(role.mention for role in roles),
            color=0x6B7280,
        )
    await answer(interaction, "Quité tu rol de color. Tus insignias siguen intactas.")


@bot.tree.command(name="inventario", description="Muestra tus insignias, modificadores y tickets.")
@app_commands.describe(miembro="Inventario de otro miembro; solo para administradores")
@app_commands.guild_only()
async def inventario(
    interaction: discord.Interaction,
    miembro: discord.Member | None = None,
) -> None:
    requester = guild_member(interaction)
    guild = interaction.guild
    if requester is None or guild is None:
        return
    if miembro is not None and not requester.guild_permissions.administrator:
        await answer(
            interaction,
            "Solo los administradores pueden consultar el inventario de otro miembro.",
        )
        return
    member = miembro or requester
    rows = await bot.db.list_badges(guild.id)
    owned = [row for row in rows if guild.get_role(row["badge_role_id"]) in member.roles]
    whitelisted = await member_is_whitelisted(member)
    whitelist_badges = [
        row
        for row in rows
        if whitelisted
        and row["whitelist_enabled"]
        and guild.get_role(row["badge_role_id"]) not in member.roles
    ]
    owned_keys = {row["name_key"] for row in owned}
    whitelist_keys = {row["name_key"] for row in whitelist_badges}
    available_badges = [
        row
        for row in rows
        if row["name_key"] in owned_keys or row["name_key"] in whitelist_keys
    ]
    modifiers = await bot.db.list_modifier_inventory(guild.id, member.id)
    tickets = await bot.db.list_ticket_inventory(guild.id, member.id)
    embed = discord.Embed(title=f"Inventario de {member.display_name}", color=0x8B5CF6)
    embed.set_thumbnail(url=member.display_avatar.url)
    sections = []
    if available_badges:
        badge_lines = "\n".join(
            f"• "
            f"{f'{whitelist_marker(guild.id)} ' if row['name_key'] in whitelist_keys else ''}"
            f"{badge_emoji(row['emoji'], guild)}**{row['name']}** "
            f"(<@&{row['color_role_id']}>)"
            for row in available_badges
        )
        sections.append(f"__**Insignias**__\n{badge_lines}")
    if modifiers:
        modifier_lines = "\n".join(
            f"• {badge_emoji(row['emoji'], guild)}**{row['name']}** "
            f"× **{row['quantity']}** · {modifier_scope_label(row['effect_scope'])} · "
            f"{row['duration_minutes']} min"
            for row in modifiers
        )
        sections.append(f"__**Modificadores consumibles**__\n{modifier_lines}")
    if tickets:
        ticket_lines = "\n".join(
            f"• {badge_emoji(row['emoji'], guild)}**{row['name']}** "
            f"× **{row['quantity']}**"
            f"{' · **Inactivo**' if not row['active'] else ''}\n  {row['description']}"
            for row in tickets
        )
        sections.append(f"__**Tickets consumibles**__\n{ticket_lines}")
    if sections:
        embed.description = "\n\n".join(sections)[:4000]
        footer_parts = []
        if member.id == requester.id:
            footer_parts.append("Usa /usar para activar una insignia o consumir un objeto.")
        if whitelist_badges:
            _, whitelist_footer_reason = await whitelist_access_source(member)
            footer_parts.append(
                f"{whitelist_marker(guild.id)} Los objetos con esta marca están disponibles "
                f"{whitelist_footer_reason}."
            )
        if footer_parts:
            embed.set_footer(text=" ".join(footer_parts)[:2048])
    else:
        embed.description = (
            "No tienes insignias, modificadores ni tickets. Puedes conseguir objetos en **/tienda** "
            "y consultar tus monedas con **/balance**."
            if member.id == requester.id
            else f"{member.mention} no tiene insignias, modificadores ni tickets."
        )
    await answer(interaction, embed=embed)


@bot.tree.command(name="objetos", description="Muestra a administradores toda la configuración de objetos.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def objetos(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    badges = await bot.db.list_badges(guild.id)
    modifiers = await bot.db.list_modifiers(guild.id)
    tickets = await bot.db.list_tickets(guild.id)

    embed = discord.Embed(title="Configuración de objetos", color=0x3B82F6)
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    sections = []
    if badges:
        badge_details = []
        for row in badges:
            sale = (
                f"Sí — {money(row['price'], guild.id)} · "
                f"Categoría: **{shop_section_name(row['shop_section'])}**"
                if row["purchasable"]
                else "No"
            )
            badge_details.append(
                f"• {badge_emoji(row['emoji'], guild)}**{row['name']}**\n"
                f"  Insignia: <@&{row['badge_role_id']}> · "
                f"Color: <@&{row['color_role_id']}> · Comprable: {sale} · "
                f"Whitelist: **{'Sí' if row['whitelist_enabled'] else 'No'}**"
            )
        sections.append("__**Insignias**__\n" + "\n".join(badge_details))
    if modifiers:
        modifier_details = []
        for row in modifiers:
            sale = (
                f"Sí — {money(row['price'], guild.id)} · "
                f"Categoría: **{shop_section_name(row['shop_section'])}**"
                if row["purchasable"]
                else "No"
            )
            modifier_details.append(
                f"• {badge_emoji(row['emoji'], guild)}**{row['name']}**\n"
                f"  Modificador {modifier_scope_label(row['effect_scope'])} consumible · "
                f"Comprable: {sale} · "
                f"Mensajes configurados: **{len(row['messages'])}**\n"
                f"  Probabilidad: **{modifier_probability_label(row['trigger_numerator'], row['trigger_denominator'])}** · "
                f"Cooldown: **{row['cooldown_seconds']} s** · "
                f"Duración: **{row['duration_minutes']} min**"
            )
        sections.append("__**Modificadores**__\n" + "\n".join(modifier_details))
    if tickets:
        ticket_details = []
        for row in tickets:
            sale = (
                f"Sí — {money(row['price'], guild.id)} · "
                f"Categoría: **{shop_section_name(row['shop_section'])}**"
                if row["purchasable"]
                else "No"
            )
            ticket_details.append(
                f"• {badge_emoji(row['emoji'], guild)}**{row['name']}**\n"
                f"  Ticket consumible · Comprable: {sale} · "
                f"Estado: **{'Activo' if row['active'] else 'Inactivo'}**\n"
                f"  Descripción: {row['description']}"
            )
        sections.append("__**Tickets**__\n" + "\n".join(ticket_details))
    embed.description = (
        "\n\n".join(sections)[:4000]
        if sections
        else "No hay objetos configurados en este servidor."
    )
    embed.set_footer(text="Usa /inventario miembro para consultar a un jugador.")
    await answer(interaction, embed=embed)


def modifier_message_pages(modifiers: list, guild: discord.Guild) -> list[str]:
    pages: list[str] = []
    current = ""
    max_length = 3900

    def append_block(block: str, continuation_header: str) -> None:
        nonlocal current
        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(block) <= max_length:
            current += separator + block
            return
        if current:
            pages.append(current)
        current = (
            f"{continuation_header}\n{block}"
            if continuation_header
            else block
        )

    for modifier in modifiers:
        header = f"__**{badge_emoji(modifier['emoji'], guild)}{modifier['name']}**__"
        continuation = (
            f"__**{badge_emoji(modifier['emoji'], guild)}"
            f"{modifier['name']} (continuación)**__"
        )
        messages = modifier["messages"]
        if not messages:
            append_block(f"{header}\n*Sin mensajes configurados.*", "")
            continue
        append_block(f"{header}\n**1.** {messages[0]}", "")
        for index, message in enumerate(messages[1:], start=2):
            append_block(f"**{index}.** {message}", continuation)
    if current:
        pages.append(current)
    return pages


@bot.tree.command(
    name="mensajes",
    description="Muestra los mensajes configurados de todos los modificadores.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def mensajes(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    modifiers = await bot.db.list_modifiers(guild.id)
    if not modifiers:
        embed = discord.Embed(
            title="Mensajes de modificadores",
            description="No hay modificadores configurados en este servidor.",
            color=0xA855F7,
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
        await answer(interaction, embed=embed)
        return

    pages = modifier_message_pages(list(modifiers), guild)
    await interaction.response.defer()
    for index, page in enumerate(pages, start=1):
        embed = discord.Embed(
            title=f"Mensajes de modificadores · {index}/{len(pages)}",
            description=page,
            color=0xA855F7,
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="tienda", description="Abre el Mercado de Sularea por categorías.")
@app_commands.guild_only()
async def tienda(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None and interaction.guild is not None
    member = guild_member(interaction)
    disabled_message = await object_section_disabled_message(
        interaction,
        "shop",
    )
    if disabled_message is not None:
        await answer(interaction, disabled_message)
        return
    rows, categories, discount_percent = await asyncio.gather(
        bot.db.list_shop_items(interaction.guild_id),
        bot.db.list_shop_categories(interaction.guild_id),
        (
            bot.db.get_whitelist_discount(
                interaction.guild_id,
                member.id,
                [role.id for role in member.roles],
            )
            if member is not None
            else asyncio.sleep(0, result=0)
        ),
    )
    if not rows and not categories:
        embed = discord.Embed(title="Mercado de Sularea", color=0xF59E0B)
        if interaction.guild.icon is not None:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.description = "No hay objetos a la venta en este momento."
        await answer(interaction, embed=embed)
        return

    sections = group_shop_badges(list(rows), list(categories))
    view = ShopView(
        interaction.guild,
        sections,
        interaction.user.id,
        discount_percent,
    )
    await interaction.response.send_message(
        embed=make_shop_embed(
            interaction.guild,
            sections,
            0,
            discount_percent,
        ),
        view=view,
    )
    view.message = await interaction.original_response()


@bot.tree.command(name="comprar", description="Compra una insignia, modificador o ticket disponible.")
@app_commands.describe(objeto="Nombre del objeto que quieres comprar")
@app_commands.autocomplete(objeto=shop_autocomplete)
@app_commands.guild_only()
async def comprar(interaction: discord.Interaction, objeto: str) -> None:
    await process_purchase(interaction, objeto)


@bot.tree.command(name="balance", description="Muestra tu balance de monedas.")
@app_commands.guild_only()
async def balance(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    value = await bot.db.get_balance(interaction.guild_id, interaction.user.id)
    await answer(
        interaction,
        f"Tu balance es **{money(value, interaction.guild_id)}**. Puedes conseguir dinero "
        "participando en los eventos de Sularea.",
    )


@bot.tree.command(name="historial", description="Muestra tus últimos 10 movimientos.")
@app_commands.guild_only()
async def historial(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    rows = await bot.db.get_history(interaction.guild_id, interaction.user.id, 10)
    embed = discord.Embed(title="Tus últimos movimientos", color=0x6366F1)
    if isinstance(interaction.user, discord.Member):
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
    if not rows:
        embed.description = (
            "Todavía no has tenido movimientos. Puedes conseguir dinero "
            "participando en los eventos de Sularea."
        )
        await answer(interaction, embed=embed)
        return
    icons = {
        "balance_add": "➕",
        "balance_remove": "➖",
        "balance_set": "⚙️",
        "purchase": "🛍️",
        "event_reward": "🎉",
        "event_object_reward": "🎁",
        "badge_grant": "🏅",
        "badge_remove": "🗑️",
        "modifier_purchase": "🧪",
        "modifier_use": "✨",
        "channel_modifier_use": "📢",
        "modifier_grant": "🎁",
        "modifier_remove": "➖",
        "modifier_expire": "⌛",
        "channel_modifier_expire": "⌛",
        "modifier_admin_activate": "✨",
        "modifier_admin_deactivate": "⛔",
        "ticket_purchase": "🎟️",
        "object_delete_refund": "💰",
        "ticket_use": "📣",
        "ticket_grant": "🎁",
        "ticket_remove": "➖",
    }
    embed.description = "\n".join(
        f"{icons.get(row['action'], '•')} {row['description']} "
        f"— <t:{int(row['created_at'].timestamp())}:R>"
        for row in rows
    )[:4000]
    await answer(interaction, embed=embed)


@bot.tree.command(name="ranking", description="Muestra los 10 balances más altos.")
@app_commands.guild_only()
async def ranking(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    rows = await bot.db.get_ranking(interaction.guild_id, 10)
    embed = discord.Embed(title="Ranking de monedas", color=0xF59E0B)
    if interaction.guild is not None and interaction.guild.icon is not None:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    if not rows:
        embed.description = "Nadie tiene monedas aún. ¡Participa en los eventos de Sularea para conseguirlas!"
    else:
        medals = ["🥇", "🥈", "🥉"]
        embed.description = "\n".join(
            f"{medals[index] if index < 3 else f'**{index + 1}.**'} "
            f"<@{row['user_id']}> — **{money(row['balance'], interaction.guild_id)}**"
            for index, row in enumerate(rows)
        )
    await answer(interaction, embed=embed)


@bot.tree.command(name="revisarbalance", description="Consulta el balance de un miembro.")
@app_commands.describe(miembro="Miembro cuyo balance quieres consultar")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def revisarbalance(
    interaction: discord.Interaction,
    miembro: discord.Member,
) -> None:
    assert interaction.guild_id is not None
    value = await bot.db.get_balance(interaction.guild_id, miembro.id)
    await answer(
        interaction,
        f"El balance de {miembro.mention} es **{money(value, interaction.guild_id)}**.",
    )


@bot.tree.command(name="configurarmoneda", description="Configura el emoji de la moneda.")
@app_commands.describe(
    emoji="Emoji, :nombre: o ID del servidor/bot; escribe quitar para restaurar",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarmoneda(
    interaction: discord.Interaction,
    emoji: str,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    emoji_value = edited_badge_emoji(emoji)
    final_emoji, emoji_error = await resolve_configured_emoji(guild, emoji_value)
    if emoji_error is not None:
        await answer(interaction, emoji_error)
        return
    old_emoji = bot.coin_emojis.get(interaction.guild_id, DEFAULT_COIN_EMOJI)
    await bot.db.set_coin_emoji(interaction.guild_id, final_emoji)
    if final_emoji is None:
        bot.coin_emojis.pop(interaction.guild_id, None)
        selected_emoji = DEFAULT_COIN_EMOJI
    else:
        bot.coin_emojis[interaction.guild_id] = final_emoji
        selected_emoji = final_emoji
    await bot.db.replace_coin_emoji_in_movements(
        interaction.guild_id,
        old_emoji,
        selected_emoji,
    )
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "coin_emoji_config",
        None,
        f"Configuró el emoji de moneda como {selected_emoji}.",
    )
    await send_audit_log(
        guild,
        "Emoji de moneda configurado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Emoji:** {selected_emoji}",
        color=0xF59E0B,
    )
    await answer(
        interaction,
        f"El emoji de la moneda ahora es {selected_emoji}. Ejemplo: "
        f"**{money(100, interaction.guild_id)}**.",
    )


@bot.tree.command(
    name="configuraremojiwhitelist",
    description="Configura el emoji que identifica el acceso por whitelist.",
)
@app_commands.describe(
    emoji="Emoji, :nombre: o ID del servidor/bot; escribe quitar para usar ⭐",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configuraremojiwhitelist(
    interaction: discord.Interaction,
    emoji: str,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    emoji_value = edited_badge_emoji(emoji)
    final_emoji, emoji_error = await resolve_configured_emoji(guild, emoji_value)
    if emoji_error is not None:
        await answer(interaction, emoji_error)
        return
    await bot.db.set_whitelist_emoji(interaction.guild_id, final_emoji)
    if final_emoji is None:
        bot.whitelist_emojis.pop(interaction.guild_id, None)
        selected_emoji = DEFAULT_WHITELIST_EMOJI
    else:
        bot.whitelist_emojis[interaction.guild_id] = final_emoji
        selected_emoji = final_emoji
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "whitelist_emoji_config",
        None,
        f"Configuró el emoji de whitelist como {selected_emoji}.",
    )
    await send_audit_log(
        guild,
        "Emoji de whitelist configurado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Emoji:** {selected_emoji}",
        color=0x22C55E,
    )
    await answer(
        interaction,
        f"El emoji de acceso por whitelist ahora es {selected_emoji}.",
    )


@bot.tree.command(
    name="configurardescuentowhitelist",
    description="Configura el descuento de tienda para la whitelist.",
)
@app_commands.describe(
    porcentaje="Descuento entre 0% y 100%; usa 0 para desactivarlo",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurardescuentowhitelist(
    interaction: discord.Interaction,
    porcentaje: app_commands.Range[int, 0, 100],
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    await bot.db.set_whitelist_discount(
        interaction.guild_id,
        porcentaje,
    )
    bot.invalidate_log_settings(interaction.guild_id)
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "whitelist_discount_config",
        None,
        f"Configuró el descuento de whitelist en {porcentaje}%.",
    )
    await send_audit_log(
        guild,
        "Descuento de whitelist configurado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Descuento:** {porcentaje}%\n"
        f"**Estado:** {'Activo' if porcentaje > 0 else 'Desactivado'}",
        color=0xF59E0B,
    )
    await answer(
        interaction,
        (
            f"El descuento de la whitelist ahora es de **{porcentaje}%**."
            if porcentaje > 0
            else "El descuento de la whitelist quedó **desactivado**."
        ),
    )


@bot.tree.command(
    name="configurarmultiplicadorwhitelist",
    description="Configura el multiplicador de monedas de eventos para la whitelist.",
)
@app_commands.describe(
    multiplicador="Acepta 1.5, 1,5, 1.5x, 3/2 o 150%",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarmultiplicadorwhitelist(
    interaction: discord.Interaction,
    multiplicador: app_commands.Range[str, 1, 20],
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    multiplier_percent, multiplier_error = parse_whitelist_multiplier(
        multiplicador
    )
    if multiplier_error is not None or multiplier_percent is None:
        await answer(
            interaction,
            multiplier_error or "Ese multiplicador no es válido.",
            ephemeral=True,
        )
        return
    multiplier_label = format_multiplier(multiplier_percent)
    await bot.db.set_whitelist_event_multiplier(
        interaction.guild_id,
        multiplier_percent,
    )
    bot.invalidate_log_settings(interaction.guild_id)
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "whitelist_event_multiplier_config",
        None,
        f"Configuró el multiplicador de eventos de whitelist en {multiplier_label}.",
    )
    await send_audit_log(
        guild,
        "Multiplicador de eventos configurado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Multiplicador:** {multiplier_label}\n"
        f"**Estado:** {'Activo' if multiplier_percent > 100 else 'Desactivado'}",
        color=0xEC4899,
    )
    await answer(
        interaction,
        (
            f"El multiplicador de monedas para la whitelist ahora es "
            f"**{multiplier_label}**."
            if multiplier_percent > 100
            else "El multiplicador de eventos quedó en **1×**, sin bono adicional."
        ),
    )


@bot.tree.command(name="configurarregistro", description="Configura el canal de movimientos.")
@app_commands.describe(
    activado="Activa o desactiva el registro de movimientos",
    canal="Canal donde se enviarán los registros",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarregistro(
    interaction: discord.Interaction,
    activado: bool,
    canal: discord.TextChannel | None = None,
) -> None:
    assert interaction.guild_id is not None and interaction.guild is not None
    current = await bot.get_log_settings_cached(interaction.guild_id)
    channel_id = canal.id if canal is not None else (
        current["log_channel_id"] if current else None
    )
    if activado and channel_id is None:
        await answer(interaction, "Selecciona un canal para activar los registros.")
        return
    selected_channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if activado and not isinstance(selected_channel, discord.TextChannel):
        await answer(interaction, "Selecciona un canal de texto válido para los registros.")
        return
    if activado and isinstance(selected_channel, discord.TextChannel):
        bot_member = interaction.guild.me
        permissions = selected_channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links:
            await answer(
                interaction,
                "Necesito **Enviar mensajes** e **Insertar enlaces** en ese canal.",
            )
            return
    await bot.db.set_log_settings(interaction.guild_id, channel_id, activado)
    bot.invalidate_log_settings(interaction.guild_id)
    if activado:
        await answer(
            interaction,
            f"Registro de movimientos activado en "
            f"{selected_channel.mention if selected_channel else 'el canal seleccionado'}.",
        )
        await send_audit_log(
            interaction.guild,
            "Registro de movimientos activado",
            f"{interaction.user.mention} activó los registros en <#{channel_id}>.",
            color=0x22C55E,
        )
    else:
        await answer(interaction, "Registro de movimientos desactivado.")


@bot.tree.command(
    name="configurarmensajeautomatico",
    description="Configura el canal y frecuencia de los mensajes automáticos.",
)
@app_commands.describe(
    activado="Activa o desactiva los mensajes automáticos",
    canal="Canal de texto; déjalo vacío para conservar el actual",
    minutos="Minutos entre mensajes; déjalo vacío para conservar el valor actual",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarmensajeautomatico(
    interaction: discord.Interaction,
    activado: bool,
    canal: discord.TextChannel | None = None,
    minutos: app_commands.Range[
        int,
        1,
        MAX_AUTOMATIC_INTERVAL_MINUTES,
    ] | None = None,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    current = await bot.db.get_automatic_message_settings(interaction.guild_id)
    if current is None and (canal is None or minutos is None):
        await answer(
            interaction,
            "La primera vez debes seleccionar **canal** y **minutos**.",
        )
        return
    channel_id = canal.id if canal is not None else current["channel_id"]
    interval_minutes = (
        minutos if minutos is not None else current["interval_minutes"]
    )
    selected_channel = guild.get_channel(channel_id)
    if activado and not isinstance(selected_channel, discord.TextChannel):
        await answer(
            interaction,
            "El canal configurado ya no existe. Selecciona otro canal de texto.",
        )
        return
    if isinstance(selected_channel, discord.TextChannel):
        bot_member = guild.me
        permissions = selected_channel.permissions_for(bot_member)
        if activado and (
            not permissions.view_channel
            or not permissions.send_messages
        ):
            await answer(
                interaction,
                "Necesito **Ver canal** y **Enviar mensajes** en el canal elegido.",
            )
            return

    await interaction.response.defer()
    settings = await bot.db.set_automatic_message_settings(
        interaction.guild_id,
        channel_id,
        interval_minutes,
        activado,
        interaction.user.id,
    )
    message_count = (
        current["message_count"]
        if current is not None
        else len(await bot.db.list_automatic_messages(interaction.guild_id))
    )
    state = "activó" if activado else "desactivó"
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "automatic_messages_config",
        None,
        (
            f"{state.capitalize()} los mensajes automáticos en el canal "
            f"{channel_id} cada {interval_minutes} minutos."
        ),
    )
    await send_audit_log(
        guild,
        "Mensajes automáticos configurados",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Estado:** {'Activo' if activado else 'Inactivo'}\n"
        f"**Canal:** <#{channel_id}>\n"
        f"**Intervalo:** {interval_minutes} minutos\n"
        f"**Mensajes configurados:** {message_count}",
        color=0x22C55E if activado else 0x6B7280,
    )
    next_send = (
        f"\n**Próximo envío:** <t:{int(settings['next_send_at'].timestamp())}:R>"
        if activado and message_count > 0
        else ""
    )
    warning = (
        "\n⚠️ Todavía no hay mensajes. Añade uno con "
        "`/editarmensajeautomatico`."
        if activado and message_count == 0
        else ""
    )
    await answer(
        interaction,
        f"Mensajes automáticos **{'activados' if activado else 'desactivados'}** "
        f"en <#{channel_id}> cada **{interval_minutes} minutos**."
        f"{next_send}{warning}",
    )


@bot.tree.command(
    name="editarmensajeautomatico",
    description="Añade, edita o quita un mensaje automático.",
)
@app_commands.describe(
    accion="Operación que se realizará",
    mensaje="Texto al añadir; mensaje existente al editar o quitar",
    nuevo_mensaje="Texto de reemplazo cuando la acción sea Editar",
)
@app_commands.choices(
    accion=[
        app_commands.Choice(name="Añadir", value="add"),
        app_commands.Choice(name="Editar", value="edit"),
        app_commands.Choice(name="Quitar", value="remove"),
    ],
)
@app_commands.autocomplete(mensaje=automatic_message_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def editarmensajeautomatico(
    interaction: discord.Interaction,
    accion: str,
    mensaje: app_commands.Range[str, 1, 2000],
    nuevo_mensaje: app_commands.Range[str, 1, 2000] | None = None,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return

    if accion == "add":
        if nuevo_mensaje is not None:
            await answer(
                interaction,
                "Para añadir, escribe el contenido solamente en **mensaje**.",
            )
            return
        content = mensaje.strip()
        if not content:
            await answer(interaction, "El mensaje no puede estar vacío.")
            return
        await interaction.response.defer()
        result = await bot.db.add_automatic_message(
            interaction.guild_id,
            content,
            interaction.user.id,
            MAX_AUTOMATIC_MESSAGES,
        )
        if result["status"] == "limit":
            await answer(
                interaction,
                f"Solo puedes configurar hasta **{MAX_AUTOMATIC_MESSAGES} mensajes**.",
            )
            return
        if result["status"] == "duplicate":
            await answer(interaction, "Ese mensaje ya está configurado.")
            return
        changed = result["message"]
        action_label = "añadido"
        movement_action = "automatic_message_add"
        response = (
            f"Añadí el mensaje automático **#{changed['id']}**. "
            "Se incluirá en la siguiente ronda aleatoria."
        )
    else:
        rows = await bot.db.list_automatic_messages(interaction.guild_id)
        selected = selected_automatic_message(rows, mensaje)
        if selected is None:
            await answer(
                interaction,
                "Ese mensaje automático no existe. Elígelo de la lista.",
            )
            return
        if accion == "edit":
            if nuevo_mensaje is None or not nuevo_mensaje.strip():
                await answer(
                    interaction,
                    "Para editar debes escribir **nuevo_mensaje**.",
                )
                return
            content = nuevo_mensaje.strip()
            await interaction.response.defer()
            result = await bot.db.edit_automatic_message(
                interaction.guild_id,
                selected["id"],
                content,
            )
            if result["status"] == "duplicate":
                await answer(interaction, "Ese texto ya pertenece a otro mensaje.")
                return
            if result["status"] == "not_found":
                await answer(interaction, "Ese mensaje ya no existe.")
                return
            changed = result["message"]
            action_label = "editado"
            movement_action = "automatic_message_edit"
            response = f"Actualicé el mensaje automático **#{changed['id']}**."
        elif accion == "remove":
            if nuevo_mensaje is not None:
                await answer(
                    interaction,
                    "La opción **nuevo_mensaje** no se usa al quitar.",
                )
                return
            await interaction.response.defer()
            removed = await bot.db.remove_automatic_message(
                interaction.guild_id,
                selected["id"],
            )
            if removed is None:
                await answer(interaction, "Ese mensaje ya no existe.")
                return
            changed = dict(removed)
            action_label = "quitado"
            movement_action = "automatic_message_remove"
            response = f"Quité el mensaje automático **#{changed['id']}**."
        else:
            await answer(interaction, "La acción seleccionada no es válida.")
            return

    preview = " ".join(changed["content"].split())[:300]
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        movement_action,
        None,
        f"Mensaje automático {action_label} #{changed['id']}: {preview}",
    )
    await send_audit_log(
        guild,
        f"Mensaje automático {action_label}",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**ID:** {changed['id']}\n"
        f"**Contenido:** {changed['content'][:1000]}",
        color=0x3B82F6,
    )
    await answer(interaction, response)


def automatic_message_pages(rows) -> list[str]:
    if not rows:
        return ["*No hay mensajes automáticos configurados.*"]
    pages = []
    current = ""
    for index, row in enumerate(rows, start=1):
        block = f"**{index}.** {row['content']}\n`ID: #{row['id']}`"
        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(block) > 3800:
            pages.append(current)
            current = block
        else:
            current += separator + block
    if current:
        pages.append(current)
    return pages


@bot.tree.command(
    name="mensajesautomaticos",
    description="Muestra la configuración y lista de mensajes automáticos.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def mensajesautomaticos(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    settings, rows = await asyncio.gather(
        bot.db.get_automatic_message_settings(interaction.guild_id),
        bot.db.list_automatic_messages(interaction.guild_id),
    )
    pages = automatic_message_pages(rows)
    await interaction.response.defer()
    for index, page in enumerate(pages, start=1):
        embed = discord.Embed(
            title=f"Mensajes automáticos · {index}/{len(pages)}",
            description=page,
            color=0x3B82F6,
        )
        if index == 1:
            if settings is None:
                embed.add_field(
                    name="Configuración",
                    value=(
                        "Sin configurar. Usa `/configurarmensajeautomatico` "
                        "para elegir canal e intervalo."
                    ),
                    inline=False,
                )
            else:
                next_send = (
                    f"<t:{int(settings['next_send_at'].timestamp())}:R>"
                    if settings["enabled"] and rows
                    else "En pausa"
                )
                embed.add_field(
                    name="Configuración",
                    value=(
                        f"**Estado:** {'Activo' if settings['enabled'] else 'Inactivo'}\n"
                        f"**Canal:** <#{settings['channel_id']}>\n"
                        f"**Intervalo:** {settings['interval_minutes']} minutos\n"
                        f"**Próximo envío:** {next_send}\n"
                        f"**Mensajes:** {len(rows)}/{MAX_AUTOMATIC_MESSAGES}"
                    ),
                    inline=False,
                )
        embed.set_footer(
            text=(
                "El bot mezcla la lista y envía cada mensaje una vez antes "
                "de volver a mezclar."
            )
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="añadiradmin", description="Añade un miembro al equipo de tickets.")
@app_commands.describe(miembro="Miembro que recibirá las alertas de tickets")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def añadiradmin(
    interaction: discord.Interaction,
    miembro: discord.Member,
) -> None:
    assert interaction.guild_id is not None
    if miembro.bot:
        await answer(interaction, "No puedes añadir bots al equipo de tickets.")
        return
    current = await bot.db.list_ticket_admins(interaction.guild_id)
    if any(row["user_id"] == miembro.id for row in current):
        await answer(interaction, f"{miembro.mention} ya está en el equipo de tickets.")
        return
    if len(current) >= MAX_TICKET_ADMINS:
        await answer(
            interaction,
            f"El equipo de tickets admite un máximo de {MAX_TICKET_ADMINS} miembros.",
        )
        return
    await bot.db.add_ticket_admin(interaction.guild_id, miembro.id)
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "ticket_admin_add",
        None,
        f"Añadió a {miembro} al equipo de tickets.",
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Administrador de tickets añadido",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Miembro añadido:** {miembro.mention}",
            color=0x22C55E,
        )
    await answer(
        interaction,
        f"Añadí a {miembro.mention} al equipo que recibe las alertas de tickets.",
    )


@bot.tree.command(name="quitaradmin", description="Quita un miembro del equipo de tickets.")
@app_commands.describe(miembro="Miembro que dejará de recibir alertas de tickets")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def quitaradmin(
    interaction: discord.Interaction,
    miembro: discord.Member,
) -> None:
    assert interaction.guild_id is not None
    removed = await bot.db.remove_ticket_admin(interaction.guild_id, miembro.id)
    if not removed:
        await answer(interaction, f"{miembro.mention} no está en el equipo de tickets.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "ticket_admin_remove",
        None,
        f"Quitó a {miembro} del equipo de tickets.",
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Administrador de tickets retirado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Miembro retirado:** {miembro.mention}",
            color=0xEF4444,
        )
    await answer(interaction, f"Quité a {miembro.mention} del equipo de tickets.")


@bot.tree.command(name="admins", description="Muestra el equipo que recibe alertas de tickets.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def admins(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    rows = await bot.db.list_ticket_admins(guild.id)
    active_members = [
        member
        for row in rows
        if (member := guild.get_member(row["user_id"])) is not None
    ]
    missing_ids = [
        row["user_id"] for row in rows if guild.get_member(row["user_id"]) is None
    ]
    embed = discord.Embed(title="Equipo de tickets", color=0x06B6D4)
    if active_members:
        embed.description = "\n".join(f"• {member.mention}" for member in active_members)
    else:
        embed.description = "No hay miembros activos configurados."
    if missing_ids:
        embed.add_field(
            name="Ya no están en el servidor",
            value="\n".join(f"• `{user_id}`" for user_id in missing_ids)[:1024],
            inline=False,
        )
    embed.set_footer(text=f"{len(rows)} de {MAX_TICKET_ADMINS} espacios utilizados")
    await answer(interaction, embed=embed)


@bot.tree.command(name="añadirwhitelist", description="Añade un miembro o rol a la whitelist.")
@app_commands.describe(objetivo="Miembro o rol que podrá usar insignias habilitadas")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def añadirwhitelist(
    interaction: discord.Interaction,
    objetivo: discord.Member | discord.Role,
) -> None:
    assert interaction.guild_id is not None
    target_type = "member" if isinstance(objetivo, discord.Member) else "role"
    if isinstance(objetivo, discord.Member) and objetivo.bot:
        await answer(interaction, "No puedes añadir bots a la whitelist.")
        return
    if isinstance(objetivo, discord.Role) and objetivo.is_default():
        await answer(interaction, "No puedes añadir el rol @everyone a la whitelist.")
        return
    added = await bot.db.add_whitelist_entry(
        interaction.guild_id,
        target_type,
        objetivo.id,
    )
    if not added:
        await answer(interaction, f"{objetivo.mention} ya está en la whitelist.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "whitelist_add",
        None,
        f"Añadió {objetivo} a la whitelist.",
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Entrada añadida a la whitelist",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Objetivo:** {objetivo.mention}\n"
            f"**Tipo:** {'Miembro' if target_type == 'member' else 'Rol'}",
            color=0x22C55E,
        )
    await answer(interaction, f"Añadí {objetivo.mention} a la whitelist.")


@bot.tree.command(name="quitarwhitelist", description="Quita un miembro o rol de la whitelist.")
@app_commands.describe(objetivo="Miembro o rol que perderá el acceso por whitelist")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def quitarwhitelist(
    interaction: discord.Interaction,
    objetivo: discord.Member | discord.Role,
) -> None:
    assert interaction.guild_id is not None
    target_type = "member" if isinstance(objetivo, discord.Member) else "role"
    removed = await bot.db.remove_whitelist_entry(
        interaction.guild_id,
        target_type,
        objetivo.id,
    )
    if not removed:
        await answer(interaction, f"{objetivo.mention} no está en la whitelist.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "whitelist_remove",
        None,
        f"Quitó {objetivo} de la whitelist.",
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Entrada retirada de la whitelist",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Objetivo:** {objetivo.mention}",
            color=0xEF4444,
        )
    await answer(interaction, f"Quité {objetivo.mention} de la whitelist.")


@bot.tree.command(name="whitelist", description="Muestra los miembros y roles de la whitelist.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def whitelist(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    rows, settings = await asyncio.gather(
        bot.db.list_whitelist_entries(interaction.guild_id),
        bot.db.get_log_settings(interaction.guild_id),
    )
    members = [row for row in rows if row["target_type"] == "member"]
    roles = [row for row in rows if row["target_type"] == "role"]
    embed = discord.Embed(title="Whitelist de Sularea", color=0x22C55E)
    embed.add_field(
        name="Miembros",
        value=(
            "\n".join(f"• <@{row['target_id']}>" for row in members)[:1024]
            if members
            else "Ninguno"
        ),
        inline=False,
    )
    embed.add_field(
        name="Roles",
        value=(
            "\n".join(f"• <@&{row['target_id']}>" for row in roles)[:1024]
            if roles
            else "Ninguno"
        ),
        inline=False,
    )
    discount_percent = (
        settings["whitelist_discount_percent"] if settings is not None else 0
    )
    multiplier_percent = (
        settings["whitelist_event_multiplier_percent"]
        if settings is not None
        else 100
    )
    embed.set_footer(
        text=(
            f"{len(rows)} entradas configuradas · "
            f"Descuento de tienda: {discount_percent}% · "
            f"Multiplicador de eventos: {format_multiplier(multiplier_percent)}"
        )
    )
    await answer(interaction, embed=embed)


@bot.tree.command(
    name="configuracion",
    description="Muestra todas las configuraciones generales del bot.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configuracion(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    (
        settings,
        automatic_settings,
        maintenance_rows,
        statistics,
        categories,
        ticket_admins,
        whitelist_entries,
    ) = await asyncio.gather(
        bot.db.get_log_settings(interaction.guild_id),
        bot.db.get_automatic_message_settings(interaction.guild_id),
        bot.db.list_object_section_settings(interaction.guild_id),
        bot.db.get_statistics(interaction.guild_id),
        bot.db.list_shop_categories(interaction.guild_id),
        bot.db.list_ticket_admins(interaction.guild_id),
        bot.db.list_whitelist_entries(interaction.guild_id),
    )

    coin_emoji = (
        settings["coin_emoji"]
        if settings is not None and settings["coin_emoji"]
        else DEFAULT_COIN_EMOJI
    )
    configured_whitelist_emoji = (
        settings["whitelist_emoji"]
        if settings is not None and settings["whitelist_emoji"]
        else DEFAULT_WHITELIST_EMOJI
    )
    discount_percent = (
        settings["whitelist_discount_percent"] if settings is not None else 0
    )
    event_multiplier_percent = (
        settings["whitelist_event_multiplier_percent"]
        if settings is not None
        else 100
    )
    embed = discord.Embed(
        title="Configuración actual de Sularea",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(
        name="Economía y whitelist",
        value=(
            f"**Emoji de moneda:** {coin_emoji}\n"
            f"**Emoji whitelist:** {configured_whitelist_emoji}\n"
            f"**Descuento whitelist:** {discount_percent}%\n"
            f"**Multiplicador en eventos:** "
            f"{format_multiplier(event_multiplier_percent)}\n"
            f"**Entradas whitelist:** {len(whitelist_entries)}"
        ),
        inline=False,
    )
    logs_enabled = bool(settings is not None and settings["logs_enabled"])
    log_channel_id = (
        settings["log_channel_id"] if settings is not None else None
    )
    embed.add_field(
        name="Registro de movimientos",
        value=(
            f"**Estado:** {'Activo' if logs_enabled else 'Inactivo'}\n"
            f"**Canal:** {f'<#{log_channel_id}>' if log_channel_id else 'Sin configurar'}"
        ),
        inline=True,
    )
    if automatic_settings is None:
        automatic_value = "Sin configurar."
    else:
        next_send_at = automatic_settings["next_send_at"]
        automatic_value = (
            f"**Estado:** "
            f"{'Activo' if automatic_settings['enabled'] else 'Inactivo'}\n"
            f"**Canal:** <#{automatic_settings['channel_id']}>\n"
            f"**Intervalo:** {automatic_settings['interval_minutes']} min\n"
            f"**Mensajes:** {automatic_settings['message_count']}\n"
            f"**Orden:** Aleatorio sin repeticiones\n"
            f"**Próximo envío:** <t:{int(next_send_at.timestamp())}:R>"
        )
    embed.add_field(
        name="Mensajes automáticos",
        value=automatic_value,
        inline=True,
    )
    embed.add_field(
        name="Objetos y tienda",
        value=(
            f"**Insignias:** {statistics['badges']}\n"
            f"**Modificadores:** {statistics['modifiers']}\n"
            f"**Tickets:** {statistics['tickets']}\n"
            f"**Categorías:** {len(categories)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Administración de tickets",
        value=f"**Administradores configurados:** {len(ticket_admins)}",
        inline=True,
    )

    maintenance_by_section = {
        row["section"]: row for row in maintenance_rows
    }
    maintenance_lines = []
    for section, label in OBJECT_SECTION_LABELS.items():
        row = maintenance_by_section.get(section)
        if row is None or row["enabled"]:
            maintenance_lines.append(f"✅ **{label.capitalize()}**")
            continue
        reason = " ".join((row["disabled_reason"] or "Sin razón").split())[:100]
        bypass = (
            "admins permitidos"
            if row["admins_bypass"]
            else "admins bloqueados"
        )
        maintenance_lines.append(
            f"⛔ **{label.capitalize()}** — {reason} ({bypass})"
        )
    embed.add_field(
        name="Mantenimiento",
        value="\n".join(maintenance_lines)[:1024],
        inline=False,
    )
    embed.set_footer(
        text=(
            "Detalles: /objetos · /mensajes · /mensajesautomaticos · "
            "/whitelist · /admins"
        )
    )
    await answer(interaction, embed=embed)


@bot.tree.command(name="estadisticas", description="Muestra estadísticas generales del sistema.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def estadisticas(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    stats = await bot.db.get_statistics(interaction.guild_id)
    embed = discord.Embed(title="Estadísticas de Sularea", color=0x14B8A6)
    if interaction.guild is not None and interaction.guild.icon is not None:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.add_field(name="Miembros registrados", value=str(stats["users"]))
    embed.add_field(
        name="Dinero total",
        value=money(stats["total_money"], interaction.guild_id),
    )
    embed.add_field(name="Insignias configuradas", value=str(stats["badges"]))
    embed.add_field(name="Modificadores configurados", value=str(stats["modifiers"]))
    embed.add_field(name="Tickets configurados", value=str(stats["tickets"]))
    embed.add_field(name="Equipo de tickets", value=str(stats["ticket_admins"]))
    embed.add_field(name="Entradas whitelist", value=str(stats["whitelist_entries"]))
    embed.add_field(name="Compras realizadas", value=str(stats["purchases"]))
    embed.add_field(name="Eventos ganados", value=str(stats["event_wins"]))
    embed.add_field(name="Eventos activos", value=str(stats["active_events"]))
    embed.add_field(
        name="Mensajes automáticos",
        value=str(stats["automatic_messages"]),
    )
    embed.add_field(name="Modificadores activos", value=str(stats["active_modifiers"]))
    embed.add_field(name="Movimientos guardados", value=str(stats["movements"]))
    await answer(interaction, embed=embed)


@bot.tree.command(name="exportardatos", description="Exporta un respaldo de los datos del servidor.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def exportardatos(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    await interaction.response.defer()
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "data_export",
        None,
        "Generó un respaldo de los datos del servidor.",
    )
    data = await bot.db.export_guild_data(interaction.guild_id)
    payload = json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
    ).encode("utf-8")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    file = discord.File(
        io.BytesIO(payload),
        filename=f"sularea_respaldo_{timestamp}.json",
    )
    await interaction.followup.send(
        "Respaldo generado. Guárdalo en un lugar seguro.",
        file=file,
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Respaldo exportado",
            f"{interaction.user.mention} generó un respaldo de los datos del servidor.",
        )


@bot.tree.command(
    name="modificarbalance",
    description="Añade, resta o establece el balance de un miembro o rol.",
)
@app_commands.describe(
    objetivo="Miembro o rol cuyo balance se modificará",
    operacion="Cambio que quieres realizar",
    cantidad="Cantidad que se añadirá, quitará o establecerá",
)
@app_commands.choices(
    operacion=[
        app_commands.Choice(name="Añadir", value="add"),
        app_commands.Choice(name="Restar", value="remove"),
        app_commands.Choice(name="Establecer", value="set"),
    ]
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def modificarbalance(
    interaction: discord.Interaction,
    objetivo: discord.Member | discord.Role,
    operacion: str,
    cantidad: app_commands.Range[int, 0, MAX_MONEY],
) -> None:
    if operacion in {"add", "remove"} and cantidad == 0:
        await answer(interaction, "La cantidad debe ser mayor que 0 para añadir o restar.")
        return
    await handle_balance_command(interaction, objetivo, cantidad, operacion)


@bot.tree.command(name="darobjeto", description="Entrega un objeto a un miembro.")
@app_commands.describe(
    miembro="Miembro que recibirá el objeto",
    objeto="Insignia, modificador o ticket que recibirá",
    cantidad="Cantidad; para insignias debe ser 1",
)
@app_commands.autocomplete(objeto=deletable_object_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def darobjeto(
    interaction: discord.Interaction,
    miembro: discord.Member,
    objeto: str,
    cantidad: app_commands.Range[int, 1, 1000] = 1,
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    badge = await find_badge(interaction, objeto)
    modifier = None if badge is not None else await find_modifier(interaction, objeto)
    ticket = (
        None
        if badge is not None or modifier is not None
        else await find_ticket(interaction, objeto)
    )
    if badge is None and modifier is None and ticket is None:
        await answer(interaction, "Ese objeto no existe.")
        return

    if modifier is not None:
        await interaction.response.defer()
        quantity = await bot.db.add_modifier_inventory(
            guild.id,
            miembro.id,
            modifier["id"],
            cantidad,
        )
        await bot.db.record_movement(
            guild.id,
            miembro.id,
            interaction.user.id,
            "modifier_grant",
            cantidad,
            f"Un administrador entregó {cantidad} de {modifier['name']}.",
        )
        await send_audit_log(
            guild,
            "Modificador entregado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Miembro:** {miembro.mention}\n"
            f"**Modificador:** {modifier['name']}\n"
            f"**Cantidad entregada:** {cantidad}\n"
            f"**Total:** {quantity}",
            color=0xA855F7,
        )
        await answer(
            interaction,
            f"Entregaste **{cantidad}** de **{modifier['name']}** a "
            f"{miembro.mention}. Ahora tiene **{quantity}**.",
        )
        return

    if ticket is not None:
        await interaction.response.defer()
        quantity = await bot.db.add_ticket_inventory(
            guild.id,
            miembro.id,
            ticket["id"],
            cantidad,
        )
        await bot.db.record_movement(
            guild.id,
            miembro.id,
            interaction.user.id,
            "ticket_grant",
            cantidad,
            f"Un administrador entregó {cantidad} de {ticket['name']}.",
        )
        await send_audit_log(
            guild,
            "Ticket entregado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Miembro:** {miembro.mention}\n"
            f"**Ticket:** {ticket['name']}\n"
            f"**Cantidad entregada:** {cantidad}\n"
            f"**Total:** {quantity}",
            color=0x06B6D4,
        )
        await answer(
            interaction,
            f"Entregaste **{cantidad}** de **{ticket['name']}** a "
            f"{miembro.mention}. Ahora tiene **{quantity}**.",
        )
        return

    if cantidad != 1:
        await answer(interaction, "Las insignias son únicas; usa **cantidad: 1**.")
        return
    role = guild.get_role(badge["badge_role_id"])
    if role is None:
        await answer(interaction, "El rol de esa insignia ya no existe.")
        return
    if role in miembro.roles:
        await answer(interaction, f"{miembro.mention} ya tiene **{badge['name']}**.")
        return
    if not role_can_be_managed(role):
        await answer(interaction, "No puedo asignar ese rol; colócalo debajo del rol del bot.")
        return
    await interaction.response.defer()
    await miembro.add_roles(role, reason=f"Insignia entregada por {interaction.user}")
    await bot.db.record_movement(
        guild.id,
        miembro.id,
        interaction.user.id,
        "badge_grant",
        None,
        f"Recibió la insignia {badge['name']}.",
    )
    await send_audit_log(
        guild,
        "Insignia entregada",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Miembro:** {miembro.mention}\n"
        f"**Insignia:** {badge['name']} ({role.mention})",
        color=0x22C55E,
    )
    await answer(interaction, f"Entregaste **{badge['name']}** a {miembro.mention}.")


@bot.tree.command(
    name="quitarobjeto",
    description="Retira un objeto del inventario de un miembro.",
)
@app_commands.describe(
    miembro="Miembro que perderá el objeto",
    objeto="Elige primero al miembro; solo aparecen los objetos que posee",
    cantidad="Cantidad; para insignias debe ser 1",
)
@app_commands.autocomplete(objeto=removable_object_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def quitarobjeto(
    interaction: discord.Interaction,
    miembro: discord.Member,
    objeto: str,
    cantidad: app_commands.Range[int, 1, 1000] = 1,
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    badge = await find_badge(interaction, objeto)
    modifier = None if badge is not None else await find_modifier(interaction, objeto)
    ticket = (
        None
        if badge is not None or modifier is not None
        else await find_ticket(interaction, objeto)
    )
    if badge is None and modifier is None and ticket is None:
        await answer(interaction, "Ese objeto no existe.")
        return

    if badge is not None:
        if cantidad != 1:
            await answer(interaction, "Las insignias son únicas; usa **cantidad: 1**.")
            return
        badge_role = guild.get_role(badge["badge_role_id"])
        color_role = guild.get_role(badge["color_role_id"])
        if badge_role is None or badge_role not in miembro.roles:
            await answer(interaction, f"{miembro.mention} no tiene esa insignia.")
            return
        roles = [badge_role]
        if color_role is not None and color_role in miembro.roles:
            roles.append(color_role)
        await interaction.response.defer()
        await miembro.remove_roles(
            *roles,
            reason=f"Insignia retirada por {interaction.user}",
        )
        await bot.db.record_movement(
            guild.id,
            miembro.id,
            interaction.user.id,
            "badge_remove",
            None,
            f"Se retiró la insignia {badge['name']}.",
        )
        await send_audit_log(
            guild,
            "Insignia retirada",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Miembro:** {miembro.mention}\n"
            f"**Insignia:** {badge['name']} ({badge_role.mention})",
            color=0xEF4444,
        )
        await answer(interaction, f"Quitaste **{badge['name']}** a {miembro.mention}.")
        return

    item = modifier or ticket
    is_modifier = modifier is not None
    inventory = (
        await bot.db.list_modifier_inventory(guild.id, miembro.id)
        if is_modifier
        else await bot.db.list_ticket_inventory(guild.id, miembro.id)
    )
    current_quantity = next(
        (row["quantity"] for row in inventory if row["id"] == item["id"]),
        0,
    )
    if current_quantity <= 0:
        await answer(interaction, f"{miembro.mention} no tiene **{item['name']}**.")
        return
    removed = min(current_quantity, cantidad)
    await interaction.response.defer()
    quantity = (
        await bot.db.remove_modifier_inventory(
            guild.id,
            miembro.id,
            item["id"],
            cantidad,
        )
        if is_modifier
        else await bot.db.remove_ticket_inventory(
            guild.id,
            miembro.id,
            item["id"],
            cantidad,
        )
    )
    item_type = "modificador" if is_modifier else "ticket"
    action = "modifier_remove" if is_modifier else "ticket_remove"
    await bot.db.record_movement(
        guild.id,
        miembro.id,
        interaction.user.id,
        action,
        -removed,
        f"Un administrador retiró {removed} de {item['name']}.",
    )
    await send_audit_log(
        guild,
        f"{item_type.capitalize()} retirado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Miembro:** {miembro.mention}\n"
        f"**{item_type.capitalize()}:** {item['name']}\n"
        f"**Cantidad retirada:** {removed}\n"
        f"**Restantes:** {quantity}",
        color=0xEF4444,
    )
    await answer(
        interaction,
        f"Retiraste **{removed}** de **{item['name']}** a {miembro.mention}. "
        f"Ahora tiene **{quantity}**.",
    )


@bot.tree.command(
    name="configurarcategoria",
    description="Crea una categoría para organizar la tienda.",
)
@app_commands.describe(
    nombre="Nombre de la nueva categoría",
    descripcion="Descripción que aparecerá en la tienda",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarcategoria(
    interaction: discord.Interaction,
    nombre: str,
    descripcion: str,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    display_name = nombre.strip()
    final_description = descripcion.strip()
    if not display_name or len(display_name) > 100:
        await answer(interaction, "El nombre debe tener entre 1 y 100 caracteres.")
        return
    if not final_description or len(final_description) > 1000:
        await answer(
            interaction,
            "La descripción debe tener entre 1 y 1.000 caracteres.",
        )
        return
    name_key = normalize_name(display_name)
    if await bot.db.get_shop_category(interaction.guild_id, name_key):
        await answer(interaction, "Ya existe una categoría con ese nombre.")
        return
    categories = await bot.db.list_shop_categories(interaction.guild_id)
    projected_keys = {row["name_key"] for row in categories}
    projected_keys.update({normalize_name(DEFAULT_SHOP_SECTION), name_key})
    if len(projected_keys) > MAX_SHOP_SECTIONS:
        await answer(
            interaction,
            f"La tienda admite un máximo de {MAX_SHOP_SECTIONS} categorías "
            "contando **General**.",
        )
        return
    try:
        await bot.db.create_shop_category(
            interaction.guild_id,
            display_name,
            name_key,
            final_description,
        )
    except asyncpg.UniqueViolationError:
        await answer(interaction, "Ya existe una categoría con ese nombre.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "shop_category_config_create",
        None,
        f"Configuró la categoría {display_name}.",
    )
    await send_audit_log(
        guild,
        "Categoría configurada",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Nombre:** {display_name}\n"
        f"**Descripción:** {final_description}",
        color=0xF59E0B,
    )
    await answer(
        interaction,
        f"Configuré la categoría **{display_name}** correctamente.",
    )


@bot.tree.command(name="configurarinsignia", description="Configura una insignia y su rol de color.")
@app_commands.describe(
    rol_insignia="Rol que representa la propiedad de la insignia",
    rol_color="Rol decorativo que se activará con /usar",
    comprable="Indica si aparecerá en la tienda",
    permitir_whitelist="Permite usarla a miembros y roles de la whitelist",
    precio="Precio; usa 0 si será gratuita",
    nombre="Nombre para los comandos; por defecto usa el nombre del rol",
    apartado="Categoría existente de la tienda; por defecto será General",
    emoji="Emoji opcional: Unicode, :nombre: o ID del servidor/bot",
)
@app_commands.autocomplete(apartado=shop_section_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarinsignia(
    interaction: discord.Interaction,
    rol_insignia: discord.Role,
    rol_color: discord.Role,
    comprable: bool,
    permitir_whitelist: bool,
    precio: app_commands.Range[int, 0, MAX_MONEY] = 0,
    nombre: str | None = None,
    apartado: str | None = None,
    emoji: str | None = None,
) -> None:
    assert interaction.guild_id is not None
    if interaction.guild is None:
        return
    display_name = (nombre or rol_insignia.name).strip()
    if not display_name or len(display_name) > 100:
        await answer(interaction, "El nombre debe tener entre 1 y 100 caracteres.")
        return
    if await bot.db.get_modifier(interaction.guild_id, normalize_name(display_name)):
        await answer(interaction, "Ya existe un modificador con ese nombre.")
        return
    if await bot.db.get_ticket(interaction.guild_id, normalize_name(display_name)):
        await answer(interaction, "Ya existe un ticket con ese nombre.")
        return
    if rol_insignia == rol_color:
        await answer(interaction, "El rol de insignia y el rol de color deben ser diferentes.")
        return
    if not role_can_be_managed(rol_insignia) or not role_can_be_managed(rol_color):
        await answer(interaction, "Ambos roles deben estar debajo del rol del bot.")
        return
    if not comprable:
        precio = 0
    final_section = None
    if comprable:
        requested_section = shop_section_name(apartado)
        final_section = await resolve_shop_section(
            interaction.guild_id,
            requested_section,
        )
        if final_section is None:
            await answer(
                interaction,
                "Esa categoría no existe. Créala primero con `/configurarcategoria`.",
            )
            return
    emoji_value = edited_badge_emoji(emoji)
    final_emoji, emoji_error = await resolve_configured_emoji(
        interaction.guild,
        emoji_value,
    )
    if emoji_error is not None:
        await answer(interaction, emoji_error)
        return
    if final_emoji is not None and (len(final_emoji) > 100 or "\n" in final_emoji):
        await answer(interaction, "El emoji no es válido o es demasiado largo.")
        return
    await interaction.response.defer()
    try:
        await bot.db.create_badge(
            interaction.guild_id, display_name, normalize_name(display_name),
            rol_insignia.id, rol_color.id, comprable, precio, final_section,
            final_emoji, permitir_whitelist,
        )
    except asyncpg.UniqueViolationError:
        await answer(interaction, "Ya existe una insignia con ese nombre o ese rol.")
        return
    if interaction.guild is not None:
        await bot.db.record_movement(
            interaction.guild_id,
            None,
            interaction.user.id,
            "badge_config_create",
            None,
            f"Configuró la insignia {display_name}.",
        )
        await send_audit_log(
            interaction.guild,
            "Insignia configurada",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Nombre:** {display_name}\n"
            f"**Rol de insignia:** {rol_insignia.mention}\n"
            f"**Rol de color:** {rol_color.mention}\n"
            f"**Comprable:** {'Sí' if comprable else 'No'}\n"
            f"**Acceso por whitelist:** {'Sí' if permitir_whitelist else 'No'}\n"
            f"**Precio:** {money(precio, interaction.guild_id)}\n"
            f"**Categoría:** {final_section or 'No aplica'}\n"
            f"**Emoji:** {final_emoji or 'Ninguno'}",
        )
    await answer(interaction, f"Configuré la insignia **{display_name}** correctamente.")


@bot.tree.command(
    name="configurarmodificador",
    description="Configura un consumible con mensajes, probabilidad, cooldown y duración.",
)
@app_commands.describe(
    nombre="Nombre del modificador",
    comprable="Indica si aparecerá en la tienda",
    mensajes="Mensajes posibles separados por el símbolo |",
    tipo="Individual para un miembro o Canal para todos los que escriban allí",
    probabilidad="Porcentaje (25) o fracción (1/4)",
    cooldown="Segundos mínimos entre mensajes generados",
    duracion="Minutos que permanecerá activo",
    precio="Precio; usa 0 si no será comprable",
    apartado="Categoría existente de la tienda; por defecto será General",
    emoji="Emoji opcional: Unicode, :nombre: o ID del servidor/bot",
)
@app_commands.choices(
    tipo=[
        app_commands.Choice(name="Individual", value="individual"),
        app_commands.Choice(name="Canal", value="channel"),
    ],
)
@app_commands.autocomplete(apartado=shop_section_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarmodificador(
    interaction: discord.Interaction,
    nombre: str,
    comprable: bool,
    mensajes: str,
    tipo: app_commands.Choice[str],
    probabilidad: str = MODIFIER_PROBABILITY,
    cooldown: app_commands.Range[int, 0, MAX_MODIFIER_COOLDOWN_SECONDS] = MODIFIER_COOLDOWN_SECONDS,
    duracion: app_commands.Range[int, 1, MAX_MODIFIER_DURATION_MINUTES] = MODIFIER_DURATION_MINUTES,
    precio: app_commands.Range[int, 0, MAX_MONEY] = 0,
    apartado: str | None = None,
    emoji: str | None = None,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    display_name = nombre.strip()
    if not display_name or len(display_name) > 100:
        await answer(interaction, "El nombre debe tener entre 1 y 100 caracteres.")
        return
    name_key = normalize_name(display_name)
    if await bot.db.get_badge(interaction.guild_id, name_key):
        await answer(interaction, "Ya existe una insignia con ese nombre.")
        return
    if await bot.db.get_ticket(interaction.guild_id, name_key):
        await answer(interaction, "Ya existe un ticket con ese nombre.")
        return
    parsed_messages, messages_error = parse_modifier_messages(mensajes)
    if messages_error is not None or parsed_messages is None:
        await answer(interaction, messages_error or "Los mensajes no son válidos.")
        return
    parsed_probability, probability_error = parse_modifier_probability(probabilidad)
    if probability_error is not None or parsed_probability is None:
        await answer(interaction, probability_error or "La probabilidad no es válida.")
        return
    probability_numerator, probability_denominator = parsed_probability
    if not comprable:
        precio = 0
    final_section = None
    if comprable:
        requested_section = shop_section_name(apartado)
        final_section = await resolve_shop_section(
            interaction.guild_id,
            requested_section,
        )
        if final_section is None:
            await answer(
                interaction,
                "Esa categoría no existe. Créala primero con `/configurarcategoria`.",
            )
            return
    emoji_value = edited_badge_emoji(emoji)
    final_emoji, emoji_error = await resolve_configured_emoji(guild, emoji_value)
    if emoji_error is not None:
        await answer(interaction, emoji_error)
        return
    if final_emoji is not None and (len(final_emoji) > 100 or "\n" in final_emoji):
        await answer(interaction, "El emoji no es válido o es demasiado largo.")
        return
    await interaction.response.defer()
    try:
        await bot.db.create_modifier(
            interaction.guild_id,
            display_name,
            name_key,
            comprable,
            precio,
            final_section,
            final_emoji,
            parsed_messages,
            probability_numerator,
            probability_denominator,
            cooldown,
            duracion,
            tipo.value,
        )
    except asyncpg.UniqueViolationError:
        await answer(interaction, "Ya existe un modificador con ese nombre.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "modifier_config_create",
        None,
        f"Configuró el modificador {display_name}.",
    )
    await send_audit_log(
        guild,
        "Modificador configurado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Nombre:** {display_name}\n"
        f"**Tipo:** {modifier_scope_label(tipo.value)}\n"
        f"**Comprable:** {'Sí' if comprable else 'No'}\n"
        f"**Precio:** {money(precio, interaction.guild_id)}\n"
        f"**Categoría:** {final_section or 'No aplica'}\n"
        f"**Emoji:** {final_emoji or 'Ninguno'}\n"
        f"**Mensajes:** {len(parsed_messages)}\n"
        f"**Probabilidad:** {modifier_probability_label(probability_numerator, probability_denominator)}\n"
        f"**Cooldown:** {cooldown} segundos\n"
        f"**Duración:** {duracion} minutos",
        color=0xA855F7,
    )
    await answer(
        interaction,
        f"Configuré el modificador **{display_name}** con "
        f"tipo **{modifier_scope_label(tipo.value)}**, "
        f"**{len(parsed_messages)} mensajes**, probabilidad "
        f"**{modifier_probability_label(probability_numerator, probability_denominator)}**, "
        f"cooldown de **{cooldown} segundos** y duración de **{duracion} minutos**.",
    )


@bot.tree.command(
    name="editarmensajes",
    description="Añade, edita o quita mensajes individuales de un modificador.",
)
@app_commands.describe(
    modificador="Modificador cuyos mensajes quieres cambiar",
    accion="Añadir, editar o quitar un mensaje",
    mensaje="Mensaje nuevo al añadir; mensaje actual al editar o quitar",
    nuevo_mensaje="Texto de reemplazo; solo se usa con la acción Editar",
)
@app_commands.choices(
    accion=[
        app_commands.Choice(name="Añadir", value="add"),
        app_commands.Choice(name="Editar", value="edit"),
        app_commands.Choice(name="Quitar", value="remove"),
    ]
)
@app_commands.autocomplete(
    modificador=modifier_autocomplete,
    mensaje=modifier_message_autocomplete,
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def editarmensajes(
    interaction: discord.Interaction,
    modificador: str,
    accion: str,
    mensaje: str,
    nuevo_mensaje: str | None = None,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    current = await find_modifier(interaction, modificador)
    if current is None:
        await answer(interaction, "Ese modificador no existe.")
        return
    messages = list(current["messages"])
    old_message: str | None = None
    added_or_updated_message: str | None = None

    if accion == "add":
        if nuevo_mensaje is not None:
            await answer(
                interaction,
                "Para añadir, escribe el texto solamente en la opción mensaje.",
            )
            return
        added_or_updated_message = mensaje.strip()
        if not added_or_updated_message or len(added_or_updated_message) > 1900:
            await answer(interaction, "El mensaje debe tener entre 1 y 1.900 caracteres.")
            return
        if len(messages) >= 20:
            await answer(interaction, "Ese modificador ya tiene el máximo de 20 mensajes.")
            return
        if any(
            normalize_name(item) == normalize_name(added_or_updated_message)
            for item in messages
        ):
            await answer(interaction, "Ese mensaje ya existe en el modificador.")
            return
        messages.append(added_or_updated_message)
        action_label = "Añadido"
        response_verb = "Añadí"
        movement_action = "modifier_message_add"
    elif accion in {"edit", "remove"}:
        selected_index = modifier_message_index(messages, mensaje)
        if selected_index is None:
            await answer(
                interaction,
                "Selecciona uno de los mensajes actuales que muestra el comando.",
            )
            return
        old_message = messages[selected_index]
        if accion == "remove":
            if nuevo_mensaje is not None:
                await answer(
                    interaction,
                    "La opción nuevo_mensaje solo se usa con la acción Editar.",
                )
                return
            if len(messages) == 1:
                await answer(
                    interaction,
                    "No puedes quitar el último mensaje del modificador. Edítalo en su lugar.",
                )
                return
            messages.pop(selected_index)
            action_label = "Quitado"
            response_verb = "Quité"
            movement_action = "modifier_message_remove"
        else:
            added_or_updated_message = (nuevo_mensaje or "").strip()
            if not added_or_updated_message or len(added_or_updated_message) > 1900:
                await answer(
                    interaction,
                    "Para editar debes escribir nuevo_mensaje con entre 1 y 1.900 caracteres.",
                )
                return
            if any(
                index != selected_index
                and normalize_name(item) == normalize_name(added_or_updated_message)
                for index, item in enumerate(messages)
            ):
                await answer(interaction, "Ese mensaje ya existe en el modificador.")
                return
            messages[selected_index] = added_or_updated_message
            action_label = "Editado"
            response_verb = "Edité"
            movement_action = "modifier_message_edit"
    else:
        await answer(interaction, "La acción seleccionada no es válida.")
        return

    await interaction.response.defer()
    updated = await bot.db.update_modifier(
        interaction.guild_id,
        current["name_key"],
        current["name"],
        current["name_key"],
        current["purchasable"],
        current["price"],
        current["shop_section"],
        current["emoji"],
        messages,
        current["trigger_numerator"],
        current["trigger_denominator"],
        current["cooldown_seconds"],
        current["duration_minutes"],
        current["effect_scope"],
    )
    if not updated:
        await answer(interaction, "Ese modificador ya no existe.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        movement_action,
        None,
        f"{action_label} un mensaje de {current['name']}.",
    )
    details = (
        f"**Anterior:** {old_message[:700]}\n"
        f"**Nuevo:** {added_or_updated_message[:700]}"
        if old_message is not None and added_or_updated_message is not None
        else f"**Mensaje:** {(added_or_updated_message or old_message or '')[:1400]}"
    )
    await send_audit_log(
        guild,
        f"Mensaje de modificador: {action_label.lower()}",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Modificador:** {current['name']}\n"
        f"**Acción:** {action_label}\n"
        f"**Mensajes totales:** {len(messages)}\n"
        f"{details}",
        color=0xA855F7,
    )
    await answer(
        interaction,
        f"{response_verb} correctamente el mensaje de **{current['name']}**. "
        f"Ahora tiene **{len(messages)} mensajes**.",
    )


@bot.tree.command(name="configurarticket", description="Configura un ticket consumible.")
@app_commands.describe(
    nombre="Nombre del ticket",
    comprable="Indica si aparecerá en la tienda",
    descripcion="Explica para qué sirve el ticket",
    activo="Indica si el ticket se puede usar actualmente",
    precio="Precio; usa 0 si no será comprable",
    apartado="Categoría existente de la tienda; por defecto será General",
    emoji="Emoji opcional: Unicode, :nombre: o ID del servidor/bot",
)
@app_commands.autocomplete(apartado=shop_section_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarticket(
    interaction: discord.Interaction,
    nombre: str,
    comprable: bool,
    descripcion: str,
    activo: bool,
    precio: app_commands.Range[int, 0, MAX_MONEY] = 0,
    apartado: str | None = None,
    emoji: str | None = None,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    display_name = nombre.strip()
    if not display_name or len(display_name) > 100:
        await answer(interaction, "El nombre debe tener entre 1 y 100 caracteres.")
        return
    final_description = descripcion.strip()
    if not final_description or len(final_description) > 1000:
        await answer(interaction, "La descripción debe tener entre 1 y 1.000 caracteres.")
        return
    name_key = normalize_name(display_name)
    if await bot.db.get_badge(interaction.guild_id, name_key):
        await answer(interaction, "Ya existe una insignia con ese nombre.")
        return
    if await bot.db.get_modifier(interaction.guild_id, name_key):
        await answer(interaction, "Ya existe un modificador con ese nombre.")
        return
    if not comprable:
        precio = 0
    final_section = None
    if comprable:
        requested_section = shop_section_name(apartado)
        final_section = await resolve_shop_section(
            interaction.guild_id,
            requested_section,
        )
        if final_section is None:
            await answer(
                interaction,
                "Esa categoría no existe. Créala primero con `/configurarcategoria`.",
            )
            return
    emoji_value = edited_badge_emoji(emoji)
    final_emoji, emoji_error = await resolve_configured_emoji(guild, emoji_value)
    if emoji_error is not None:
        await answer(interaction, emoji_error)
        return
    if final_emoji is not None and (len(final_emoji) > 100 or "\n" in final_emoji):
        await answer(interaction, "El emoji no es válido o es demasiado largo.")
        return
    await interaction.response.defer()
    try:
        await bot.db.create_ticket(
            interaction.guild_id,
            display_name,
            name_key,
            comprable,
            precio,
            final_section,
            final_emoji,
            final_description,
            activo,
        )
    except asyncpg.UniqueViolationError:
        await answer(interaction, "Ya existe un ticket con ese nombre.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "ticket_config_create",
        None,
        f"Configuró el ticket {display_name}.",
    )
    await send_audit_log(
        guild,
        "Ticket configurado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Nombre:** {display_name}\n"
        f"**Comprable:** {'Sí' if comprable else 'No'}\n"
        f"**Precio:** {money(precio, interaction.guild_id)}\n"
        f"**Categoría:** {final_section or 'No aplica'}\n"
        f"**Emoji:** {final_emoji or 'Ninguno'}\n"
        f"**Estado:** {'Activo' if activo else 'Inactivo'}\n"
        f"**Descripción:** {final_description}",
        color=0x06B6D4,
    )
    await answer(
        interaction,
        f"Configuré el ticket **{display_name}** como "
        f"**{'activo' if activo else 'inactivo'}**.",
    )


@bot.tree.command(
    name="editar",
    description="Edita la configuración de un objeto o una categoría.",
)
@app_commands.describe(
    objeto="Objeto o categoría que quieres editar",
    nuevo_nombre="Nuevo nombre (opcional)",
    rol_insignia="Nuevo rol de insignia; no aplica a consumibles",
    rol_color="Nuevo rol de color; no aplica a consumibles",
    comprable="Cambiar si aparece en la tienda",
    permitir_whitelist="Permitir o impedir el acceso por whitelist a esta insignia",
    precio="Nuevo precio",
    apartado="Nueva categoría existente de la tienda",
    emoji="Emoji, :nombre: o ID; escribe quitar para eliminarlo",
    descripcion="Para tickets o categorías: nueva descripción",
    activo="Para tickets: permitir o impedir su uso",
    tipo_modificador="Para modificadores: Individual o Canal",
    probabilidad="Para modificadores: porcentaje (25) o fracción (1/4)",
    cooldown="Para modificadores: segundos entre intentos",
    duracion="Para modificadores: minutos que permanece activo",
)
@app_commands.choices(
    tipo_modificador=[
        app_commands.Choice(name="Individual", value="individual"),
        app_commands.Choice(name="Canal", value="channel"),
    ],
)
@app_commands.autocomplete(
    objeto=editable_object_autocomplete,
    apartado=shop_section_autocomplete,
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def editar(
    interaction: discord.Interaction,
    objeto: str,
    nuevo_nombre: str | None = None,
    rol_insignia: discord.Role | None = None,
    rol_color: discord.Role | None = None,
    comprable: bool | None = None,
    permitir_whitelist: bool | None = None,
    precio: int | None = None,
    apartado: str | None = None,
    emoji: str | None = None,
    descripcion: str | None = None,
    activo: bool | None = None,
    tipo_modificador: app_commands.Choice[str] | None = None,
    probabilidad: str | None = None,
    cooldown: int | None = None,
    duracion: int | None = None,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    current_badge = await find_badge(interaction, objeto)
    current_modifier = (
        None if current_badge is not None else await find_modifier(interaction, objeto)
    )
    current_ticket = (
        None
        if current_badge is not None or current_modifier is not None
        else await find_ticket(interaction, objeto)
    )
    current_category = (
        None
        if current_badge is not None
        or current_modifier is not None
        or current_ticket is not None
        else await find_shop_category(interaction, objeto)
    )
    if current_category is not None:
        category_only_options = (
            rol_insignia,
            rol_color,
            comprable,
            permitir_whitelist,
            precio,
            apartado,
            emoji,
            activo,
            tipo_modificador,
            probabilidad,
            cooldown,
            duracion,
        )
        if any(value is not None for value in category_only_options):
            await answer(
                interaction,
                "Las categorías solo permiten cambiar **nuevo_nombre** y "
                "**descripcion**.",
            )
            return
        if nuevo_nombre is None and descripcion is None:
            await answer(
                interaction,
                "Indica **nuevo_nombre**, **descripcion** o ambos.",
            )
            return
        final_name = (
            nuevo_nombre.strip()
            if nuevo_nombre is not None
            else current_category["name"]
        )
        final_description = (
            descripcion.strip()
            if descripcion is not None
            else current_category["description"]
        )
        if not final_name or len(final_name) > 100:
            await answer(
                interaction,
                "El nombre debe tener entre 1 y 100 caracteres.",
            )
            return
        if not final_description or len(final_description) > 1000:
            await answer(
                interaction,
                "La descripción debe tener entre 1 y 1.000 caracteres.",
            )
            return
        await interaction.response.defer()
        try:
            updated = await bot.db.update_shop_category(
                interaction.guild_id,
                current_category["name_key"],
                final_name,
                normalize_name(final_name),
                final_description,
            )
        except asyncpg.UniqueViolationError:
            await answer(interaction, "Ya existe una categoría con ese nombre.")
            return
        if not updated:
            await answer(interaction, "No pude encontrar esa categoría.")
            return
        await bot.db.record_movement(
            interaction.guild_id,
            None,
            interaction.user.id,
            "shop_category_config_edit",
            None,
            f"Editó la categoría {current_category['name']} como {final_name}.",
        )
        await send_audit_log(
            guild,
            "Categoría editada",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Nombre anterior:** {current_category['name']}\n"
            f"**Nombre actual:** {final_name}\n"
            f"**Descripción:** {final_description}",
            color=0xF59E0B,
        )
        await answer(interaction, f"Actualicé la categoría **{final_name}**.")
        return

    if current_badge is None and current_modifier is None and current_ticket is None:
        await answer(interaction, "Ese objeto o categoría no existe.")
        return

    current = current_badge or current_modifier or current_ticket
    final_name = nuevo_nombre.strip() if nuevo_nombre is not None else current["name"]
    if not final_name or len(final_name) > 100:
        await answer(interaction, "El nombre debe tener entre 1 y 100 caracteres.")
        return
    final_name_key = normalize_name(final_name)
    final_purchasable = comprable if comprable is not None else current["purchasable"]
    final_price = precio if precio is not None else current["price"]
    if final_price < 0 or final_price > MAX_MONEY:
        await answer(
            interaction,
            f"El precio debe estar entre 0 y {money(MAX_MONEY, interaction.guild_id)}.",
        )
        return
    if not final_purchasable:
        final_price = 0
    final_section = None
    if final_purchasable:
        requested_section = shop_section_name(
            apartado if apartado is not None else current["shop_section"]
        )
        final_section = await resolve_shop_section(
            interaction.guild_id,
            requested_section,
        )
        if final_section is None:
            await answer(
                interaction,
                "Esa categoría no existe. Créala primero con `/configurarcategoria`.",
            )
            return
    emoji_value = edited_badge_emoji(emoji, current["emoji"])
    final_emoji, emoji_error = await resolve_configured_emoji(guild, emoji_value)
    if emoji_error is not None:
        await answer(interaction, emoji_error)
        return
    if final_emoji is not None and (len(final_emoji) > 100 or "\n" in final_emoji):
        await answer(interaction, "El emoji no es válido o es demasiado largo.")
        return

    if current_modifier is not None:
        if rol_insignia is not None or rol_color is not None:
            await answer(interaction, "Los modificadores no utilizan roles de Discord.")
            return
        if descripcion is not None:
            await answer(interaction, "La opción descripción solo se usa con tickets.")
            return
        if activo is not None:
            await answer(interaction, "La opción activo solo se usa con tickets.")
            return
        if permitir_whitelist is not None:
            await answer(
                interaction,
                "La opción permitir_whitelist solo se usa con insignias.",
            )
            return
        if await bot.db.get_badge(interaction.guild_id, final_name_key):
            await answer(interaction, "Ya existe una insignia con ese nombre.")
            return
        if await bot.db.get_ticket(interaction.guild_id, final_name_key):
            await answer(interaction, "Ya existe un ticket con ese nombre.")
            return
        final_messages = list(current_modifier["messages"])
        if probabilidad is not None:
            parsed_probability, probability_error = parse_modifier_probability(probabilidad)
            if probability_error is not None or parsed_probability is None:
                await answer(
                    interaction,
                    probability_error or "La probabilidad no es válida.",
                )
                return
            final_probability_numerator, final_probability_denominator = parsed_probability
        else:
            final_probability_numerator = current_modifier["trigger_numerator"]
            final_probability_denominator = current_modifier["trigger_denominator"]
        final_cooldown = (
            cooldown if cooldown is not None else current_modifier["cooldown_seconds"]
        )
        final_duration = (
            duracion if duracion is not None else current_modifier["duration_minutes"]
        )
        final_scope = (
            tipo_modificador.value
            if tipo_modificador is not None
            else current_modifier["effect_scope"]
        )
        if not 0 <= final_cooldown <= MAX_MODIFIER_COOLDOWN_SECONDS:
            await answer(
                interaction,
                f"El cooldown debe estar entre 0 y {MAX_MODIFIER_COOLDOWN_SECONDS} segundos.",
            )
            return
        if not 1 <= final_duration <= MAX_MODIFIER_DURATION_MINUTES:
            await answer(
                interaction,
                f"La duración debe estar entre 1 y {MAX_MODIFIER_DURATION_MINUTES} minutos.",
            )
            return
        await interaction.response.defer()
        try:
            updated = await bot.db.update_modifier(
                interaction.guild_id,
                current_modifier["name_key"],
                final_name,
                final_name_key,
                final_purchasable,
                final_price,
                final_section,
                final_emoji,
                final_messages,
                final_probability_numerator,
                final_probability_denominator,
                final_cooldown,
                final_duration,
                final_scope,
            )
        except asyncpg.UniqueViolationError:
            await answer(interaction, "Ya existe un modificador con ese nombre.")
            return
        if not updated:
            await answer(interaction, "No pude encontrar ese modificador.")
            return
        await bot.db.record_movement(
            interaction.guild_id,
            None,
            interaction.user.id,
            "modifier_config_edit",
            None,
            f"Editó el modificador {current_modifier['name']} como {final_name}.",
        )
        await send_audit_log(
            guild,
            "Modificador editado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Nombre anterior:** {current_modifier['name']}\n"
            f"**Nombre actual:** {final_name}\n"
            f"**Tipo:** {modifier_scope_label(final_scope)}\n"
            f"**Comprable:** {'Sí' if final_purchasable else 'No'}\n"
            f"**Precio:** {money(final_price, interaction.guild_id)}\n"
            f"**Categoría:** {final_section or 'No aplica'}\n"
            f"**Emoji:** {final_emoji or 'Ninguno'}\n"
            f"**Mensajes:** {len(final_messages)}\n"
            f"**Probabilidad:** "
            f"{modifier_probability_label(final_probability_numerator, final_probability_denominator)}\n"
            f"**Cooldown:** {final_cooldown} segundos\n"
            f"**Duración:** {final_duration} minutos",
            color=0xA855F7,
        )
        await answer(interaction, f"Actualicé el modificador **{final_name}**.")
        return

    if current_ticket is not None:
        if rol_insignia is not None or rol_color is not None:
            await answer(interaction, "Los tickets no utilizan roles de Discord.")
            return
        if probabilidad is not None or cooldown is not None or duracion is not None:
            await answer(
                interaction,
                "Las opciones probabilidad, cooldown y duracion solo se usan con modificadores.",
            )
            return
        if tipo_modificador is not None:
            await answer(
                interaction,
                "La opción tipo_modificador solo se usa con modificadores.",
            )
            return
        if permitir_whitelist is not None:
            await answer(
                interaction,
                "La opción permitir_whitelist solo se usa con insignias.",
            )
            return
        if await bot.db.get_badge(interaction.guild_id, final_name_key):
            await answer(interaction, "Ya existe una insignia con ese nombre.")
            return
        if await bot.db.get_modifier(interaction.guild_id, final_name_key):
            await answer(interaction, "Ya existe un modificador con ese nombre.")
            return
        final_description = (
            descripcion.strip() if descripcion is not None else current_ticket["description"]
        )
        final_active = activo if activo is not None else current_ticket["active"]
        if not final_description or len(final_description) > 1000:
            await answer(
                interaction,
                "La descripción debe tener entre 1 y 1.000 caracteres.",
            )
            return
        await interaction.response.defer()
        try:
            updated = await bot.db.update_ticket(
                interaction.guild_id,
                current_ticket["name_key"],
                final_name,
                final_name_key,
                final_purchasable,
                final_price,
                final_section,
                final_emoji,
                final_description,
                final_active,
            )
        except asyncpg.UniqueViolationError:
            await answer(interaction, "Ya existe un ticket con ese nombre.")
            return
        if not updated:
            await answer(interaction, "No pude encontrar ese ticket.")
            return
        await bot.db.record_movement(
            interaction.guild_id,
            None,
            interaction.user.id,
            "ticket_config_edit",
            None,
            f"Editó el ticket {current_ticket['name']} como {final_name}.",
        )
        await send_audit_log(
            guild,
            "Ticket editado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Nombre anterior:** {current_ticket['name']}\n"
            f"**Nombre actual:** {final_name}\n"
            f"**Comprable:** {'Sí' if final_purchasable else 'No'}\n"
            f"**Precio:** {money(final_price, interaction.guild_id)}\n"
            f"**Categoría:** {final_section or 'No aplica'}\n"
            f"**Emoji:** {final_emoji or 'Ninguno'}\n"
            f"**Estado:** {'Activo' if final_active else 'Inactivo'}\n"
            f"**Descripción:** {final_description}",
            color=0x06B6D4,
        )
        await answer(interaction, f"Actualicé el ticket **{final_name}**.")
        return

    if (
        descripcion is not None
        or activo is not None
        or tipo_modificador is not None
        or probabilidad is not None
        or cooldown is not None
        or duracion is not None
    ):
        await answer(
            interaction,
            "Las opciones de modificadores o tickets no se usan con insignias.",
        )
        return
    if await bot.db.get_modifier(interaction.guild_id, final_name_key):
        await answer(interaction, "Ya existe un modificador con ese nombre.")
        return
    if await bot.db.get_ticket(interaction.guild_id, final_name_key):
        await answer(interaction, "Ya existe un ticket con ese nombre.")
        return
    final_badge_role = (
        rol_insignia.id if rol_insignia else current_badge["badge_role_id"]
    )
    final_color_role = rol_color.id if rol_color else current_badge["color_role_id"]
    final_whitelist_enabled = (
        permitir_whitelist
        if permitir_whitelist is not None
        else current_badge["whitelist_enabled"]
    )
    if final_badge_role == final_color_role:
        await answer(interaction, "El rol de insignia y el rol de color deben ser diferentes.")
        return
    if any(
        role is not None and not role_can_be_managed(role)
        for role in (rol_insignia, rol_color)
    ):
        await answer(interaction, "Los roles deben estar debajo del rol del bot.")
        return
    await interaction.response.defer()
    try:
        updated = await bot.db.update_badge(
            interaction.guild_id,
            current_badge["name_key"],
            final_name,
            final_name_key,
            final_badge_role,
            final_color_role,
            final_purchasable,
            final_price,
            final_section,
            final_emoji,
            final_whitelist_enabled,
        )
    except asyncpg.UniqueViolationError:
        await answer(interaction, "Ese nombre o rol ya pertenece a otra insignia.")
        return
    if not updated:
        await answer(interaction, "No pude encontrar esa insignia.")
        return
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "badge_config_edit",
        None,
        f"Editó la insignia {current_badge['name']} como {final_name}.",
    )
    await send_audit_log(
        guild,
        "Insignia editada",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Nombre anterior:** {current_badge['name']}\n"
        f"**Nombre actual:** {final_name}\n"
        f"**Rol de insignia:** <@&{final_badge_role}>\n"
        f"**Rol de color:** <@&{final_color_role}>\n"
        f"**Comprable:** {'Sí' if final_purchasable else 'No'}\n"
        f"**Acceso por whitelist:** {'Sí' if final_whitelist_enabled else 'No'}\n"
        f"**Precio:** {money(final_price, interaction.guild_id)}\n"
        f"**Categoría:** {final_section or 'No aplica'}\n"
        f"**Emoji:** {final_emoji or 'Ninguno'}",
    )
    await answer(interaction, f"Actualicé la insignia **{final_name}**.")


async def delete_configured_object(
    interaction: discord.Interaction,
    item_type: str,
    name_key: str,
    badge_role_id: int | None,
    refund_enabled: bool,
) -> str | None:
    guild = interaction.guild
    if guild is None:
        return None
    badge_user_ids = []
    if item_type == "badge" and badge_role_id is not None:
        badge_role = guild.get_role(badge_role_id)
        if badge_role is not None:
            badge_user_ids = [
                member.id
                for member in badge_role.members
                if not member.bot
            ]
    result = await bot.db.delete_object_with_balance_refunds(
        guild.id,
        item_type,
        name_key,
        badge_user_ids,
        refund_enabled,
        interaction.user.id,
        MAX_MONEY,
    )
    if result is None:
        return None
    for user_id in result["active_user_ids"]:
        bot.active_modifier_users.discard((guild.id, user_id))
    for channel_id in result["active_channel_ids"]:
        bot.active_modifier_channels.discard((guild.id, channel_id))
    for event in result["cancelled_events"]:
        bot.remove_question_event(
            guild.id,
            event["channel_id"],
            event["message_id"],
        )
        await finalize_question_embed(
            guild,
            event,
            "🚫 Cancelado porque se eliminó el objeto del premio.",
        )

    item = result["item"]
    type_label = {
        "badge": "insignia",
        "modifier": "modificador",
        "ticket": "ticket",
    }[item_type]
    action = {
        "badge": "badge_config_delete",
        "modifier": "modifier_config_delete",
        "ticket": "ticket_config_delete",
    }[item_type]
    extra = {
        "badge": "Los roles de Discord no fueron eliminados.",
        "modifier": "Sus unidades y activaciones también fueron eliminadas.",
        "ticket": "Sus unidades de inventario también fueron eliminadas.",
    }[item_type]
    await bot.db.record_movement(
        guild.id,
        None,
        interaction.user.id,
        action,
        None,
        f"Borró el {type_label} {item['name']}.",
    )

    if refund_enabled:
        refund_summary = (
            f"**Personas consideradas:** {result['eligible_users']}\n"
            f"**Unidades consideradas:** {result['eligible_units']}\n"
            f"**Saldo reembolsado:** {money(result['total_credited'], guild.id)}"
        )
        if result["total_requested"] > result["total_credited"]:
            refund_summary += (
                "\nParte del reembolso se limitó porque algún balance alcanzó "
                f"el máximo de {money(MAX_MONEY, guild.id)}."
            )
    else:
        refund_summary = (
            "**Reembolso:** No solicitado\n"
            f"**Personas que perdieron objetos:** {result['eligible_users']}\n"
            f"**Unidades eliminadas:** {result['eligible_units']}"
        )
    if result["direct_admin_activations"]:
        refund_summary += (
            "\n**Activaciones administrativas sin reembolso:** "
            f"{result['direct_admin_activations']}"
        )
    if result["cancelled_events"]:
        refund_summary += (
            "\n**Eventos de pregunta cancelados:** "
            f"{len(result['cancelled_events'])}"
        )
    await send_audit_log(
        guild,
        "Objeto borrado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Tipo:** {type_label.capitalize()}\n"
        f"**Objeto:** {item['name']}\n"
        f"**Precio usado por unidad:** {money(result['price'], guild.id)}\n"
        f"{refund_summary}\n"
        f"{extra}",
        color=0xEF4444,
    )
    return (
        f"Borré el {type_label} **{item['name']}**. {extra}\n"
        f"{refund_summary}"
    )


class DeleteObjectConfirmView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        item_type: str,
        name: str,
        name_key: str,
        price: int,
        badge_role_id: int | None,
        source_interaction: discord.Interaction,
    ) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.item_type = item_type
        self.name = name
        self.name_key = name_key
        self.price = price
        self.badge_role_id = badge_role_id
        self.source_interaction = source_interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo el administrador que abrió esta confirmación puede responder.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.source_interaction.edit_original_response(
                content="La confirmación expiró. No se borró ningún objeto.",
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            await interaction.edit_original_response(
                content="Ocurrió un error al borrar el objeto.",
                view=None,
            )
        except discord.HTTPException:
            pass

    async def finish_deletion(
        self,
        interaction: discord.Interaction,
        refund_enabled: bool,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Borrando el objeto y calculando los reembolsos...",
            view=self,
        )
        response = await delete_configured_object(
            interaction,
            self.item_type,
            self.name_key,
            self.badge_role_id,
            refund_enabled,
        )
        if response is None:
            await interaction.edit_original_response(
                content="Ese objeto ya no existe.",
                view=None,
            )
            self.stop()
            return
        published = False
        if interaction.channel is not None:
            try:
                await interaction.channel.send(
                    response,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                published = True
            except discord.HTTPException:
                pass
        await interaction.edit_original_response(
            content=(
                "Objeto borrado. El resultado se publicó en este canal."
                if published
                else f"El objeto se borró, pero no pude publicar el resultado.\n{response}"
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(
        label="Borrar y reembolsar",
        style=discord.ButtonStyle.danger,
        emoji="💰",
    )
    async def delete_with_refund(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.finish_deletion(interaction, True)

    @discord.ui.button(
        label="Borrar sin reembolsar",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
    )
    async def delete_without_refund(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.finish_deletion(interaction, False)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Borrado cancelado. No se modificó ningún objeto ni balance.",
            view=None,
        )
        self.stop()


@bot.tree.command(
    name="borrarobjeto",
    description="Borra un objeto configurado o una categoría de la tienda.",
)
@app_commands.describe(
    objeto="Insignia, modificador o ticket que se borrará",
    categoria="Categoría que se borrará; sus objetos pasarán a General",
)
@app_commands.autocomplete(
    objeto=deletable_object_autocomplete,
    categoria=category_autocomplete,
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def borrarobjeto(
    interaction: discord.Interaction,
    objeto: str | None = None,
    categoria: str | None = None,
) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    if (objeto is None) == (categoria is None):
        await answer(
            interaction,
            "Elige exactamente una opción: **objeto** o **categoria**.",
        )
        return
    if categoria is not None:
        category = await find_shop_category(interaction, categoria)
        if category is None:
            await answer(
                interaction,
                "Esa categoría no existe. **General** solo puede borrarse si fue "
                "configurada expresamente.",
            )
            return
        await interaction.response.defer()
        deleted = await bot.db.delete_shop_category(
            guild.id,
            category["name_key"],
        )
        if deleted is None:
            await answer(interaction, "Esa categoría ya no existe.")
            return
        affected = deleted["affected_items"]
        await bot.db.record_movement(
            guild.id,
            None,
            interaction.user.id,
            "shop_category_config_delete",
            None,
            f"Eliminó la categoría {deleted['name']}; "
            f"{affected} objetos pasaron a General.",
        )
        await send_audit_log(
            guild,
            "Categoría eliminada",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Categoría:** {deleted['name']}\n"
            f"**Objetos enviados a General:** {affected}",
            color=0xEF4444,
        )
        await answer(
            interaction,
            f"Eliminé la categoría **{deleted['name']}**. "
            f"**{affected}** objeto{'s' if affected != 1 else ''} "
            f"{'pasaron' if affected != 1 else 'pasó'} a **General**.",
        )
        return

    assert objeto is not None
    badge = await find_badge(interaction, objeto)
    modifier = None if badge is not None else await find_modifier(interaction, objeto)
    ticket = (
        None
        if badge is not None or modifier is not None
        else await find_ticket(interaction, objeto)
    )
    current = badge or modifier or ticket
    if current is None:
        await answer(interaction, "Ese objeto no existe.")
        return
    item_type = (
        "badge"
        if badge is not None
        else "modifier" if modifier is not None else "ticket"
    )
    view = DeleteObjectConfirmView(
        interaction.user.id,
        item_type,
        current["name"],
        current["name_key"],
        current["price"],
        current["badge_role_id"] if badge is not None else None,
        interaction,
    )
    await interaction.response.send_message(
        f"⚠️ Vas a borrar **{current['name']}** de forma permanente.\n"
        f"¿Quieres reembolsar a cada propietario "
        f"**{money(current['price'], interaction.guild_id)} por unidad**?\n\n"
        "Se incluyen objetos comprados o entregados por administradores. En "
        "modificadores activos se reembolsa a quien gastó la unidad; las activaciones "
        "creadas directamente por un administrador no reciben reembolso.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="duplicar",
    description="Duplica la configuración de una insignia, modificador o ticket.",
)
@app_commands.describe(objeto="Objeto configurado que se duplicará")
@app_commands.autocomplete(objeto=deletable_object_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def duplicar(interaction: discord.Interaction, objeto: str) -> None:
    assert interaction.guild_id is not None
    guild = interaction.guild
    if guild is None:
        return
    current = await bot.db.get_configured_object(
        interaction.guild_id,
        normalize_name(objeto),
    )
    if current is None:
        await answer(interaction, "Ese objeto no existe.")
        return
    duplicated_name = await next_duplicate_object_name(
        interaction.guild_id,
        current["name"],
    )
    if duplicated_name is None:
        await answer(
            interaction,
            "Ya existen demasiados duplicados de ese objeto. Renombra o borra uno "
            "antes de intentarlo otra vez.",
        )
        return

    item_type = current["item_type"]
    original_badge_role = None
    color_role = None
    if item_type == "badge":
        original_badge_role = guild.get_role(current["badge_role_id"])
        color_role = guild.get_role(current["color_role_id"])
        if original_badge_role is None or color_role is None:
            await answer(
                interaction,
                "No puedo duplicar esa insignia porque uno de sus roles ya no existe.",
            )
            return

    await interaction.response.defer()
    duplicated_badge_role: discord.Role | None = None

    async def remove_duplicated_role() -> None:
        if duplicated_badge_role is None:
            return
        try:
            await duplicated_badge_role.delete(
                reason="Se revirtió la duplicación de una insignia",
            )
        except discord.HTTPException:
            traceback.print_exc()

    if original_badge_role is not None:
        role_kwargs = {
            "name": duplicate_object_name(original_badge_role.name),
            "permissions": original_badge_role.permissions,
            "colour": original_badge_role.colour,
            "hoist": original_badge_role.hoist,
            "mentionable": original_badge_role.mentionable,
            "reason": f"Insignia duplicada por {interaction.user}",
        }
        secondary_colour = getattr(
            original_badge_role,
            "secondary_colour",
            None,
        )
        tertiary_colour = getattr(
            original_badge_role,
            "tertiary_colour",
            None,
        )
        if secondary_colour is not None:
            role_kwargs["secondary_colour"] = secondary_colour
        if tertiary_colour is not None:
            role_kwargs["tertiary_colour"] = tertiary_colour
        display_icon = getattr(original_badge_role, "display_icon", None)
        if isinstance(display_icon, str):
            role_kwargs["display_icon"] = display_icon
        try:
            duplicated_badge_role = await guild.create_role(**role_kwargs)
        except discord.HTTPException:
            await answer(
                interaction,
                "No pude crear el nuevo rol de insignia. Revisa mi permiso "
                "**Gestionar roles**.",
            )
            return
        if role_can_be_managed(original_badge_role):
            try:
                moved_role = await duplicated_badge_role.edit(
                    position=original_badge_role.position,
                    reason="Colocar el rol duplicado junto al original",
                )
                if moved_role is not None:
                    duplicated_badge_role = moved_role
            except discord.HTTPException:
                pass

    try:
        result = await bot.db.duplicate_configured_object(
            interaction.guild_id,
            item_type,
            current["name_key"],
            duplicated_name,
            normalize_name(duplicated_name),
            (
                duplicated_badge_role.id
                if duplicated_badge_role is not None
                else None
            ),
        )
    except asyncpg.UniqueViolationError:
        await remove_duplicated_role()
        await answer(
            interaction,
            "El nombre o el rol del duplicado entró en conflicto con otro objeto.",
        )
        return
    except Exception:
        await remove_duplicated_role()
        raise

    if result["status"] != "duplicated":
        await remove_duplicated_role()
        await answer(
            interaction,
            (
                "Ese objeto ya no existe."
                if result["status"] == "not_found"
                else "El nombre del duplicado ya está ocupado. Intenta nuevamente."
            ),
        )
        return

    type_label = {
        "badge": "insignia",
        "modifier": "modificador",
        "ticket": "ticket",
    }[item_type]
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        f"{item_type}_config_duplicate",
        None,
        f"Duplicó el {type_label} {current['name']} como {duplicated_name}.",
    )
    role_details = (
        f"\n**Nuevo rol de insignia:** {duplicated_badge_role.name} "
        f"(`{duplicated_badge_role.id}`)"
        f"\n**Rol de color conservado:** {color_role.name} (`{color_role.id}`)"
        if duplicated_badge_role is not None and color_role is not None
        else ""
    )
    await send_audit_log(
        guild,
        "Objeto duplicado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Tipo:** {type_label.capitalize()}\n"
        f"**Original:** {current['name']}\n"
        f"**Duplicado:** {duplicated_name}"
        f"{role_details}",
        color=0x3B82F6,
    )
    await answer(
        interaction,
        f"Dupliqué el {type_label} **{current['name']}** como "
        f"**{duplicated_name}**.{role_details}\n"
        "Puedes cambiar su configuración con `/editar`. No se copiaron "
        "propietarios ni unidades de inventario.",
    )


async def apply_object_section_toggle(
    interaction: discord.Interaction,
    section: str,
    reason: str,
    admins_bypass: bool,
) -> str:
    guild = interaction.guild
    if guild is None:
        return "No pude identificar el servidor."
    result = await bot.db.toggle_object_section(
        guild.id,
        section,
        reason,
        admins_bypass,
    )
    bot.invalidate_maintenance(guild.id)
    label = OBJECT_SECTION_LABELS[section]
    if result["enabled"]:
        action = "object_section_enable"
        description = f"Reactivó la sección de {label}."
        audit_description = (
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Sección:** {label.capitalize()}\n"
            "**Nuevo estado:** Activa"
        )
        response = f"Activé nuevamente la sección de **{label}**. Ya se pueden usar."
        color = 0x22C55E
    else:
        action = "object_section_disable"
        description = f"Desactivó la sección de {label}. Razón: {reason}"
        removed = result["removed"]
        refunds = result["refunds"]
        for row in removed:
            if row["target_kind"] == "individual":
                bot.active_modifier_users.discard(
                    (guild.id, row["target_id"])
                )
            else:
                bot.active_modifier_channels.discard(
                    (guild.id, row["target_id"])
                )
        for row in refunds:
            await bot.db.record_movement(
                guild.id,
                row["owner_user_id"],
                interaction.user.id,
                "modifier_section_refund",
                None,
                f"Recibió 1 unidad de {row['name']} porque se desactivaron "
                "temporalmente los modificadores.",
            )
        refund_details = []
        refunds_by_owner: dict[int, dict[str, int]] = {}
        for row in refunds:
            owner_refunds = refunds_by_owner.setdefault(row["owner_user_id"], {})
            owner_refunds[row["name"]] = owner_refunds.get(row["name"], 0) + 1
        for owner_id, refunded_items in refunds_by_owner.items():
            item_summary = ", ".join(
                f"{quantity} × {name}"
                for name, quantity in refunded_items.items()
            )
            total = sum(refunded_items.values())
            refund_details.append(
                f"• <@{owner_id}> recibió **{total} "
                f"{'unidad' if total == 1 else 'unidades'}**: {item_summary}."
            )
        for row in removed:
            if row["refundable"]:
                continue
            if row["target_kind"] == "channel":
                target_name = f"canal <#{row['target_id']}>"
            else:
                target = guild.get_member(row["target_id"])
                target_name = (
                    discord.utils.escape_mentions(
                        discord.utils.escape_markdown(target.display_name)
                    )
                    if target is not None
                    else f"Usuario {row['target_id']}"
                )
            if not row.get("was_active", False):
                no_refund_reason = "el modificador ya había vencido"
            else:
                no_refund_reason = (
                    "fue una activación administrativa y no consumió una unidad"
                )
            refund_details.append(
                f"• Sin reembolso por **{row['name']}** de **{target_name}**: "
                f"{no_refund_reason}."
            )
        if not refund_details and section in {
            "modifiers",
            "all_objects",
            "all_commands",
        }:
            refund_details.append(
                "• No había modificadores activos; no se realizó ningún reembolso."
            )
        modifier_summary = (
            f"\n**Activaciones canceladas:** {len(removed)}"
            f"\n**Unidades reembolsadas:** {len(refunds)}"
            if section in {"modifiers", "all_objects", "all_commands"}
            else ""
        )
        refund_detail_text = (
            "\n\n**Detalle de reembolsos:**\n" + "\n".join(refund_details)
            if refund_details
            else ""
        )
        admin_summary = (
            "\n**Administradores:** pueden ignorar este mantenimiento."
            if admins_bypass
            else "\n**Administradores:** también quedan sujetos al mantenimiento."
        )
        audit_description = (
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Sección:** {label.capitalize()}\n"
            "**Nuevo estado:** Inactiva\n"
            f"**Razón:** {reason}"
            f"{admin_summary}"
            f"{modifier_summary}"
        )
        response = (
            f"Desactivé temporalmente la sección de **{label}**.\n"
            f"**Razón:** {reason}"
            f"{admin_summary}"
            f"{modifier_summary}"
            f"{refund_detail_text}"
        )
        color = 0xEF4444
    await bot.db.record_movement(
        guild.id,
        None,
        interaction.user.id,
        action,
        None,
        description,
    )
    await send_audit_log(
        guild,
        "Estado de sección de objetos",
        audit_description,
        color=color,
    )
    return response


def split_maintenance_notice(content: str, limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in content.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks or [content[:limit]]


class ObjectSectionToggleConfirmView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        section: str,
        reason: str,
        admins_bypass: bool,
        source_interaction: discord.Interaction,
    ) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.section = section
        self.reason = reason
        self.admins_bypass = admins_bypass
        self.source_interaction = source_interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo el administrador que abrió esta confirmación puede responder.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.source_interaction.edit_original_response(
                content="La confirmación expiró. No se cambió ningún estado.",
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            await interaction.edit_original_response(
                content="Ocurrió un error al cambiar el mantenimiento.",
                view=None,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Confirmar cambio", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Aplicando el cambio de mantenimiento...",
            view=self,
        )
        response = await apply_object_section_toggle(
            interaction,
            self.section,
            self.reason,
            self.admins_bypass,
        )
        published = False
        if interaction.channel is not None and hasattr(interaction.channel, "send"):
            try:
                for chunk in split_maintenance_notice(response):
                    await interaction.channel.send(
                        f"📢 {chunk}",
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False,
                            users=True,
                            roles=False,
                            replied_user=False,
                        ),
                    )
                published = True
            except discord.HTTPException:
                pass
        if published:
            private_result = "Cambio aplicado. El aviso se publicó en este canal."
        else:
            private_result = (
                "El cambio sí se aplicó, pero no pude publicar el aviso en el canal. "
                "Revisa mis permisos para enviar mensajes."
            )
        await interaction.edit_original_response(content=private_result, view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Cambio cancelado. No se modificó ningún estado.",
            view=None,
        )
        self.stop()


@bot.tree.command(
    name="estadoobjetos",
    description="Alterna el mantenimiento de objetos, tienda o comandos.",
)
@app_commands.describe(
    seccion="Sección cuyo estado quieres alternar",
    permitir_admins="Permite que administradores ignoren este mantenimiento",
    razon="Motivo que verán los usuarios mientras esté desactivada",
)
@app_commands.choices(
    seccion=[
        app_commands.Choice(name="Insignias", value="badges"),
        app_commands.Choice(name="Modificadores", value="modifiers"),
        app_commands.Choice(name="Tickets", value="tickets"),
        app_commands.Choice(name="Tienda", value="shop"),
        app_commands.Choice(name="Todos los objetos y tienda", value="all_objects"),
        app_commands.Choice(name="Todos los comandos", value="all_commands"),
    ]
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def estadoobjetos(
    interaction: discord.Interaction,
    seccion: app_commands.Choice[str],
    permitir_admins: bool,
    razon: str | None = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    cleaned_reason = (razon or "Mantenimiento temporal.").strip()
    if len(cleaned_reason) > 500:
        await answer(
            interaction,
            "La razón puede tener hasta 500 caracteres.",
            ephemeral=True,
        )
        return
    current = await bot.db.get_object_section_setting(guild.id, seccion.value)
    action_text = "reactivar" if not current["enabled"] else "desactivar"
    if not current["enabled"]:
        admin_text = "Al reactivarlo, la opción `permitir_admins` deja de aplicar."
    elif permitir_admins:
        admin_text = "Los administradores podrán ignorarlo en comandos públicos."
    else:
        admin_text = "También afectará a administradores cuando usen comandos públicos."
    if current["enabled"] and seccion.value == "all_commands" and not permitir_admins:
        admin_text = (
            "También bloqueará los comandos administrativos; `/estadoobjetos` "
            "seguirá disponible."
        )
    refund_text = (
        "\n⚠️ Las activaciones de modificadores se cancelarán y sus unidades se reembolsarán."
        if current["enabled"]
        and seccion.value in {"modifiers", "all_objects", "all_commands"}
        else ""
    )
    view = ObjectSectionToggleConfirmView(
        interaction.user.id,
        seccion.value,
        cleaned_reason,
        permitir_admins,
        interaction,
    )
    await interaction.response.send_message(
        f"⚠️ Vas a **{action_text}** el mantenimiento de "
        f"**{OBJECT_SECTION_LABELS[seccion.value]}**.\n"
        f"**Razón:** {cleaned_reason}\n{admin_text}{refund_text}\n\n"
        "¿Confirmas el cambio?",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="estadomodificador",
    description="Alterna el modificador activo de un miembro o canal.",
)
@app_commands.describe(
    miembro="Objetivo para un modificador Individual",
    canal="Objetivo para un modificador de Canal",
    modificador="Se exige cuando el objetivo no tiene uno activo",
)
@app_commands.autocomplete(modificador=modifier_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def estadomodificador(
    interaction: discord.Interaction,
    miembro: discord.Member | None = None,
    canal: discord.TextChannel | None = None,
    modificador: str | None = None,
) -> None:
    guild = interaction.guild
    if guild is None or interaction.channel_id is None:
        return
    if (miembro is None) == (canal is None):
        await answer(
            interaction,
            "Selecciona exactamente un objetivo: **miembro** o **canal**.",
        )
        return
    target_kind = "channel" if canal is not None else "individual"
    target_id = canal.id if canal is not None else miembro.id
    target_label = canal.mention if canal is not None else miembro.mention
    active = (
        await bot.db.get_active_channel_modifier(guild.id, target_id)
        if target_kind == "channel"
        else await bot.db.get_active_modifier(guild.id, target_id)
    )
    if active is not None:
        deactivated = (
            await bot.db.deactivate_and_refund_channel_modifier(guild.id, target_id)
            if target_kind == "channel"
            else await bot.db.deactivate_and_refund_modifier(guild.id, target_id)
        )
        if target_kind == "individual":
            bot.active_modifier_users.discard((guild.id, target_id))
        else:
            bot.active_modifier_channels.discard((guild.id, target_id))
        if deactivated is None:
            await answer(
                interaction,
                "El estado del modificador cambió al mismo tiempo. Intenta nuevamente.",
            )
            return
        await bot.db.record_movement(
            guild.id,
            target_id if target_kind == "individual" else None,
            interaction.user.id,
            "modifier_admin_deactivate",
            None,
            f"Un administrador desactivó {deactivated['name']} para {target_label}.",
        )
        if deactivated["status"] == "refunded":
            owner_id = deactivated["owner_user_id"]
            owner = guild.get_member(owner_id)
            owner_label = owner.mention if owner is not None else f"<@{owner_id}>"
            refund_text = (
                f"\n**Reembolso:** 1 unidad para {owner_label} "
                f"(ahora tiene {deactivated['quantity']})."
            )
            await bot.db.record_movement(
                guild.id,
                owner_id,
                interaction.user.id,
                "modifier_admin_refund",
                None,
                f"Recibió 1 unidad de {deactivated['name']} al desactivarse "
                f"el efecto de {target_label}.",
            )
        elif deactivated["status"] == "deactivated_without_refund":
            refund_text = (
                "\n**Reembolso:** No corresponde; era una activación creada "
                "directamente por un administrador."
            )
        else:
            refund_text = (
                "\n**Reembolso:** No corresponde porque el efecto ya había vencido."
            )
        await send_audit_log(
            guild,
            "Modificador desactivado por administrador",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Objetivo:** {target_label}\n"
            f"**Modificador:** {deactivated['name']}"
            f"{refund_text}",
            color=0xEF4444,
        )
        await answer(
            interaction,
            f"Desactivaste **{deactivated['name']}** para {target_label}."
            f"{refund_text}",
        )
        return

    if not modificador or not modificador.strip():
        await answer(
            interaction,
            f"{target_label} no tiene un modificador activo. Debes elegir cuál activar.",
        )
        return
    item = await find_modifier(interaction, modificador)
    if item is None:
        await answer(interaction, "Ese modificador no existe.")
        return
    if item["effect_scope"] != target_kind:
        await answer(
            interaction,
            f"**{item['name']}** es de tipo "
            f"**{modifier_scope_label(item['effect_scope'])}**. Elige un objetivo compatible.",
        )
        return
    if miembro is not None and miembro.bot:
        await answer(interaction, "No puedes aplicar un modificador a un bot.")
        return
    if canal is not None:
        bot_member = guild.me
        if bot_member is None:
            await answer(interaction, "No pude comprobar mis permisos en ese canal.")
            return
        permissions = canal.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.manage_webhooks:
            await answer(
                interaction,
                f"Necesito **Enviar mensajes** y **Gestionar webhooks** en {canal.mention}.",
            )
            return
        activation = await bot.db.force_activate_channel_modifier(
            guild.id,
            canal.id,
            item["id"],
            item["duration_minutes"],
        )
    else:
        activation = await bot.db.force_activate_modifier(
            guild.id,
            miembro.id,
            item["id"],
            interaction.channel_id,
            item["duration_minutes"],
        )
    if activation["status"] == "disabled":
        await answer(
            interaction,
            disabled_object_section_text(
                "modifiers",
                activation.get("reason"),
            ),
        )
        return
    if target_kind == "individual":
        bot.active_modifier_users.add((guild.id, target_id))
    else:
        bot.active_modifier_channels.add((guild.id, target_id))
    expires_at = activation["expires_at"]
    await bot.db.record_movement(
        guild.id,
        target_id if target_kind == "individual" else None,
        interaction.user.id,
        "modifier_admin_activate",
        None,
        f"Un administrador activó {item['name']} para {target_label} durante "
        f"{item['duration_minutes']} minutos.",
    )
    await send_audit_log(
        guild,
        "Modificador activado por administrador",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Objetivo:** {target_label}\n"
        f"**Tipo:** {modifier_scope_label(item['effect_scope'])}\n"
        f"**Modificador:** {item['name']}\n"
        f"**Finaliza:** <t:{int(expires_at.timestamp())}:R>\n"
        "No se descontó ninguna unidad del inventario.",
        color=0xA855F7,
    )
    await answer(
        interaction,
        f"Activaste **{item['name']}** para {target_label} durante "
        f"**{item['duration_minutes']} minutos**. "
        f"Finaliza <t:{int(expires_at.timestamp())}:R>. "
        "No se descontó de su inventario.",
    )


@bot.tree.command(
    name="reembolsarmodificador",
    description="Quita un modificador activo y devuelve la unidad a quien la gastó.",
)
@app_commands.describe(
    miembro="Miembro que tiene activo un modificador Individual",
    canal="Canal que tiene activo un modificador de Canal",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def reembolsarmodificador(
    interaction: discord.Interaction,
    miembro: discord.Member | None = None,
    canal: discord.TextChannel | None = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    if (miembro is None) == (canal is None):
        await answer(
            interaction,
            "Selecciona exactamente un objetivo: **miembro** o **canal**.",
        )
        return
    target_kind = "channel" if canal is not None else "individual"
    target_id = canal.id if canal is not None else miembro.id
    target_label = canal.mention if canal is not None else miembro.mention
    await interaction.response.defer()
    result = (
        await bot.db.deactivate_and_refund_channel_modifier(guild.id, target_id)
        if target_kind == "channel"
        else await bot.db.deactivate_and_refund_modifier(guild.id, target_id)
    )
    if result is None:
        await answer(interaction, f"{target_label} no tiene un modificador activo.")
        return
    if target_kind == "individual":
        bot.active_modifier_users.discard((guild.id, target_id))
    else:
        bot.active_modifier_channels.discard((guild.id, target_id))
    if result["status"] == "expired":
        await answer(
            interaction,
            f"**{result['name']}** ya había terminado para {target_label}. "
            "Retiré el registro pendiente, pero no correspondía un reembolso.",
        )
        return
    if result["status"] == "deactivated_without_refund":
        await bot.db.record_movement(
            guild.id,
            target_id if target_kind == "individual" else None,
            interaction.user.id,
            "modifier_admin_remove_without_refund",
            None,
            f"Retiró {result['name']}; la activación no había consumido una unidad.",
        )
        await send_audit_log(
            guild,
            "Modificador retirado sin reembolso",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Objetivo afectado:** {target_label}\n"
            f"**Modificador:** {result['name']}\n"
            "La activación no tenía un propietario de consumo registrado.",
            color=0xF59E0B,
        )
        await answer(
            interaction,
            f"Retiré **{result['name']}** de {target_label}. No devolví ninguna "
            "unidad porque esa activación no había consumido un objeto del inventario.",
        )
        return

    owner_id = result["owner_user_id"]
    owner = guild.get_member(owner_id)
    owner_label = owner.mention if owner is not None else f"<@{owner_id}>"
    await bot.db.record_movement(
        guild.id,
        owner_id,
        interaction.user.id,
        "modifier_admin_refund",
        None,
        f"Recibió 1 unidad de {result['name']} tras retirar el efecto de "
        f"{target_label}.",
    )
    await send_audit_log(
        guild,
        "Modificador retirado y reembolsado",
        f"**Administrador:** {interaction.user.mention}\n"
        f"**Objetivo afectado:** {target_label}\n"
        f"**Modificador:** {result['name']}\n"
        f"**Reembolso para:** {owner_label}\n"
        f"**Cantidad actual:** {result['quantity']}",
        color=0x22C55E,
    )
    await answer(
        interaction,
        f"Retiré **{result['name']}** de {target_label} y devolví **1 unidad** "
        f"a {owner_label}. Ahora tiene **{result['quantity']}**.",
    )


@bot.tree.command(name="ayuda", description="Muestra la guía de comandos de Sularea.")
@app_commands.describe(admin="Muestra únicamente la ayuda administrativa")
@app_commands.guild_only()
async def ayuda(interaction: discord.Interaction, admin: bool = False) -> None:
    member = guild_member(interaction)
    if admin and (
        member is None or not member.guild_permissions.administrator
    ):
        await answer(
            interaction,
            "Solo los administradores pueden abrir la sección administrativa de ayuda.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Ayuda administrativa de Sularea" if admin else "Ayuda de Sularea",
        description=(
            "Configuración y administración del mercado, objetos, eventos y registros."
            if admin
            else (
                "Participa en eventos para conseguir monedas, insignias, modificadores "
                "y tickets. Responde directamente al mensaje de cada pregunta. "
                "Después puedes comprar y activar roles de color especiales."
            )
        ),
        color=0xEF4444 if admin else 0x8B5CF6,
    )
    if interaction.guild is not None and interaction.guild.icon is not None:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    if admin:
        embed.add_field(
            name="Objetos e inventarios",
            value=(
                "`/inventario [miembro]` · `/objetos` · `/mensajes`\n"
                "`/darobjeto` · `/quitarobjeto miembro objeto [cantidad]`\n"
                "`/duplicar objeto` · `/borrarobjeto [objeto/categoría]`\n"
                "`/estadoobjetos sección permitir_admins [razón]`\n"
                "`/estadomodificador` · `/reembolsarmodificador [miembro/canal]`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Configuración de objetos",
            value=(
                "`/configurarcategoria` · `/configurarinsignia`\n"
                "`/configurarmodificador` · `/configurarticket`\n"
                "`/editar` · `/editarmensajes`\n"
                "`/añadiradmin` · `/quitaradmin` · `/admins`\n"
                "`/añadirwhitelist` · `/quitarwhitelist` · `/whitelist`\n"
                "`/configurardescuentowhitelist`\n"
                "`/configurarmultiplicadorwhitelist`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Economía, eventos y servidor",
            value=(
                "`/revisarbalance` · `/modificarbalance`\n"
                "`/eventopregunta` · `/cancelarevento`\n"
                "`/configurarregistro` · `/estadisticas` · `/exportardatos` · `/say`\n"
                "`/configurarmensajeautomatico` · `/editarmensajeautomatico`\n"
                "`/mensajesautomaticos`\n"
                "`/configurarmoneda` · `/configuraremojiwhitelist`\n"
                "`/configuracion`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Premios de eventos",
            value=(
                "`/eventopregunta` permite combinar **monedas** con hasta "
                "**tres objetos distintos**. La cantidad puede ser mayor para "
                "modificadores y tickets; las insignias siempre entregan una. "
                "El multiplicador de whitelist solo aumenta las monedas. "
                "Al finalizar se revela la respuesta correcta."
            ),
            inline=False,
        )
        embed.add_field(
            name="Notas de modificadores",
            value=(
                "Cada modificador es **Individual** o de **Canal**. "
                "`/estadomodificador` alterna el efecto del miembro o canal elegido; "
                "si no hay uno activo, exige seleccionar un modificador compatible. "
                "El de Canal tiene prioridad sobre los Individuales dentro de ese "
                "canal. La probabilidad acepta `25` o `1/4`."
            ),
            inline=False,
        )
        embed.set_footer(text="Sección disponible únicamente para administradores.")
    else:
        embed.add_field(
            name="Inventario y objetos",
            value=(
                "`/inventario` — Ver tus insignias y consumibles.\n"
                "`/usar objeto [miembro/canal]` — Activar una insignia, consumir un "
                "ticket o aplicar un modificador Individual o de Canal.\n"
                "`/quitar` — Quitar tu color sin perder insignias."
            ),
            inline=False,
        )
        embed.add_field(
            name="Monedas y mercado",
            value=(
                "`/balance` — Consultar tus monedas.\n"
                "`/historial` — Ver tus últimos 10 movimientos.\n"
                "`/ranking` — Ver los balances más altos.\n"
                "`/tienda` — Abrir el mercado por categorías.\n"
                "`/comprar objeto` — Comprar por nombre; un ticket inactivo pide "
                "confirmación privada.\n"
                "La whitelist puede tener descuento en el mercado y multiplicador "
                "de monedas al ganar eventos."
            ),
            inline=False,
        )
    await answer(interaction, embed=embed)


@bot.tree.error
async def command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CheckFailure) and interaction.response.is_done():
        return
    if isinstance(error, app_commands.MissingPermissions):
        await answer(interaction, "Solo los administradores pueden usar este comando.")
        return
    if isinstance(error, app_commands.BotMissingPermissions):
        await answer(interaction, "Me faltan permisos para hacer eso.")
        return
    if isinstance(error, app_commands.NoPrivateMessage):
        await answer(interaction, "Este comando solo funciona dentro de un servidor.")
        return
    original = getattr(error, "original", error)
    if isinstance(original, discord.Forbidden):
        await answer(
            interaction,
            "Discord no me permitió hacerlo. Dame **Gestionar roles** y coloca mi rol "
            "encima de los roles configurados.",
        )
        return
    traceback.print_exception(type(original), original, original.__traceback__)
    await answer(interaction, "Ocurrió un error inesperado. Revisa los registros del bot.")


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Falta DISCORD_TOKEN en el archivo .env o en Railway.")

bot.run(token)
