import asyncio
import os
import traceback
import unicodedata

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import Database


load_dotenv()
MAX_MONEY = 1_000_000_000_000


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


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

    async def close(self) -> None:
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


@bot.event
async def on_ready() -> None:
    if bot.user:
        print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")


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
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await interaction.delete_original_response()


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
    await member.remove_roles(*roles, reason="El usuario quitó su color")
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
    assert interaction.guild_id is not None
    rows = await bot.db.list_badges(interaction.guild_id, purchasable_only=True)
    embed = discord.Embed(title="Mercado de Sularea", color=0xF59E0B)
    if interaction.guild is not None and interaction.guild.icon is not None:
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
    member = guild_member(interaction)
    guild = interaction.guild
    badge = await find_badge(interaction, insignia)
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
        await answer(interaction, "No puedo entregar esa insignia. Coloca su rol debajo del rol del bot.")
        return

    lock = bot.purchase_locks.setdefault((guild.id, member.id), asyncio.Lock())
    async with lock:
        if role in member.roles:
            await answer(interaction, "Ya tienes esa insignia.")
            return
        new_balance = await bot.db.spend_balance(guild.id, member.id, badge["price"])
        if new_balance is None:
            current = await bot.db.get_balance(guild.id, member.id)
            await answer(interaction, f"No tienes suficiente dinero. Tienes **{money(current)}**.")
            return
        try:
            await member.add_roles(role, reason=f"Compra: {badge['name']}")
        except Exception:
            await bot.db.add_balance(guild.id, member.id, badge["price"])
            raise
    await answer(
        interaction,
        f"Compraste **{badge['name']}** por **{money(badge['price'])}** monedas. "
        f"Tu nuevo balance es **{money(new_balance)}**.",
    )


@bot.tree.command(name="balance", description="Muestra tu balance de monedas.")
@app_commands.guild_only()
async def balance(interaction: discord.Interaction) -> None:
    assert interaction.guild_id is not None
    value = await bot.db.get_balance(interaction.guild_id, interaction.user.id)
    await answer(interaction, f"Tu balance es **{money(value)} monedas**.")


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
    assert interaction.guild_id is not None
    members = members_from_target(objetivo)
    if not members:
        await answer(interaction, f"El rol {objetivo.mention} no tiene miembros.")
        return
    await interaction.response.defer()
    affected = await bot.db.add_balance_many(
        interaction.guild_id,
        [member.id for member in members],
        cantidad,
    )
    if isinstance(objetivo, discord.Member):
        value = await bot.db.get_balance(interaction.guild_id, objetivo.id)
        await answer(
            interaction,
            f"Añadiste **{money(cantidad)}** monedas a {objetivo.mention}. "
            f"Nuevo balance: **{money(value)}**. Se aplicó a **1 miembro**.",
        )
        return
    await answer(
        interaction,
        f"Añadiste **{money(cantidad)}** monedas a **{affected} miembros** "
        f"con el rol {objetivo.mention}.",
    )


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
    assert interaction.guild_id is not None
    members = members_from_target(objetivo)
    if not members:
        await answer(interaction, f"El rol {objetivo.mention} no tiene miembros.")
        return
    await interaction.response.defer()
    affected = await bot.db.remove_balance_many(
        interaction.guild_id,
        [member.id for member in members],
        cantidad,
    )
    if isinstance(objetivo, discord.Member):
        current = await bot.db.get_balance(interaction.guild_id, objetivo.id)
        if affected == 0:
            await answer(
                interaction,
                f"{objetivo.mention} no tiene suficiente dinero. "
                f"Su balance es **{money(current)}**.",
            )
            return
        await answer(
            interaction,
            f"Quitaste **{money(cantidad)}** monedas a {objetivo.mention}. "
            f"Nuevo balance: **{money(current)}**. Se aplicó a **1 miembro**.",
        )
        return
    skipped = len(members) - affected
    message = (
        f"Quitaste **{money(cantidad)}** monedas a **{affected} de "
        f"{len(members)} miembros** con el rol {objetivo.mention}."
    )
    if skipped:
        message += f" Se omitieron **{skipped}** porque no tenían saldo suficiente."
    await answer(interaction, message)


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
    assert interaction.guild_id is not None
    members = members_from_target(objetivo)
    if not members:
        await answer(interaction, f"El rol {objetivo.mention} no tiene miembros.")
        return
    await interaction.response.defer()
    affected = await bot.db.set_balance_many(
        interaction.guild_id,
        [member.id for member in members],
        cantidad,
    )
    await answer(
        interaction,
        f"Establecí el balance de {objetivo.mention} en **{money(cantidad)} monedas**. "
        f"Se aplicó a **{affected} {'miembro' if affected == 1 else 'miembros'}**.",
    )


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
    await miembro.add_roles(role, reason=f"Insignia entregada por {interaction.user}")
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
    await miembro.remove_roles(*roles, reason=f"Insignia retirada por {interaction.user}")
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
    try:
        await bot.db.create_badge(
            interaction.guild_id, display_name, normalize_name(display_name),
            rol_insignia.id, rol_color.id, comprable, precio,
        )
    except asyncpg.UniqueViolationError:
        await answer(interaction, "Ya existe una insignia con ese nombre o ese rol.")
        return
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
    await answer(interaction, f"Actualicé la insignia **{final_name}**.")


@bot.tree.command(name="borrarinsignia", description="Borra la configuración de una insignia.")
@app_commands.describe(insignia="Nombre de la insignia que se borrará")
@app_commands.autocomplete(insignia=badge_autocomplete)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def borrarinsignia(interaction: discord.Interaction, insignia: str) -> None:
    assert interaction.guild_id is not None
    deleted = await bot.db.delete_badge(interaction.guild_id, normalize_name(insignia))
    if deleted is None:
        await answer(interaction, "Esa insignia no existe.")
        return
    await answer(interaction, f"Borré **{deleted['name']}**. No eliminé ningún rol del servidor.")


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
