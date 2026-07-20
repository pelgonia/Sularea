import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()


class SulareaBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self) -> None:
        # Registra los comandos slash globalmente en Discord.
        await self.tree.sync()


bot = SulareaBot()


@bot.event
async def on_ready() -> None:
    print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="say", description="Hace que el bot envíe un mensaje.")
@app_commands.describe(mensaje="El mensaje que quieres que diga el bot")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def say(interaction: discord.Interaction, mensaje: str) -> None:
    await interaction.response.send_message(
        mensaje,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@say.error
async def say_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "No tienes permiso para usar este comando.",
            ephemeral=True,
        )
        return

    raise error


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Falta la variable DISCORD_TOKEN en el archivo .env o en Railway.")

bot.run(token)
