# Environment file reference

Starguard is configured entirely through a single `.env` file in the root of
the repository. Copy `.env.example` to `.env` and fill it in. Do not share
this file or commit it: it holds your Discord bot token, your GitHub OAuth
client secret and your database password. 🔒

The sections below are in the same order as `.env.example`, so the two can be
read side by side.

## How the file is read

`.env` is consumed in two different ways, which is why some variables never
reach the application itself:

- **Docker Compose** reads `.env` from the project directory to expand
  `${VARIABLE}` references inside the compose files, and hands the whole file
  to the `discord-bot` and `server` containers through `env_file:`.
- **The application** reads `.env` through `python-dotenv` when you run
  `python -m bot.bot` or `python -m server.server` directly, outside Docker.

Variables marked **Compose only** below are consumed by Docker Compose or by
another image (MongoDB, Mongo Express, Nginx Proxy Manager). Starguard's own
code never looks at them, so setting them changes nothing when you run the two
processes directly.

## How values are validated

Both processes validate their configuration in `main()`, before anything else
happens. A missing or unusable value is reported by name and the process exits
with status 1:

```
2026-01-01 12:00:00 ERROR starguard.bot: Configuration error: Required environment variable TOKEN is not set.
```

Under Docker this looks like a container that starts and immediately exits,
with `restart: always` putting it into a restart loop. Read the reason with
`docker compose logs discord-bot` or `docker compose logs server`.

Four kinds of validation appear below:

- **Required.** Unset or blank is a fatal error. Leading and trailing
  whitespace is stripped from every value, so a line of spaces counts as
  blank.
- **Clamped.** A number below the documented minimum is silently raised to
  that minimum. This is not an error and is not logged. Every clamped value
  is an interval or a duration, where the floor is still a value you can run
  with.
- **A TCP port.** `1` to `65535`, and anything outside it is a fatal error
  naming the variable. Ports are the one number that is not clamped, because
  a clamped port is a different address rather than a smaller value. This
  applies to `SERVER_BIND_PORT` and `BOT_HEALTH_PORT`.
- **Falls back.** An unrecognised value is silently replaced with the default.
  This applies only to `LOG_LEVEL` and `LOG_FORMAT`.

Anything else that cannot be parsed, such as a non-numeric value where a
number is expected, is a fatal error.

## Discord

### `TOKEN`

**Required.** Read by the bot. No default.

The Discord app token, from the **Bot** tab of your application at
<https://discord.com/developers/applications>.

If it is unset the bot exits with
`Required environment variable TOKEN is not set.` If it is set but wrong or
revoked, configuration passes and the bot then fails to log in, raising
`LoginError: An improper token was passed` and restarting in a loop.

### `CLIENT_ID`

**Optional.** Read by the bot. Default: empty.

The application ID of your Discord app. It is used for one thing only: logging
an invite URL on startup. Leaving it empty simply omits that line.

### `ROLE_ID`

**Required.** Read by the bot. No default. Must be a numeric Discord ID.

The role granted to members who have starred the repository. Enable Developer
Mode in Discord (User Settings, Advanced, Developer Mode), then right-click
the role and choose **Copy ID**.

A non-numeric value is fatal:

```
Configuration error: ROLE_ID must be a Discord ID (a number), got '@Stargazer'. Enable Developer Mode in Discord and use 'Copy ID'.
```

