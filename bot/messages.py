"""Every string the bot says, in one place.

Here you can edit, add or remove the messages the bot sends. The lists are
picked from at random; the rest are used as they are, except where a ``{}``
or a named field marks a value the caller fills in.
"""

THANKS = [
    "<@{}>, Thank you for your support! 😊",
    "We appreciate you <@{}>! 🙌",
    "<@{}>, Thanks for being a part of our community! 💯",
    "You rock <@{}>! Thanks for the star! 🌟",
    "We're so grateful for your interest <@{}>! 🙏",
    "<@{}>, You're awesome! Thank you for your feedback! 👍",
    "We're glad you like our project <@{}>! 🎉",
    "<@{}>, You made our day! Thanks for the star! 😘",
    "<@{}>, You're amazing! 😎",
    "<@{}>, Thanks for joining our community! 🥰",
    "You're the best <@{}>! Thanks for the star! ✨",
    "<@{}>, You're incredible! Thank you! 🙌",
    "Thank you <@{}>! 💖",
    "We're happy you like our project <@{}>! 😁",
    "<@{}>, You brightened our day! Thanks for the star! 🌞",
    "<@{}>, You're a star! Literally! Thanks for the star! 🌠",
    "We love you <@{}>! Thanks for your support! 💕",
    "<@{}>, You're a legend! Thank you for your interest! 🙌",
    "You're awesome <@{}>! Thanks for the star! 😍",
    "We're so thankful for your feedback <@{}>! 🙏",
    "<@{}>, You're a gem! Thank you for your support! 💎",
    "You're amazing <@{}>! Thanks for the star! 😊",
    "We're grateful for your interest <@{}>! 🙌",
    "<@{}>, You're a hero! Thank you for your feedback! 👏",
    "You're fantastic <@{}>! Thanks for the star! 🎊",
]

SORRY = [
    "<@{}>, We respect your decision. 😌",
    "<@{}>, We wish you all the best. 🙏",
    "<@{}>, We hope you'll come back soon. 😔",
    "<@{}>, We're sorry to see you go. 😢",
    "<@{}>, We're sad to lose you. 😭",
    "<@{}>, We hope you'll reconsider. 😇",
    "<@{}>, We're always here for you. 💖",
]

# --- /ping ---------------------------------------------------------------
PING = "Ping: {latency}ms"

# --- /help ---------------------------------------------------------------
HELP_TITLE = "GitHub 🌟 Verification Bot"
HELP_DESCRIPTION = "Here is a list of available commands:"
HELP_URL = "https://github.com/fuegovic/Starguard"
HELP_COLOR = 0xFFAC33
HELP_PING = "**Ping the bot**\n- ☎️ Ping the bot, returns the latency in milliseconds"
HELP_VERIFY = (
    "**GitHub verification**\n- ✨ Star **{repo}**\n- 🔑 Link your GitHub account\n- 🎁 Get a role"
)
HELP_STARCOUNT = "**💫 Displays the number of stargazers for:\n{repo_url}**"
HELP_CUSTOM = "**{description}**\n- {extended_description}"
HELP_FOOTER_NAME = (
    "Visit our GitHub page for the latest updates, additional information, "
    "or to report any problems"
)
HELP_FOOTER_VALUE = "**[GitHub](https://github.com/fuegovic/Starguard)**"

# --- /starcount ----------------------------------------------------------
STARCOUNT = "There are {count} stargazers! ✨"
GITHUB_UNREACHABLE = "Could not reach GitHub right now: {reason}"

# --- /verify -------------------------------------------------------------
VERIFY_STEPS = (
    "💫 Self Verification:\n"
    "- 1: Make sure you've starred this repo\n"
    "- 2: Authenticate with GitHub\n"
    "- 3: Claim your role\n"
    "_The GitHub link is personal to you and expires in 15 minutes._"
)
VERIFY_BUTTON_STAR = "1: Star this repo 🌟"
VERIFY_BUTTON_LOGIN = "2: Log in with GitHub 🔑"
VERIFY_BUTTON_CLAIM = "3: Claim your role ❤️‍🔥"
# Shown once the first link has had time to expire, so nobody has to remember
# the command name to start over.
VERIFY_BUTTON_RELINK = "Get a new link 🔄"
RELINK_SENT = (
    "Here is a fresh GitHub link. It expires in 15 minutes, so use it now and "
    "then press **{claim_label}** back on the verification message."
)
LINK_BUTTONS_TITLE = "Useful links:"

# --- 🎁 claiming the role -------------------------------------------------
CLAIM_DATABASE_UNAVAILABLE = "The database is unavailable right now, please try again later."
CLAIM_LOOKUP_FAILED = "Could not check your verification, please try again later."
CLAIM_NOT_LINKED = (
    "Please make sure to link your GitHub account by using the "
    "**Log in with GitHub** button. If your link has expired, press "
    "**{relink_label}** below for a new one."
)
CLAIM_NEEDS_A_SERVER = (
    "Roles only exist inside a server, so this button cannot do anything in a "
    "direct message. Run /verify again in the server you want the role in."
)
CLAIM_NOT_STARRED = "Please star the repo to get the role 🌟"
CLAIM_ALREADY_HELD = "You already claimed your role 😁\n💫Thanks!"
CLAIM_ROLE_FAILED = (
    "I could not assign the role. Please ask a moderator to check my permissions and role position."
)

# --- /checkstars ----------------------------------------------------------
CHECK_ALREADY_RUNNING = (
    "A star check is already running. Its results will be announced in the "
    "channel as usual, so there is nothing to do."
)
CHECK_NO_CHANGES = "Star status checked, no changes."
CHECK_REMOVED = "Removed the role from {count} member(s) for un-starring the repo: {names}"
DATABASE_UNREACHABLE = "Could not reach the database right now."
