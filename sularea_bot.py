import asyncio
import hashlib
import io
import json
import os
import traceback
import unicodedata
from datetime import datetime, timezone

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database


load_dotenv()
MAX_MONEY = 1_000_000_000_000


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def answer_hash(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    normalized = " ".join(without_accents.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def money(value: int) -> str:
    formatted = f"{value:,}".replace(",", ".")
    return f"🪙 {formatted}"


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


class SulareaBot(commands.Bot):
    db: Database

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.purchase_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "Falta DATABASE_URL. Añade PostgreSQL en Railway y conecta su "
                "variable DATABASE_URL al servicio del bot."
            )
        self.db = Database(database_url)
        await self.db.connect()
        await self.tree.sync()
        if not event_expiration_loop.is_running():
            event_expiration_loop.start()

    async def close(self) -> None:
        if event_expiration_loop.is_running():
            event_expiration_loop.cancel()
        if hasattr(self, "db"):
            await self.db.close()
        await super().close()


bot = SulareaBot()


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
    settings = await bot.db.get_log_settings(guild.id)
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
        await bot.db.record_movement(
            event["guild_id"],
            None,
            event["created_by"],
            "event_expire",
            event["reward"],
            f"Caducó el evento de pregunta: {event['question']}",
        )
        guild = bot.get_guild(event["guild_id"])
        if guild is None:
            continue
        channel = guild.get_channel(event["channel_id"]) or guild.get_thread(
            event["channel_id"]
        )
        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(
                    f"⌛ El evento **{event['question']}** caducó sin ganador."
                )
            except discord.HTTPException:
                traceback.print_exc()
        await send_audit_log(
            guild,
            "Evento de pregunta caducado",
            f"**Pregunta:** {event['question']}\n"
            f"**Recompensa:** {money(event['reward'])} monedas\n"
            "Terminó sin ganador.",
            color=0x6B7280,
        )


@event_expiration_loop.before_loop
async def before_event_expiration_loop() -> None:
    await bot.wait_until_ready()


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
        history_text = f"Un administrador añadió {money(amount)} monedas."
    elif action == "remove":
        affected_ids = await bot.db.remove_balance_many(guild.id, user_ids, amount)
        movement_action = "balance_remove"
        movement_amount = -amount
        verb = "Quitó"
        history_text = f"Un administrador quitó {money(amount)} monedas."
    else:
        await bot.db.set_balance_many(guild.id, user_ids, amount)
        affected_ids = user_ids
        movement_action = "balance_set"
        movement_amount = amount
        verb = "Estableció"
        history_text = f"Un administrador estableció el balance en {money(amount)} monedas."

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
                f"Su balance es **{money(current_balance)} monedas**."
            )
        elif action == "set":
            result = (
                f"El balance de {members[0].mention} ahora es "
                f"**{money(current_balance)} monedas**. Se aplicó a **1 miembro**."
            )
        else:
            action_result = "Añadiste" if action == "add" else "Quitaste"
            result = (
                f"{action_result} **{money(amount)} monedas** a {members[0].mention}. "
                f"Nuevo balance: **{money(current_balance)} monedas**."
            )
    elif action == "set":
        result = (
            f"Establecí el balance de {target_mention} en **{money(amount)} monedas**. "
            f"Se aplicó a **{affected} {'miembro' if affected == 1 else 'miembros'}**."
        )
    else:
        result = (
            f"{verb} **{money(amount)} monedas** a **{affected} de "
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
        f"**Acción:** {verb} {money(amount)} monedas\n"
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
            "remove": "quitar",
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
            f"⚠️ Vas a **{action_text} {money(amount)} monedas** para "
            f"**{len(members)} miembros** con el rol {target.mention}. ¿Confirmar?",
            view=view,
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


async def badge_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    rows = await bot.db.list_badges(interaction.guild_id)
    search = normalize_name(current)
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
        if search in row["name_key"]
    ][:25]


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


async def shop_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None or not hasattr(bot, "db"):
        return []
    rows = await bot.db.list_badges(interaction.guild_id, purchasable_only=True)
    member = guild_member(interaction)
    owned_role_ids = {role.id for role in member.roles} if member is not None else set()
    search = normalize_name(current)
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in rows
        if row["badge_role_id"] not in owned_role_ids and search in row["name_key"]
    ][:25]


