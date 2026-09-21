# Contributing to Starguard

## Development setup

```sh
git clone https://github.com/fuegovic/Starguard.git
cd Starguard
pip install --require-hashes -r requirements-dev.lock
cp .env.example .env   # fill in real values before running the app
```

Run the two processes directly:

```sh
python -m bot.bot
python -m server.server
```

or through Docker: see [docs/installation.md](./docs/installation.md).

## Running the checks locally

CI runs five jobs on every pull request. The same five run on `main`, where
`release.yml` calls `ci.yml` as its gate before anything is released or
published. Running them first saves a round trip. Each command below is the
one `ci.yml` runs, so this list is the whole gate rather than the convenient
half of it.

**Quality**, the lint, format and type job:

```sh
ruff check .
ruff format --check .
mypy .
pylint $(git ls-files '*.py')
```

**Security**, which is bandit plus an audit of each lockfile. Both audits
matter: they cover the full transitive closure, which is where most
advisories land, and neither is implied by the other.

```sh
bandit -r bot common server
pip-audit -r requirements.lock --require-hashes --disable-pip
pip-audit -r requirements-dev.lock --require-hashes --disable-pip
```

**Test**, with coverage. This is the one where a plain `pytest -q` misleads
you: `pyproject.toml` sets `fail_under = 100` on branch coverage of `bot`,
`common` and `server`, and that gate only runs when coverage does, which
means passing `--cov`. Warnings are errors as well, so a deprecation counts
as a failure.

```sh
pytest -q --cov --cov-report=term-missing
```

CI also passes `--cov-report=xml`, which only writes a file, and runs the
job twice, on Python 3.11 and on 3.12. A failure that depends on the Python
version will not show up locally unless you run both.

