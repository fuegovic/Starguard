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

- Go to https://github.com/settings/apps and log in with your GitHub account.
- Click on the "New OAuth App" button and give your app a name, a homepage URL, and a callback URL. You can also add a description and a logo if you want.
- For the **Authorization callback URL**, enter `http://your-domain/authorize`. You need to use a public domain to make the oauth accessible to your users.
- - In `Permissions`, select `Account permissions`, set `Email addresses` and `Starring` to `Read-only`
- Click on the **Register application** button and copy your client ID and client secret. You will need them later.
- Copy and paste the **client ID** to the `GITHUB_CLIENT_ID` variable and the **client secret** to the `GITHUB_CLIENT_SECRET` variable in your .env file.
- Create the GitHub app and save your changes.

## Step 5: Create a classic GitHub public access token (PAT)

- Go to your GitHub account settings, select Developer settings, then Personal access tokens, then Generate new token (classic).
- Choose a name for your token and the scopes you want to grant to it. The scopes determine what resources and actions the token can access on GitHub.
- Click Generate token and copy the token to your clipboard. You can also view or delete your tokens at any time on the Personal access tokens page.
- Copy and paste the token to the `GITHUB_TOKEN` variable in your .env file.

## Step 6: Get the role ID, guild ID, and channel ID from Discord

- Enable developer mode in discord. You can do this by going to User Settings > Advanced > Developer Mode and toggling it on.
- Right-click on the role that you want to give to users who have starred your GitHub repo. You will see a "Copy ID" option. Click on it and paste it to the `ROLE_ID` variable in your .env file.
- Right-click on the server where you want to use the bot. You will see a "Copy ID" option. Click on it and paste it to the `GUILD_ID` variable in your .env file.
- Right-click on the channel where you want the bot to post messages. You will see a "Copy ID" option. Click on it and paste it to the `CHANNEL_ID` variable in your .env file.

## Step 7: Configure the .env file
- see: [env_file.md](./env_file.md) for more informations about the .env configuration
- Rename the file `.env.example` to `.env` in the root directory of the project and add the necessary variables

## Step 8: Run the bot in a Docker container

- Install Docker desktop or Docker and Docker Compose on your machine if you don't have them already.
- Run this command in the root directory of the project: `docker-compose up -d`
- Wait for the bot to start and log in to your Discord server. You should see your bot online and ready to use.

## Step 9: Advanced permissions
- On your server, in `Server Settings`, in the `Integrations` tab, you can limit the bot usage to a specific channel and limit the bot commands to specific user(s)/role(s)

## NGINX 
- There is a docker image that includes `nginx-proxy-manager`, you can use it with: `docker-compose -f ./deploy-compose.yml up --build`
- Access `nginx-proxy-manager` at http://localhost:81
  - login with: email: `admin@example.com` | password: `changeme`
  - Immediately after logging in with this default user you will be asked to modify your details and change your password.

🎉 Congratulations! You have successfully installed Discord Starguard on your server. You can now use slash commands to interact with it. 
