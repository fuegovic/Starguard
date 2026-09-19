# Bot Configuration

This file contains the environment variables for the discord bot. You need to fill in the values for each variable according to your needs. Do not share this file with anyone, as it contains sensitive information such as tokens and secrets. 🔒

## Discord Variables
### For detailled instructions: [Discord dev](./installation.md#step-1-obtain-the-app-token-and-client-id-from-the-discord-dev-portal), [Discord Server](./installation.md#step-5-get-the-role-id-guild-id-and-channel-id-from-discord) 
- `TOKEN`: The discord app token for your bot. You can get it from https://discord.com/developers/applications
- `CLIENT_ID`: The discord client ID for your bot. You can get it from https://discord.com/developers/applications

- `ROLE_ID`: The ID of the role that you want to give to users who have starred your GitHub repo. You can get it by enabling developer mode in discord and right-clicking on the role.
- `GUILD_ID`: The ID of the server where you want to use the bot. You can get it by enabling developer mode in discord and right-clicking on the server.
- `CHANNEL_ID`: The ID of the channel where you want the bot to post messages. You can get it by enabling developer mode in discord and right-clicking on the channel.

## GitHub Variables
### For detailled instructions: [GitHub OAuth](./installation.md#step-3-create-a-github-oauth-app), [GitHub PAT](./installation.md#step-4-create-a-classic-github-public-access-token-pat) 
- `REPO_OWNER`: The username of the owner of the GitHub repo that you want to promote with the bot.
- `GITHUB_REPO`: The name of the GitHub repo that you want to promote with the bot.

- `SERVER_PORT`: The port published on the **host** for the OAuth server. The default is `5000`. The application always listens on port `5000` inside its container, so you can change this freely.
- `DOMAIN`: The public HTTPS address of the OAuth server, e.g. `https://starguard.example.com`. Users are sent here from Discord, so it has to be reachable from outside your network.

- `GITHUB_CLIENT_ID`: The client ID of the GitHub OAuth app that you have created for the bot. You can create one at https://github.com/settings/developers
- `GITHUB_CLIENT_SECRET`: The client secret of the GitHub OAuth app that you have created for the bot.

- `GITHUB_TOKEN`: A GitHub personal access token, used **only** to list the repository's stargazers. Optional for a public repo, but without it GitHub allows just 60 requests per hour, which is not enough beyond a few thousand stargazers. No scopes are required for a public repo.

- `SECRET_KEY`: **Required.** Signs session cookies *and* the personal verification links handed out by `/verify`, so it must be unguessable and **identical for the bot and the server**. The application refuses to start on a placeholder or on anything shorter than 16 characters. Generate one with:
  ```sh
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

- `LINK_TOKEN_MAX_AGE`: How long a `/verify` link stays usable, in seconds. The default is `900` (15 minutes), the minimum is `60`.

## MongoDB Variables

- `MONGO_HOST`: The MongoDB connection string. If you are using the bundled docker mongo (from `override.example.yml`), set it to `mongodb://mongodb:27017/`.
- `MONGO_DATABASE`: The name of the MongoDB database used for storing user data. The default is `starguard`
- Note: The external access is disabled by default, but you can enable it by editing the `docker-compose.override.yml` file, See the `override.example` file.

## Mongo-Express
You can enable this by editing the `docker-compose.override.yml` file, See the `override.example` file.
- Mongo Express is published on the **loopback interface only**, so it is not reachable from the internet. Access it at `http://localhost:8081/`, or over an SSH tunnel from another machine.

- `MONGO_EXPRESS_USERNAME`: The username for mongo-express. **Required** when the service is enabled; compose refuses to start without it (it used to fall back to `admin`).
- `MONGO_EXPRESS_PASSWORD`: The password for mongo-express. **Required** when the service is enabled (it used to fall back to `password`).
- `MONGO_EXPRESS_PORT`: The port used to access mongo-express. The default is `8081`

## Other Variables

- `AUTOMATIC_CHECK`: A boolean value (`true` or `false`) that indicates whether you want the bot to automatically check if the verified users have removed their star from your GitHub repo, and then remove their role on discord and update their status in the database.

- `AUTOMATIC_CHECK_DELAY`: A numeric value (in seconds) that indicates how often you want the bot to perform the automatic check. The minimum value is 300 (5 minutes). The default value is 3600 (1 hour).

- `COMMAND_NAME`: The name of the custom command that you want to create for displaying useful links. It only supports lowercase letters. **Optional** — leave it empty and the command is simply not registered (it used to crash the bot).

- `COMMAND_DESCRIPTION`: A short description of what the custom command does.

- `COMMAND_EXTENDED_DESCRIPTION`: A longer description of what the custom command does, supports some formatting options such as emojis and bold text.

- `BTN1`, `BTN2`, `BTN3`, and `BTN4`: The labels of the buttons that you want to display for each link.

- `URL1`, `URL2`, `URL3`, and `URL4`: The URLs of the links that you want to display for each button. Only buttons that have **both** a label and a URL are shown.

- `LOG_LEVEL`: Logging verbosity — `DEBUG`, `INFO` (default), `WARNING` or `ERROR`.