**Lockfile drift**, which regenerates both locks and fails if either one
moved. Run the two `uv pip compile` commands from [The
lockfiles](#the-lockfiles) below, then:

```sh
git diff --exit-code -- requirements.lock requirements-dev.lock
```

**Docker build**, which lints each Dockerfile, builds it, and scans the
result for known vulnerabilities. This is the one job that needs tools the
checks above do not: hadolint, trivy, and a Docker daemon you can reach.

```sh
hadolint --config .hadolint.yaml Dockerfile.bot Dockerfile.server
docker build -f Dockerfile.bot -t starguard-bot:ci .
docker build -f Dockerfile.server -t starguard-server:ci .
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 starguard-bot:ci
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 starguard-server:ci
```

`--ignore-unfixed` is not a way of looking away: it is what keeps the job
actionable. A HIGH in the Debian base layer with no fixed version published
yet cannot be resolved from this repository, and failing every pull request
on it teaches people to skip the job. One that *is* fixed upstream is a base
image digest bump away, and blocks.

To build the way an operator runs it, through compose rather than by hand:

```sh
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Without that second file compose pulls the published images and your local
changes have no effect, which is the first thing to check when a code change
appears to do nothing.

## The lockfiles

`requirements.txt` and `requirements-dev.txt` are the human-edited sources,
with comments explaining why each dependency is there and, where relevant,
why it is pinned to a particular version. `requirements.lock` and
`requirements-dev.lock` are generated from them: every package's hash is
pinned, including transitive dependencies neither `.txt` file lists (for
example `astroid`, which `requirements-dev.txt` never mentions but which
comes in under `pylint`), so a build or a dev install fails rather than
silently pulling in something else. Do not hand edit either lock.

`requirements.lock` is what the Docker images install; `requirements-dev.lock`
is what CI and the setup command above install. Regenerate the matching lock
with [uv](https://docs.astral.sh/uv/) any time you change the corresponding
`.txt` file, including after merging a Dependabot PR that bumps one:

```sh
uv pip compile requirements.txt --generate-hashes --python-version 3.11 -o requirements.lock
uv pip compile requirements-dev.txt --generate-hashes --python-version 3.11 -o requirements-dev.lock
```

Both compiled against 3.11 because that is the lowest Python version CI
tests, so the result installs on every version in the matrix. Commit each
lock together with the `.txt` file it came from; CI's lockfile drift check
regenerates both the same way and fails the build if either result does not
match what you committed.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/): a type
prefix (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `ci`, and so on),
an optional scope, and a short imperative summary, for example
`fix(server): validate the link token before creating a session`. No emoji in
commit messages.

The type is not decoration. release-please reads it to decide the next
version and which heading the commit lands under in `CHANGELOG.md`:

| Commit | Version | CHANGELOG heading |
| --- | --- | --- |
| `fix: ...` | patch | Fixed |
| `feat: ...` | minor | Added |
| `perf:`, `refactor:`, `revert:` | patch | Changed |
| `docs: ...` | patch | Documentation |
| `deps: ...` | patch | Dependencies |
| `chore:`, `ci:`, `test:`, `style:`, `build:` | none | not shown |
| any type with `!`, or a `BREAKING CHANGE:` footer | major | ! Breaking |

A commit whose type is in the last-but-one row changes nothing a user can
see, so it is deliberately invisible in the release notes and does not on
its own justify a release. The full mapping lives in
`release-please-config.json`.

## Releases

Releases are cut by [release-please](https://github.com/googleapis/release-please),
and nobody pushes a tag by hand.

1. You merge an ordinary pull request into `main`.
2. `release.yml` runs the CI gate, then release-please opens (or updates) a
   pull request titled `chore(main): release X.Y.Z`. It contains exactly two
   changes: the new `CHANGELOG.md` section, written from the commit messages
   since the last release, and the bumped `version.txt`. Nothing is tagged
   and nothing is published yet, so this pull request can sit open for as
   long as you like, collecting further merges and re-computing its version.
3. The images for that commit are published as `:main` and as the commit
   sha, so `main` is always runnable.
4. When you want the release, you merge that pull request. release-please
   drafts the GitHub release, the images are built, signed and published as
   `vX.Y.Z`, `vX.Y` and `latest`, and the release is taken out of draft
   last. Publishing the draft is also what creates the `vX.Y.Z` tag, since
   GitHub holds the tag back while a release is a draft.

That order is deliberate. A release names image tags that an operator is
about to pull, so it must not exist before they do. If a build or a
signature fails, the release stays a draft, no tag is created, and the
previous release is still the newest thing anybody can find; re-running the
failed jobs promotes the same draft once the images are there. A draft you
decide to abandon has to be deleted by hand.

Tags move only after every image has been built and signed. Each image is
pushed by digest first, which publishes the layers under no tag at all,
then signed, and only then do `:main`, `:latest` and the version tags move
onto that digest. So a failed build, a failed signature, or a bot image
that built while the server image did not all leave every tag pointing at
the last release that completed, rather than at something half-published.

Two things about the first release, because neither is recoverable from
reading the workflow. `.release-please-manifest.json` records the version
already released rather than the version to release next, so the `1.0.0` in
it means release-please starts *after* 1.0.0 and the first release it cuts
carries the next version up. And because `latest` follows releases rather
than `main`, it does not exist until that first release pull request has
been merged; a deployment standing up before then pins
`STARGUARD_IMAGE_TAG=main`.

Reviewing the release pull request is the whole point of the mechanism: the
version it proposes is derived from the commit types, so a `feat:` that
should have been a `fix:` shows up there as a minor bump you can still
correct. To override the computed version for one release, put a
`Release-As: 2.0.0` footer in a commit on `main`.

Two things to know about the release pull request. GitHub does not run
workflows on a pull request opened by `GITHUB_TOKEN`, so it arrives without
checks; it only ever touches `CHANGELOG.md` and `version.txt`, and the
commit it is based on was gated before release-please ran. And a prerelease
(`1.2.0-rc.1`) publishes its own version tags but deliberately leaves
`:latest` on the last stable release, so nobody running `docker compose
pull` is moved onto a release candidate.

## Pull requests

- Keep each pull request focused on one change; unrelated cleanup makes it
  harder to review.
- Run the checks above before opening the pull request.
- Describe what changed and why, and call out anything a reviewer should pay
  particular attention to, such as a database migration or a changed
  environment variable.
- Update `.env.example` and the docs under `docs/` when you add, rename, or
  remove a configuration variable.
