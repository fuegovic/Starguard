# GitHub Star ⭐ Discord Verification bot

This bot integrates Discord with GitHub. It provides various functionalities such as user validation, role assignment, and periodic checks of starred users.

## Features

- ✔️ User Validation: The bot verifies users with GitHub OAuth.
- 💫 Role Assignment: The bot assigns a role **if** the users have starred a specified repo.
- 🔍 Periodic Checks: The bot periodically checks the starred users.

## Installation

1. 🧑‍🤝‍🧑 Clone the repository.
2. ✏️ Configure the .env file
3. 🐳 Run the bot in a Docker container for easier deployment.

## Usage

The bot uses slash commands for operation. Here are some of the commands:

- `/verify`: Validates a user and checks if they have starred the repository.
- `/checkstars`: Forces re-verification and updates role assignments.
- `/your-custom-name` A customizable command that displays 4 buttons to access 4 custom URLs of your choice

## License

[MIT](https://choosealicense.com/licenses/mit/)
