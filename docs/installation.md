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
- Optionally, admin access to the repository, to add the star webhook in
  [Step 12](#step-12-set-up-the-star-webhook-optional). Without it the bot
  still works; it just learns about stars on a timer instead of immediately.

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
  prefer to scope it explicitly.
- Copy the token into `GITHUB_TOKEN`.

Without a token GitHub allows 60 API requests per hour from your server's
address. The stargazer listing is fetched 100 entries per request, so one pass
over a repository with more than a few thousand stars will run out. With a
token the limit is 5000 requests per hour.

> **Private repositories do not work end to end.** A `repo`-scoped
> `GITHUB_TOKEN` does let the **bot** list a private repository's stargazers,
> so the periodic check runs. The **server** does not use that token: it asks
> `GET /user/starred/{owner}/{repo}` with the **member's own** OAuth token,
> and that token carries only `read:user`, which cannot see a private
> repository. GitHub answers 404, the server reads 404 as "not starred", and
> every member is told they have not starred it however many times they try.
> Point Starguard at a public repository.

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

Both options below put values in `.env`, so create it now if you have not
already. [Step 8](#step-8-configure-the-env-file) is where you fill in the
rest of it:

```sh
cp .env.example .env
```

`cp` overwrites without asking, so run it once, before you type anything into
the file.

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
someone later appends a database name to the path.

`MONGO_HOST` is a URI, so **percent-encode the reserved characters in the
password**: `@ : / ? % +`. (`# [ ] ! $` need no encoding.) Encode them in
`MONGO_HOST` only. `MONGO_INITDB_ROOT_PASSWORD` is the password
MongoDB is actually created with, and the healthcheck and Mongo Express use
it literally too, so encoding it in both places gives you a database that
reports healthy while the bot and the server cannot sign in to it. The
easiest way out is to generate the password rather than invent one, with the
same command `.env.example` suggests, since its alphabet contains none of
those characters:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

See [`MONGO_HOST`](./env_file.md#mongo_host) for what each character does.

The override also brings up Mongo Express, which needs two credentials of its
own. Uncomment them in `.env` and fill them in:

```ini
MONGO_EXPRESS_USERNAME=<a name for the admin UI>
MONGO_EXPRESS_PASSWORD=<a long random password>
```

All four of these are required, and Compose checks them before it starts
**anything**. Leave one out and no container comes up at all, not even the
bot and the server:

```
error while interpolating services.mongo-express.environment.ME_CONFIG_BASICAUTH_USERNAME: required variable MONGO_EXPRESS_USERNAME is missing a value: set MONGO_EXPRESS_USERNAME in .env
```

**Option B: your own MongoDB.** Leave `docker-compose.override.yml` out and
point `MONGO_HOST` at your instance, for example a
`mongodb+srv://` connection string for Atlas. Starguard needs read and write
access, including the right to create indexes, on two collections in
`MONGO_DATABASE`: `users` and `webhook_deliveries`. Both are created and
indexed at every startup, **whether or not you set up the optional star
webhook** in [Step 12](#step-12-set-up-the-star-webhook-optional); an
installation without a webhook simply leaves `webhook_deliveries` empty.
Granting `readWrite` on `MONGO_DATABASE` covers all of it.

## Step 8: Configure the .env file

`.env` already exists, from Step 7, with your database values in it. Fill in
every remaining value from the steps above. The full reference, including
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

**The server's port is published on loopback only**, as
`127.0.0.1:${SERVER_PORT}:${SERVER_BIND_PORT}`, so after this command the
application is reachable from the host machine and from nowhere else. Your
TLS-terminating proxy has to run on the same host to reach it, which is what
the all-in-one file below does. If your proxy runs on a different machine,
remove the `127.0.0.1:` from that mapping and set `TRUSTED_PROXY_COUNT` to
your real hop count, knowing the origin is then reachable without TLS by
anything that can route to it.

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

Check the server on the host, where the port is published:

```sh
curl -fsS http://127.0.0.1:5000/healthz
```

Then from the outside, through your real domain, which is what members use:

```sh
curl -fsS https://starguard.example.com/healthz
```

The first working and the second not is a proxy or DNS problem, not a
Starguard one.

A healthy server answers `{"status":"ok"}`. It answers 503 with
`{"database":"unavailable","status":"degraded"}` whenever it sends a `ping`
command to MongoDB and does not get an answer. The probe really does reach
the database, so a 200 here is more than the process being up.

**It is not proof that verification will save anything, though.** `ping` asks
whether the database answers, not whether this account may write to it, and
the startup that creates the indexes treats a failure as best effort and
carries on. So a MongoDB user who can connect but cannot update `users`
produces exactly this 200 while every OAuth callback fails at the last step.
That is the deployment this check will certify and the walkthrough below will
not, which is why the walkthrough is the one that settles it.

A `MONGO_HOST` the driver cannot use does not appear here at all: the server
checks that value as it loads its configuration and exits rather than serving,
with `Configuration error: MONGO_HOST is not a usable MongoDB connection
string`.

Then run `/verify` in Discord and walk the three buttons yourself. If anything
goes wrong, [troubleshooting.md](./troubleshooting.md) lists each failure by
the symptom you actually see.

## Step 11: Restrict who can use the bot

In **Server Settings**, **Integrations**, select the bot. You can limit its
commands to specific channels and specific roles or members there. Restricting
`/checkstars` to moderators is worth doing: it forces a full pass over the
stargazer list and therefore spends GitHub API budget.

## Step 12: Set up the star webhook (optional)

Everything so far works without this step, but it is worth understanding what
you are skipping, because the two directions are not symmetrical.

Without the webhook, the only automatic mechanism is the periodic sweep, and
**the sweep only ever takes the role away**. It compares the stargazer listing
against the database and removes the role from anyone who is no longer in the
listing; there is no code path in which it grants one. So an un-star is
noticed at the next sweep, up to `AUTOMATIC_CHECK_DELAY` seconds later, and a
**star is never noticed at all**: the member has to go back through the GitHub
sign-in, because that is the one thing that records a star. Each sweep also
pays one GitHub API request per 100 stargazers. On a repository with 45,000
stars an hourly sweep is 450 requests an hour, and that number grows every
time somebody stars it.

With the webhook, GitHub tells the server the moment a star changes, in either
direction. The delivery costs no GitHub API budget at all, and the member's
role moves within `ROLE_SYNC_INTERVAL` seconds instead of waiting for a sweep
that, for a new star, would never come.

You need admin access to the repository to add a webhook, and the public
HTTPS address from [Step 9](#step-9-start-the-stack) has to be reachable from
GitHub.

### 1. Generate the secret

This is a **second** secret, separate from `SECRET_KEY`. Generate it the same
way:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `.env` and keep the terminal open, because you need the same value
on GitHub in a moment:

```ini
GITHUB_WEBHOOK_SECRET=<the value you just generated>
```

Only the server reads it. Both containers load the same `.env`, so there is
nothing extra to do for the bot.

### 2. Restart the server

```sh
docker compose up -d server
```

The receiver is registered **only** when the secret is set, so this restart is
what creates the route. Until the server has restarted, GitHub's test delivery
in step 4 will come back 404.

Confirm it started cleanly:

```sh
docker compose logs -n 20 server
```

A rejected secret stops the server outright, with the variable named:

```
Configuration error: GITHUB_WEBHOOK_SECRET must be at least 16 characters, got 8.
```

### 3. Add the webhook on GitHub

Go to your repository, then **Settings**, **Webhooks**, **Add webhook**.

| Field | Value |
| --- | --- |
| **Payload URL** | Your public HTTPS address with `/webhooks/github` appended, for example `https://starguard.example.com/webhooks/github` |
| **Content type** | `application/json` |
| **Secret** | The value you put in `GITHUB_WEBHOOK_SECRET` |
| **SSL verification** | Leave it enabled |
| **Which events** | **Let me select individual events**, then tick **Stars** and nothing else |
| **Active** | Ticked |

Two of those are worth being deliberate about.

**The content type must be `application/json`.** With
`application/x-www-form-urlencoded` GitHub wraps the payload in a
`payload=<json>` form field, so the body is no longer a JSON object. The
signature still verifies and the `ping` in step 4 still comes back green,
which makes this one easy to miss: it is only the real `star` deliveries that
then fail, every one of them with 400 and `Body is not a JSON object.`

**Do not choose "Send me everything".** The receiver answers 204 and does
nothing for every event that is not `star`, so nothing breaks, but you would
be sending your entire repository event stream to a process that discards
almost all of it. Untick **Pushes**, which GitHub selects by default, when you
switch to individual events.

Click **Add webhook**.

### 4. Confirm the ping delivery

GitHub sends a `ping` event as soon as the webhook is created, and its result
is the one piece of evidence worth waiting for. Open the webhook, go to
**Recent Deliveries**, and open the `ping` entry.

A green tick and **200** in the **Response** tab, with the body `pong`, means
the address, the TLS, the path and the secret are all correct. The server logs
it as:

```
Ping received for hook 512345678.
```

Anything else is covered by
[the webhook section of the troubleshooting guide](./troubleshooting.md#the-star-webhook-is-not-working);
the status code in the delivery log tells you which entry to read. You can
resend the ping from the **Redeliver** button on that page after each change,
rather than un-starring and starring the repository again to test.

Then test the real thing: un-star and re-star the repository with an account
that has already verified with the bot, and watch the role come back within
`ROLE_SYNC_INTERVAL` seconds.

### 5. Only now, raise `AUTOMATIC_CHECK_DELAY`

Once you have seen real `star` deliveries come back green for a day or so,
move the sweep from hourly to daily:

```ini
AUTOMATIC_CHECK_DELAY=86400
```

```sh
docker compose up -d discord-bot
```

**Do not set `AUTOMATIC_CHECK=false`.** The sweep is not made redundant by the
webhook, and this is the part that is easy to get wrong.

[GitHub does not automatically retry a failed
delivery](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries).
Redelivery is a manual action, from the **Redeliver** button in the repository's
delivery log. So a delivery that arrived while your server was restarting, or
while the database was away, is simply lost, and nothing will ever bring it
back on its own. The member keeps a role they un-starred for, or never gets
the one they starred for, until something else notices.

The sweep is that something else. It is the only thing that repairs a missed
delivery on its own, which is why it stays on even when the webhook is
healthy. What the webhook buys you is the freedom to run it daily instead of
hourly: 450 API requests a day rather than 450 an hour, with role changes
still landing in seconds.

If you do turn the sweep off anyway, know which direction you are exposed in.
The sweep only ever **takes** the role away; it never grants one. So with it
off, a lost delivery for somebody who un-starred is repaired by nothing at all
until a moderator runs `/checkstars`.

A lost delivery for somebody who **starred** is not repaired by the sweep
either, on or off. **Claim your role** does not help: it reads the star state
recorded in the database, which a lost delivery never updated, so the button
will keep saying they have not starred. The two things that do work are
redelivering the event from GitHub's delivery log, and having the member press
**Get a new link 🔄** and sign in with GitHub again, which is what re-records
the star.

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
through them **before** starting the new containers. A fifth costs you nothing
today but changes what happens the next time you point the bot at a different
repository, so read it and remember it.

### 1. Revoke the OAuth tokens the old version stored

Earlier versions requested the `repo` scope and saved each user's GitHub
access token in the database in clear text. Every start attempts to delete
those stored tokens, and logs how many it removed:

```
Removed stored OAuth tokens/emails from 37 existing user record(s). Any GitHub tokens previously issued to this app should be revoked.
```

The purge is attempted on its own, independently of the index creation and the
schema upgrade that run beside it, so a failure in either of those no longer
takes the purge with it. It is still best effort: if the purge itself fails,
you get

```
Could not purge credentials written by older versions: <reason>
```

and the tokens are **still in the database**. Fix the reason, usually a
missing `readWrite` grant, and restart. Confirm it with a count of the rows
that still carry one:

```sh
docker compose exec mongodb mongosh --quiet \
  -u starguard -p 'the password you put in .env' \
  --authenticationDatabase admin \
  --eval 'db.getSiblingDB("starguard").users.countDocuments({github_token:{$exists:true}})'
```

Zero means the purge has run.

Tokens already handed out stay valid until they are revoked, and **the purge
does not revoke them**. It deletes your copy; GitHub still honours them.

There is no setting to change on the app itself. An OAuth app has no
configured scope: the scope is what the code asks for in the authorization
URL, and the current code asks for `read:user` only.

**Do this before the first start of the new version**, because the purge
deletes the only copy of the tokens you hold and GitHub's revocation endpoint
needs the token itself. Write them to a file:

```sh
docker compose exec mongodb mongosh --quiet \
  -u starguard -p 'the password you put in .env' \
  --authenticationDatabase admin \
  --eval 'db.getSiblingDB("starguard").users.find({github_token:{$exists:true}}, {_id:0, github_token:1}).forEach(d => print(d.github_token))' \
  > legacy-tokens.txt
```

Then revoke each one against your own app, with the client id and secret as
the HTTP basic credentials:

```sh
while IFS= read -r token; do
  curl -sS -o /dev/null -w '%{http_code}\n' -X DELETE \
    -u "$GITHUB_CLIENT_ID:$GITHUB_CLIENT_SECRET" \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/applications/$GITHUB_CLIENT_ID/grant" \
    -d "{\"access_token\":\"$token\"}"
done < legacy-tokens.txt
```

`204` is a revoked grant, and with it every token that grant had issued to
that member. `404` means there was nothing left to revoke. Delete the file
when you are done: `rm legacy-tokens.txt`.

**If the purge has already run**, you no longer hold the tokens, and GitHub
offers app owners no way to revoke without them. Every affected member then
has to revoke for themselves, at
`https://github.com/settings/connections/applications/<your client id>`, and
you can neither do it for them nor see who has. The only alternative is
deleting the OAuth app, which costs everyone a re-verification. See
[SECURITY.md](../SECURITY.md) for the full account of the issue and of the
pages that look like they would help but do not.

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

**Pulling this release does not change your database service, and that alone
will leave it unauthenticated.** If you use the bundled database through
`docker-compose.override.yml`, that file is a copy you made of
`override.example.yml` and it is listed in `.gitignore`, so `git pull` leaves
your old copy exactly as it was, `command: mongod --noauth` and all. Setting
the two variables in `.env` changes nothing while that line is still there:
you would create a root user in step 5 below and go on running a database
that asks nobody for it. Replace or merge the file, after the backup in step
2 below and before you start anything:

```sh
cp docker-compose.override.yml docker-compose.override.yml.bak
cp override.example.yml docker-compose.override.yml
```

If your copy carries edits of your own, such as a memory limit or an extra
service, merge them into the new file by hand rather than keeping the old one.
Then ask Compose what it will actually run, which folds every file it reads
into one document and drops the comments:

```sh
docker compose config | grep -n noauth
```

That must print nothing. A stale override shows itself as

```
      - --noauth
```

`docker-compose.alt.yml` is tracked, so `git pull` does update it; the trap is
the override copy only. Add `-f docker-compose.alt.yml` to the command above
if that is the file you deploy with.

> **The new file also moves the image from `mongo:4.4.18` to `mongo:7`, and
> MongoDB cannot make that jump in one go.** MongoDB supports upgrading one
> major release at a time, 4.4 to 5.0 to 6.0 to 7.0, setting
> `featureCompatibilityVersion` at each step; started straight on 4.4 data
> files, `mongod` 7.0 refuses to come up and says so in the `mongodb`
> container's log. If your `./server/mongo-data` was written by the 4.4 image
> that earlier versions of this file pinned, do **not** simply start the new
> one. Either follow MongoDB's own
> [release upgrade procedure](https://www.mongodb.com/docs/manual/release-notes/7.0-upgrade-standalone/)
> through each major version in turn, pinning `image:` to each as you go, or
> take a `mongodump` with 4.4, start 7 on an empty data directory, and
> `mongorestore` into it. Either way the backup in step 2 below is what makes
> this recoverable. This is independent of the authentication change; it is
> the same data directory and the same restart, so do both in one pass.
>
> A deployment that never used the bundled database, or whose data directory
> was created by `mongo:7` already, is unaffected.

Here is the trap. The official `mongo` image adds `--auth` to `mongod` as soon
as those two variables are set, but it only **creates** the root user when the
data directory is empty. Its entrypoint skips initialisation if any of
`/data/db/WiredTiger`, `/data/db/journal`, `/data/db/local.0` or
`/data/db/storage.bson` already exists, which on an existing deployment they
all do, because `./server/mongo-data` is bind-mounted there.

So on an existing deployment the result of simply setting the variables is:
authentication is **on**, and there is **no user to authenticate as**. The
`mongodb` container's healthcheck fails permanently, and on every start the
bot and the server log four `Could not ...` lines, one per piece of startup
work, each ending in an authentication failure from the driver:

```
Could not index the users collection: <reason>
Could not index the deliveries collection: <reason>
Could not purge credentials written by older versions: <reason>
Could not upgrade user records: <reason>
```

They do not exit over it. They keep running with no working database, which
means verification appears to work right up to the point where the result is
saved. The server's `/healthz` does report it, with 503 and
`{"database":"unavailable","status":"degraded"}`.

You do not have to delete anything to fix this. MongoDB has a documented
[localhost exception](https://www.mongodb.com/docs/manual/core/localhost-exception/):
while access control is enabled and **no users or roles exist anywhere in the
deployment**, a connection made over the loopback interface may create the
first user. That is exactly the situation here, and `mongosh` run inside the
container is exactly such a connection.

**Step by step:**

> **If you deploy with `docker-compose.alt.yml`, every command below needs
> `-f docker-compose.alt.yml`**, right after `docker compose` and before the
> subcommand, for example `docker compose -f docker-compose.alt.yml down`.
> Without it Compose reads `docker-compose.yml` plus any override, which is a
> different set of services: `docker compose down` then stops the wrong
> stack and `docker compose exec mongodb` reports no such service. Exporting
> `COMPOSE_FILE=docker-compose.alt.yml` in your shell for the duration of the
> upgrade does the same thing once, for every command in this section and the
> one in [section 1](#1-revoke-the-oauth-tokens-the-old-version-stored).

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

3. **Replace `docker-compose.override.yml` and set the new variables.** The
   override file is the part that is easy to skip, because nothing in the
   release changes it for you; the two `cp` commands and the
   `docker compose config | grep -n noauth` check are above, and that grep
   must print nothing before you go on. Then set the variables in `.env`,
   exactly as in [Step 7](#step-7-choose-your-mongodb): the credentials in
   `MONGO_HOST` must match `MONGO_INITDB_ROOT_USERNAME` and
   `MONGO_INITDB_ROOT_PASSWORD`, and `MONGO_HOST` should carry
   `?authSource=admin`.

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

### 5. If you ever change which repository the bot watches

This one does not bite on this upgrade. It bites the first time you change
`REPO_OWNER` or `GITHUB_REPO` while keeping the same database, so read it now
and remember where it is.

**Claiming the role is now refused unless the member's link was made for the
repository you have configured.** Every row records the repository it was
created against, and the claim button compares it. Existing rows name the old
repository, so after a change **every member has to run `/verify` and sign in
with GitHub again** before they can claim.

That is a security fix, not a limitation. Without the comparison, a
`starred_repo: true` row proved only that somebody had starred *some*
repository at some point, and repointing the bot silently handed the role to
everyone who had starred the old one, none of whom had starred the new one.
There was nothing in the logs to show it happening.

Plan for it when you repoint:

- Tell members in advance that they have to re-verify, because the button
  will otherwise look broken to them.
- Run `/checkstars` afterwards. Roles granted for the old repository are not
  taken back by the change itself; the sweep is what reconciles them against
  the new stargazer listing.
- If you changed the values by mistake, put them back and the existing rows
  work again immediately. Nothing is deleted or rewritten by the refusal.

The symptom, and what the member sees, is in
[the troubleshooting guide](./troubleshooting.md#everybody-who-verified-before-is-suddenly-not-linked).

### Also worth knowing

- **The bot now has a healthcheck.** It serves `GET /healthz` on
  `127.0.0.1:8080` inside its own container, and both compose files probe it.
  If you set `BOT_HEALTH_ENABLED=false`, nothing answers the probe and the
  container is reported `unhealthy` forever; disable the `healthcheck` block
  too if you turn the endpoint off.
- **It measures both reconciling loops, so a container that looked healthy
  before may now report `unhealthy`.** The payload names `star_check` for the
  periodic sweep and `role_sync` for the webhook drain, each `disabled`,
  `pending`, `ok` or `stale`, and either one going stale is a 503. The case
  that changes on upgrade is a webhook-only deployment,
  `AUTOMATIC_CHECK=false` with `ROLE_SYNC_ENABLED=true`: the drain is then the
  only thing reconciling anything, and a bot whose drain had never once
  reached the database still answered 200 for as long as it ran. If a
  container goes `unhealthy` after this upgrade, read the payload before
  assuming the healthcheck is at fault: it is more likely to be telling you
  about a database the bot has never reached. The field meanings are in
  [env_file.md](./env_file.md#bot-health-check) and the failures in
  [the troubleshooting guide](./troubleshooting.md#discord-bot).
- **Eleven variables are new**: `SERVER_BIND_PORT`, `TRUSTED_PROXY_COUNT`,
  `LOGIN_RATE_LIMIT`, `LOGIN_RATE_LIMIT_WINDOW`, `BOT_HEALTH_ENABLED`,
  `BOT_HEALTH_HOST`, `BOT_HEALTH_PORT`, `LOG_FORMAT`, `GITHUB_WEBHOOK_SECRET`,
  `ROLE_SYNC_ENABLED` and `ROLE_SYNC_INTERVAL`. All of them have working
  defaults, so an existing `.env` keeps working once the three changes above
  are handled. `GITHUB_WEBHOOK_SECRET` defaults to unset, which means no
  webhook receiver and the behaviour you already had; see
  [Step 12](#step-12-set-up-the-star-webhook-optional) when you want it.
- **Existing verified members do not need to do anything.** Their rows are
  kept and brought forward to the current schema at startup, which is logged
  as `Upgraded N user record(s) to schema version 3`. Members are now keyed by
  Discord ID rather than by email address, and a GitHub account can only be
  linked to one Discord user at a time.
- **The un-star check now matches on the numeric GitHub account id** rather
  than the login. Anybody who renamed their GitHub account used to lose the
  role at the next check even though their star was still there. Rows written
  by much older versions have no id stored and still fall back to the login,
  so they keep that behaviour until the member verifies once more.

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
