# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-21

### Added

- **Prebuilt container images**, published to the GitHub Packages registry
  as `ghcr.io/librechat-ai/starguard-bot` and
  `ghcr.io/librechat-ai/starguard-server`.
  They are built for `linux/amd64` and `linux/arm64`, carry an SBOM and a
  `mode=max` provenance attestation, and are signed with cosign keyless, so a
  pulled image can be traced back to the workflow run and the commit that
  produced it. The compose files pull them by default; installing no longer
  means building from source. `docker-compose.build.yml` is the opt-in for
  building from a checkout.
- **Automated releases.** A merge to `main` opens or updates a release pull
  request describing what has accumulated; merging that pull request tags the
  commit, publishes the GitHub release, and publishes the version-tagged
  images. See [CONTRIBUTING.md](./CONTRIBUTING.md#releases).
- A Trivy scan of both images in CI, failing on a fixable HIGH or CRITICAL
  vulnerability and staying quiet about one with no fix available.
- A shared `common` package for configuration, storage, the GitHub API client
  and signed link tokens, so the bot and the server no longer duplicate this
  logic and the risky parts can be tested directly.
- A health check for the OAuth server, a non-root container user for both
  images, and Docker layer ordering that keeps dependency installs separate
  from code changes.
- A gated CI pipeline: lint, type-check, a security scan, tests on Python
  3.11 and 3.12, and a Docker build all have to pass before an image is
  published.
- **Optional GitHub `star` webhook support.** Set `GITHUB_WEBHOOK_SECRET` and
  the OAuth server serves `POST /webhooks/github`, so a star or an un-star is
  acted on within seconds instead of at the next periodic check, and costs no
  GitHub API budget at all. On a repository with 45,000 stars the hourly
  check is 450 API requests an hour; the webhook replaces almost all of that
  with one small signed request per event. Every delivery is authenticated by
  its HMAC over the raw body and nothing else, and a delivery id is
  remembered for ten minutes so a duplicate is not acted on twice. Without
  the secret the route is not registered at all, and the bot behaves exactly
  as it did before.
- The bot applies those queued changes on its own loop, controlled by
  `ROLE_SYNC_ENABLED` and `ROLE_SYNC_INTERVAL` (30 seconds by default). The
  webhook reaches the server and only the bot can change a Discord role, so
  the server records what changed and the bot picks it up; the two still
  never talk to each other. The periodic check is unchanged and still
  required, because GitHub does not automatically retry a failed delivery and
  the check is the only thing that repairs one that was missed. It can now be
  run daily rather than hourly.

### Changed

- The compose files run the published images instead of building them, so
  `docker compose up -d` no longer needs a compiler, a checkout of the source
  or ten minutes. `STARGUARD_IMAGE_OWNER` and `STARGUARD_IMAGE_TAG` in `.env`
  choose which images and which version; `:latest` follows the newest stable
  release rather than the tip of `main`, which is published as `:main`.
- The database's own exception type no longer reaches the rest of the code.
  Eight modules used to catch `PyMongoError`, the driver's base class, so the
  data access was behind named functions but the failures were not.
  `common/storage_errors.py` now defines one `StorageError` and the decorators
  that translate into it, and nothing under `bot/` or `server/` names pymongo
  in its error handling. The health probe goes through a `ping` function for
  the same reason: it was the last route calling the driver directly.
- The webhook delivery functions moved to `common/deliveries.py`.
  `common/storage.py` had reached the thousand lines pylint allows a module,
  so the next change to it failed the build whatever the change was. The
  deliveries collection shares nothing with the users collection but the
  database handle, which makes it the seam that costs least.
- `ruff`'s `target-version` is `py311`, matching the lowest Python the test
  matrix runs. Told `py312` it proposed PEP 695 type parameters, which are a
  syntax error on 3.11, so following its advice would have passed lint and
  then failed the 3.11 test job.
- Image publishing lives in one reusable workflow (`publish.yml`) called from
  both paths, rather than a copy in `ci.yml` and another in `release.yml`
  that had already drifted apart on their tag lists.
- `ci.yml` no longer triggers on a push to `main`. `release.yml` owns `main`
  and calls `ci.yml` as its gate, so the branch runs one pipeline instead of
  two overlapping ones and a release can only be cut from a commit whose
  tests passed.
- The GitHub OAuth flow now verifies a signed, expiring link token in
  `/login` instead of trusting the Discord ID and name from the query
  string.
- Session cookies are marked `HttpOnly`, `SameSite=Lax` and `Secure`.
- The OAuth callback server is served with waitress instead of Flask's
  development server.
- Blocking MongoDB and GitHub API calls now run in a worker thread, off the
  bot's event loop.
- Discord IDs are converted to `int` so cache lookups actually hit, and
  stargazer logins are compared case-insensitively via a set.
- User documents are now keyed on the Discord ID instead of the GitHub email
  address, which is what let a second link overwrite the first user's row. A
  GitHub account can now only be linked to one Discord user, enforced both in
  code and by a unique index.
- Both Docker images install from the pinned `requirements.txt` instead of a
  separate, unpinned `pip install` line, and moved to `python:3.12-slim`
  running as a non-root user.
- `docker-compose.alt.yml` now builds (it previously referenced Dockerfiles
  that did not exist), MongoDB moved from the end-of-life 4.x series to 7,
  and the database admin UIs are published on the loopback interface only.
- Dependencies bumped to current releases: authlib, Flask and requests all
  had published advisories at their previous pins.
- **A linked member who stars the repository now gets the role
  automatically**, as soon as the star webhook reports it. The recorded star
  state used to be refreshed only during verification, and **Claim your role**
  reads that recorded state rather than asking GitHub, so somebody who had
  already linked their account and then starred had to go back through the
  GitHub sign-in before the button would grant them anything. The button is
  unchanged and is still how the first-time flow ends.
- The result page shown to a member who has not starred yet now tells them to
  run `/verify` again rather than to claim their role. Claiming reads the star
  state recorded during sign-in, and without the optional webhook nothing
  turns a recorded false back into true, so the old wording failed for
  precisely the person who followed it.
- The result page's message no longer sets `aria-live` alongside `role` or
  takes focus. The two roles already imply the right live region, and moving
  focus opened a second, competing announcement path.
- **Documented: private repositories are not supported.** A `repo`-scoped
  `GITHUB_TOKEN` lets the bot's sweep list a private repository's stargazers,
  but the OAuth callback asks GitHub whether the member starred it using the
  member's own token, which carries only `read:user` and cannot see a private
  repository. GitHub answers 404, which means "not starred", so every member
  fails verification. Nothing changed in the code; the limitation is now
  stated in the installation and environment guides rather than implied away
  by a mention of the `repo` scope.

### Fixed

- Every GitHub call the OAuth server makes now has a deadline. The token
  exchange, the profile read and the starred check inherited requests' default
  of no timeout at all, so a connection GitHub never closed held a waitress
  worker for the life of the process; waitress serves the whole application
  from four threads, and `/login`, `/authorize` and the webhook receiver draw
  on the same four. The bot's GitHub client already had this and the server
  now uses the same 30 seconds.
- **Claim your role** now acknowledges the interaction before it does any
  work. Discord drops the interaction token unless something answers within
  three seconds, and the answer was the handler's final message, behind a
  member lock, a database read and a role change. A lock held by the periodic
  check working on the same member, or a role call retried through a rate
  limit, put that answer past the window: the member was told the interaction
  failed while the role had in fact been granted and recorded. The thank-you
  is still posted publicly, now as a follow-up message, with a private
  confirmation to whoever pressed the button.
- A page of stargazers whose body is not JSON is now refused as a
  `GitHubError` rather than escaping as requests' own `JSONDecodeError`. A
  200 carrying a truncated body or a proxy interstitial took the whole check
  cycle down with a traceback, because every caller catches `GitHubError` and
  nothing else.
- One unusable link no longer strands every link behind it. The un-star sweep
  walks the collection from the beginning on each cycle, so an entry that
  raised aborted the cycle and the retry reached the same entry again: nobody
  positioned after it was checked again for as long as the bot ran. It is now
  reported and stepped over, the way the role sync drain already did.
- The published image reference is lowercased before it is used. It was built
  from `github.repository_owner`, which preserves the account's capitals, so
  every push from a fork under an owner with an uppercase letter in its name
  failed with `invalid reference format` after the whole build had already
  run.
- **A GitHub outage no longer records that a member has not starred the
  repository.** The callback read any answer other than 204 from the starred
  check as "not starred", so a 401, a 403, a 429 or a 5xx during an outage
  overwrote a true star with a false one and sent the member off to star a
  repository they had already starred. There are exactly two answers, 204 and
  404; anything else is now reported as HTTP 502 `Could not read your GitHub
  profile. Please try again.` and nothing is written.
- **The bot's `/healthz` now measures both loops that reconcile roles, not
  just the sweep.** The payload gained `role_sync`, carrying the same
  `disabled`, `pending`, `ok` and `stale` values as `star_check`, plus
  `last_role_sync_age_seconds` once a pass has completed; either loop going
  stale makes the response `degraded` with 503, and both fields are reported
  so the payload names which one stopped. The drain's budget is
  `ROLE_SYNC_INTERVAL * 3 + 300` seconds, 390 with the default. A
  webhook-only deployment, `AUTOMATIC_CHECK=false` with
  `ROLE_SYNC_ENABLED=true`, had the drain as its only reconciling loop and no
  deadline at all, so a bot whose drain had never once reached the database
  answered 200 for as long as it ran. **Such a container will now correctly
  report `unhealthy`.** `star_check` and `last_check_age_seconds` are
  unchanged in name, values and meaning.
- **The sweep, the drain and the claim button now exclude each other one
  member at a time instead of sharing one process-wide mutex.** The single
  lock was the star check's own cycle lock, held across the stargazer listing
  and the whole member sweep, which is minutes on a large repository: a
  queued webhook waited out an entire cycle whichever member it was about, so
  the configured drain interval described nothing that actually happened, and
  a member pressing **Claim your role** watched a spinner for the same
  minutes. Each path now takes only the lock for the member it is acting on,
  held across that member's read, role change and write, so different members
  are handled concurrently and the conflict that mattered is still excluded.
  `/checkstars` also stops reporting that a star check is running when only a
  drain is in progress.
- **The server's `/healthz` now reaches the database.** It sends a `ping` on
  every probe instead of treating the existence of a client object as proof of
  a connection, so an unreachable or refusing MongoDB answers 503 rather than
  a green 200 that kept the container in service while every verification
  failed at the last step.
- **`/login` and `/authorize` are rate limited in separate buckets.** One
  verification is one request to each, and a single bucket charged an ordinary
  flow twice: `LOGIN_RATE_LIMIT=1` refused the callback of the one attempt it
  had just allowed, and the default of 10 permitted five verifications a
  minute rather than ten.
- **`TRUSTED_PROXY_COUNT=0` now means what it says.** The floor used to be 1,
  so an operator writing 0 to say "nothing is in front of me" was clamped back
  up to trusting one hop of `X-Forwarded-For`, which anybody reaching the
  published port can send. Zero now installs no `ProxyFix` at all and believes
  no forwarded header.
- **The un-star check no longer strips the role from everybody on a newly
  opened page.** Stargazers come back oldest first, so a star that lands on a
  full final page opens a new one without changing the page before it: the
  ETag still matched, GitHub answered 304, and the cached "no next page" ended
  the walk one page early, which read as though everyone on the new page had
  un-starred. A full final page is no longer cached.
- **A star webhook delivery that failed can be redelivered immediately.** The
  receiver claims a delivery id before acting on it and drops a repeat of a
  claimed id, but a delivery whose database write was refused never completed,
  so the claim is now released and the redelivery is treated as new work
  instead of being answered `Already handled.` for ten minutes, which is the
  documented recovery from exactly that failure.
- **A star webhook delivery that arrives behind a newer one no longer
  overwrites it.** Deliveries are not ordered, so an old `deleted` could land
  after a new `created` and take back a role the member had just earned. Such
  a delivery is now refused and answered 202 with `Superseded by a newer
  event.`, leaving the row holding the newer state. Whether the star state
  moved is also decided by the write itself rather than by a read taken a
  moment earlier, so two deliveries racing cannot queue a role change for the
  older of the two events.
- **The sweep no longer writes stale state over a webhook's.** A cycle over a
  large repository takes minutes, and a star recorded during the crawl used to
  be overwritten when the sweep reached that row, leaving a member who had
  starred with no role and a database that agreed with itself for ever after.
  Every write the sweep makes is now conditional on no newer star event having
  reached the row, and a refused write hands the row to the role-sync drain to
  reconcile.
- **A GitHub account linked by a very old version can no longer be linked a
  second time.** The check for an existing link looked only at the numeric
  account id, which the oldest rows do not carry, so the same GitHub account
  could take a second row under another Discord ID until the original member
  happened to re-verify.
- The legacy-secret purge, the schema upgrade and the two index creations at
  startup are now attempted independently. They shared one `try`, and the
  pairing was the worst available: a collection carrying rows from the version
  keyed on the GitHub email raises on the unique `discord_id` index, which
  took the purge of that same version's stored OAuth tokens down with it. Each
  step now reports itself by name, as `Could not index the users collection`,
  `Could not index the deliveries collection`, `Could not purge credentials
  written by older versions` or `Could not upgrade user records`.
- `/checkstars` no longer fails to answer when a sweep removed enough roles
  for the list of names to exceed Discord's 2000 character limit. The list is
  shortened with an exact count of what was left out; the roles had already
  been removed, so failing the reply reported work that did happen as a
  failure.
- The primary GitHub rate limit is no longer retried. It wears a retryable
  status but takes up to an hour to clear, so the bot slept out three
  `Retry-After` waits before reporting the exhaustion it could have reported
  at once.
- `SERVER_BIND_PORT` and `BOT_HEALTH_PORT` are validated as TCP ports and
  refused by name when they are outside 1 to 65535. A clamped port is a
  different address rather than a smaller value, and `BOT_HEALTH_PORT=70000`
  used to kill the bot outright with an `OverflowError` from `bind`, over an
  endpoint that is optional.
- A queued role change for a row with no Discord ID, which only the version
  keyed on the GitHub email could have written, is now taken off the queue by
  its database id. The flag could not be lowered by a Discord ID the row does
  not have, so the same line was logged every thirty seconds for the life of
  the process.
- `/starcount` no longer raises a `TypeError` when GitHub rate-limits the
  request.
- The periodic star check no longer dies silently when a verified member has
  left the guild; it now catches and logs errors, backs off, and keeps a
  reference to its task so it cannot be garbage collected mid-run.
- The application binds port 5000 inside the container consistently with the
  compose port mapping; previously any `SERVER_PORT` other than 5000 was
  unreachable.
- The custom links command is skipped when unconfigured instead of
  registering a command named "None" and crashing on buttons with empty
  URLs.
- `remove_role` is no longer called on members who do not hold the role.
- **Renaming your GitHub account no longer costs you the role.** The periodic
  check compared the stored GitHub login against the logins in the stargazer
  listing, and a login is not immutable: anybody who renamed their account
  stopped matching and had the role taken away on the next cycle, despite
  never having un-starred. The comparison is now on the numeric GitHub
  account id, which cannot change. Rows written by much older versions have
  no id stored and still fall back to the login, because one cannot be
  derived from a login without another API call; they are corrected the next
  time that member verifies.

### Security

- **Claiming the role now requires a link made for the repository you have
  configured.** The claim button read only `starred_repo`, which proved that
  somebody had starred *some* repository at some point, so an operator who
  repointed `REPO_OWNER` or `GITHUB_REPO` while keeping the database silently
  handed the new role to everyone who had starred the **old** one, none of
  whom had starred the new one, with nothing in the logs to show it. Each row
  records the repository it was created against and the two are now compared.
  **The necessary consequence: after changing either variable, every member
  must run `/verify` and sign in again before they can claim.** Nothing is
  deleted or rewritten, so restoring the old values restores the old rows.
- **The OAuth server's port is now published on the loopback interface only**,
  as `127.0.0.1:${SERVER_PORT}:${SERVER_BIND_PORT}`, in both compose files.
  Published on every interface, a client could skip the reverse proxy, reach
  `ProxyFix` directly and hand itself any `X-Forwarded-For` it liked, taking a
  fresh rate-limit bucket for every request; it also left an origin speaking
  plain HTTP open to anything that could route to it. A proxy on the same host
  is unaffected. **If your reverse proxy runs on another machine, this breaks
  your deployment until you remove the `127.0.0.1:` from the mapping** and set
  `TRUSTED_PROXY_COUNT` to your real hop count.
- `GITHUB_WEBHOOK_SECRET` is now named in the redaction guidance, in
  `.env.example`, the environment reference and the troubleshooting guide. It
  is the only thing authenticating a star event, so anybody holding it can
  forge one for any account, and it was missing from the list of values to
  remove before pasting logs into an issue while the guide asked operators to
  print it.
- Reduced the requested GitHub OAuth scope from `repo` to `read:user`. The
  old scope granted read and write access to every private repository a
  verifying user owns.
- Stopped storing the GitHub OAuth access token. It was previously written to
  MongoDB in cleartext, which combined with the `repo` scope made the
  collection a set of credentials for every verified user; nothing ever read
  it back. Existing installations purge any previously stored tokens at
  startup, and see [SECURITY.md](./SECURITY.md) for what operators upgrading
  from an older version should do. The purge removes your copy but does not
  revoke anything, and GitHub's revocation endpoint needs the token itself,
  so take a copy before the first start if you intend to revoke through the
  API rather than asking every member to do it.
- Stopped logging the access token and the verifying user's email address.
- `.env.example` is no longer baked into the Docker images, which previously
  shipped a publicly known `SECRET_KEY` as the runtime default.

[1.0.0]: https://github.com/fuegovic/Starguard/releases/tag/v1.0.0
