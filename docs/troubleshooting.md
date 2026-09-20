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
- [Everybody who verified before is suddenly "not linked"](#everybody-who-verified-before-is-suddenly-not-linked)
- [The star webhook is not working](#the-star-webhook-is-not-working)
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
| `BOT_HEALTH_PORT must be a TCP port between 1 and 65535, got 70000.` | A mistyped port. Same message for `SERVER_BIND_PORT`. Ports are refused rather than clamped, because a clamped port is a different address. |
| `MONGO_HOST is not a usable MongoDB connection string: <reason>` | The driver could not parse the value. Usually an unencoded character in the password or a mistyped port; see [a malformed connection string](#a-malformed-connection-string). **From the server only.** The bot does not check this value while loading its configuration, so the same `MONGO_HOST` fails it later and less tidily. |

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
- **`MONGO_HOST` carries a port the driver cannot read**, and this one hits
  the bot only. The traceback ends in `ValueError: Port contains non-digit
  characters`. The bot reads `MONGO_HOST` as a plain string, so an
  unparseable one is not reported as a configuration error the way the
  server reports it; see [a malformed connection
  string](#a-malformed-connection-string).

MongoDB being *unreachable* is **not** a reason for either process to exit.
Both start anyway and run degraded; see
[MongoDB connection and authentication failures](#mongodb-connection-and-authentication-failures).
A `MONGO_HOST` the driver cannot parse at all is the exception, and it is the
case above.

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

Both print a live credential, so if you are pasting the result anywhere,
compare fingerprints instead: append `| sha256sum` to each, inside
`sh -c '...'`.

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

Two different calls produce this. The profile fetch failing gives a reason
naming the missing field. The star check failing gives

```
Could not read the GitHub profile: the starred check answered 500
```

GitHub answers that check with 204 for starred and 404 for not starred, and
nothing else, so any other status means it did not answer the question. The
server refuses to guess and records nothing, which is the point: reading an
outage as "not starred" used to overwrite a member's true star state and send
them off to star a repository they had already starred.

**Fix.** Usually transient. Check <https://www.githubstatus.com> and retry.
Nothing was written, so the member simply signs in again.

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

**Cause.** The per-address rate limit, 10 requests per 60 seconds by default.
`/login` and `/authorize` are counted in **separate buckets**, so an ordinary
verification, which is one request to each, spends one of the ten on each
route rather than two on a shared count. The response carries a `Retry-After`
header and the server logs the route that refused it:
`Rate limited login for <address>` or `Rate limited authorize for <address>`.

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
Manager alone is `1`; a CDN in front of your own proxy is `2`. Nothing in
front at all is `0`, which installs no `ProxyFix` and believes no forwarded
header. With the compose files as shipped that last case does not arise: the
server's port is published as `127.0.0.1:${SERVER_PORT}:${SERVER_BIND_PORT}`,
so only something on the same host can reach it and a proxy is always in the
path.

There is no log line for an ordinary request, so a single visit tells you
nothing about which value is right. The procedure that does show you the
resolved address is
[under `TRUSTED_PROXY_COUNT` in the environment reference](./env_file.md#trusted_proxy_count).

**A connection refused from another machine is this, not a bug.** Because the
port is on loopback, a proxy running elsewhere cannot reach the container at
all. Either move the proxy onto the host, or drop the `127.0.0.1:` from the
mapping and set `TRUSTED_PROXY_COUNT` to match.

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

**Symptom.** At startup, either process logs one of two different kinds of
line, and which one you get decides what else you will see.

```
Could not index the users collection: <reason>
Could not index the deliveries collection: <reason>
Could not purge credentials written by older versions: <reason>
Could not upgrade user records: <reason>
```

This is the usual one. The connection string parsed, but the queries failed:
the host is unreachable, or authentication was refused. Each piece of startup
work is attempted separately and reports itself by name, so a database that is
away gives you all four lines while a permission that is missing for only one
of them gives you only that one. Startup continues deliberately, because a
database that is briefly away should not stop the process.

The server's `/healthz` **does** report this: it sends a `ping` to MongoDB on
every probe, so it answers 503 and
`{"database":"unavailable","status":"degraded"}` for as long as the database
is unreachable. Each individual operation also fails, at the moment it is
attempted:

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
no client was built at all. In practice it comes from the bot, which then
logs `Skipping star check: no database connection.` every cycle and grants no
roles for as long as it runs. The server checks `MONGO_HOST` with the driver
while loading its configuration, so a value this would reject stops it at
startup with `Configuration error: MONGO_HOST is not a usable MongoDB
connection string` instead. The usual cause is an unescaped character in the
password. See [a malformed connection string](#a-malformed-connection-string)
below.

Neither process exits over either of these, but they recover differently and
the difference decides whether you have to restart anything.

After the **first** kind, a client exists and the driver reconnects on its
own, so ordinary work starts succeeding again as soon as MongoDB does:
verification saves, the claim button reads, the sweep runs. What does not come
back on its own is the startup work itself. The indexes, the legacy-secret
purge and the schema upgrade are attempted once, at startup, so whichever of
them was refused stays undone until you restart the process.

After the **second** kind there is no client at all, and nothing creates one
later. That process is degraded for its whole life and a restart is the only
fix.

A third outcome is neither of these and prints no such line, because the
process does not get far enough to log one: a `MONGO_HOST` whose port the
driver cannot read kills the bot with a traceback. See [a malformed
connection string](#a-malformed-connection-string).

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
3. Percent-encode the reserved characters in the password: `@ : / ? % +`.
   (`# [ ] ! $` need no encoding.) Encode them in `MONGO_HOST` and **only**
   there. `MONGO_INITDB_ROOT_PASSWORD` is the literal password
   MongoDB was created with, and the healthcheck and Mongo Express use it
   literally too, so a database that reports healthy while the bot and the
   server cannot sign in is the signature of encoding it in both places.
   Watch for `+` in particular: it does not break the connection string, it
   is silently decoded to a space, so the password that reaches MongoDB is
   simply the wrong one. If the `mongodb` container is reporting healthy
   while only the bot and the server fail, read [that
   entry](#the-database-is-healthy-and-only-the-bot-and-the-server-cannot-sign-in)
   first.
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
have the rights the startup work needs: creating the indexes on `users` and on
`webhook_deliveries`, purging legacy tokens, and bringing documents forward to
the current schema. It appears as the reason on whichever of the four
`Could not ...` lines above names the step that was refused, and startup
continues without it.

Read which line you got, because they are not equally harmless. A refused
index leaves the uniqueness constraints off. A refused purge leaves the
cleartext OAuth tokens written by older versions **still in the database**,
which is the one worth acting on today; see
[the upgrade guide](./installation.md#1-revoke-the-oauth-tokens-the-old-version-stored).

**Fix.** The root user created by the bundled image already has these rights.
With your own MongoDB, grant the user `readWrite` on `MONGO_DATABASE`, which
covers all four steps, then restart the process so they run again.

### A malformed connection string

**Symptom.** One of three, and which one tells you which container to look
at. The **server** refuses to start and names the variable:

```
Configuration error: MONGO_HOST is not a usable MongoDB connection string: <reason>
```

The **bot** either logs an `Error connecting to MongoDB` line, usually
`Username and password must be escaped according to RFC 3986, use
urllib.parse.quote_plus` and sometimes `Bad database name "..."`, and carries
on with no database, or dies outright with a traceback ending in:

```
ValueError: Port contains non-digit characters. Hint: username and password must be escaped according to RFC 3986, use urllib.parse.quote_plus
```

**Cause.** A character in the username or password that has a meaning in a
URI, or a port that is not one.

The server parses `MONGO_HOST` with the driver as it loads its configuration,
so every one of these reaches it as an ordinary named configuration error.
The bot does not, so it is the container that shows the two messier
outcomes, and which of them depends on how the driver fails:

- `@`, `:`, `/` or `%` is reported as a MongoDB error. The bot catches it,
  logs the `Error connecting to MongoDB` line, and runs on granting no roles.
- **A non-numeric port is not caught**, and it is the case that ends in the
  traceback. A mistyped port, `mongodb://mongodb:70000/` or
  `mongodb://mongodb:notaport/`, reaches it directly. An unencoded `?` in
  the password reaches it the long way round, and in the connection string
  this project ships it reaches it every time: everything after the `?` is
  read as the query part, which leaves the rest of the password sitting
  where the port belongs. Either way the `ValueError` that raises is not a
  MongoDB error, so nothing catches it. The container exits, `restart:
  always` brings it back, and it exits again.

There is a third character with no symptom at all here: a `+` neither breaks
the string nor stops the process, it is silently decoded to a space, so the
password reaching MongoDB is not the one you set. That one has its own entry,
[the database is healthy and only the bot and the server cannot sign
in](#the-database-is-healthy-and-only-the-bot-and-the-server-cannot-sign-in).

**Fix.** Percent-encode the credentials in `MONGO_HOST`, and only there:
`MONGO_INITDB_ROOT_PASSWORD`, the healthcheck and Mongo Express all take the
literal password. To encode an existing one:

```sh
python -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" 'your password'
```

### The database is healthy and only the bot and the server cannot sign in

**Symptom.** The `mongodb` container reports `healthy`, `mongosh` with the
credentials from `.env` works, Mongo Express signs in, and yet both Python
processes log `Authentication failed`. No parse error, no traceback: the
connection string is perfectly valid, it just carries a password that is not
the one you set.

**Cause.** A `+` in the password. PyMongo decodes the credential part of the
URI, and in that decoding a literal `+` becomes a space. Nothing else in the
stack does that: `MONGO_INITDB_ROOT_PASSWORD` reaches MongoDB, the compose
healthcheck and Mongo Express as a plain string, so all three agree with each
other and only the two processes that parse a URI disagree. That is why this
one looks like a database that is fine and an application that is broken.

Ask the driver what it will actually send:

```sh
python -c "import sys; from pymongo.uri_parser import parse_uri; print(repr(parse_uri(sys.argv[1])['password']))" \
  'mongodb://user:pa+ss@mongodb:27017/?authSource=admin'
```

That prints `'pa ss'` where you wrote `pa+ss`. With the `+` encoded as `%2B`
it prints `'pa+ss'`, which is the password MongoDB is holding.

**Fix.** Percent-encode the `+` as `%2B` in `MONGO_HOST`, and leave
`MONGO_INITDB_ROOT_PASSWORD` as the literal password. Encoding both is how
you get here in the first place.

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
  "import urllib.error, urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8080/healthz')
except urllib.error.HTTPError as e:
    r = e
print(r.status, r.read().decode())"
```

The two states worth diagnosing, `starting` and `stale`, are both answered
with 503, and `urlopen` raises `HTTPError` on any 4xx or 5xx. Catching it is
what makes those two print their body instead of a traceback; a bare
`urlopen(...).read()` shows you nothing for exactly the cases you are looking
at.

The body names two loops, `star_check` for the periodic star sweep and
`role_sync` for the role sync drain, and each is `disabled`, `pending`, `ok`
or `stale`. Read the one that is not `ok`:

| Body | Status | Meaning |
| --- | --- | --- |
| `{"status": "starting", ..., "gateway": "connecting"}` | 503 | Not connected to the Discord gateway yet, and neither loop field is present. Normal for the first few seconds; the healthcheck allows 60. If it persists, see [restarts in a loop](#without-a-configuration-error). |
| `..., "star_check": "disabled"` or `"role_sync": "disabled"` | 200 | Healthy. That loop is off, through `AUTOMATIC_CHECK=false` or `ROLE_SYNC_ENABLED=false`. |
| `..., "star_check": "pending"` or `"role_sync": "pending"` | 200 | Healthy. That loop is on and its first pass has not finished yet, so it carries no age field. |
| `{"status": "degraded", ..., "star_check": "stale"}` | 503 | Automatic checks are on, but no pass has completed within `AUTOMATIC_CHECK_DELAY * 3 + 300` seconds, counting from startup if none ever has. Look for `Automatic star check failed` in the log: usually GitHub is rate limiting or the database is unreachable. |
| `{"status": "degraded", ..., "role_sync": "stale"}` | 503 | The drain is on, but no pass has completed within `ROLE_SYNC_INTERVAL * 3 + 300` seconds, counting from startup if none ever has. Almost always the database: a pass that gives up before walking the queue does not count as completed. Which log line you have decides whether it clears by itself; see [MongoDB connection and authentication failures](#mongodb-connection-and-authentication-failures). `Skipping the role sync drain: no database connection.` every pass means the bot started without a usable `MONGO_HOST` and will never recover, so restart it once the value is right. `Role sync drain failed (N in a row)` means a client exists and the driver keeps trying, so this clears on its own when MongoDB answers again. |

A `degraded` body still reports the healthy loop beside the stale one, and
`last_check_age_seconds` and `last_role_sync_age_seconds` appear only for a
loop that has completed at least one pass. Their absence is not a fault.

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

**The probe reaches the database.** It sends a `ping` command on every
request, so it answers 503 whenever the database is unreachable or refusing
the credentials, and it logs `Health probe could not reach MongoDB:
<reason>`. A 200 therefore does mean the database answered a moment ago. A
`MONGO_HOST` the driver cannot use at all does not show up here, because the
server now rejects that while loading its configuration and never starts. What it does not
prove is that the startup work succeeded: a user with no right to create an
index answers a `ping` perfectly well, so if members are failing at the last
step with a healthy probe, read the four `Could not ...` lines in the server's
log.

The consequence worth expecting: **a MongoDB outage now shows up as a
`server` container marked `unhealthy`**, because the compose healthcheck
calls this endpoint. Docker does not restart a container for being unhealthy,
so the server keeps serving and keeps answering 503 here; the mark is a
signal for you or your orchestrator, not an action. It clears on its own once
the database answers again, without restarting the server, because only the
startup work needs a restart and the probe does not.

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

- **The repository is private.** Then it fails for everybody, every time, and
  no amount of starring helps. The check is made with the member's own OAuth
  token, which carries only the `read:user` scope and therefore cannot see a
  private repository; GitHub answers 404 and 404 means "not starred". A
  `repo`-scoped `GITHUB_TOKEN` does not change this, because that token
  belongs to the bot's sweep and is not used here. See
  [Step 5 of the installation guide](./installation.md#step-5-create-a-github-personal-access-token).
- **They starred a different repository.** Check `REPO_OWNER` and
  `GITHUB_REPO`. The **1: Star this repo 🌟** button in `/verify` goes to
  exactly the repository Starguard checks, so ask them to use it.
- **They signed in to GitHub as a different account** from the one that
  starred. GitHub's answer is about the authenticated user only.
- **They starred after signing in.** The check happens during the callback,
  once, and **3: Claim your role** reads that recorded answer rather than
  asking GitHub again. They should star first, then press **Get a new link
  🔄** and sign in again. Running `/verify` afresh does the same thing, and
  is what the result page tells them to do.

  With the star webhook configured they do not have to do any of that: the
  event arrives on its own and the role follows within `ROLE_SYNC_INTERVAL`
  seconds. Without it, waiting does not help. The periodic check only ever
  **removes** the role, so it will never notice a star that appeared after
  verification.

The reverse case, a member who un-starred but keeps the role, resolves at the
next automatic check, immediately with `/checkstars`, or within seconds if
the star webhook is configured.

## Everybody who verified before is suddenly "not linked"

**Symptom.** Members who already hold the role press **3: Claim your role
❤️‍🔥** and get the reply that is meant for somebody who never linked at all:

```
Please make sure to link your GitHub account by using the Log in with GitHub button. If your link has expired, press Get a new link 🔄 below for a new one.
```

Nothing changed in Discord and nothing is wrong with the database.

Put another way: members who could claim before now cannot, nothing in Discord
changed, and neither log says why. The bot logs nothing at all for this, so
there is no line to search for.

**Cause.** `REPO_OWNER` or `GITHUB_REPO` was changed while the same database
was kept, or the repository was renamed or transferred on GitHub. Each row
records the repository it was made for, and the claim button compares it
against the repository currently configured. Existing rows name the old one,
and a star on the old repository is no evidence at all about the new one, so
the claim is refused rather than redeemed.

**This is working as intended, and it is a security fix rather than a
limitation.** Without the comparison, `starred_repo: true` proved only that
somebody had starred *some* repository at some point, so repointing the bot
silently handed the new role to everyone who had starred the old one, none of
whom had starred the new one, with nothing in the logs to show it.

**Fix.** Nothing is broken and nothing needs repairing in the database. Each
member runs `/verify` and signs in with GitHub once more, which rewrites their
row against the new repository; tell them in advance if you are planning the
change, because the button otherwise looks broken. Then run `/checkstars`, so
that roles still held for the old repository are reconciled against the new
stargazer listing. If you changed the values by mistake, put them back and the
existing rows work again immediately: the refusal rewrites nothing.

The upgrade guide covers the same ground from the operator's side, in
[section 5](./installation.md#5-if-you-ever-change-which-repository-the-bot-watches).

## The star webhook is not working

The optional star webhook is set up in
[Step 12 of the installation guide](./installation.md#step-12-set-up-the-star-webhook-optional).
Everything below assumes you have been through it.

**Start at GitHub's delivery log, not at your own.** Open the repository,
then **Settings**, **Webhooks**, click the hook, and open **Recent
Deliveries**. Every delivery is there with the status the server answered,
the exact bytes GitHub sent, and a **Redeliver** button that repeats it with
the same payload. That is the fastest loop you have for this, and for most of
these failures it is the only place the evidence exists at all.

The receiver deliberately says very little in your own log. Its route is not
rate limited, because every real delivery comes from a handful of GitHub
addresses and putting them all in one bucket would drop exactly the burst of
stars you care about. A route that is not rate limited must not write a log
line per request, or anyone on the internet could fill your disk, so a
**rejected signature is logged at `DEBUG` on purpose**, not at warning. It is
not a missing log line; it is a deliberate one.

Find the status code in the delivery log and read the matching entry below.

### Deliveries show 401 with `Invalid signature.`

**Cause.** Almost always, the secret GitHub signs with is not the secret the
server verifies with. Every delivery is authenticated by its HMAC and by
nothing else, so a signature that does not verify is the only thing that
produces this.

The other way to get here is a proxy that strips the `X-Hub-Signature-256`
header, since a missing signature and a wrong one are answered the same way.
That is rare, and worth suspecting only once the secret has been ruled out.

**Fix.**

1. Check the server is running with the value you think it is. `.env` is read
   at startup, so a secret edited afterwards has not reached the process:

   ```sh
   docker compose exec server printenv GITHUB_WEBHOOK_SECRET
   ```

   That prints a live credential to your terminal. Keep it out of anything
   you share: it is the only thing authenticating a star event, so whoever
   has it can forge one. If all you need to know is whether the running
   process matches your `.env`, compare fingerprints instead of values, and
   paste those safely:

   ```sh
   docker compose exec server sh -c 'printenv GITHUB_WEBHOOK_SECRET | sha256sum'
   grep '^GITHUB_WEBHOOK_SECRET=' .env | cut -d= -f2- | sha256sum
   ```

   The two hashes must be identical. If they differ, the container is running
   an older value and `docker compose up -d server` is the fix.

2. Compare it with the hook's **Secret** field on GitHub. GitHub never shows
   you the stored value, so you cannot read it back to compare: paste the
   value from step 1 in again, exactly, and press **Update webhook**. Watch
   for a trailing space or newline picked up when it was copied.
3. Press **Redeliver** on the failed delivery. It should turn green without
   anybody having to star anything.

If you changed `.env` but not the container, `docker compose up -d server`
picks up the new value.

To see the rejections in your own log while you work on it, set
`LOG_LEVEL=DEBUG` and restart the server. Each one then logs
`Rejected a webhook delivery with a bad or missing signature.` Turn it back
down afterwards.

### Deliveries show 404

There are two different 404s here, and the response body tells them apart.

**With `Hook is configured for another repository.`** The signature was
valid, so this is your hook, but the `repository.full_name` in the payload is
not the repository the server is configured for. The server logs it at
`ERROR`:

```
Refused a star event for 'someone/other-repo', which is not this repository.
```

*Cause.* Either the hook is on the wrong repository, or `REPO_OWNER` and
`GITHUB_REPO` do not name the one the hook is on. The second is easy to do
when the repository has been renamed or transferred since you set it up.

*Fix.* Compare the two. `REPO_OWNER` is the owner on its own and
`GITHUB_REPO` is the repository name on its own, never `owner/repo`:

```sh
docker compose exec server printenv REPO_OWNER GITHUB_REPO
```

Capitalisation does not matter here. The comparison is case-insensitive on
both sides, so `Owner/Repo` in the environment matches `owner/repo` in the
payload. Restart the server after correcting either value, then
**Redeliver**.

**With an HTML "Not Found" page instead of that one line.** Nothing matched
the path, so the answer came from the framework's default handler or from
your proxy rather than from the receiver. The route does not exist.

*Cause.* `GITHUB_WEBHOOK_SECRET` is not set in the process. The receiver is
registered only when a secret is configured, so without one there is no
`/webhooks/github` to reach: not a stub that answers an error, genuinely no
such path. This is also what you get if the secret is set in `.env` but the
server has not been restarted since.

*Fix.*

```sh
docker compose exec server printenv GITHUB_WEBHOOK_SECRET
docker compose up -d server
```

If the variable prints nothing, set it in `.env` first. If the server refuses
to start, the log names the reason: the secret is held to the same rules as
`SECRET_KEY`, so a placeholder or anything under 16 characters is rejected.

A wrong path in the **Payload URL** looks identical from GitHub's side. It
must end in exactly `/webhooks/github`.

### Deliveries show 503 with `Database unavailable.`

**Cause.** The signature and the repository were both fine and the event was
simply not recorded, because the server cannot reach MongoDB. Two log lines
produce this, depending on whether the connection string was usable at
startup:

```
Cannot record a star event: no database connection.
Could not record a star event: <reason>
```

**Fix.** This is not a webhook problem. Work through
[MongoDB connection and authentication failures](#mongodb-connection-and-authentication-failures),
then come back.

**Then redeliver, by hand.** This is the part that catches people out.
[GitHub does not automatically retry a failed
delivery](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries),
so once the database is back, nothing replays the ones that failed while it
was away. Press **Redeliver** on each red delivery in the log.

It is worth doing rather than skipping, because the sweep only covers half of
it. The sweep takes the role from anyone no longer in the stargazer listing,
so it does repair a missed **un-star** at its next pass. It never grants a
role, so a missed **star** leaves that member without one until the delivery
is replayed, or until they go back through **Get a new link 🔄** and sign in
again.

**Redeliver as soon as the database is back; you do not have to wait.** The
receiver claims a delivery id before it acts on it, and drops a second
delivery carrying an id it has already claimed. A delivery that failed here
never got that far: the write was refused, so the claim is released again on
the way out, and the redelivery is treated as new work however quickly you
press the button. Ten minutes was the wait before that release existed, and
it is still the window for a delivery that **was** processed, which is what
drops GitHub's own immediate duplicates.

The one case where a redelivery changes nothing is a stale one: if a newer
star event for the same member has arrived in the meantime, the receiver
answers 202 with `Superseded by a newer event.` and leaves the newer state
alone. That is the correct outcome, not a failure. Redelivering an old
`deleted` after the member has starred again cannot take their role away.

### Deliveries are green but roles do not move

**Symptom.** The delivery log shows 202 and `Recorded.`, and nothing happens
in Discord.

**Cause.** The delivery reached the **server**, and the server cannot touch
Discord. Only the **bot** can. The two processes never talk to each other:
the server writes the new star state to the database and marks the row
pending, and the bot polls for those rows every `ROLE_SYNC_INTERVAL` seconds
and moves the role. A green delivery proves the first half only.

One thing to rule out first: `Recorded.` means the delivery was written, not
that anything was queued. The row is marked pending only when the star state
actually **moved**. A `created` for a member the database already has down as
starred records the event and queues nothing, because there is no role change
to make, and that is what you are looking at if the member already holds the
role. `Recorded.` with the member missing the role is the real symptom, and
the list below is for that.

**Fix, in order.**

1. **Is the bot running at all?** `docker compose ps discord-bot`.
2. **Is the drain on?** It is on by default. On startup the bot logs one of:

   ```
   Draining queued role changes every 30 seconds
   The role sync drain is disabled (ROLE_SYNC_ENABLED=false)
   ```

   If you see the second line, set `ROLE_SYNC_ENABLED=true` and restart the
   bot.
3. **Do both processes use the same database?** This is the one that produces
   exactly this symptom with nothing in either log to explain it, because
   each process is working perfectly against its own database.

   ```sh
   docker compose exec server printenv MONGO_HOST MONGO_DATABASE
   docker compose exec discord-bot printenv MONGO_HOST MONGO_DATABASE
   ```

   Both must match.
4. **Read the bot's log.** A pass that did something logs one line; a pass
   over an empty queue logs nothing, which is almost every pass.

   ```
   Role sync drain complete: examined=1 granted=1 removed=0 failed=0
   ```

   | Line | Meaning |
   | --- | --- |
   | `failed=` above zero | The role change itself was refused. Look for `Could not add the role to ...` and see [the bot cannot assign or remove the role](#the-bot-cannot-assign-or-remove-the-role). The row keeps its flag and is retried on the next pass. |
   | `Skipping the role sync drain: no database connection.` | The bot started without a usable `MONGO_HOST`. It does not reconnect on its own; fix the value and restart it. |
   | `Guild <id> is not in the cache; skipping.` | `GUILD_ID` names a server the bot is not in. |
   | `Role sync drain failed (1 in a row); retrying in 33 seconds` | Transient. The wait doubles on each consecutive failure up to about 300 seconds, with a quarter of jitter either way, and it keeps trying. |

5. **Was the delivery a 204, or a 202 with `Superseded by a newer event.`?**
   Neither queued anything and the bot is not the problem. A 204 means the
   event was not tied to a verified member; see
   [a star by somebody who never verified](#a-star-by-somebody-who-never-verified-does-nothing).
   A superseded 202 means a later star event for that member reached the row
   first, so the row holds the newer state and this older delivery was
   deliberately not written. The server logs:

   ```
   Star created for GitHub id 583231 arrived behind a newer event; left as it stands.
   ```

### Nothing arrives at all: the delivery log is empty

**Cause.** GitHub never reached your server, or the hook is not sending.

**Fix, cheapest first.**

1. **Is the hook active?** The **Active** checkbox at the bottom of the hook's
   settings. GitHub shows a banner on the hook's page when it has disabled it
   for you.
2. **Are the right events selected?** Under **Which events**, **Stars** must
   be ticked. A hook set to **Pushes** only, which is GitHub's default, sends
   nothing when somebody stars the repository.
3. **Can anything reach the address from outside?** Try it from a machine
   that is not yours, not from the server itself, and go through the public
   HTTPS address, not the container's port:

   ```sh
   curl -fsS https://starguard.example.com/healthz
   ```

   If that fails, this is not a webhook problem: your public HTTPS address or
   your proxy is down, and member verification is broken too.

   **`curl` to `SERVER_PORT` from your laptop failing is expected, not a
   fault.** Both compose files publish the server as
   `127.0.0.1:${SERVER_PORT}:${SERVER_BIND_PORT}`, so the application is
   reachable from the host machine and from nowhere else, and GitHub never
   talks to that port either: it reaches your proxy on 443 and the proxy
   reaches loopback. Testing the port directly only tells you something when
   you run the test **on the host**:

   ```sh
   curl -fsS http://127.0.0.1:5000/healthz
   ```

   That working while the public address does not is a proxy or DNS problem.
   If your proxy runs on a different machine, it cannot reach the container
   at all as shipped: remove the `127.0.0.1:` from the mapping in your
   compose file and set `TRUSTED_PROXY_COUNT` to your real hop count, knowing
   the origin is then reachable over plain HTTP by anything that can route to
   it.
4. **Does the proxy forward the path?** A proxy configured only for `/login`
   and `/authorize` returns its own 404 for `/webhooks/github` without the
   request ever reaching the server. Forward everything to the `server`
   service on `SERVER_BIND_PORT`, as in
   [Step 9](./installation.md#step-9-start-the-stack).
5. **Is the Payload URL right?** It has to be the public HTTPS address with
   `/webhooks/github` appended, and nothing else. GitHub shows the exact URL
   it used on each delivery.

Press **Redeliver** on the `ping`, or use **Recent Deliveries** and the ping
GitHub sent when the hook was created, rather than un-starring the repository
each time you change something. A working ping answers 200 with the body
`pong`, and the server logs `Ping received for hook 512345678.`

### A star by somebody who never verified does nothing

**Symptom.** The delivery log shows 204 with an empty body, and the bot never
wakes up.

**Usually this is correct, and it is the common case by a wide margin.** The
`star` event fires for everybody who stars the repository, and almost none of
them have ever used the bot. Starguard can only act on a star it can tie to a
Discord account, which means somebody who has completed `/verify`. Everybody
else costs one indexed lookup and is dropped.

At `LOG_LEVEL=DEBUG` the server says so:

```
Star created by GitHub id 583231 belongs to no verified member.
```

**The one case where it is not correct** is a member who verified with a very
old version of Starguard. The lookup is by the numeric GitHub account id,
because a login can be renamed and an id cannot, and the oldest rows were
written before the id was recorded at all. Such a row is a real link, and the
member may well be holding the role, but no webhook delivery can ever find
it: every star and un-star they make answers 204 and the row is never
updated. They are not hypothetical if you upgraded rather than installed
fresh. Count them:

```sh
docker compose exec mongodb mongosh --quiet \
  -u starguard -p 'the password from .env' \
  --authenticationDatabase admin \
  --eval 'db.getSiblingDB("starguard").users.countDocuments({github_id:{$exists:false}})'
```

Anything above zero is that many members whose stars the webhook cannot see.
**Each of them has to run `/verify` and sign in with GitHub once more**, which
rewrites their row with the id; there is no way to derive one from a login
without asking GitHub about every row. Until they do, the sweep is what keeps
them honest: it falls back to comparing logins for rows with no id, so it can
still take the role from them, and only the webhook path is blind to them.

The same 204 also covers two other harmless cases: an event other than `star`
arriving because the hook is subscribed to more than you meant, and a `star`
action this version does not know about, which would mean GitHub added one.
None of them is a fault, and answering 204 rather than an error is what keeps
the delivery log green for them.

### Other statuses in the delivery log

Every answer the receiver can give is here or in a section of its own. The
three with their own sections are
[401 `Invalid signature.`](#deliveries-show-401-with-invalid-signature),
[404](#deliveries-show-404) and
[503 `Database unavailable.`](#deliveries-show-503-with-database-unavailable).
The rest:

| Status | Body | Meaning |
| --- | --- | --- |
| 200 | `pong` | The `ping` GitHub sends when the hook is created. What you want to see after setting it up. |
| 200 | `Already handled.` | A delivery id claimed in the last ten minutes and processed. GitHub's own retry of a delivery whose response it did not receive, dropped so the bot is not asked to re-apply a role. A delivery that was claimed and then failed does not land here: its claim is released, so redelivering it is new work. |
| 202 | `Recorded.` | A star event for a verified member was written to the database. It says the row now holds this delivery's state, and nothing more: the bot is queued only if the state actually moved, so a `created` for somebody already recorded as starred is recorded and queues nothing. |
| 202 | `Superseded by a newer event.` | A star event for a verified member that arrived behind a newer one for the same member, so the row was left holding the newer state and nothing was written or queued. Ordinary for an out-of-order pair of deliveries and for a redelivery of an old event; not a failure, and not something to redeliver again. |
| 204 | empty | Accepted and nothing to do: no verified member carries that GitHub account id, or the event was not a `star` at all. See the entry above, including the one case where it hides a real member. |
| 400 | `Body is not a JSON object.` | The hook's **Content type** is `application/x-www-form-urlencoded`. Change it to `application/json`. Note that the `ping` still passes with the wrong content type, so this shows up only on real star events. |
| 400 | `Missing X-GitHub-Delivery.` | GitHub always sends that header, so this means something between GitHub and the server is stripping it. Check your proxy's header rules. |
| 400 | `Missing action.`, `Missing sender id.` | The payload is not shaped like a star event. Not something a real delivery produces. |
| 405 | an HTML page | Something sent a `GET` to the receiver. It takes `POST` only, so a browser cannot be used to test it. |
| 413 | an HTML page | The body was over 1 MiB, and was refused before it was read. A star payload is a few kilobytes, so this is not a real delivery. |

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
used. Redact every secret before pasting anything: `TOKEN`, `SECRET_KEY`,
`GITHUB_CLIENT_SECRET`, `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` and any
database password, including the one inside `MONGO_HOST`.

`GITHUB_WEBHOOK_SECRET` is easy to overlook because it is newer than the
others and because diagnosing a signature problem means printing it. It is
the **only** thing authenticating a star event: anybody holding it can sign a
delivery claiming any GitHub account starred or un-starred your repository,
and the server will believe it. If one has been exposed, generate a new one,
put it in `.env` and in the hook's **Secret** field on GitHub, and restart the
server.

For a suspected security problem, do not open an issue. Follow
[SECURITY.md](../SECURITY.md).
