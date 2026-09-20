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

CI runs five jobs on every pull request, and on pushes to `main`. Running
them first saves a round trip. Each command below is the one `ci.yml` runs,
so this list is the whole gate rather than the convenient half of it.

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

**Docker build**, which lints each Dockerfile and then builds it. This is
the one job that needs tools the checks above do not: hadolint, and a Docker
daemon you can reach.

```sh
hadolint --config .hadolint.yaml Dockerfile.bot Dockerfile.server
docker build -f Dockerfile.bot -t starguard-bot:ci .
docker build -f Dockerfile.server -t starguard-server:ci .
```

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

## Pull requests

- Keep each pull request focused on one change; unrelated cleanup makes it
  harder to review.
- Run the checks above before opening the pull request.
- Describe what changed and why, and call out anything a reviewer should pay
  particular attention to, such as a database migration or a changed
  environment variable.
- Update `.env.example` and the docs under `docs/` when you add, rename, or
  remove a configuration variable.
