# How to install Starguard on your server

Starguard is a bot that integrates Discord with GitHub. It provides various functionalities such as user validation, role assignment, and periodic checks of starred users. It's perfect for developers who want to reward their supporters and grow their community.

To install Discord Starguard on your server, you need to follow these steps:

## Step 1: Clone the repository
- `git clone https://github.com/fuegovic/Starguard.git`

## Step 2: Obtain the app token and client ID from the Discord Dev Portal

- Go to the [Discord Developer Portal](https://discord.com/developers/applications) and log in with your Discord account.
- Click on the **New Application** button and give your application a name.
- Go to the **Bot** tab and click on the **Add Bot** button. You can also customize your bot's username and avatar.
- You will see your application's general information, including the client ID. Copy and paste it to the `CLIENT_ID` variable in your .env file.
- Copy your bot's token and paste it to the `TOKEN` variable in your .env file.

## Step 3: Invite the bot to your server

- Go to the **OAuth2** tab and select the **bot** scope under **Scopes**.
- Select the permissions you want to give to your bot under **Bot Permissions**. For Discord Starguard, you need at least the following permissions: **Send Messages**, **Manage Roles** and **Use Slash Commands**.
- Copy the URL generated under **Scopes** and paste it in your browser.
- Choose the server you want to invite the bot to and click on the **Authorize** button.

## Step 4: Create a GitHub OAuth app

- Go to https://github.com/settings/developers and log in with your GitHub account.
- Click on the "New OAuth App" button and give your app a name, a homepage URL, and a callback URL. You can also add a description and a logo if you want.
- For the **Authorization callback URL**, enter `https://your-domain/authorize`. You need a public domain with HTTPS to make the OAuth flow accessible to your users.
- Starguard requests only the `read:user` scope, which is enough to read the signed-in user's public profile and check whether they starred a public repository. You do not need to grant it anything else.
- Click on the **Register application** button and copy your client ID and client secret. You will need them later.
- Copy and paste the **client ID** to the `GITHUB_CLIENT_ID` variable and the **client secret** to the `GITHUB_CLIENT_SECRET` variable in your .env file.
- Create the GitHub app and save your changes.

## Step 5: Create a classic GitHub public access token (PAT)

- Go to your GitHub account settings, select Developer settings, then Personal access tokens, then Generate new token (classic).
- Choose a name for your token. **No scopes are required** for a public repository — this token is used only to list the repository's stargazers, which is public information. For a private repository, grant `repo`.
- This token is optional. Without it GitHub allows only 60 requests per hour, which is not enough for a repository with more than a few thousand stargazers.
- Click Generate token and copy the token to your clipboard. You can also view or delete your tokens at any time on the Personal access tokens page.
- Copy and paste the token to the `GITHUB_TOKEN` variable in your .env file.

## Step 6: Get the role ID, guild ID, and channel ID from Discord

- Enable developer mode in discord. You can do this by going to User Settings > Advanced > Developer Mode and toggling it on.
- Right-click on the role that you want to give to users who have starred your GitHub repo. You will see a "Copy ID" option. Click on it and paste it to the `ROLE_ID` variable in your .env file.
- Right-click on the server where you want to use the bot. You will see a "Copy ID" option. Click on it and paste it to the `GUILD_ID` variable in your .env file.
- Right-click on the channel where you want the bot to post messages. You will see a "Copy ID" option. Click on it and paste it to the `CHANNEL_ID` variable in your .env file.

## Step 7: Configure the .env file
- see: [env_file.md](./env_file.md) for more informations about the .env configuration
- Copy the file `.env.example` to `.env` in the root directory of the project and fill in the necessary variables
- Generate a real `SECRET_KEY`. Both containers refuse to start without one:
  ```sh
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- If a required variable is missing or invalid, the container exits immediately with a message naming it. Check the logs with `docker compose logs`.

## Step 8: Run the bot in a Docker container

- Install Docker desktop or Docker and Docker Compose on your machine if you don't have them already.
- You also need a MongoDB. To use the bundled one, copy `override.example.yml` to `docker-compose.override.yml` and set `MONGO_HOST=mongodb://mongodb:27017/` in your `.env`.
- Run this command in the root directory of the project: `docker compose up -d`
- Wait for the bot to start and log in to your Discord server. You should see your bot online and ready to use.

## Step 9: Advanced permissions
- On your server, in `Server Settings`, in the `Integrations` tab, you can limit the bot usage to a specific channel and limit the bot commands to specific user(s)/role(s)

## NGINX 
- There is a compose file that includes `nginx-proxy-manager`, MongoDB and Mongo Express. Use it with: `docker compose -f docker-compose.alt.yml up -d --build`
- Access `nginx-proxy-manager` at http://localhost:81
  - login with: email: `admin@example.com` | password: `changeme`
  - Immediately after logging in with this default user you will be asked to modify your details and change your password.

🎉 Congratulations! You have successfully installed Discord Starguard on your server. You can now use slash commands to interact with it. 

## Upgrading from an older version

Two changes need your attention when upgrading an existing deployment.

**1. Revoke the OAuth tokens the old version stored.** Earlier versions requested
the `repo` scope and saved each user's access token in the database in clear
text. On first start the server removes those stored tokens automatically and
logs how many it deleted, but tokens already handed out stay valid until they
are revoked. Revoke them from your OAuth app's page under
https://github.com/settings/developers, and reduce the app's requested scope to
`read:user`.

**2. Set a real `SECRET_KEY`.** It is now required, must be at least 16
characters, and must be the same for the bot and the server. Placeholder values
such as the `SecretKey` that used to ship in `.env.example` are rejected.

Existing verified users do not need to do anything else: their rows are kept,
and the next `/verify` fills in the new fields. Users are now linked by Discord
ID rather than by email address, and a GitHub account can only be linked to one
Discord user at a time.

## Running the tests

```sh
pip install -r requirements-dev.txt
pytest
pylint $(git ls-files '*.py')
```
