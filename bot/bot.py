import os
import logging
from dotenv import load_dotenv
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

TOKEN = os.getenv('TOKEN')
REPO = os.getenv('GITHUB_REPO')
OWNER = os.getenv('REPO_OWNER')
ROLE = os.getenv('ROLE_ID')
GTOKEN = os.getenv('GITHUB_TOKEN')
CLIENT_ID = os.getenv('CLIENT_ID')
GITHUB_CLIENT_ID = os.getenv('GITHUB_CLIENT_ID')
DOMAIN = os.getenv('DOMAIN')

logging.basicConfig()
cls_log = logging.getLogger('MyLogger')
cls_log.setLevel(logging.DEBUG)

client = Client(
    intents=Intents.ALL,
    token=TOKEN,
    sync_interactions=True,
    asyncio_debug=False,
    logger=cls_log,
    send_command_tracebacks=False
)


@listen()
async def on_startup():
    print(f'{client.user} connected to discord')
    print('----------------------------------------------------------------------------------------------------------------')
    print(f'Bot invite link: https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot')
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
                style=ButtonStyle.RED,
                label="🤷‍♂️",
                custom_id="2",
            ),
            Button(
                style=ButtonStyle.URL,
                label="⚠️ new issue",
                url=f"https://github.com/{OWNER}/{REPO}/issues/new",
            ),
            Button(
                style=ButtonStyle.URL,
                label="💬 new discussion",
                url=f"https://github.com/{OWNER}/{REPO}/discussions/new/choose",
            ),
            Button(
                style=ButtonStyle.GREY,
                label="other button",
                custom_id="5",
            )
        )
    ]
    await ctx.send("💫 GitHub Commands:", components=git_btns, ephemeral=True)


@component_callback("auth")
async def start_callback(ctx: ComponentContext):
    user = ctx.author
    userid = ctx.author_id

    oauth_url = f"{DOMAIN}/login?id={userid}&name={user}"

    await ctx.send(
        content="click this button to authorize me to access your connected GitHub account:",
        components=[ActionRow(Button(style=ButtonStyle.LINK, label="Authorize", url=oauth_url))],
        ephemeral=True
    )

client.start()
