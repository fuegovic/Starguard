import interactions
import logging
import os
from dotenv import load_dotenv

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

client = interactions.Client(
    intents=interactions.Intents.ALL,
    token=TOKEN,
    sync_interactions=True,
    asyncio_debug=False,
    logger=cls_log,
    send_command_tracebacks=False
)


@interactions.listen()
async def on_startup():
    print(f'{client.user} connected to discord')
    print('----------------------------------------------------------------------------------------------------------------')
    print(f'Bot invite link: https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot')
    print('----------------------------------------------------------------------------------------------------------------')


@interactions.slash_command(name="ping", description="☎️ Ping")
async def ping(ctx: interactions.SlashContext):
    latency_ms = round(client.latency * 1000, 2)
    await ctx.send(f"Ping: {latency_ms}ms", ephemeral=True)


@interactions.slash_command(name="help", description="Show a list of available commands")
async def help_command(ctx: interactions.SlashContext):
    embed = interactions.Embed(
        title="LibreChat Updater",
        description="Here is a list of available commands:",
        color=0x8000ff,
        url="https://github.com/Berry-13/LibreChat-DiscordBot"
    )

    # Add fields for each command
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


@interactions.slash_command(name=f"{os.getenv('COMMAND_NAME')}", description=f"{os.getenv('COMMAND_DESCRIPTION')}")
async def links(ctx: interactions.SlashContext):
    links = [
        interactions.ActionRow(
            interactions.Button(
                style=interactions.ButtonStyle.URL,
                label=f"{os.getenv('BTN1')}",
                url=f"{os.getenv('URL1')}"
            ),
            interactions.Button(
                style=interactions.ButtonStyle.URL,
                label=f"{os.getenv('BTN2')}",
                url=f"{os.getenv('URL2')}"
            ),
            interactions.Button(
                style=interactions.ButtonStyle.URL,
                label=f"{os.getenv('BTN3')}",
                url=f"{os.getenv('URL3')}"
            ),
            interactions.Button(
                style=interactions.ButtonStyle.URL,
                label=f"{os.getenv('BTN4')}",
                url=f"{os.getenv('URL4')}"
            )
        )
    ]
    await ctx.send("Useful links:", components=links, ephemeral=True)


@interactions.slash_command(name='github', description='💻 GitHub Commands')
async def docker(ctx: interactions.SlashContext):
    docker = [
        interactions.ActionRow(
            interactions.Button(
                style=interactions.ButtonStyle.GREEN,
                label="Auth",
                custom_id="auth",
            ),
            interactions.Button(
                style=interactions.ButtonStyle.RED,
                label="🤷‍♂️",
                custom_id="2",
            ),
            interactions.Button(
                style=interactions.ButtonStyle.URL,
                label="⚠️ new issue",
                url=f"https://github.com/{OWNER}/{REPO}/issues/new",
            ),
            interactions.Button(
                style=interactions.ButtonStyle.URL,
                label="💬 new discussion",
                url=f"https://github.com/{OWNER}/{REPO}/discussions/new/choose",
            ),
            interactions.Button(
                style=interactions.ButtonStyle.GREY,
                label="other button",
                custom_id="5",
            )
        )
    ]
    await ctx.send("💫 GitHub Commands:", components=docker, ephemeral=True)


@interactions.component_callback("auth")
async def start_callback(ctx: interactions.ComponentContext):
    user = ctx.author
    userid = ctx.author_id

    oauth_url = f"{DOMAIN}/login?id={userid}&name={user}"

    await ctx.send(
        content=f"Please authorize me to access your connected GitHub account by clicking this button:",
        components=[interactions.ActionRow(interactions.Button(style=interactions.ButtonStyle.LINK, label="Authorize", url=oauth_url))],
        ephemeral=True
    )

client.start()
