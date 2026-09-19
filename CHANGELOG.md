# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Fixed

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

- Reduced the requested GitHub OAuth scope from `repo` to `read:user`. The
  old scope granted read and write access to every private repository a
  verifying user owns.
- Stopped storing the GitHub OAuth access token. It was previously written to
  MongoDB in cleartext, which combined with the `repo` scope made the
  collection a set of credentials for every verified user; nothing ever read
  it back. Existing installations purge any previously stored tokens at
  startup, and see [SECURITY.md](./SECURITY.md) for what operators upgrading
  from an older version should do.
- Stopped logging the access token and the verifying user's email address.
- `.env.example` is no longer baked into the Docker images, which previously
  shipped a publicly known `SECRET_KEY` as the runtime default.

[Unreleased]: https://github.com/fuegovic/Starguard/compare/main...HEAD
