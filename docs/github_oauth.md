# How to create a GitHub OAuth app

- Go to https://github.com/settings/apps and log in with your GitHub account.
- Click on the "New OAuth App" button and give your app a name, a homepage URL, and a callback URL. You can also add a description and a logo if you want.
- Click on the "Create OAuth App" button. You will see your app's information, including the client ID and client secret.
- Copy and paste the client ID to the `GITHUB_CLIENT_ID` variable and the client secret to the `GITHUB_CLIENT_SECRET` variable in your .env file.

That's it! You have successfully created a GitHub OAuth app and obtained the client ID and client secret. 🙌