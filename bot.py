import interactions #Interactions.py 5.10.0 
# https://interactions-py.github.io/interactions.py
from interactions import *
#import bot_config
import logging
import os
#import time
#import asyncio
#import subprocess
#import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('TOKEN')
REPO = os.getenv('GITHUB_REPO')
ROLE = os.getenv('ROLE_ID')
GTOKEN = os.getenv('GITHUB_TOKEN') 
CLIENT_ID = os.getenv('CLIENT_ID')
GITHUB_CLIENT_ID= os.getenv('GITHUB_CLIENT_ID')

logging.basicConfig()
cls_log = logging.getLogger('MyLogger')
cls_log.setLevel(logging.INFO)

client = Client(intents=interactions.Intents.ALL, token=TOKEN, sync_interactions=True, asyncio_debug=False, logger=cls_log, send_command_tracebacks=False)


# 👂
@listen()
async def on_startup():
    print(f'{client.user} connected to discord')
    print('----------------------------------------------------------------------------------------------------------------')
    print(f'Bot invite link: https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot')
    print('----------------------------------------------------------------------------------------------------------------')


# 📞 PING
@slash_command(name="ping", description="📞 Ping")
async def ping(ctx: SlashContext):
    latency_ms = round(client.latency * 1000, 2)
    await ctx.send(f"Ping: {latency_ms}ms", ephemeral=True)


# 📄 HELP
@slash_command(name="help", description="Show a list of available commands")
async def help_command(ctx: SlashContext):
    embed = interactions.Embed(title="LibreChat Updater", description="Here is a list of available commands:", color=0x8000ff, url="https://github.com/Berry-13/LibreChat-DiscordBot")
    
    # Add fields for each command
    embed.add_field(
        name="/ping", 
        value="📞 Ping the bot \n"
        "- ping the bot, returns the latency in milliseconds"
        )
    embed.add_field(
        name="/librechat", 
        value="🌐 Explore LibreChat URLs \n"
        "- Quick access to LibreChat **GitHub**, **Documentation**, **Discord** and **YouTube**"
        )
    embed.add_field(
        name="---",
        value=" \n"
    )
    embed.add_field(
        name="Visit our GitHub page for the latest updates, additional information or to report any problems",
        value="**[GitHub](https://github.com/fuegovic)**"
    )

    await ctx.send(embed=embed, ephemeral=True)


# 🌐 LIBRECHAT HYPERLINKS 
@slash_command(name='librechat', description='🌐 LibreChat URLs')
async def librechat(ctx: SlashContext):
    librechat = [
        ActionRow(
            Button(
                style=ButtonStyle.URL,
                label="GitHub",
                url="https://librechat.ai"
            ),
            Button(
                style=ButtonStyle.URL,
                label="Docs",
                url="https://docs.librechat.ai"
            ),
            Button(
                style=ButtonStyle.URL,
                label="Discord",
                url="https://discord.librechat.ai"
            ),
            Button(
                style=ButtonStyle.URL,
                label="Youtube",
                url="https://www.youtube.com/@LibreChat"
            )
        )
    ]
    await ctx.send("Useful LibreChat links:", components=librechat)

# ⭐ GitHub
@slash_command(name='GitHub', description='⭐ GitHub Commands')
async def docker(ctx: SlashContext):
    docker = [
        ActionRow(
            Button(
                style=ButtonStyle.GREEN,
                label="Auth",
                custom_id="auth",
            ),
            Button(
                style=ButtonStyle.RED,
                label="Stop",
                custom_id="stop",
            ),
            Button(
                style=ButtonStyle.BLUE,
                label="Update",
                custom_id="update",
            ),
            Button(
                style=ButtonStyle.GREY,
                label="Status",
                custom_id="status",
            )
        )
    ]
    await ctx.send("⭐ GitHub Commands:", components=docker, ephemeral=True)

@component_callback("auth")
async def start_callback(ctx: ComponentContext):
    user = ctx.author
    userid = ctx.author_id
    user_perm = ctx.author_permissions

    await ctx.send(content=f'Discord Username = "{user}"\nDiscord User ID = "{userid}"\nDiscord User Permissions = "{user_perm}"\n')

    oauth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}"

    await ctx.send(content=f"Please authorize me to access your connected accounts by clicking this button:", components=[ActionRow(Button(style=ButtonStyle.LINK, label="Authorize", url=oauth_url))])

client.start()