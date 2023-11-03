# Bot Configuration

This file contains the environment variables for the discord bot. You need to fill in the values for each variable according to your needs. Do not share this file with anyone, as it contains sensitive information such as tokens and secrets. 🔒

## Discord Variables

- `TOKEN`: The discord app token for your bot. You can get it from https://discord.com/developers/applications
- `CLIENT_ID`: The discord client ID for your bot. You can get it from https://discord.com/developers/applications

For detailled instructions: [Discord dev](./installation.md#step-1-obtain-the-app-token-and-client-id-from-the-discord-dev-portal) 

- `ROLE_ID`: The ID of the role that you want to give to users who have starred your GitHub repo. You can get it by enabling developer mode in discord and right-clicking on the role.
- `GUILD_ID`: The ID of the server where you want to use the bot. You can get it by enabling developer mode in discord and right-clicking on the server.
- `CHANNEL_ID`: The ID of the channel where you want the bot to post messages. You can get it by enabling developer mode in discord and right-clicking on the channel.

For detailled instructions: [Discord Server](./installation.md#step-5-get-the-role-id-guild-id-and-channel-id-from-discord) 

## GitHub Variables

- `REPO_OWNER`: The username of the owner of the GitHub repo that you want to promote with the bot.
- `GITHUB_REPO`: The name of the GitHub repo that you want to promote with the bot.

- `GITHUB_CLIENT_ID`: The client ID of the GitHub OAuth app that you have created for the bot. You can create one at https://github.com/settings/apps
- `GITHUB_CLIENT_SECRET`: The client secret of the GitHub OAuth app that you have created for the bot. You can get it from https://github.com/settings/apps

For detailled instructions: [GitHub OAuth](./installation.md#step-3-create-a-github-oauth-app)

- `GITHUB_TOKEN`: GitHub Public Access Token needed to fetch the stargazers name from the specified repo.

For detailled instructions: [GitHub PAT](./installation.md#step-4-create-a-classic-github-public-access-token-pat) 

- `SECRET_KEY`: A random secret key that you have generated for securing the OAuth process. You can use any mix of letters, numbers, and symbols.

## MongoDB Variables

- `MONGO_HOST`: The host name of the MongoDB database that you want to use for storing user data. If you are using docker, set it to `mongodb`.
- `MONGO_DATABASE`: The name of the MongoDB database that you want to use for storing user data. The default is `mongodb`

## Mongo-Express
- You can access the database by visiting `http://localhost:8085/`. Please change the default credentials for something more secure.

- `ME_CONFIG_BASICAUTH_USERNAME`: The username used to connect to the database with mongo-express
- `ME_CONFIG_BASICAUTH_PASSWORD`: The password used to connect to the database with mongo-express

## Other Variables

- `AUTOMATIC_CHECK`: A boolean value (`True` or `False`) that indicates whether you want the bot to automatically check if the verified users have removed their star from your GitHub repo, and then remove their role on discord and update their status in the database.
- `AUTOMATIC_CHECK_DELAY`: A numeric value (in seconds) that indicates how often you want the bot to perform the automatic check. The minimum value is 300 (5 minutes). The default value is 3600 (1 hour).
- `COMMAND_NAME`: The name of the custom command that you want to create for displaying useful links. It only supports lowercase letters.
- `COMMAND_DESCRIPTION`: A short description of what the custom command does.
- `COMMAND_EXTENDED_DESCRIPTION`: A longer description of what the custom command does, supports some formatting options such as emojis and bold text.
- `BTN1`, `BTN2`, `BTN3`, and `BTN4`: The labels of the buttons that you want to display for each link.
- `URL1`, `URL2`, `URL3`, and `URL4`: The URLs of the links that you want to display for each button.