A numeric but wrong value, or a role positioned above the bot's own highest
role, passes validation and fails at the moment the role is granted. See
[the role assignment section of the troubleshooting
guide](./troubleshooting.md#the-bot-cannot-assign-or-remove-the-role).

### `GUILD_ID`

**Required.** Read by the bot. No default. Must be a numeric Discord ID.

The server the bot operates in. Right-click the server icon and choose
**Copy ID**.

If this names a server the bot is not a member of, every star check logs
`Guild <id> is not in the cache; skipping.` and does nothing.

### `CHANNEL_ID`

**Required.** Read by the bot. No default. Must be a numeric Discord ID.

The channel where the bot announces that someone lost the role after
un-starring. If the bot cannot post there it logs
`Could not post to the announcement channel: <reason>` and continues; the role
change itself still happens.

### `AUTOMATIC_CHECK`

**Optional.** Read by the bot. Default: `true`.

Whether to periodically re-check every linked member and remove the role from
anyone who has un-starred the repository. Accepted values, in any case:
`1`, `true`, `yes`, `on`, `0`, `false`, `no`, `off`.

Anything else is fatal:

```
Configuration error: AUTOMATIC_CHECK must be a boolean such as true/false, got 'yes please'.
```

With this off, `/checkstars` still works on demand, and the bot's health
endpoint reports `"star_check": "disabled"` rather than tracking that loop's
staleness. It still tracks the **other** loop: a webhook-only deployment,
`AUTOMATIC_CHECK=false` with `ROLE_SYNC_ENABLED=true`, is measured by
`role_sync` instead, which is the point of reporting both. Only with both
loops off does the endpoint stop measuring reconciliation altogether and
report no more than that the bot reached Startup.

### `AUTOMATIC_CHECK_DELAY`

**Optional.** Read by the bot. Default: `3600` (one hour). Whole number of
seconds, **clamped** to a minimum of `300` (five minutes).

How long to wait between automatic checks. Each cycle costs at least one
GitHub API call per 100 stargazers, so a short interval on a popular
repository burns through the API rate limit. Useful values: `300` (5 minutes),
`3600` (1 hour), `86400` (1 day), `604800` (1 week).

A non-numeric value is fatal:
`AUTOMATIC_CHECK_DELAY must be a whole number, got '1h'.`

This value also sets the star check's health deadline, one of two the
endpoint tracks: a completed check older than
`AUTOMATIC_CHECK_DELAY * 3 + 300` seconds makes `star_check` report `stale`,
and the whole response `degraded` with 503. The drain has its own deadline
from [`ROLE_SYNC_INTERVAL`](#role_sync_interval), and either one going stale
is enough for the 503.

### `ROLE_SYNC_ENABLED`

**Optional.** Read by the bot. Default: `true`. Same accepted spellings as
`AUTOMATIC_CHECK`.

Whether the bot drains the role changes the GitHub star webhook recorded. The
webhook arrives at the **server** process, which writes the new star state to
the database and marks the row pending; only the **bot** process is connected
to Discord, so only it can move the role. This loop is that second half.

It is independent of `AUTOMATIC_CHECK`, and the two answer different needs, so
any combination is valid:

| `AUTOMATIC_CHECK` | `ROLE_SYNC_ENABLED` | Result |
| --- | --- | --- |
| `true` | `true` | Recommended with a webhook configured. Role changes land in seconds, and the sweep repairs anything a delivery missed. |
| `true` | `false` | No webhook configured. An un-star is acted on at the next sweep, up to `AUTOMATIC_CHECK_DELAY` seconds later. A new star is never acted on automatically at all: the sweep only ever takes the role away, so the member has to sign in with GitHub again. |
| `false` | `true` | Webhook only. Nothing repairs a delivery that was lost while the server was down, because GitHub does not retry failed deliveries. |
| `false` | `false` | Nothing happens automatically. Only `/checkstars`, which removes, and the **Claim your role** button, which grants from the star state recorded at sign-in, move a role. |

Turning it off when no webhook is configured costs you nothing either way: the
queue it polls is empty forever. It is worth turning off only to keep one
fewer task and one fewer log line in play.

On startup the bot logs which way it went:

```
Draining queued role changes every 30 seconds
The role sync drain is disabled (ROLE_SYNC_ENABLED=false)
```

With this off, the bot's health endpoint reports `"role_sync": "disabled"`
rather than tracking staleness. See [Bot health check](#bot-health-check).

### `ROLE_SYNC_INTERVAL`

**Optional.** Read by the bot. Default: `30` (seconds). Whole number,
**clamped** to a minimum of `5`.

How often the bot looks for queued role changes when it finds none: the idle
polling interval. A queued change is normally applied within one interval of
the webhook recording it, and a sweep running at the same time does not hold
it up. The sweep, the drain and the **Claim your role** button exclude each
other one member at a time, for just as long as that member's read, role
change and write take, so two different members are handled concurrently and
the only thing a queued change ever waits for is another component acting on
that same member.

It is still an interval rather than a deadline. A pass that leaves rows
queued because Discord refused the role change backs off, as below, and a
burst of stars takes as long as Discord takes to accept them. Nothing is lost
while any of that happens, because the flag stays raised until the bot itself
lowers it.

This can be seconds where `AUTOMATIC_CHECK_DELAY` has to be an hour, because
the two do completely different work. A sweep is one GitHub API request per
100 stargazers. A drain is one read of a database index that is built over
only the rows actually waiting, so a poll that finds nothing reads nothing and
writes nothing, however many verified members you have.

A pass that did something logs a one-line summary; a pass that found an empty
queue, which is nearly all of them, logs nothing at all:

```
Role sync drain complete: examined=1 granted=1 removed=0 failed=0
```

A non-numeric value is fatal:
`ROLE_SYNC_INTERVAL must be a whole number, got '30s'.`

On a failure the bot retries with the same exponential backoff the sweep uses:
the first retry is one interval, it doubles on each consecutive failure up to
about 300 seconds, and a quarter of jitter is applied either way.

```
Role sync drain failed (1 in a row); retrying in 33 seconds
```

That ceiling is far lower than the sweep's half hour, because a queued row is
somebody holding, or missing, a role right now.

This value also sets the drain's health deadline, at
`ROLE_SYNC_INTERVAL * 3 + 300` seconds, or 390 with the default. Only a pass
that actually walked the queue counts: one that returned early for want of a
database connection or a cached guild did no reconciling, so it does not
reset the clock and the endpoint eventually reports `"role_sync": "stale"`.

### `COMMAND_NAME`

**Optional.** Read by the bot. Default: empty, meaning the command is not
registered at all.

The name of the optional "useful links" slash command. Discord only accepts
lowercase names, so whatever you write here is lowercased before it is
registered.

The command is only registered when this is set **and** at least one complete
button pair exists. See `BTN1` below.

### `COMMAND_DESCRIPTION`

**Optional.** Read by the bot. Default: `Useful links`.

The one-line description Discord shows next to the command.

### `COMMAND_EXTENDED_DESCRIPTION`

**Optional.** Read by the bot. Default: empty.

A longer description, shown only in the `/help` embed. Discord message
formatting such as bold text and emoji works here.

### `BTN1`, `BTN2`, `BTN3`, `BTN4` and `URL1`, `URL2`, `URL3`, `URL4`

**Optional.** Read by the bot. No defaults. Four pairs, no more.

Each pair is one button: `BTN1` is its label, `URL1` is the address it opens.
**Only pairs where both halves are set are shown.** A label with no URL, or a
URL with no label, is skipped rather than registered as a button Discord would
reject.

Each URL must be a complete address including the scheme, for example
`https://github.com/`.

## Bot health check

The bot serves `GET /healthz` inside its own container. The `discord-bot`
healthcheck in both compose files calls it.

Every response carries `status`, `uptime_seconds` and `gateway`. Until the
Discord gateway connects, that is the whole of it, with **503**:

```json
{"status": "starting", "uptime_seconds": 5.0, "gateway": "connecting"}
```

Neither loop field appears there, because nothing has been measured yet. A
monitor that reads them has to tolerate their absence in this state as well
as in the ones below.

Once connected, the payload also names each of the bot's two reconciling
loops, whether or not that loop is turned on:

| Field | The loop | Turned on by | Its deadline |
| --- | --- | --- | --- |
| `star_check` | the periodic star sweep | `AUTOMATIC_CHECK` | `AUTOMATIC_CHECK_DELAY * 3 + 300` seconds |
| `role_sync` | the role sync drain | `ROLE_SYNC_ENABLED` | `ROLE_SYNC_INTERVAL * 3 + 300` seconds |

Each field is one of four values:

- `disabled`: the loop is off, so nothing can be late.
- `pending`: on, and no pass has finished yet. Its grace runs from startup
  rather than from a last pass.
- `ok`: on, and the last completed pass is inside the deadline.
- `stale`: on, and it is not. The response is then **503** with
  `"status": "degraded"`, whichever of the two loops it was. One stale loop
  is enough; the other keeps reporting its own state beside it.

**The age fields are optional, and a monitor has to treat them that way.** A
loop reports the age of its last completed pass, in `last_check_age_seconds`
for `star_check` and `last_role_sync_age_seconds` for `role_sync`, only once
a pass has actually completed. A `disabled` loop carries no age, a `pending`
one carries none, and a loop that went `stale` without ever finishing a pass
carries none either. So `"status": "ok"` does not imply either age field is
present, and a perfectly healthy bot answers like this for as long as its
first pass takes:

```json
{"status": "ok", "uptime_seconds": 60.0, "gateway": "connected", "star_check": "pending", "role_sync": "pending"}
```

A bot with both loops running and both fresh answers **200** with:

```json
{"status": "ok", "uptime_seconds": 60.0, "gateway": "connected", "star_check": "ok", "last_check_age_seconds": 10.0, "role_sync": "ok", "last_role_sync_age_seconds": 5.0}
```

### `BOT_HEALTH_ENABLED`

**Optional.** Read by the bot. Default: `true`. Same accepted spellings as
`AUTOMATIC_CHECK`.

Whether to serve the health endpoint at all.

**Setting this to `false` does not disable the compose healthcheck.** The
healthcheck keeps calling a port nothing is listening on, so after three
failures Docker marks the container `unhealthy` and leaves it that way
forever. If you turn the endpoint off, also override the `healthcheck` block
for the `discord-bot` service, for example with `disable: true` in your
`docker-compose.override.yml`.

### `BOT_HEALTH_HOST`

**Optional.** Read by the bot. Default: `127.0.0.1`.

The address the health endpoint binds to. The default is loopback, which is
reachable from the container healthcheck (it runs inside the container) and
from nowhere else. Set it to `0.0.0.0` only if you also publish the port and
want to scrape the endpoint from outside, for example from a monitoring
system.

### `BOT_HEALTH_PORT`

**Optional.** Read by the bot **and by Docker Compose**. Default: `8080`.
A TCP port, `1` to `65535`, refused by name at startup rather than clamped,
exactly as [`SERVER_BIND_PORT`](#server_bind_port) is:

```
Configuration error: BOT_HEALTH_PORT must be a TCP port between 1 and 65535, got 70000.
```

The port the health endpoint listens on. Both compose files interpolate this
into the healthcheck command, so changing it here moves both sides together.

If the port is in the range but cannot be bound, because something else holds
it, the bot logs
`Could not start the health endpoint on <host>:<port>: <reason>` and carries
on without it. The bot keeps working; the container is reported unhealthy.

## GitHub OAuth and the callback server

### `REPO_OWNER`

**Required.** Read by both the bot and the server. No default.

The user or organisation that owns the repository members are asked to star,
for example `fuegovic` in `fuegovic/Starguard`.

### `GITHUB_REPO`

**Required.** Read by both the bot and the server. No default.

The repository name on its own, **not** `owner/repo`. For
`fuegovic/Starguard` this is `Starguard`.

Getting either of these wrong passes validation and fails on the first GitHub
call:

```
Repository not found (404). Check REPO_OWNER and GITHUB_REPO, and note that a private repository needs a GITHUB_TOKEN that can read it.
```

### `SERVER_PORT`

**Optional. Compose only.** Default: `5000`.

The port published on the **host** machine for the OAuth server. Change it
freely if something else on the host already uses 5000. The application never
reads this.

**Both compose files publish it on the loopback interface only**, as
`127.0.0.1:${SERVER_PORT}:${SERVER_BIND_PORT}`, so nothing off the machine can
reach the application directly. That is not a restriction in the intended
setup: `DOMAIN` must be an HTTPS address, so a TLS-terminating proxy is
required anyway, and a proxy on the same host reaches loopback. If your proxy
runs on a **different** machine, remove the `127.0.0.1:` from the mapping in
your own compose file, and set [`TRUSTED_PROXY_COUNT`](#trusted_proxy_count)
to your real hop count, knowing that the origin is then reachable without TLS
by anything that can route to it.

### `SERVER_BIND_PORT`

**Optional.** Read by the server **and by Docker Compose**. Default: `5000`.
A TCP port, `1` to `65535`. A value outside that range is **refused by name**
at startup rather than clamped, because a clamped port is not a smaller
version of what you asked for, it is a different address.

The port the server listens on **inside** its container. Leave it at 5000
unless something else in your setup needs that port internally. Both compose
files interpolate it into the port mapping and into the server healthcheck, so
the host mapping and the probe follow it automatically.

### `DOMAIN`

**Required by the bot.** Not read by the server. No default.

The public address of the OAuth server, for example
`https://starguard.example.com`. The bot builds each member's personal login
link from it, so it has to be reachable from outside your network and it has
to match the **Authorization callback URL** registered on your GitHub OAuth
app, which must be this value with `/authorize` appended.

It is validated strictly, because a malformed value breaks the whole flow with
nothing useful in the log: Discord refuses to render a button whose URL has no
scheme, and GitHub refuses a `redirect_uri` it was not configured with. The
value must be an absolute `https://` URL with a host, and must carry no query
string and no fragment. A trailing slash is accepted and stripped.

```
Configuration error: DOMAIN must be an absolute https URL such as https://starguard.example.com, got 'http://starguard.example.com'.
Configuration error: DOMAIN must be an absolute https URL such as https://starguard.example.com, got 'starguard.example.com'.
Configuration error: DOMAIN must be a plain URL with no query string or fragment, got 'https://starguard.example.com/?a=1'.
```

Plain HTTP is refused deliberately. The session cookie the OAuth flow depends
on is marked `Secure`, so no browser would send it back over HTTP and the
callback would fail for every user. Terminate TLS in front of the server, then
point `DOMAIN` at the HTTPS address.

Note that the **server** does not read `DOMAIN` at all. It derives the OAuth
`redirect_uri` from the incoming request instead, which is why
`TRUSTED_PROXY_COUNT` has to be right for the callback to be built as
`https://`.

### `GITHUB_CLIENT_ID`

**Required.** Read by the server. No default.

The client ID of your GitHub OAuth app, from
<https://github.com/settings/developers>.

### `GITHUB_CLIENT_SECRET`

**Required.** Read by the server. No default.

The matching client secret. If either of these is wrong, verification fails at
the callback and the visitor sees `GitHub sign-in failed: <reason>` with HTTP
400.

### `GITHUB_TOKEN`

**Optional.** Read by the bot. No default.

A GitHub personal access token, used only to list the repository's
stargazers. **No scopes are needed for a public repository**; `public_repo` is
enough if you prefer to grant one.

**A private repository cannot be made to work by granting this token more.**
It would let the bot's sweep read the stargazer listing, but the sweep is
only half of the flow. The server decides whether a member has starred by
asking GitHub with the **member's own** OAuth token, which carries only
`read:user` and cannot see a private repository, so GitHub answers 404 and
the server records "not starred" for everyone. See
[Step 5 of the installation guide](./installation.md#step-5-create-a-github-personal-access-token).

The listing is fetched 100 stargazers per request. Without a token, GitHub
allows 60 requests per hour from your server's address, so a single full pass
over a repository with 6000 stargazers exhausts the hour's budget. The bot
warns about this at startup:

```
GITHUB_TOKEN is not set. Unauthenticated GitHub requests are limited to 60 per hour, which is not enough for a repository with more than a few thousand stargazers.
```

With a token the limit is 5000 requests per hour. An invalid or revoked token
produces `GitHub rejected GITHUB_TOKEN (401). Check that it is valid.`

### `SECRET_KEY`

**Required.** Read by both the bot and the server. No default. At least 16
characters.

Signs the Flask session cookie and the personal, expiring verification links
handed out by `/verify`. The bot signs a link and the server verifies it, so
**the two processes must have exactly the same value**. A predictable key
would let anyone mint a verification link for any Discord account.

Generate one with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Three things are rejected:

```
Configuration error: Required environment variable SECRET_KEY is not set. Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"
Configuration error: SECRET_KEY is still set to a placeholder value. Generate a real one with: python -c "import secrets; print(secrets.token_urlsafe(32))"
Configuration error: SECRET_KEY must be at least 16 characters, got 8.
```

The rejected placeholders are `secretkey`, `changeme`, `change-me`, `secret`,
`your-secret-key` and `please-change-me`, compared without regard to case.

If the bot and the server hold different keys, both start normally and every
verification link fails with `This verification link is not valid.`

Changing the key invalidates all outstanding verification links and signs
every existing session out. Nobody loses their role or their link record.

### `GITHUB_WEBHOOK_SECRET`

**Optional.** Read by the server. No default. At least 16 characters when it
is set.

The shared secret GitHub signs each `star` webhook delivery with. Setting it
turns on the receiver at `POST /webhooks/github`, which is how the server
learns about a star the moment it happens. Without it the server learns about
a new star only when the member signs in with GitHub again, because the sweep
only ever takes the role away. The same value goes in the **Secret** field of
the webhook on GitHub. The full setup is
[Step 12 of the installation guide](./installation.md#step-12-set-up-the-star-webhook-optional).

**This is a second, separate secret. Do not reuse `SECRET_KEY`.** Generate
another one the same way:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Leaving it unset is a supported configuration, and it is the default.** The
route is then never registered: there is no `/webhooks/github` on the server
at all, not a stub that answers an error, and a request for it gets the
ordinary 404 any unknown path gets. The bot keeps working exactly as before,
noticing star changes only at each sweep.

It is validated against the same two rules as `SECRET_KEY`, which is the
point: a webhook secret copied out of `.env.example` would let anybody sign a
star event for anybody.

```
Configuration error: GITHUB_WEBHOOK_SECRET is still set to a placeholder value. Generate a real one with: python -c "import secrets; print(secrets.token_urlsafe(32))" and paste the same value into the webhook's secret field on GitHub.
Configuration error: GITHUB_WEBHOOK_SECRET must be at least 16 characters, got 8.
```

The rejected placeholders are the same list as for `SECRET_KEY`.

**Treat it as a credential of the same weight as the others.** Every delivery
is authenticated by this value and by nothing else, so anybody who has it can
sign a star event claiming any GitHub account starred or un-starred your
repository, and the server will act on it. Redact it from anything you paste
into an issue, and if it is ever exposed, generate a new one, put it in both
`.env` and the hook's **Secret** field on GitHub, and restart the server.

The source address and the `User-Agent` are not checked, because both can be
forged and the signature cannot. A delivery whose signature does not match is
answered with HTTP 401 and `Invalid signature.`, and the server logs that
only at `DEBUG`: the route is deliberately not rate limited, so a log line per
rejected request would be a way to fill your disk from outside. GitHub's own
delivery log is where you read those, not yours. See
[the webhook section of the troubleshooting
guide](./troubleshooting.md#the-star-webhook-is-not-working).

Only the server reads this value. The bot does not need it, and the two
processes still never talk to each other: the server records what changed in
the database and the bot picks it up, which is what `ROLE_SYNC_ENABLED` and
`ROLE_SYNC_INTERVAL` control.

### `LINK_TOKEN_MAX_AGE`

**Optional.** Read by the server. Default: `900` (15 minutes). Whole number of
seconds, **clamped** to a minimum of `60`.

How long a `/verify` link stays usable. After that the visitor sees
`This verification link has expired.` and can press **Get a new link 🔄** back
in Discord.

The bot's own wording is fixed text that says fifteen minutes, so if you
change this value the message will no longer match. Edit
`VERIFY_STEPS` and `RELINK_SENT` in `bot/messages.py` if that matters to you.

### `TRUSTED_PROXY_COUNT`

**Optional.** Read by the server. Default: `1`. Whole number, **clamped** to a
minimum of `0`.

How many reverse proxies **you operate** sit directly in front of the server.
The server passes this to Werkzeug's `ProxyFix`, which rewrites the request's
scheme, host and client address from the last N entries of the
`X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` headers. Two
things depend on getting it right: the OAuth `redirect_uri` is rebuilt as
`https://` from the forwarded scheme, and the rate limit on `/login` and
`/authorize` keys on the resulting client address.

Count the hops, starting at the server and working outwards, that you control:

| Your setup | Value |
| --- | --- |
| Nothing in front: you removed the `127.0.0.1:` from the port mapping and clients reach the container directly | `0` |
| The bundled Nginx Proxy Manager from `docker-compose.alt.yml`, nothing in front of it | `1` |
| Your own Nginx, Caddy or Traefik in front of the container | `1` |
| A CDN or load balancer (Cloudflare, an ALB) in front of your own proxy | `2` |
| Two of your own proxies plus a CDN | `3` |

Do not count proxies that are not yours, and do not count the client.

`0` is not a hop count but an instruction: install no `ProxyFix` at all and
believe no forwarded header. Use it whenever a client can reach the server's
port without passing through something you run. With even one trusted hop,
such a client can put any address it likes in `X-Forwarded-For` and take a
fresh rate-limit bucket for every request.

Out of the box that cannot happen, because both compose files publish the
port as `127.0.0.1:${SERVER_PORT}:${SERVER_BIND_PORT}` and only something on
the same host can reach it. `1` is therefore the right default. If you widen
that mapping so the container is reachable from elsewhere, decide again: `0`
if clients arrive directly, your hop count if they still pass through a proxy
you run.

Setting it **too low** means every visitor behind your own proxy is seen as
that proxy's address, so one busy proxy trips the rate limit for everyone.
Setting it **too high** is the dangerous direction: `ProxyFix` counts entries
from the right, so a client that prepends forged entries to `X-Forwarded-For`
can make the server read an address the client chose, defeating the rate limit
entirely and putting a value of the attacker's choosing into the logs.

**How to check what the server actually resolved.** Nothing is logged for an
ordinary request, so a single visit tells you nothing. The client address
appears in exactly one line, the one written when the rate limit refuses a
request, so the way to see it is to trip the limit on purpose from a machine
that reaches the server the way a real member does:

```sh
for i in $(seq 1 12); do
  curl -s -o /dev/null -w '%{http_code}\n' 'https://starguard.example.com/login?token=x'
done
```

The first ten answer 400, because the token is nonsense, and the rest answer
429. Then read the server's log:

```sh
docker compose logs -n 20 server | grep 'Rate limited'
```

```
Rate limited login for 203.0.113.7
```

That address is what `TRUSTED_PROXY_COUNT` resolved to. Compare it with the
public address of the machine you ran `curl` on. If it is your proxy's
address, or a container address such as `172.18.0.1`, the value is too low.
If it matches the client, it is right. Wait out
`LOGIN_RATE_LIMIT_WINDOW` afterwards, or restart the server, since the
limiter is held in memory.

Do the same check from a second machine if you can: two different clients
must produce two different addresses. One address for both is the collapse
that too low a value causes, and it is invisible from a single client.

### `LOGIN_RATE_LIMIT`

**Optional.** Read by the server. Default: `10`. Whole number, **clamped** to
a minimum of `1`.

How many requests a single client address may make within
`LOGIN_RATE_LIMIT_WINDOW`. Over the limit, the visitor gets HTTP 429 with a
`Retry-After` header and the page
`Too many verification attempts from your address. Please wait a moment and
try again.`, and the server logs
`Rate limited <endpoint> for <address>`.

**`/login` and `/authorize` are counted separately**, one bucket per route
per address, so the default of `10` is ten of each rather than ten between
them. One verification is one request to each, so it spends one of the ten on
each side. A shared bucket would charge an ordinary flow twice, and
`LOGIN_RATE_LIMIT=1` would then refuse the callback of the single attempt it
had just allowed. Only these two routes are limited at all; `/healthz`, the
landing page and the webhook receiver are not.

The limiter is a sliding window held in memory by the single server process.
It resets when the server restarts, and it counts per process rather than
across replicas.

### `LOGIN_RATE_LIMIT_WINDOW`

**Optional.** Read by the server. Default: `60`. Whole number of seconds,
**clamped** to a minimum of `1`.

The width of the window `LOGIN_RATE_LIMIT` applies over.

## MongoDB

### `MONGO_HOST`

**Required.** Read by both the bot and the server. No default.

The MongoDB connection string. Examples:

```
mongodb://<user>:<password>@mongodb:27017/?authSource=admin
mongodb://127.0.0.1:27017/
mongodb+srv://user:password@cluster.example.mongodb.net/
```

The first is the bundled container from `override.example.yml` or
`docker-compose.alt.yml`. **That database requires authentication**, so the
credentials must be present and must match `MONGO_INITDB_ROOT_USERNAME` and
`MONGO_INITDB_ROOT_PASSWORD` below.

Keep `?authSource=admin`. The driver authenticates against the database named
in the connection string's path, falling back to `admin` when the path is
empty, and the bundled image creates its root user in `admin`. So the form
above works either way, but the moment anyone appends a database name, as in
`mongodb://user:password@mongodb:27017/starguard`, the driver would look for
the user in `starguard` and authentication would start failing. Saying
`authSource` explicitly makes that impossible.

**Percent-encode the credentials.** PyMongo parses this value as a URI, so a
reserved character in the username or password has to be percent-encoded.
The ones that matter, and what each does when you leave it unencoded:

- `@`, `:`, `/` and `%` raise a caught error. Something goes wrong at
  startup and says so.
- `?` raises an **uncaught** error, in the form this file's examples use:
  everything after the `?` is read as the connection string's query part,
  which leaves the rest of the password sitting where the port belongs, and
  a port that is not a number is fatal. A mistyped port such as
  `mongodb://mongodb:70000/` fails the same way for the same reason. (A `?`
  in a string carrying no `?authSource=admin` or other trailing option is
  caught instead, but every example here carries one.)
- `+` is the worst of them, because nothing complains at all. It is silently
  decoded to a space, so the password is quietly wrong.
- `#`, `[`, `]`, `!` and `$` need no encoding. Encoding them anyway does no
  harm if you would rather not remember the list.

**Encode it in `MONGO_HOST` and nowhere else.** MongoDB is handed the
password literally, through `MONGO_INITDB_ROOT_PASSWORD`, and so are the
`mongosh` healthcheck and Mongo Express in both compose files. Encoding it
there too creates the nastiest version of this: the `mongodb` container
reports healthy, because it and its healthcheck agree on a password, while
the bot and the server authenticate with a different one and fail. Encode an
existing password with

```sh
python -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" 'your password'
```

A database that is simply unreachable, or that refuses the credentials, does
not stop either process. A client is built, both attempt four pieces of
startup work, each reported by name when it fails, and both keep going:

```
Could not index the users collection: <reason>
Could not index the deliveries collection: <reason>
Could not purge credentials written by older versions: <reason>
Could not upgrade user records: <reason>
```

Each database operation then fails as it is attempted, and the server's
`/healthz` answers 503, because it sends a `ping` to MongoDB on every probe
rather than assuming that a client which exists is a database that answers.

A value the driver cannot use is a different matter, and **the two processes
do not treat it alike**.

**The server refuses to start, naming the variable.** It parses `MONGO_HOST`
with the driver while loading its configuration, so anything the driver
rejects is reported the way every other bad setting is, and the process exits
1 rather than serving:

```
Configuration error: MONGO_HOST is not a usable MongoDB connection string: <reason>
```

**The bot does not.** It reads `MONGO_HOST` as a plain string, so a value the
driver cannot use is only discovered when it tries to connect, and what
happens then depends on how the driver fails. A rejection it reports as a
MongoDB error, such as an unencoded `@`, `:`, `/` or `%`, is caught: the bot
logs `Error connecting to MongoDB: <reason>` and runs on with no database,
granting no roles. A malformed **port** is not caught. `mongodb://mongodb:70000/`,
`mongodb://mongodb:notaport/` and the parse that an unencoded `?` produces
when it leaves password text sitting in the port position all raise a plain
`ValueError`, which is not a MongoDB error, so nothing catches it:

```
ValueError: Port contains non-digit characters. Hint: username and password must be escaped according to RFC 3986, use urllib.parse.quote_plus
```

The bot exits with that traceback, `restart: always` brings the container
back, and it exits again. If the `discord-bot` container is looping while the
`server` container is up, or if the server exited with the configuration
error above and the bot did not, this asymmetry is why. See [a malformed
connection string](./troubleshooting.md#a-malformed-connection-string) and
[MongoDB connection and authentication
failures](./troubleshooting.md#mongodb-connection-and-authentication-failures).

### `MONGO_DATABASE`

**Required.** Read by both the bot and the server. `.env.example` ships
`starguard`, but the value is not optional: blanking it is a fatal error.

The database name. Starguard uses two collections inside it: `users`, one
document per verified member, and `webhook_deliveries`, which remembers the id
of each webhook delivery for ten minutes so a redelivered one is not acted on
twice.

**Both are created and indexed at every startup**, whether or not a webhook
is configured, so a database user that can write only one of them logs a
failure for the other. On an installation with no webhook,
`webhook_deliveries` exists and stays empty. Its rows expire by themselves,
through a TTL index, so nothing has to clean it up.

### `MONGO_INITDB_ROOT_USERNAME`

**Required with the bundled MongoDB. Compose only.** No default.

Read by `override.example.yml` and `docker-compose.alt.yml`, and consumed by
the official `mongo` image, which creates this user with the `root` role in
the `admin` database and starts `mongod` with authentication enabled. Both
compose files refuse to start without it:
`set MONGO_INITDB_ROOT_USERNAME in .env`.

It must match the username in `MONGO_HOST`.

**The user is only created when the data directory is empty.** If you are
adding these variables to a deployment that already has data in
`./server/mongo-data`, read
[the MongoDB authentication section of the upgrade
guide](./installation.md#3-the-bundled-mongodb-now-requires-authentication)
before restarting anything.

### `MONGO_INITDB_ROOT_PASSWORD`

**Required with the bundled MongoDB. Compose only.** No default.

The password for the user above. Must match the password in `MONGO_HOST`.
Setting only one of the two makes the `mongo` image exit with
`error: missing 'MONGO_INITDB_ROOT_USERNAME' or 'MONGO_INITDB_ROOT_PASSWORD'`.

## Mongo Express

These are read only by `override.example.yml` and `docker-compose.alt.yml`,
both of which publish the Mongo Express UI on the **loopback interface only**.
Reach it at `http://localhost:8081/`, or over an SSH tunnel from another
machine. Do not publish it on a public interface.

### `MONGO_EXPRESS_USERNAME`

**Required when the mongo-express service is present. Compose only.** No
default.

The basic-auth username for the UI. Compose refuses to start without it rather
than falling back to `admin`, which is what earlier versions did.

### `MONGO_EXPRESS_PASSWORD`

**Required when the mongo-express service is present. Compose only.** No
default.

The basic-auth password. Compose refuses to start without it rather than
falling back to `password`.

### `MONGO_EXPRESS_PORT`

**Optional. Compose only.** Default: `8081`.

The loopback port the UI is published on.

## Nginx Proxy Manager

Read only by `docker-compose.alt.yml`.

### `NPM_INITIAL_ADMIN_EMAIL`

**Optional but strongly recommended. Compose only.** Default: empty.

Seeds the first administrator account. Without it, Nginx Proxy Manager uses
its well-known default login (`admin@example.com` / `changeme`) until somebody
signs in and changes it, which on a listener published on ports 80 and 443 is
a real window. If you leave both of these empty, sign in and change the
credentials before exposing those ports.

### `NPM_INITIAL_ADMIN_PASSWORD`

**Optional but strongly recommended. Compose only.** Default: empty.

The password for the account above.

## Logging

Both processes log to stderr, which Docker collects. Both compose files cap
the JSON log driver at three 10 MB files per container.

### `LOG_LEVEL`

**Optional.** Read by both the bot and the server. Default: `INFO`.

One of `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case. An
unrecognised value **falls back** to `INFO` silently rather than failing, so a
typo here costs you nothing but is also not reported.

### `LOG_FORMAT`

**Optional.** Read by both the bot and the server. Default: `text`.

Either `text` or `json`. An unrecognised value **falls back** to `text`
silently.

`text` produces one readable line per record, with the request ID appended on
the server's request-scoped lines:

```
2026-01-01 12:00:00,123 INFO starguard.server: Linking Discord ID 1234 to GitHub user someone (starred=True) [request_id=6f1c...]
```

`json` produces one JSON object per line, which a log shipper can index
without reversing the text format. Structured fields are typed rather than
embedded in the message, which is what makes the per-cycle star check summary
queryable:

```json
{"timestamp": "2026-01-01T12:00:00+0000", "level": "INFO", "logger": "starguard.bot", "message": "Star check complete: examined=42 roles_removed=1 ...", "examined": 42, "roles_removed": 1, "api_calls": 3, "pages_fetched": 1, "pages_unchanged": 2, "rate_limit_remaining": 4987, "duration_seconds": 1.8}
```

## See also

- [Installation guide](./installation.md), including the upgrade path from
  older versions.
- [Troubleshooting](./troubleshooting.md), for what each failure looks like in
  the logs.
