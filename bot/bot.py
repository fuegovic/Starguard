# pylint: disable=missing-module-docstring
# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=line-too-long

import os
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
import pymongo
from interactions import (
    Client,
    Intents,
    listen,
    slash_command,
    SlashContext,
    ActionRow,
    Button,
    ButtonStyle,
    ComponentContext,
    component_callback,
    Embed
)

load_dotenv()

token = os.getenv('TOKEN')
repo = os.getenv('GITHUB_REPO')
owner = os.getenv('REPO_OWNER')
role = os.getenv('ROLE_ID')
client_id = os.getenv('CLIENT_ID')
domain = os.getenv('DOMAIN')
CLIENT = None
DB = None

logging.basicConfig()
cls_log = logging.getLogger('MyLogger')
cls_log.setLevel(logging.DEBUG)

# Connect to the MongoDB server
try:
    CLIENT = MongoClient(host=os.getenv('MONGO_HOST'))
    DB = CLIENT.get_database(os.getenv('MONGO_DATABASE'))
except pymongo.errors.ConfigurationError as configuration_error:
    print(f"Error connecting to MongoDB: {configuration_error}")
except pymongo.errors.OperationFailure as operation_error:
    print(f"Error connecting to MongoDB: {operation_error}")
except pymongo.errors.ServerSelectionTimeoutError as timeout_error:
    print(f"Server selection timeout error: {timeout_error}")

client = Client(
    intents=Intents.ALL,
    token=token,
    sync_interactions=True,
    asyncio_debug=False,
    logger=cls_log,
    send_command_tracebacks=False
)


@listen()
async def on_startup():
    print(f'{client.user} connected to discord')
    print('----------------------------------------------------------------------------------------------------------------')
    print(f'Bot invite link: https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions=268453888&scope=bot')
    print('----------------------------------------------------------------------------------------------------------------')


@slash_command(name="ping", description="☎️ Ping")
async def ping(ctx: SlashContext):
    latency_ms = round(client.latency * 1000, 2)
    await ctx.send(f"Ping: {latency_ms}ms", ephemeral=True)


@slash_command(name="help", description="Show a list of available commands")
async def help_command(ctx: SlashContext):
    embed = Embed(
        title="LibreChat Updater",
        description="Here is a list of available commands:",
        color=0x8000ff,
        url="https://github.com/Berry-13/LibreChat-DiscordBot"
    )

    embed.add_field(
        name="/ping",
        value="☎️ Ping the bot\n"
              "- ping the bot, returns the latency in milliseconds"
    )
    embed.add_field(
        name="/github",
        value="💻 github command\n"
              "- github verification and other useful github buttons"
    )
    embed.add_field(
        name=f"{os.getenv('COMMAND_NAME')}",
        value=f"{os.getenv('COMMAND_DESCRIPTION')}\n"
              f"{os.getenv('COMMAND_EXTENDED_DESCRIPTION')}"
    )
    embed.add_field(
        name="---",
        value=" \n"
    )
    embed.add_field(
        name="Visit our GitHub page for the latest updates, additional information, or to report any problems",
        value="**[GitHub](https://github.com/fuegovic/Discord-GH-bot)**"
    )

    await ctx.send(embed=embed, ephemeral=True)


@slash_command(
        name=f"{os.getenv('COMMAND_NAME')}",
        description=f"{os.getenv('COMMAND_DESCRIPTION')}")
async def hyperlinks(ctx: SlashContext):
    hyperlinks_btns = [
        ActionRow(
            Button(
                style=ButtonStyle.URL,
                label=f"{os.getenv('BTN1')}",
                url=f"{os.getenv('URL1')}"
            ),
            Button(
                style=ButtonStyle.URL,
                label=f"{os.getenv('BTN2')}",
                url=f"{os.getenv('URL2')}"
            ),
            Button(
                style=ButtonStyle.URL,
                label=f"{os.getenv('BTN3')}",
                url=f"{os.getenv('URL3')}"
            ),
            Button(
                style=ButtonStyle.URL,
                label=f"{os.getenv('BTN4')}",
                url=f"{os.getenv('URL4')}"
            )
        )
    ]
    await ctx.send("Useful links:", components=hyperlinks_btns, ephemeral=True)


@slash_command(
        name='github',
        description='💻 GitHub Commands')
async def github(ctx: SlashContext):
    git_btns = [
        ActionRow(
            Button(
                style=ButtonStyle.GREEN,
                label="Auth",
                custom_id="auth",
            ),
            Button(
                style=ButtonStyle.URL,
                label="⚠️ new issue",
                url=f"https://github.com/{owner}/{repo}/issues/new",
            ),
            Button(
                style=ButtonStyle.URL,
                label="💬 new discussion",
                url=f"https://github.com/{owner}/{repo}/discussions/new/choose",
            )
        )
    ]
    await ctx.send("💫 GitHub Commands:", components=git_btns, ephemeral=True)


@component_callback("auth")
async def start_callback(ctx: ComponentContext):
    user = ctx.author
    userid = ctx.author_id

    oauth_url = f"{domain}/login?id={userid}&name={user}"

    await ctx.send(
        content="click this button to connect your GitHub account:",
        components=[ActionRow(Button(style=ButtonStyle.LINK, label="Authorize", url=oauth_url))],
        ephemeral=True
    )

    start_time = datetime.now()

    while datetime.now() < start_time + timedelta(minutes=1):
        user_collection = DB['users']
        user_entry = user_collection.find_one({'discord_id': f'{userid}', 'linked_repo': f"https://github.com/{owner}/{repo}/"})

        if user_entry:
            if user_entry.get('starred_repo', False):
                await ctx.author.add_role(role, reason='star')
                await ctx.send("Confirmation: Your account has been successfully linked!", ephemeral=True)
            else:
                await ctx.author.remove_role(role, reason='no_star')
                await ctx.send("Your account has been unlinked!", ephemeral=True)
            break

        await asyncio.sleep(5)

    else:
        await ctx.send("Could not find your account. Please try again.", ephemeral=True)

client.start()
