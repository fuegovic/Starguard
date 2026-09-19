# How to install Starguard on your server

Starguard is a Discord bot that grants a role to members who star a GitHub
repository, and takes the role back when they un-star it. It runs as two
containers, the bot and a small OAuth callback server, plus a MongoDB.

Read [README.md](../README.md#architecture) first if you want the shape of the
system before the steps.

## Before you start

You need:

- Docker and Docker Compose.
- A MongoDB. One is bundled, see [Step 7](#step-7-choose-your-mongodb).
- A Discord application with a bot token.
- A GitHub OAuth app (client ID and secret).
- **A public HTTPS address pointing at the OAuth server.** Members are sent
  there from Discord, and the flow does not work over plain HTTP. If you do
  not have one yet, [Step 9](#step-9-start-the-stack) includes a compose file
  that brings up Nginx Proxy Manager to obtain and terminate TLS for you.
- Optionally, a GitHub personal access token to raise the API rate limit.

## Step 1: Clone the repository

```sh
git clone https://github.com/fuegovic/Starguard.git
cd Starguard
```

## Step 2: Create the Discord application

- Go to the [Discord Developer Portal](https://discord.com/developers/applications)
  and sign in.
- Click **New Application** and give it a name.
- On the **Bot** tab, add a bot and customise its username and avatar if you
  want to.
- Copy the **Application ID** from the **General Information** tab into
  `CLIENT_ID`.
- Copy the bot's token into `TOKEN`. The token is shown once; if you lose it,
  reset it and use the new one.
- On the **Bot** tab, enable the **Server Members Intent** under **Privileged
  Gateway Intents**. The bot requests the guild members intent, and Discord
  refuses the connection if the application is not allowed to use it.

## Step 3: Invite the bot to your server

- On the **OAuth2** tab, select the **bot** scope.
- Under **Bot Permissions**, select at least **Manage Roles**, **Send
  Messages** and **Use Slash Commands**.
- Open the generated URL, choose your server, and click **Authorize**.
- In **Server Settings**, **Roles**, drag the bot's own role **above** the
  role it will hand out. Discord does not let a bot grant a role positioned
  at or above its own highest role, and this is the single most common reason
  for a verification that succeeds but never produces a role.

## Step 4: Create a GitHub OAuth app

- Go to <https://github.com/settings/developers> and click **New OAuth App**.
- Give it a name and a homepage URL.
- For the **Authorization callback URL**, enter your public address with
  `/authorize` appended, for example
  `https://starguard.example.com/authorize`. It must match exactly, including
  the scheme, or GitHub refuses the redirect.
- Click **Register application**, then copy the **Client ID** into
  `GITHUB_CLIENT_ID` and generate a **Client secret** and copy it into
  `GITHUB_CLIENT_SECRET`.

Starguard requests only the `read:user` scope, which is enough to read the
signed-in user's public profile and check whether they starred a public
repository. It asks for nothing else, and it discards the access token as soon
as the request that used it finishes.

## Step 5: Create a GitHub personal access token

This step is optional but recommended for anything beyond a small repository.

- Go to **Settings**, **Developer settings**, **Personal access tokens**,
  **Tokens (classic)**, **Generate new token (classic)**.
- **No scopes are required for a public repository.** The token is used only
  to list stargazers, which is public information. Grant `public_repo` if you
  prefer to scope it explicitly, or `repo` for a private repository.
- Copy the token into `GITHUB_TOKEN`.

Without a token GitHub allows 60 API requests per hour from your server's
address. The stargazer listing is fetched 100 entries per request, so one pass
over a repository with more than a few thousand stars will run out. With a
token the limit is 5000 requests per hour.

## Step 6: Get the role, server and channel IDs

- Enable Developer Mode in Discord: **User Settings**, **Advanced**,
  **Developer Mode**.
- Right-click the role you want to hand out, choose **Copy ID**, and paste it
  into `ROLE_ID`.
- Right-click your server's icon, choose **Copy ID**, and paste it into
  `GUILD_ID`.
- Right-click the channel the bot should announce in, choose **Copy ID**, and
  paste it into `CHANNEL_ID`.

All three must be the numeric IDs. A role name such as `@Stargazer` is
rejected at startup.

## Step 7: Choose your MongoDB

**Option A: the bundled MongoDB.** Copy the override file so Compose picks it
up automatically:

```sh
cp override.example.yml docker-compose.override.yml
```

This adds a `mongodb` service and a Mongo Express UI published on loopback
only. The database **requires authentication**: pick a username and a password
and set all three of these consistently in `.env`.

```ini
MONGO_INITDB_ROOT_USERNAME=starguard
MONGO_INITDB_ROOT_PASSWORD=<a long random password>
MONGO_HOST=mongodb://starguard:<the same password>@mongodb:27017/?authSource=admin
```

Keep `?authSource=admin`. The bundled image creates its root user in the
`admin` database, and saying so explicitly keeps the string correct even if
someone later appends a database name to the path. If your password contains
any of `: / ? # [ ] @`, percent-encode it or choose one that does not.

Compose refuses to start the database if the username or password is missing,
and Mongo Express refuses to start without `MONGO_EXPRESS_USERNAME` and
`MONGO_EXPRESS_PASSWORD`.

**Option B: your own MongoDB.** Leave `docker-compose.override.yml` out and
point `MONGO_HOST` at your instance, for example a
`mongodb+srv://` connection string for Atlas. Starguard needs read and write
access to one collection, `users`, in `MONGO_DATABASE`.

## Step 8: Configure the .env file

```sh
cp .env.example .env
```

Fill in every value from the steps above. The full reference, including
defaults and what each wrong value does, is in
[env_file.md](./env_file.md).

Generate a real `SECRET_KEY`. Both processes refuse to start without one, and
**both must have the same value**:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set `DOMAIN` to your public HTTPS address, with no path, query string or
fragment:

```ini
DOMAIN=https://starguard.example.com
```

Set `TRUSTED_PROXY_COUNT` to the number of reverse proxies you operate in
front of the server. With the bundled Nginx Proxy Manager, or one proxy of
your own, that is `1`. Add one for each additional hop you control, such as a
CDN. Getting this wrong either collapses every visitor onto one address for
rate-limiting purposes or lets a client spoof its address, so it is worth a
minute's thought. See
[`TRUSTED_PROXY_COUNT`](./env_file.md#trusted_proxy_count).

If a required variable is missing or invalid, the container exits immediately
with a message naming it. Read it with `docker compose logs`.

## Step 9: Start the stack

For the bot and the server, with a database from Step 7:

```sh
docker compose up -d --build
```

If you also want Nginx Proxy Manager to terminate TLS, use the all-in-one file
instead. It contains the bot, the server, MongoDB, Mongo Express and NPM, and
it is a **replacement** for `docker-compose.yml`, not an addition to it:

```sh
docker compose -f docker-compose.alt.yml up -d --build
```

Before you expose ports 80 and 443, set `NPM_INITIAL_ADMIN_EMAIL` and
`NPM_INITIAL_ADMIN_PASSWORD` in `.env`. Without them, Nginx Proxy Manager
starts with its well-known default login (`admin@example.com` / `changeme`)
until someone signs in and changes it.

The NPM admin UI is published on loopback only, at <http://localhost:81>.
Reach it over an SSH tunnel from another machine. Inside it, add a proxy host
for your domain forwarding to the host `server` on port `SERVER_BIND_PORT`
(5000 unless you changed it), and request a Let's Encrypt certificate for it.
`server` is the compose service name, which resolves on the shared
`Starguard` network.

## Step 10: Check that it works

```sh
docker compose ps
```

Both `discord-bot` and `server` should be `running` and, after a minute or so,
`healthy`. The bot's healthcheck reports unhealthy until it has connected to
the Discord gateway, which is why it has a 60 second start period.

```sh
docker compose logs -n 50 discord-bot
docker compose logs -n 50 server
```

You are looking for `<botname> connected to Discord` and
`Starguard OAuth server listening on port 5000`.

Check the server from the outside, through your real domain:

```sh
curl -fsS https://starguard.example.com/healthz
```

A healthy server answers `{"status":"ok"}`. It answers 503 with
`{"database":"unavailable","status":"degraded"}` only when it could not build
a database client at all, which means `MONGO_HOST` is malformed. A database
that is merely unreachable does **not** show up here: check the logs for
`Could not prepare the users collection:` as well.

Then run `/verify` in Discord and walk the three buttons yourself. If anything
goes wrong, [troubleshooting.md](./troubleshooting.md) lists each failure by
the symptom you actually see.

## Step 11: Restrict who can use the bot

In **Server Settings**, **Integrations**, select the bot. You can limit its
commands to specific channels and specific roles or members there. Restricting
`/checkstars` to moderators is worth doing: it forces a full pass over the
stargazer list and therefore spends GitHub API budget.

## Running without Docker

Useful for development. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full
setup.

```sh
pip install -r requirements.txt
python -m bot.bot        # in one shell
python -m server.server  # in another
```

Both processes read the same `.env` through `python-dotenv`. The server binds
`SERVER_BIND_PORT` (default 5000) on all interfaces and still expects to sit
behind something that terminates TLS, because the session cookie it sets is
marked `Secure`.

## Upgrading from an older version

Four changes need your attention when upgrading an existing deployment. Work
through them **before** starting the new containers.

### 1. Revoke the OAuth tokens the old version stored

Earlier versions requested the `repo` scope and saved each user's GitHub
access token in the database in clear text. On first start the new code
removes those stored tokens automatically and logs how many it deleted:

```
Removed stored OAuth tokens/emails from 37 existing user record(s). Any GitHub tokens previously issued to this app should be revoked.
```

Tokens already handed out stay valid until they are revoked. Revoke them from
your OAuth app's page under <https://github.com/settings/developers>, and
reduce the app's requested scope to `read:user`. See
[SECURITY.md](../SECURITY.md) for the full account of the issue.

### 2. Set a real `SECRET_KEY`

It is now required, must be at least 16 characters, and must be identical for
the bot and the server. Placeholder values such as the `SecretKey` that used
to ship in `.env.example` are rejected outright.

### 3. The bundled MongoDB now requires authentication

**This is the breaking part of the upgrade. Read all of it before you start
anything.**

The bundled database used to run with `mongod --noauth`, reachable without
credentials by anything on the compose network. It now sets
`MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD`, and both
compose files refuse to start it without them.

Here is the trap. The official `mongo` image adds `--auth` to `mongod` as soon
as those two variables are set, but it only **creates** the root user when the
data directory is empty. Its entrypoint skips initialisation if any of
`/data/db/WiredTiger`, `/data/db/journal`, `/data/db/local.0` or
`/data/db/storage.bson` already exists, which on an existing deployment they
all do, because `./server/mongo-data` is bind-mounted there.

So on an existing deployment the result of simply setting the variables is:
authentication is **on**, and there is **no user to authenticate as**. The
`mongodb` container's healthcheck fails permanently, and on every start the
bot and the server log a `Could not prepare the users collection:` line whose
reason is an authentication failure from the driver.

They do not exit over it. They keep running with no working database, which
means verification appears to work right up to the point where the result is
saved.

You do not have to delete anything to fix this. MongoDB has a documented
[localhost exception](https://www.mongodb.com/docs/manual/core/localhost-exception/):
while access control is enabled and **no users or roles exist anywhere in the
deployment**, a connection made over the loopback interface may create the
first user. That is exactly the situation here, and `mongosh` run inside the
container is exactly such a connection.

**Step by step:**

1. **Stop everything.** From the repository root:

   ```sh
   docker compose down
   ```

2. **Back up the data directory before you touch anything else.** The files
   are owned by the `mongodb` user inside the container, so this usually needs
   `sudo`:

   ```sh
   sudo tar czf ~/starguard-mongo-backup-$(date +%F).tar.gz -C ./server mongo-data
   ls -lh ~/starguard-mongo-backup-*.tar.gz
   ```

   Do not continue until you have confirmed the archive exists and is not
   empty. Everything below is reversible from this backup; nothing below is
   reversible without it.

3. **Set the new variables** in `.env`, exactly as in
   [Step 7](#step-7-choose-your-mongodb). The credentials in `MONGO_HOST` must
   match `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD`, and
   `MONGO_HOST` should carry `?authSource=admin`.

4. **Start the database on its own**, so nothing else is retrying against it
   while you work:

   ```sh
   docker compose up -d mongodb
   docker compose logs -n 30 mongodb
   ```

   You should see mongod come up normally. The container will report
   `unhealthy` after a minute or so, because its healthcheck authenticates
   with credentials that do not exist yet. That is expected at this point.

5. **Create the root user through the localhost exception.** Open a shell on
   the container and use `mongosh` with no credentials:

   ```sh
   docker compose exec mongodb mongosh
   ```

   Then, at the `mongosh` prompt, substituting your own values:

   ```js
   use admin
   db.createUser({
     user: "starguard",
     pwd: "the password you put in .env",
     roles: [ { role: "root", db: "admin" } ]
   })
   exit
   ```

   `createUser` must be the first user-creating command you run: the localhost
   exception closes as soon as any user or role exists. If it fails with an
   authorization error, a user already exists in your deployment. In that case
   do not force it: authenticate as that user and create the root user with
   it, or set `MONGO_HOST` to use the account you already have.

6. **Verify the credentials work**, the same way the healthcheck does:

   ```sh
   docker compose exec mongodb mongosh --quiet \
     -u starguard -p 'the password you put in .env' \
     --authenticationDatabase admin \
     --eval "db.adminCommand('ping')"
   ```

   This should print `{ ok: 1 }`.

7. **Check your data is still there:**

   ```sh
   docker compose exec mongodb mongosh --quiet \
     -u starguard -p 'the password you put in .env' \
     --authenticationDatabase admin \
     --eval 'db.getSiblingDB("starguard").users.countDocuments({})'
   ```

   The number should match what you had before the upgrade.

8. **Start the rest:**

   ```sh
   docker compose up -d --build
   docker compose ps
   ```

   `mongodb` should now become `healthy`, and the bot and the server should
   stop logging connection errors.

> **Last resort only.** Deleting `./server/mongo-data` makes the image
> initialise a fresh database and create the root user by itself, but it
> **permanently destroys every verified member's link record**. Everyone who
> already holds the role keeps it, with nothing left for the un-star check to
> match them against, and every member has to run `/verify` again. Do this
> only if the localhost exception genuinely does not apply to your deployment,
> only after the backup in step 2, and only knowingly. There is no automatic
> way back.

### 4. `DOMAIN` is now validated at startup

The bot refuses to start unless `DOMAIN` is an absolute `https://` URL with no
query string and no fragment:

```
Configuration error: DOMAIN must be an absolute https URL such as https://starguard.example.com, got 'http://starguard.example.com'.
```

If you were running on plain HTTP, this is not a check to work around. The
server marks its session cookie `Secure`, so no browser sends that cookie back
over HTTP, and the callback fails for every user with
`Your verification session has expired.` Put a TLS-terminating proxy in front
of the server and set `DOMAIN` to the HTTPS address. The all-in-one compose
file in [Step 9](#step-9-start-the-stack) includes one.

While you are there, set `TRUSTED_PROXY_COUNT` to match the number of proxies
you operate. It defaults to `1`.

### Also worth knowing

- **The bot now has a healthcheck.** It serves `GET /healthz` on
  `127.0.0.1:8080` inside its own container, and both compose files probe it.
  If you set `BOT_HEALTH_ENABLED=false`, nothing answers the probe and the
  container is reported `unhealthy` forever; disable the `healthcheck` block
  too if you turn the endpoint off.
- **Seven variables are new**: `SERVER_BIND_PORT`, `TRUSTED_PROXY_COUNT`,
  `LOGIN_RATE_LIMIT`, `LOGIN_RATE_LIMIT_WINDOW`, `BOT_HEALTH_ENABLED`,
  `BOT_HEALTH_HOST`, `BOT_HEALTH_PORT` and `LOG_FORMAT`. All of them have
  working defaults, so an existing `.env` keeps working once the three changes
  above are handled.
- **Existing verified members do not need to do anything.** Their rows are
  kept and brought forward to the current schema at startup, which is logged
  as `Upgraded N user record(s) to schema version 2`. Members are now keyed by
  Discord ID rather than by email address, and a GitHub account can only be
  linked to one Discord user at a time.

## Development and tests

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the development setup and the
full list of checks that CI runs.

```sh
pip install -r requirements-dev.txt
pytest -q
```

## When something is wrong

[troubleshooting.md](./troubleshooting.md) covers the failures operators
actually hit, each one starting from the symptom you see rather than the cause
you do not know yet.
