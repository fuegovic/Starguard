# Troubleshooting

Every entry starts from the symptom you actually see, then the cause, then the
fix. Log lines are quoted as the code emits them, so you can search for them.

- [Reading the logs](#reading-the-logs)
- [A container exits immediately or restarts in a loop](#a-container-exits-immediately-or-restarts-in-a-loop)
- [The bot is online but has no slash commands](#the-bot-is-online-but-has-no-slash-commands)
- [The verification page shows an error](#the-verification-page-shows-an-error)
- [GitHub rate limiting](#github-rate-limiting)
- [The bot cannot assign or remove the role](#the-bot-cannot-assign-or-remove-the-role)
- [MongoDB connection and authentication failures](#mongodb-connection-and-authentication-failures)
- [A healthcheck reports unhealthy](#a-healthcheck-reports-unhealthy)
- [Verification succeeds but the star is not detected](#verification-succeeds-but-the-star-is-not-detected)
- [The custom links command does not appear](#the-custom-links-command-does-not-appear)

## Reading the logs

Both processes log to stderr, which Docker collects.

```sh
docker compose logs -n 100 discord-bot
docker compose logs -n 100 server
docker compose logs -n 100 mongodb
```

`docker compose logs -f` follows the output live. Use it interactively only:
it never ends on its own, so piping it into anything that waits for the end of
the stream will hang.

Two settings help when a report is hard to reproduce:

- `LOG_LEVEL=DEBUG` adds the Discord library's own traffic and the health
  endpoint's request lines.
- `LOG_FORMAT=json` emits one JSON object per line, with the star check
  summary as typed fields rather than text.

Every request the OAuth server handles carries a request ID, appended to each
text log line as `[request_id=...]` and returned to the client in the
`X-Request-ID` response header. If a member can tell you that header value,
you can find their exact request. An `X-Request-ID` supplied by your reverse
proxy is echoed instead, as long as it is at most 64 characters of letters,
digits, dot, underscore or hyphen.

## A container exits immediately or restarts in a loop

### With a configuration error

**Symptom.** `docker compose ps` shows the container restarting. The last log
line is:

```
2026-01-01 12:00:00 ERROR starguard.bot: Configuration error: <something>
```

**Cause.** Both processes validate the whole environment before doing anything
else and exit with status 1 on the first problem. `restart: always` then
starts them again, which is why you see a loop rather than a stopped
container.

**Fix.** The message names the variable. The common ones:

| Message | Fix |
| --- | --- |
| `Required environment variable TOKEN is not set.` | Set it in `.env`. The same message appears for any required variable. Check for a typo in the name, and remember that a value of only whitespace counts as unset. |
| `SECRET_KEY is still set to a placeholder value.` | Generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SECRET_KEY must be at least 16 characters, got 8.` | As above. |
| `ROLE_ID must be a Discord ID (a number), got '@Stargazer'.` | Enable Developer Mode in Discord and use **Copy ID**. Same for `GUILD_ID` and `CHANNEL_ID`. |
| `DOMAIN must be an absolute https URL such as https://starguard.example.com, got 'http://...'.` | Put a TLS-terminating proxy in front of the server and use the `https://` address. See [below](#the-flow-works-locally-but-not-through-the-proxy). |
| `DOMAIN must be a plain URL with no query string or fragment, got '...'.` | Strip everything from the `?` or `#` onwards. |
| `AUTOMATIC_CHECK must be a boolean such as true/false, got '...'.` | Use `true` or `false`. |
| `AUTOMATIC_CHECK_DELAY must be a whole number, got '1h'.` | Use seconds, as a plain number. |

If the container exits before printing anything at all, Compose could not
build the environment. Look for `set MONGO_INITDB_ROOT_USERNAME in .env` or a
similar message from Compose itself, which means a `${VAR:?...}` reference in
the compose file has no value.

Full reference: [env_file.md](./env_file.md).

### Without a configuration error

**Symptom.** Configuration passes, then the bot restarts anyway.

**Causes and fixes.**

- **The Discord token is wrong or was reset.** The traceback ends in
  `interactions.client.errors.LoginError: An improper token was passed`.
  Reset the token in the Developer Portal and update `TOKEN`.
- **The Server Members Intent is not enabled.** The bot connects with the
  guild members intent, Discord closes the gateway with code 4014, and the
  library raises `You have requested privileged intents that have not been
  enabled or approved. Check the developer dashboard`. Enable **Server
  Members Intent** under **Privileged Gateway Intents** on the **Bot** tab of
  your application.
- **The container ran out of memory.** Both services are capped at 256 MB in
  the compose files. `docker inspect <container> --format '{{.State.OOMKilled}}'`
  says whether that is what happened. Raise the limit under
  `deploy.resources.limits.memory` in a `docker-compose.override.yml`.

MongoDB being unreachable is **not** a reason for either process to exit. Both
start anyway and run degraded; see
[MongoDB connection and authentication failures](#mongodb-connection-and-authentication-failures).

## The bot is online but has no slash commands

**Symptom.** The bot shows as online in the member list, but `/verify` does
not appear when you type it.

**Causes and fixes.**

- **Commands are still syncing.** The bot synchronises its commands on
  startup. Give it a minute, then fully restart your Discord client, or press
  Ctrl+R in the desktop app.
- **The bot was invited without the `applications.commands` scope.** Re-invite
  it using an OAuth2 URL that includes both `bot` and
  `applications.commands`.
- **An integration restriction hides them.** Check **Server Settings**,
  **Integrations**, and the bot's command permissions there.

## The verification page shows an error

The OAuth server returns its result as a page with a **Problem:** label and an
HTTP status. Find the message the member saw.

### "This verification link is not valid." (HTTP 400)

**Cause.** The signed link token did not verify. Almost always this means the
bot and the server hold **different** `SECRET_KEY` values: the bot signs the
link, the server checks the signature, and they must match exactly.

The server logs `Rejected verification link: This verification link is not
valid.`

**Fix.** Both containers load the same `.env`, so a mismatch usually means one
of them is running with a stale environment. Confirm with:

```sh
docker compose exec discord-bot printenv SECRET_KEY
docker compose exec server printenv SECRET_KEY
```

If they differ, `docker compose up -d --force-recreate` picks up the current
`.env`. Changing `SECRET_KEY` invalidates outstanding links and signs existing
sessions out; nobody loses their role or their record.

Other causes of the same message: the URL was truncated when it was copied, or
someone edited the `token` query parameter.

### "This verification link has expired." (HTTP 400)

**Cause.** The link is older than `LINK_TOKEN_MAX_AGE`, 15 minutes by default.

**Fix.** Nothing is broken. Tell the member to press **Get a new link 🔄** on
the verification message in Discord, which issues a fresh one without making
them start over. Raise `LINK_TOKEN_MAX_AGE` if your members routinely need
longer, bearing in mind that the bot's own wording says fifteen minutes.

### "Your verification session has expired." (HTTP 400)

**Cause.** The member reached `/authorize` without a session cookie from
`/login`. The cookie is set with `Secure`, `HttpOnly` and `SameSite=Lax`.

This is the failure you get when the flow is served over plain HTTP: the
browser accepts the redirect to GitHub, GitHub sends the member back, and the
cookie was never stored because the connection was not HTTPS.

**Fix.**

- Serve the server over HTTPS, and set `DOMAIN` to the `https://` address.
- Check `TRUSTED_PROXY_COUNT`. If the server does not see
  `X-Forwarded-Proto: https`, it builds the OAuth `redirect_uri` as `http://`,
  GitHub either refuses it or returns the member to an HTTP URL, and the
  cookie is lost. See [below](#the-flow-works-locally-but-not-through-the-proxy).
- A member who took longer than the browser session allows, or who switched
  browsers between step 2 and the callback, will also see this. Ask them to
  start again.

### "GitHub sign-in failed: ..." (HTTP 400)

**Cause.** GitHub rejected the token exchange. The reason it gave is included
in the message, and the server logs
`OAuth error for Discord ID <id>: <reason>`.

**Fix.** Check `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` against the OAuth
app at <https://github.com/settings/developers>. A regenerated client secret
that was never copied into `.env` is the usual cause.

### GitHub itself shows "The redirect_uri MUST match the registered callback URL"

**Cause.** This page comes from GitHub, before the member ever gets back to
Starguard. The `redirect_uri` the server sent does not exactly match the
**Authorization callback URL** on the OAuth app.

The server builds that URI from the incoming request, not from `DOMAIN`, so
the scheme and host it sees have to be right.

**Fix.**

- The registered callback must be your public address with `/authorize`
  appended, for example `https://starguard.example.com/authorize`. It is
  compared exactly: scheme, host, port and path.
- Make sure your proxy forwards `X-Forwarded-Proto` and `X-Forwarded-Host`,
  and that `TRUSTED_PROXY_COUNT` matches the number of proxies you run.

### "The GitHub account ... is already linked to another Discord user." (HTTP 409)

**Cause.** Working as intended. One GitHub account can only hold one link, so
a single star cannot be redeemed by several Discord accounts.

**Fix.** If the member genuinely changed Discord accounts, delete their old
row from the `users` collection, keyed on the old `discord_id`, and have them
verify again.

### "Could not read your GitHub profile. Please try again." (HTTP 502)

**Cause.** GitHub accepted the sign-in but the follow-up API call failed or
returned something unexpected. The server logs
`Could not read the GitHub profile: <reason>`.

**Fix.** Usually transient. Check <https://www.githubstatus.com> and retry.

### "The database is unavailable right now." (HTTP 503)

**Cause.** The server could not build a database client at all, which in
practice means `MONGO_HOST` is malformed. It logs `Cannot record a link: no
database connection.`

A database that exists but is unreachable, or that refuses the credentials,
produces a different message: `Could not save your verification right now.
Please try again later.`, also with HTTP 503.

Both are covered under
[MongoDB connection and authentication failures](#mongodb-connection-and-authentication-failures).

### "Too many verification attempts from your address." (HTTP 429)

**Cause.** The per-address rate limit on `/login` and `/authorize`, 10
requests per 60 seconds by default. The response carries a `Retry-After`
header and the server logs `Rate limited login for <address>`.

**Fix.** If genuine members are hitting this, look at the address in the log
line first. If it is your reverse proxy's own address rather than a real
client address, `TRUSTED_PROXY_COUNT` is too low and every visitor is sharing
one bucket. Fix that before raising `LOGIN_RATE_LIMIT`.

### The flow works locally but not through the proxy

`TRUSTED_PROXY_COUNT` is the value to check. It tells the server how many
reverse proxies **you operate** sit in front of it, and the server reads the
client address, scheme and host from that many hops back in the
`X-Forwarded-*` headers.

- **Too low.** The server sees your proxy instead of the visitor. Rate
  limiting collapses onto one bucket, and if the proxy chain does not deliver
  `X-Forwarded-Proto: https` the OAuth redirect is built as `http://`.
- **Too high.** The server reads past your own proxies into header values the
  client supplied, so a client can present any address it likes. This defeats
  the rate limit and puts an attacker-chosen value in your logs.

Count the hops you control, starting at the server: the bundled Nginx Proxy
Manager alone is `1`; a CDN in front of your own proxy is `2`. Then make one
request through your real front door and check the address the server logged
for it.

## GitHub rate limiting

**Symptom.** `/starcount` or `/checkstars` answers
`Could not reach GitHub right now: GitHub API rate limit exceeded. Set
GITHUB_TOKEN to raise the limit, or increase AUTOMATIC_CHECK_DELAY.`

Or the automatic check logs:

```
Automatic star check failed (1 in a row); retrying in 63 seconds
```

**Cause.** Without `GITHUB_TOKEN`, GitHub allows 60 REST requests per hour
from your server's address. The stargazer listing is fetched 100 entries per
request, so one pass over a repository with a few thousand stars uses a large
share of that. With a token the limit is 5000 per hour.

**Fix.**

- Set `GITHUB_TOKEN`. No scopes are needed for a public repository.
- Raise `AUTOMATIC_CHECK_DELAY`.
- Watch the per-cycle summary line to see what each pass actually costs:

  ```
  Star check complete: examined=42 roles_removed=1 api_calls=3 pages_fetched=1 pages_unchanged=2 rate_limit_remaining=4987 duration_seconds=1.8
  ```

  `pages_unchanged` counts pages served from the ETag cache as HTTP 304,
  which do not count against the rate limit at all. On a repository whose
  early pages rarely change, most of a cycle should be unchanged pages.

Related messages, all from the same code path:

| Message | Meaning |
| --- | --- |
| `GitHub rejected GITHUB_TOKEN (401). Check that it is valid.` | The token is wrong, revoked or expired. |
| `Repository not found (404). Check REPO_OWNER and GITHUB_REPO, and note that a private repository needs a GITHUB_TOKEN that can read it.` | Usually `GITHUB_REPO` was set to `owner/repo` instead of just the repository name. |
| `GitHub asked for a 300s wait before retrying (HTTP 429). Increase AUTOMATIC_CHECK_DELAY or set GITHUB_TOKEN.` | GitHub's secondary rate limit, with a `Retry-After` longer than the bot is willing to hold a worker thread for. |
| `Could not reach the GitHub API: <reason>` | A transport failure that survived four attempts. Check egress from the container. |

Retries are automatic, with exponential backoff and jitter, for HTTP 429, 500,
502, 503 and 504 and for transport errors. A 403 with no rate limit remaining
is not retried, because it does not clear for up to an hour.

## The bot cannot assign or remove the role

**Symptom.** The member completes verification, presses **3: Claim your role**
and gets `I could not assign the role. Please ask a moderator to check my
permissions and role position.` The bot logs:

```
Could not add the role to 123456789012345678: 403|Forbidden: Missing Permissions
```

The same applies in reverse during the star check, with
`Could not remove the role from ...`.

**Causes and fixes, in the order worth checking.**

1. **Role hierarchy.** A bot can only manage roles positioned **below** its
   own highest role. This is the most common cause by far, and it is silent
   until the moment the role is granted. In **Server Settings**, **Roles**,
   drag the bot's own role above the role in `ROLE_ID`.
2. **Missing Manage Roles permission.** The bot's role needs **Manage Roles**.
   Re-invite it with that permission, or grant it to the bot's role directly.
3. **`ROLE_ID` points at the wrong role.** Numeric but wrong values pass
   validation. Check the ID against the role in **Copy ID**.
4. **A managed role.** Roles owned by another integration, and the Nitro
   Booster role, cannot be assigned by a bot at all. Create a plain role.
5. **The member left the guild** between the lookup and the write, which
   produces a `NotFound` in the same log line. Nothing to fix.

Nothing here stops the bot: a failed role change is logged and reported, and
the rest of the cycle continues.

## MongoDB connection and authentication failures

**Symptom.** At startup, either process logs one of two different lines, and
which one you get decides what else you will see.

```
Could not prepare the users collection: <reason>
```

This is the usual one. The connection string parsed, but the first query
failed: the host is unreachable, or authentication was refused. Startup
continues deliberately, because a database that is briefly away should not
stop the process. **The server's `/healthz` still answers 200 in this state**,
so do not take a healthy server as proof the database is reachable. Each
individual operation fails instead, at the moment it is attempted:

- A member finishing verification gets
  `Could not save your verification right now. Please try again later.` with
  HTTP 503.
- A member pressing **Claim your role** gets
  `Could not check your verification, please try again later.`
- The automatic star check logs
  `Automatic star check failed (1 in a row); retrying in 63 seconds`, then
  doubles the wait on each consecutive failure up to about 30 minutes, with a
  quarter of jitter either way.
- `/checkstars` answers `Could not reach the database right now.`

```
Error connecting to MongoDB: <reason>
```

This one is narrower: the driver refused the **connection string itself**, so
no client was built at all. Here the server does report the failure, answering
`/healthz` with 503 and `{"database":"unavailable","status":"degraded"}`,
showing visitors `The database is unavailable right now. Please try again
later.`, and the bot logs `Skipping star check: no database connection.` every
cycle. The usual cause is an unescaped character in the password. See
[a malformed connection string](#a-malformed-connection-string) below.

Neither process exits over either of these, and neither picks up a database
that appears later: a process that started without a working connection needs
a restart once the database is back.

**Read the reason at the end of the line.**

### `Authentication failed`

**Cause.** The credentials in `MONGO_HOST` do not work.

**Fix, in order:**

1. Check the auth source. The driver authenticates against the database
   named in the connection string's path, or `admin` when the path is empty.
   The bundled image creates its root user in `admin`, so
   `mongodb://user:password@mongodb:27017/starguard` fails while
   `mongodb://user:password@mongodb:27017/?authSource=admin` works. Use the
   second form.
2. The username and password in `MONGO_HOST` must match
   `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD` exactly.
3. If the password contains any of `: / ? # [ ] @`, percent-encode it in the
   connection string, or change it to one without them.
4. **If you just upgraded from a version whose database ran without
   authentication, the root user was never created.** The official image only
   creates it when the data directory is empty, and yours is not. Do not
   delete the data directory. Follow
   [the upgrade procedure](./installation.md#3-the-bundled-mongodb-now-requires-authentication),
   which creates the user in place through MongoDB's localhost exception.

You can check whether the user exists at all:

```sh
docker compose exec mongodb mongosh --quiet \
  -u starguard -p 'the password from .env' \
  --authenticationDatabase admin --eval "db.adminCommand('ping')"
```

`{ ok: 1 }` means the credentials are fine and the problem is in
`MONGO_HOST`. An authentication error means the user is missing or the
password differs.

### `Name or service not known` / `connection refused`

**Cause.** The host in `MONGO_HOST` is not resolvable or nothing is listening.

**Fix.** With the bundled database the host must be `mongodb`, the compose
service name, not `localhost`: from inside the bot's container, `localhost` is
the bot's container. Confirm the service is actually running with
`docker compose ps mongodb`. If you use your own MongoDB on the Docker host,
`127.0.0.1` will not reach it either; use the host's LAN address or
`host.docker.internal` where your Docker supports it.

### `not authorized on starguard to execute command`

**Cause.** The connection and the credentials are fine, but the user does not
have the rights the startup work needs: creating the indexes, purging legacy
tokens and bringing documents forward to the current schema. It appears as the
reason on a `Could not prepare the users collection:` line, and startup
continues without them.

**Fix.** The root user created by the bundled image already has these rights.
With your own MongoDB, grant the user `readWrite` on `MONGO_DATABASE`, which
covers creating indexes on the `users` collection.

### A malformed connection string

**Symptom.** Either `Error connecting to MongoDB: Username and password must
be escaped according to RFC 3986, use urllib.parse.quote_plus`, or the process
dies outright with a traceback ending in:

```
ValueError: Port contains non-digit characters. Hint: username and password must be escaped according to RFC 3986, use urllib.parse.quote_plus
```

**Cause.** A character in the username or password that has a meaning in a
URI. An `@` makes the driver read the rest of the password as the host; a `:`
makes it read the rest as the port, which is the case that ends in the
traceback rather than a handled error.

**Fix.** Percent-encode the credentials in `MONGO_HOST`, or change the
password to one without `: / ? # [ ] @`. To encode an existing one:

```sh
python -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" 'your password'
```

### The mongodb container is `unhealthy`

Its healthcheck runs `mongosh` with the root credentials. It fails for exactly
the reasons above, most often the missing root user after an upgrade. Nothing
in the compose files waits for it to be healthy, so the bot and the server
start regardless and log their own connection errors.

## A healthcheck reports unhealthy

### discord-bot

The bot serves `GET /healthz` on `BOT_HEALTH_HOST:BOT_HEALTH_PORT`, by default
`127.0.0.1:8080`, and both compose files probe it. Read the body directly:

```sh
docker compose exec discord-bot python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read())"
```

| Body | Status | Meaning |
| --- | --- | --- |
| `{"status": "starting", ..., "gateway": "connecting"}` | 503 | Not connected to the Discord gateway yet. Normal for the first few seconds; the healthcheck allows 60. If it persists, see [restarts in a loop](#without-a-configuration-error). |
| `{"status": "ok", ..., "star_check": "disabled"}` | 200 | Healthy, with `AUTOMATIC_CHECK=false`. |
| `{"status": "ok", ..., "star_check": "pending"}` | 200 | Healthy, first automatic check has not finished yet. |
| `{"status": "degraded", ..., "star_check": "stale"}` | 503 | Automatic checks are on, but the last completed one is older than `AUTOMATIC_CHECK_DELAY * 3 + 300` seconds. Look for `Automatic star check failed` in the log: usually GitHub is rate limiting or the database is unreachable. |

**The probe fails with a connection error instead of a status.** Two causes:

- **`BOT_HEALTH_ENABLED=false`.** Nothing is listening, so the healthcheck can
  never pass and the container stays `unhealthy` forever. If you want the
  endpoint off, also disable the probe, for example in
  `docker-compose.override.yml`:

  ```yaml
  services:
    discord-bot:
      healthcheck:
        disable: true
  ```

- **The port could not be bound.** The bot logs
  `Could not start the health endpoint on 127.0.0.1:8080: <reason>` and keeps
  working without it. Change `BOT_HEALTH_PORT`; both the endpoint and the
  compose probe read it, so they move together.

A healthy start logs `Health endpoint listening on http://127.0.0.1:8080/healthz`.

### server

The server's `/healthz` returns `{"status":"ok"}` with 200, or
`{"database":"unavailable","status":"degraded"}` with 503. The 503 case is
covered under
[MongoDB connection and authentication failures](#mongodb-connection-and-authentication-failures).

**A 200 here does not mean the database is reachable.** It only means the
server holds a database client. If members are failing at the last step,
check the server's log for `Could not prepare the users collection:` rather
than trusting this probe.

If the probe cannot connect at all, the process is not listening: check
`docker compose logs server` for a configuration error, and check that
`SERVER_BIND_PORT` matches what the probe uses. Both compose files interpolate
the same variable into the mapping and the probe, so they only diverge if you
override one of them by hand.

## Verification succeeds but the star is not detected

**Symptom.** The member signs in and the page says
`Authentication successful, but you have not starred <owner>/<repo> yet.`,
even though they say they starred it.

**Causes and fixes.**

- **They starred a different repository.** Check `REPO_OWNER` and
  `GITHUB_REPO`. The **1: Star this repo 🌟** button in `/verify` goes to
  exactly the repository Starguard checks, so ask them to use it.
- **They signed in to GitHub as a different account** from the one that
  starred. GitHub's answer is about the authenticated user only.
- **They starred after signing in.** The check happens during the callback,
  once. They should star first, then press **Get a new link 🔄** and sign in
  again, or wait for the next automatic check to pick it up.

The reverse case, a member who un-starred but keeps the role, resolves at the
next automatic check, or immediately with `/checkstars`.

## The custom links command does not appear

**Cause.** The command is registered only when `COMMAND_NAME` is set **and**
at least one button has **both** a label (`BTN1`) and a URL (`URL1`). A
half-configured pair is skipped rather than registered as a button Discord
would reject.

**Fix.** Set at least one complete pair. Note that `/help` lists the command
whenever `COMMAND_NAME` is set, even if no complete pair exists, so `/help`
can advertise a command that is not actually registered.

## Still stuck

Open an issue at <https://github.com/fuegovic/Starguard/issues> with the
relevant log lines, your Docker Compose version, and which compose file you
used. Redact `TOKEN`, `SECRET_KEY`, `GITHUB_CLIENT_SECRET`, `GITHUB_TOKEN` and
any database password before pasting anything.

For a suspected security problem, do not open an issue. Follow
[SECURITY.md](../SECURITY.md).
