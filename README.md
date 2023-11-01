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
- `/starcount`: Output the te total amount of stargazers for specified repo
- `/your-custom-name` A customizable command that displays 4 buttons to access 4 custom URLs of your choice
- `/help`: Command names and usage

Here's an example of the `/verify` command:

![image](https://github.com/fuegovic/Starguard/assets/32828263/0790e3e3-5ff8-45df-9b25-91e32069c273)



## Installation
- **[detailed installation guide](./docs/installation.md)**
- **[detailled env configuration guide](./docs/env_file.md)**

1. 🧑‍🤝‍🧑 Clone the repository.
2. ✏️ Configure the .env file
3. 🐳 Run the bot in a Docker container for easier deployment.

## Requirements

To run this project, you need to have the following:

- Docker
- A Discord bot token and secret
- A GitHub client ID and secret
- A GitHub personal access token

## Python Libraries and Resources

- This project uses the following libraries and resources:
    - [Flask](https://pypi.org/project/Flask/) A lightweight web framework for Python that provides tools and features to create web applications.
    - [python-dotenv](https://pypi.org/project/python-dotenv/) A module that reads key-value pairs from a .env file and sets them as environment variables.
    - [authlib](https://pypi.org/project/Authlib/) A library that implements various authentication protocols and specifications, such as OAuth, OpenID Connect, and JWT.
    - [pymongo](https://pypi.org/project/pymongo/) A Python driver for MongoDB that allows you to work with MongoDB databases and collections in Python.
    - [Interactions.py](https://pypi.org/project/interactions.py/) A library that simplifies the creation and handling of Discord slash commands and components in Python.
    - [discord_py_interactions](https://pypi.org/project/discord-py-interactions/) A library that extends [discord.py](https://pypi.org/project/discord.py/) with support for Discord interactions, such as slash commands, buttons, and select menus.
    - [requests](https://pypi.org/project/requests/) A popular HTTP library for Python that allows you to send and receive HTTP requests in a simple way.

## License

[MIT](https://github.com/fuegovic/Starguard/blob/main/LICENSE)
