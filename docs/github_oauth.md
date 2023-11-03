# How to create a GitHub OAuth app

- Go to https://github.com/settings/apps and log in with your GitHub account.
- Click on the "New OAuth App" button and give your app a name, a homepage URL, and a callback URL. You can also add a description and a logo if you want.
- For the **Authorization callback URL**, enter `http://your-domain/authorize`. You need to use a public domain to make the oauth accessible to your users.
- - In `Permissions`, select `Account permissions`, set `Email addresses` and `Starring` to `Read-only`
- Click on the **Register application** button and copy your client ID and client secret. You will need them later.
- Copy and paste the **client ID** to the `GITHUB_CLIENT_ID` variable and the **client secret** to the `GITHUB_CLIENT_SECRET` variable in your .env file.
- Create the GitHub app and save your changes.

That's it! You have successfully created a GitHub OAuth app and obtained the client ID and client secret. 🙌
