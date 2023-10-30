# pylint: disable=missing-module-docstring
# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=line-too-long

import os
import logging
import random
import requests
from dotenv import load_dotenv
from pymongo import MongoClient
import pymongo
from messages import THANKS, SORRY
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
        title="GitHub 🌟 Verification Bot",
        description="Here is a list of available commands:",
        color=0x8000ff,
        url="https://github.com/fuegovic/Discord-GH-bot"
    )

    embed.add_field(
        name="> /ping",
        value="**Ping the bot**\n"
        "- ☎️ Ping the bot, returns the latency in milliseconds"
    )
    embed.add_field(
        name="> /verify",
        value="**GitHub verification**\n"
        "- ✨ Star the repo\n- 🔑 Link your GitHub account\n- 🎁 Get a role"
    )
    embed.add_field(
        name=f"> /{os.getenv('COMMAND_NAME')}",
        value=f"**{os.getenv('COMMAND_DESCRIPTION')}**\n"
        f"- {os.getenv('COMMAND_EXTENDED_DESCRIPTION')}"
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


# 🛠️ CUSTOM COMMAND - See .env.example
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


# 🔍 VERIFY USER AND GIVE A ROLE BUTTONS
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
            )
        )
    ]
    await ctx.send("💫 Self Verification:\n- 1: Make sure you've starred this repo\n- 2: Authenticate with GitHub\n- 3: Claim your role", components=ver_btns, ephemeral=True)


# 🎁 CLAIM THE ROLE
@component_callback("claim")
async def claim_callback(ctx: ComponentContext):
    userid = ctx.author_id
    user = ctx.author
    # Get user data from MongoDB
    user_collection = DB['users']
    user_entry = user_collection.find_one({'discord_id': f'{userid}', 'linked_repo': f"https://github.com/{owner}/{repo}/"})

    if user_entry:
        # Check if user has starred the repo
        if user_entry.get('starred_repo', False):
            # Check if user already has the role
            if user.has_role(role):
                # User already has the role, do nothing
                await ctx.send(content="You already claimed your role 😁\n💫Thanks!", ephemeral=True)
            else:
                # User doesn't have the role, assign it and send the message
                await user.add_role(role, reason='star')
                thank_you_message = random.choice(THANKS).format(userid)
                await ctx.send(content=thank_you_message)
        else:
            await user.remove_role(role, reason='no_star')
            await ctx.send(content="Please star the repo to get the role 🌟", ephemeral=True)
    else:
        await ctx.send(content="Please make sure to link your GitHub account by using the **Log in with GitHub** button.", ephemeral=True)


@slash_command(name="checkstars", description="Check who has un-starred the repo and remove their role")
async def check_stars_command(ctx: SlashContext):
    # Get the role ID for the moderator or administrator role
    admin_id = os.getenv('ADMIN_ID')

    # Check if the user has the moderator role
    if not ctx.author.has_role(admin_id):
        await ctx.send(content="You do not have permission to use this command.", ephemeral=True)
        return
    else:
        await check_star_status(ctx)
        await ctx.send("Star status checked", ephemeral=True)

async def check_star_status(ctx: SlashContext):
    # The header to get the star time
    headers = {"Accept": "application/vnd.github.v3.star+json"}
    # The URL for the stargazers endpoint
    url = f"https://api.github.com/repos/{owner}/{repo}/stargazers"

    # A list to store the stargazers
    stargazers = []
    while True:
        # Make a GET request to the URL
        response = requests.get(url, headers=headers, timeout=60)

        # Check if the response is successful
        if response.status_code == 200:
            # Parse the JSON data
            data = response.json()

            # Append the stargazers to the list
            stargazers.extend([user for user in data])

            # Get the next page URL from the Link header
            link = response.headers.get("Link")

            # If there is no next page, break the loop
            if not link or "rel=\"next\"" not in link:
                break

            # Otherwise, update the URL with the next page
            else:
                url = link.split(";")[0].strip("<>")
        else:
            # If the response is not successful, print an error message and break the loop
            print(f"Error: {response.status_code}")
            break

    # Get all users from the database
    user_collection = DB['users']
    users = user_collection.find()

    for user in users:
        # If the user has un-starred the repo
        if user['github_username'] not in stargazers:
            # Get the Discord member object
            guild_id = os.getenv('GUILD_ID')
            guild = client.get_guild(guild_id)
            discord_member = guild.get_member(user['discord_id'])

            # Check if the user already has the role removed
            if not discord_member.has_role(role):
                # User already has the role removed, do nothing
                continue

            # Remove the role from the user
            await discord_member.remove_role(role, reason='no_star')
            username = user['discord_username'].replace('@', '')
            sorry_message = random.choice(SORRY).format(user['discord_id'])
            await ctx.send(content=f"Removed role from **{username}** for un-starring the repo.", ephemeral=True)
            await ctx.send(content=sorry_message)

            # Update the user's star status in the database
            user_collection.update_one({'github_username': user['github_username']}, {'$set': {'starred_repo': False}})

client.start()
