# Starguard 
<p align="center"> <img src="https://github.com/fuegovic/Starguard/assets/32828263/969a9e91-6c40-4f77-ad6b-379fbfa28bbe" width="200" height="200"/> </p>


Discord Starguard is a bot that integrates Discord with GitHub. It provides various functionalities such as user validation, role assignment, and periodic checks of starred users. It's perfect for developers who want to reward their supporters and grow their community.

## Features

- ✔️ User Validation: The bot verifies users with GitHub OAuth.
- 💫 Role Assignment: The bot assigns a role **if** the users have starred a specified repo.
- 🔍 Periodic Checks: The bot periodically checks the starred users.

## Usage

The bot uses slash commands for operation. Here are some of the commands:

- `/verify`: Validates a user and checks if they have starred the repository.
- `/checkstars`: Forces re-verification and updates role assignments.
- `/starcount`: Output the total number of stargazers for the specified repo
- `/your-custom-name` A customizable command that displays 4 buttons to access 4 custom URLs of your choice
- `/help`: Command names and usage

Here's an example of the `/verify` command:

![image](https://github.com/fuegovic/Starguard/assets/32828263/0790e3e3-5ff8-45df-9b25-91e32069c273)



## Installation
- **[detailed installation guide](./docs/installation.md)**
- **[detailled env configuration guide](./docs/env_file.md)**

1. 🧑‍🤝‍🧑 Clone the repository.
2. ✏️ Copy `.env.example` to `.env` and configure it — including a real `SECRET_KEY`:
   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. 🐳 Run `docker compose up -d`.

> **Upgrading from an older version?** See [Upgrading](./docs/installation.md#upgrading-from-an-older-version) — earlier releases stored GitHub OAuth tokens that you should revoke.

## Requirements

To run this project, you need to have the following:

- Docker
- A MongoDB (one is bundled — see [override.example.yml](./override.example.yml))
- A Discord bot token
- A GitHub OAuth app client ID and secret
- A public HTTPS domain pointing at the OAuth server
- Optionally, a GitHub personal access token to raise the API rate limit

## Privacy

Starguard asks GitHub for the `read:user` scope only: enough to read your public
profile and check whether you starred the repository. It records your GitHub
username and numeric ID against your Discord ID. **The OAuth access token is
used during the request and then discarded** — it is never written to the
database or to the logs.

## Development

```sh
pip install -r requirements-dev.txt
pytest
pylint $(git ls-files '*.py')
```

Run the two processes directly with `python -m bot.bot` and `python -m server.server`.

## Python Libraries and Resources

- This project uses the following libraries and resources:
    - [Flask](https://pypi.org/project/Flask/) A lightweight web framework for Python that provides tools and features to create web applications.
    - [python-dotenv](https://pypi.org/project/python-dotenv/) A module that reads key-value pairs from a .env file and sets them as environment variables.
    - [authlib](https://pypi.org/project/Authlib/) A library that implements various authentication protocols and specifications, such as OAuth, OpenID Connect, and JWT.
    - [pymongo](https://pypi.org/project/pymongo/) A Python driver for MongoDB that allows you to work with MongoDB databases and collections in Python.
    - [Interactions.py](https://pypi.org/project/interactions.py/) A library that simplifies the creation and handling of Discord slash commands and components in Python.
    - [requests](https://pypi.org/project/requests/) A popular HTTP library for Python that allows you to send and receive HTTP requests in a simple way.
    - [waitress](https://pypi.org/project/waitress/) A production WSGI server used to serve the OAuth callback app.
    - [itsdangerous](https://pypi.org/project/itsdangerous/) Signs the verification links so a Discord identity cannot be forged.

## License

[MIT](https://github.com/fuegovic/Starguard/blob/main/LICENSE)
