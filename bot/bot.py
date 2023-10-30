# pylint: disable=missing-module-docstring
# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=line-too-long

import os
import logging
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
        name='verify',
        description='💫 Self Verification')

async def verify(ctx: SlashContext):
    user = ctx.author
    userid = ctx.author_id

    oauth_url = f"{domain}/login?id={userid}&name={user}"
    ver_btns = [
        ActionRow(
            Button(
                style=ButtonStyle.URL,
                label="1: Star this repo 🌟",
                url=f"https://github.com/{owner}/{repo}/",
            ),
            Button(
                style=ButtonStyle.URL,
                label="2: Log in with GitHub 🔑",
                url=oauth_url,
            ),
            Button(
                style=ButtonStyle.BLUE,
                label="3: Claim your role ❤️‍🔥",
                custom_id="claim",
#            ),
#            Button(
#                style=ButtonStyle.URL,
#                label="⚠️ new issue",
#                url=f"https://github.com/{owner}/{repo}/issues/new",
#            ),
#            Button(
#                style=ButtonStyle.URL,
#                label="💬 new discussion",
#                url=f"https://github.com/{owner}/{repo}/discussions/new/choose",
            )
        )
    ]
    await ctx.send("💫 Self Verification:\n- 1: Make sure you've starred this repo\n- 2: Authenticate with GitHub\n- 3: Claim your role", components=ver_btns, ephemeral=True)


@component_callback("claim")
async def claim_callback(ctx: ComponentContext):
    userid = ctx.author_id
    # Get user data from MongoDB
    user_collection = DB['users']
    user_entry = user_collection.find_one({'discord_id': f'{userid}', 'linked_repo': f"https://github.com/{owner}/{repo}/"})

    if user_entry:
        # Check if user has starred the repo
        if user_entry.get('starred_repo', False):
            # Assign role
            await ctx.author.add_role(role, reason='star')
            await ctx.send(content="Role assigned successfully!", ephemeral=True)
        else:
            await ctx.author.remove_role(role, reason='no_star')
            await ctx.send(content="You have not starred the repo.", ephemeral=True)
    else:
        await ctx.send(content="User not found.", ephemeral=True)

client.start()