async def process_purchase(
    interaction: discord.Interaction,
    badge_name: str,
) -> None:
    member = guild_member(interaction)
    guild = interaction.guild
    badge = await find_badge(interaction, badge_name)
    if member is None or guild is None or badge is None or not badge["purchasable"]:
        await answer(interaction, "Esa insignia no está disponible en la tienda.")
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

    await interaction.response.defer()
    lock = bot.purchase_locks.setdefault((guild.id, member.id), asyncio.Lock())
    async with lock:
        if role in member.roles:
            await answer(interaction, "Ya tienes esa insignia.")
            return
        new_balance = await bot.db.spend_balance(guild.id, member.id, badge["price"])
        if new_balance is None:
            current = await bot.db.get_balance(guild.id, member.id)
            await answer(
                interaction,
                f"No tienes suficiente dinero. Tienes **{money(current)} monedas**.",
            )
            return
        try:
            await member.add_roles(role, reason=f"Compra: {badge['name']}")
        except Exception:
            await bot.db.add_balance(guild.id, member.id, badge["price"])
            raise

    await bot.db.record_movement(
        guild.id,
        member.id,
        member.id,
        "purchase",
        -badge["price"],
        f"Compró la insignia {badge['name']} por {money(badge['price'])} monedas.",
    )
    await send_audit_log(
        guild,
        "Compra en el Mercado de Sularea",
        f"{member.mention} compró **{badge['name']}** (<@&{badge['color_role_id']}>) "
        f"por **{money(badge['price'])} monedas**.\n"
        f"**Nuevo balance:** {money(new_balance)} monedas",
        color=0xF59E0B,
    )
    await answer(
        interaction,
        f"{member.mention} compró **{badge['name']}** por "
        f"**{money(badge['price'])} monedas**. Su nuevo balance es "
        f"**{money(new_balance)} monedas**.",
    )


@bot.event
async def on_ready() -> None:
    if bot.user:
        print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None or not hasattr(bot, "db"):
        return
    event = await bot.db.claim_question_event(
        message.guild.id,
        message.channel.id,
        answer_hash(message.content),
        message.author.id,
    )
    if event is not None:
        await message.channel.send(
            f"🎉 {message.author.mention} respondió correctamente y ganó "
            f"**{money(event['reward'])} monedas**. Su nuevo balance es "
            f"**{money(event['new_balance'])} monedas**!"
        )
        await send_audit_log(
            message.guild,
            "Evento de pregunta ganado",
            f"**Ganador:** {message.author.mention}\n"
            f"**Pregunta:** {event['question']}\n"
            f"**Recompensa:** {money(event['reward'])} monedas\n"
            f"**Canal:** {message.channel.mention}",
            color=0x22C55E,
        )
    await bot.process_commands(message)


@bot.tree.command(name="say", description="Hace que el bot envíe un mensaje.")
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


