# How to install Starguard on your server

Starguard is a bot that integrates Discord with GitHub. It provides various functionalities such as user validation, role assignment, and periodic checks of starred users. It's perfect for developers who want to reward their supporters and grow their community.

To install Discord Starguard on your server, you need to follow these steps:

## Step 1: Create a Discord bot account

- Go to the [Discord Developer Portal](https://discord.com/developers/applications) and log in with your Discord account.
- Click on the **New Application** button and give your application a name.
- Go to the **Bot** tab and click on the **Add Bot** button. You can also customize your bot's username and avatar.
- Copy your bot's token and save it somewhere safe. You will need it later.

## Step 2: Invite the bot to your server

- Go to the **OAuth2** tab and select the **bot** scope under **Scopes**.
- Select the permissions you want to give to your bot under **Bot Permissions**. For Discord Starguard, you need at least the following permissions: **Send Messages**, **Manage Roles** and **Use Slash Commands**.
- Copy the URL generated under **Scopes** and paste it in your browser.
- Choose the server you want to invite the bot to and click on the **Authorize** button.

## Step 3: Create a GitHub OAuth app

- Go to [https://github.com/settings/apps](https://github.com/settings/apps), select New App.
- Fill in the required fields, such as application name, homepage URL, description, etc.
- For the **Authorization callback URL**, enter `http://your-domain/authorize`. You need to use a public domain to make the oauth accessible to your users.
- Click on the **Register application** button and copy your client ID and client secret. You will need them later.
- In `Permissions`, select `Account permissions`, set `Email addresses` and `Starring` to `Read-only`
- Create the GitHub app and save your changes.

## Step 4: Configure the .env file

- Clone the repository: `git clone https://github.com/fuegovic/discord-starguard.git`
- Rename the file `.env.example` to `.env` in the root directory of the project and add the necessary variables
**see: [.env configuration](./env_file.md)**

## Step 6: Run the bot in a Docker container

- Install Docker desktop or Docker and Docker Compose on your machine if you don't have them already.
- Run this command in the root directory of the project: `docker-compose up -d`
- Wait for the bot to start and log in to your Discord server. You should see your bot online and ready to use.

🎉 Congratulations! You have successfully installed Discord Starguard on your server. You can now use slash commands to interact with it. 
