"""Every string the OAuth server shows a visitor, in one place.

The same idea as ``bot/messages.py``: the wording of the pages people actually
see should be editable without reading the request handling around it. Values
with a ``{}`` are formatted by the caller.
"""

SESSION_EXPIRED = (
    "Your verification session has expired. Go back to Discord and press "
    '"Get a new link" on the verification message, or run /verify again.'
)

DATABASE_UNAVAILABLE = "The database is unavailable right now. Please try again later."

SAVE_FAILED = "Could not save your verification right now. Please try again later."

SIGN_IN_FAILED = "GitHub sign-in failed. Please try again."

SIGN_IN_FAILED_REASON = "GitHub sign-in failed: {reason}"

PROFILE_UNREADABLE = "Could not read your GitHub profile. Please try again."

ALREADY_LINKED = (
    "The GitHub account {github_username} is already linked to another "
    "Discord user. Each GitHub account can only be used once."
)

VERIFIED_AND_STARRED = "Authentication successful! Head back to Discord and claim your role."

VERIFIED_NOT_STARRED = (
    "Authentication successful, but you have not starred {owner}/{repo} yet. "
    "Star it, then claim your role in Discord."
)