@bot.tree.command(name="eventopregunta", description="Crea una pregunta con recompensa.")
@app_commands.describe(
    pregunta="Pregunta o texto que se publicará",
    respuesta="Respuesta correcta (no se mostrará)",
    minutos="Minutos antes de que el evento caduque",
    recompensa="Cantidad de monedas para el ganador",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def eventopregunta(
    interaction: discord.Interaction,
    pregunta: app_commands.Range[str, 1, 1000],
    respuesta: app_commands.Range[str, 1, 200],
    minutos: app_commands.Range[int, 1, 1440],
    recompensa: app_commands.Range[int, 1, MAX_MONEY],
) -> None:
    assert interaction.guild_id is not None and interaction.channel_id is not None
    if not " ".join(respuesta.split()):
        await answer(interaction, "La respuesta no puede estar vacía.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    expires_at = await bot.db.create_question_event(
        interaction.guild_id,
        interaction.channel_id,
        pregunta.strip(),
        answer_hash(respuesta),
        recompensa,
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

    embed = discord.Embed(
        title="❓ Evento de pregunta",
        description=pregunta.strip(),
        color=0xEC4899,
    )
    embed.add_field(name="Recompensa", value=f"{money(recompensa)} monedas")
    embed.add_field(
        name="Finaliza",
        value=f"<t:{int(expires_at.timestamp())}:R>",
    )
    embed.set_footer(
        text="La primera persona que escriba la respuesta correcta en este canal gana."
    )
    if interaction.guild is not None and interaction.guild.icon is not None:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    if interaction.channel is None:
        await interaction.edit_original_response(
            content="No pude encontrar el canal donde publicar el evento."
        )
        await bot.db.cancel_question_event(interaction.guild_id, interaction.channel_id)
        return
    try:
        await interaction.channel.send(embed=embed)
    except discord.HTTPException:
        await bot.db.cancel_question_event(interaction.guild_id, interaction.channel_id)
        await interaction.edit_original_response(
            content="No pude publicar el evento en este canal. Revisa mis permisos."
        )
        return
    await interaction.delete_original_response()

    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "event_create",
        recompensa,
        f"Creó un evento de pregunta: {pregunta.strip()}",
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Evento de pregunta creado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Pregunta:** {pregunta.strip()}\n"
            f"**Recompensa:** {money(recompensa)} monedas\n"
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
    await answer(interaction, f"Cancelé el evento **{event['question']}**.")
    await bot.db.record_movement(
        interaction.guild_id,
        None,
        interaction.user.id,
        "event_cancel",
        event["reward"],
        f"Canceló el evento de pregunta: {event['question']}",
    )
    if interaction.guild is not None:
        await send_audit_log(
            interaction.guild,
            "Evento de pregunta cancelado",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Pregunta:** {event['question']}\n"
            f"**Canal:** <#{interaction.channel_id}>",
            color=0xEF4444,
        )


@bot.tree.command(name="usar", description="Activa el color de una insignia que posees.")
@app_commands.describe(insignia="Nombre de la insignia que quieres usar")
@app_commands.autocomplete(insignia=owned_badge_autocomplete)
@app_commands.guild_only()
async def usar(interaction: discord.Interaction, insignia: str) -> None:
    member = guild_member(interaction)
    guild = interaction.guild
    badge = await find_badge(interaction, insignia)
    if member is None or guild is None or badge is None:
        await answer(interaction, "Esa insignia no existe.")
        return
    badge_role = guild.get_role(badge["badge_role_id"])
    color_role = guild.get_role(badge["color_role_id"])
    if badge_role is None or color_role is None:
        await answer(interaction, "La configuración de esa insignia tiene un rol eliminado.")
        return
    if badge_role not in member.roles:
        await answer(interaction, "No tienes esa insignia.")
        return
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
        f"{member.mention} activó **{badge['name']}** ({color_role.mention}).",
        color=0x8B5CF6,
    )
    await answer(interaction, f"Activaste el color de **{badge['name']}**.")


@bot.tree.command(name="quitar", description="Quita tu rol de color activo.")
@app_commands.guild_only()
async def quitar(interaction: discord.Interaction) -> None:
    member = guild_member(interaction)
    if member is None or interaction.guild_id is None:
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


@bot.tree.command(name="inventario", description="Muestra tus insignias disponibles.")
@app_commands.guild_only()
async def inventario(interaction: discord.Interaction) -> None:
    member = guild_member(interaction)
    guild = interaction.guild
    if member is None or guild is None:
        return
    rows = await bot.db.list_badges(guild.id)
    owned = [row for row in rows if guild.get_role(row["badge_role_id"]) in member.roles]
    embed = discord.Embed(title=f"Insignias de {member.display_name}", color=0x8B5CF6)
    embed.set_thumbnail(url=member.display_avatar.url)
    if owned:
        embed.description = "\n".join(
            f"• **{row['name']}** (<@&{row['color_role_id']}>)"
            for row in owned
        )[:4000]
        embed.set_footer(
            text="Usa /usar para activar una insignia y /quitar para quitar el rol de color."
        )
    else:
        embed.description = (
            "No tienes insignias activas. Puedes comprar una en **/tienda** "
            "y consultar tus monedas con **/balance**."
        )
    await answer(interaction, embed=embed)


@bot.tree.command(name="insignias", description="Consulta insignias y configuraciones del servidor.")
@app_commands.describe(miembro="Miembro que quieres consultar (opcional)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def insignias(
    interaction: discord.Interaction,
    miembro: discord.Member | None = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    rows = await bot.db.list_badges(guild.id)

    if miembro is not None:
        owned = [
            row
            for row in rows
            if guild.get_role(row["badge_role_id"]) in miembro.roles
        ]
        embed = discord.Embed(
            title=f"Insignias de {miembro.display_name}",
            color=0x3B82F6,
        )
        if owned:
            embed.description = "\n".join(
                f"• **{row['name']}** — <@&{row['badge_role_id']}> "
                f"(color: <@&{row['color_role_id']}>)"
                for row in owned
            )[:4000]
        else:
            embed.description = f"{miembro.mention} no tiene insignias."
        await answer(interaction, embed=embed)
        return

    embed = discord.Embed(title="Configuración de insignias", color=0x3B82F6)
    if rows:
        details = []
        for row in rows:
            sale = (
                f"Sí — {money(row['price'])} monedas"
                if row["purchasable"]
                else "No"
            )
            details.append(
                f"• **{row['name']}**\n"
                f"  Insignia: <@&{row['badge_role_id']}> · "
                f"Color: <@&{row['color_role_id']}> · Comprable: {sale}"
            )
        embed.description = "\n".join(details)[:4000]
    else:
        embed.description = "No hay insignias configuradas en este servidor."
    embed.set_footer(text="Usa /insignias miembro para consultar a un jugador.")
    await answer(interaction, embed=embed)


@bot.tree.command(name="tienda", description="Muestra las insignias disponibles para comprar.")
@app_commands.guild_only()
async def tienda(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None and interaction.guild is not None
    rows = await bot.db.list_badges(interaction.guild_id, purchasable_only=True)
    embed = discord.Embed(title="Mercado de Sularea", color=0xF59E0B)
    if interaction.guild.icon is not None:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    if rows:
        embed.description = "\n".join(
            f"• **{row['name']}** (<@&{row['color_role_id']}>) — "
            f"{money(row['price'])} monedas"
            for row in rows
        )[:4000]
        embed.set_footer(text="Usa /comprar para obtener una insignia.")
    else:
        embed.description = "No hay insignias a la venta en este momento."
    await answer(interaction, embed=embed)


@bot.tree.command(name="comprar", description="Compra una insignia de la tienda.")
@app_commands.describe(insignia="Nombre de la insignia que quieres comprar")
@app_commands.autocomplete(insignia=shop_autocomplete)
@app_commands.guild_only()
async def comprar(interaction: discord.Interaction, insignia: str) -> None:
    await process_purchase(interaction, insignia)


@bot.tree.command(name="balance", description="Muestra tu balance de monedas.")
@app_commands.guild_only()
async def balance(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    value = await bot.db.get_balance(interaction.guild_id, interaction.user.id)
    await answer(
        interaction,
        f"Tu balance es **{money(value)} monedas**. Puedes conseguir dinero "
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
        "badge_grant": "🏅",
        "badge_remove": "🗑️",
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
        embed.description = "Todavía no hay miembros con dinero registrado."
    else:
        medals = ["🥇", "🥈", "🥉"]
        embed.description = "\n".join(
            f"{medals[index] if index < 3 else f'**{index + 1}.**'} "
            f"<@{row['user_id']}> — **{money(row['balance'])} monedas**"
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
        f"El balance de {miembro.mention} es **{money(value)} monedas**.",
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
    current = await bot.db.get_log_settings(interaction.guild_id)
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
    embed.add_field(name="Dinero total", value=f"{money(stats['total_money'])} monedas")
    embed.add_field(name="Insignias configuradas", value=str(stats["badges"]))
    embed.add_field(name="Compras realizadas", value=str(stats["purchases"]))
    embed.add_field(name="Eventos ganados", value=str(stats["event_wins"]))
    embed.add_field(name="Eventos activos", value=str(stats["active_events"]))
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


@bot.tree.command(name="añadirbalance", description="Añade monedas a un miembro o rol completo.")
@app_commands.describe(
    objetivo="Miembro o rol que recibirá las monedas",
    cantidad="Cantidad a añadir",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def añadirbalance(
    interaction: discord.Interaction,
    objetivo: discord.Member | discord.Role,
    cantidad: app_commands.Range[int, 1, MAX_MONEY],
) -> None:
    await handle_balance_command(interaction, objetivo, cantidad, "add")


@bot.tree.command(name="quitarbalance", description="Quita monedas a un miembro o rol completo.")
@app_commands.describe(
    objetivo="Miembro o rol al que se quitarán monedas",
    cantidad="Cantidad a quitar",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def quitarbalance(
    interaction: discord.Interaction,
    objetivo: discord.Member | discord.Role,
    cantidad: app_commands.Range[int, 1, MAX_MONEY],
) -> None:
    await handle_balance_command(interaction, objetivo, cantidad, "remove")


@bot.tree.command(name="setbalance", description="Establece el balance de un miembro o rol completo.")
@app_commands.describe(
    objetivo="Miembro o rol cuyo balance cambiará",
    cantidad="Nuevo balance",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setbalance(
    interaction: discord.Interaction,
    objetivo: discord.Member | discord.Role,
    cantidad: app_commands.Range[int, 0, MAX_MONEY],
) -> None:
    await handle_balance_command(interaction, objetivo, cantidad, "set")


@bot.tree.command(name="darinsignia", description="Entrega una insignia a un miembro.")
@app_commands.describe(miembro="Miembro que recibirá la insignia", insignia="Nombre de la insignia")
@app_commands.autocomplete(insignia=badge_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def darinsignia(interaction: discord.Interaction, miembro: discord.Member, insignia: str) -> None:
    guild = interaction.guild
    badge = await find_badge(interaction, insignia)
    if guild is None or badge is None:
        await answer(interaction, "Esa insignia no existe.")
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


@bot.tree.command(name="quitarinsignia", description="Retira una insignia a un miembro.")
@app_commands.describe(miembro="Miembro que perderá la insignia", insignia="Nombre de la insignia")
@app_commands.autocomplete(insignia=badge_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def quitarinsignia(interaction: discord.Interaction, miembro: discord.Member, insignia: str) -> None:
    guild = interaction.guild
    badge = await find_badge(interaction, insignia)
    if guild is None or badge is None:
        await answer(interaction, "Esa insignia no existe.")
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
    await miembro.remove_roles(*roles, reason=f"Insignia retirada por {interaction.user}")
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


@bot.tree.command(name="configurarinsignia", description="Configura una insignia y su rol de color.")
@app_commands.describe(
    rol_insignia="Rol que representa la propiedad de la insignia",
    rol_color="Rol decorativo que se activará con /usar",
    comprable="Indica si aparecerá en la tienda",
    precio="Precio; usa 0 si será gratuita",
    nombre="Nombre para los comandos; por defecto usa el nombre del rol",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def configurarinsignia(
    interaction: discord.Interaction,
    rol_insignia: discord.Role,
    rol_color: discord.Role,
    comprable: bool,
    precio: app_commands.Range[int, 0, MAX_MONEY] = 0,
    nombre: str | None = None,
) -> None:
    assert interaction.guild_id is not None
    display_name = (nombre or rol_insignia.name).strip()
    if not display_name or len(display_name) > 100:
        await answer(interaction, "El nombre debe tener entre 1 y 100 caracteres.")
        return
    if rol_insignia == rol_color:
        await answer(interaction, "El rol de insignia y el rol de color deben ser diferentes.")
        return
    if not role_can_be_managed(rol_insignia) or not role_can_be_managed(rol_color):
        await answer(interaction, "Ambos roles deben estar debajo del rol del bot.")
        return
    if not comprable:
        precio = 0
    await interaction.response.defer()
    try:
        await bot.db.create_badge(
            interaction.guild_id, display_name, normalize_name(display_name),
            rol_insignia.id, rol_color.id, comprable, precio,
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
            f"**Precio:** {money(precio)} monedas",
        )
    await answer(interaction, f"Configuré la insignia **{display_name}** correctamente.")


@bot.tree.command(name="editarinsignia", description="Edita una insignia configurada.")
@app_commands.describe(
    insignia="Nombre actual", nuevo_nombre="Nuevo nombre (opcional)",
    rol_insignia="Nuevo rol de insignia", rol_color="Nuevo rol de color",
    comprable="Cambiar si aparece en la tienda", precio="Nuevo precio",
)
@app_commands.autocomplete(insignia=badge_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def editarinsignia(
    interaction: discord.Interaction,
    insignia: str,
    nuevo_nombre: str | None = None,
    rol_insignia: discord.Role | None = None,
    rol_color: discord.Role | None = None,
    comprable: bool | None = None,
    precio: int | None = None,
) -> None:
    assert interaction.guild_id is not None
    current = await find_badge(interaction, insignia)
    if current is None:
        await answer(interaction, "Esa insignia no existe.")
        return
    final_name = nuevo_nombre.strip() if nuevo_nombre is not None else current["name"]
    final_badge_role = rol_insignia.id if rol_insignia else current["badge_role_id"]
    final_color_role = rol_color.id if rol_color else current["color_role_id"]
    final_purchasable = comprable if comprable is not None else current["purchasable"]
    final_price = precio if precio is not None else current["price"]
    if not final_name or len(final_name) > 100:
        await answer(interaction, "El nombre debe tener entre 1 y 100 caracteres.")
        return
    if final_price < 0 or final_price > MAX_MONEY:
        await answer(interaction, f"El precio debe estar entre 0 y {money(MAX_MONEY)}.")
        return
    if not final_purchasable:
        final_price = 0
    if final_badge_role == final_color_role:
        await answer(interaction, "El rol de insignia y el rol de color deben ser diferentes.")
        return
    if any(role is not None and not role_can_be_managed(role) for role in (rol_insignia, rol_color)):
        await answer(interaction, "Los roles deben estar debajo del rol del bot.")
        return
    await interaction.response.defer()
    try:
        updated = await bot.db.update_badge(
            interaction.guild_id, current["name_key"], final_name,
            normalize_name(final_name), final_badge_role, final_color_role,
            final_purchasable, final_price,
        )
    except asyncpg.UniqueViolationError:
        await answer(interaction, "Ese nombre o rol ya pertenece a otra insignia.")
        return
    if not updated:
        await answer(interaction, "No pude encontrar esa insignia.")
        return
    if interaction.guild is not None:
        await bot.db.record_movement(
            interaction.guild_id,
            None,
            interaction.user.id,
            "badge_config_edit",
            None,
            f"Editó la insignia {current['name']} como {final_name}.",
        )
        await send_audit_log(
            interaction.guild,
            "Insignia editada",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Nombre anterior:** {current['name']}\n"
            f"**Nombre actual:** {final_name}\n"
            f"**Rol de insignia:** <@&{final_badge_role}>\n"
            f"**Rol de color:** <@&{final_color_role}>\n"
            f"**Comprable:** {'Sí' if final_purchasable else 'No'}\n"
            f"**Precio:** {money(final_price)} monedas",
        )
    await answer(interaction, f"Actualicé la insignia **{final_name}**.")


@bot.tree.command(name="borrarinsignia", description="Borra la configuración de una insignia.")
@app_commands.describe(insignia="Nombre de la insignia que se borrará")
@app_commands.autocomplete(insignia=badge_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def borrarinsignia(interaction: discord.Interaction, insignia: str) -> None:
    assert interaction.guild_id is not None
    await interaction.response.defer()
    deleted = await bot.db.delete_badge(interaction.guild_id, normalize_name(insignia))
    if deleted is None:
        await answer(interaction, "Esa insignia no existe.")
        return
    if interaction.guild is not None:
        await bot.db.record_movement(
            interaction.guild_id,
            None,
            interaction.user.id,
            "badge_config_delete",
            None,
            f"Borró la configuración de {deleted['name']}.",
        )
        await send_audit_log(
            interaction.guild,
            "Configuración de insignia borrada",
            f"**Administrador:** {interaction.user.mention}\n"
            f"**Insignia:** {deleted['name']}\n"
            "Los roles de Discord no fueron eliminados.",
            color=0xEF4444,
        )
    await answer(interaction, f"Borré **{deleted['name']}**. No eliminé ningún rol del servidor.")


@bot.tree.command(name="ayuda", description="Muestra la guía de comandos de Sularea.")
@app_commands.guild_only()
async def ayuda(interaction: discord.Interaction) -> None:
    member = guild_member(interaction)
    embed = discord.Embed(
        title="Ayuda de Sularea",
        description=(
            "Participa en eventos para conseguir monedas e insignias. "
            "Responde las preguntas directamente en el canal. Después puedes "
            "comprar y activar roles de color especiales."
        ),
        color=0x8B5CF6,
    )
    if interaction.guild is not None and interaction.guild.icon is not None:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.add_field(
        name="Insignias",
        value=(
            "`/inventario` — Ver tus insignias.\n"
            "`/usar` — Activar un rol de color.\n"
            "`/quitar` — Quitar tu rol de color."
        ),
        inline=False,
    )
    embed.add_field(
        name="Monedas y mercado",
        value=(
            "`/balance` — Consultar tus monedas.\n"
            "`/historial` — Ver tus últimos 10 movimientos.\n"
            "`/ranking` — Ver los balances más altos.\n"
            "`/tienda` — Ver las insignias del mercado.\n"
            "`/comprar` — Comprar escribiendo el nombre."
        ),
        inline=False,
    )
    if member is not None and member.guild_permissions.administrator:
        embed.add_field(
            name="Administración",
            value=(
                "`/insignias [miembro]` · `/darinsignia` · `/quitarinsignia`\n"
                "`/configurarinsignia` · `/editarinsignia` · `/borrarinsignia`\n"
                "`/revisarbalance` · `/añadirbalance` · `/quitarbalance` · `/setbalance`\n"
                "`/eventopregunta` · `/cancelarevento`\n"
                "`/configurarregistro` · `/estadisticas` · `/exportardatos` · `/say`"
            ),
            inline=False,
        )
    embed.set_footer(text="Los comandos administrativos requieren permiso de Administrador.")
    await answer(interaction, embed=embed)


@bot.tree.error
async def command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
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
