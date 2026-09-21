# Security Policy

## Supported Versions

Only the newest release receives security fixes. It is what the `:latest`
image tag points at, and what `docker compose pull && docker compose up -d`
gives you.

Fixes land on `main` first and are published there immediately as the `:main`
tag and the commit sha, so an urgent fix can be run before the release that
contains it is cut. There is no long-term support branch: an older `vX.Y`
tag keeps working and keeps its images, but it does not get patched.

## Verifying what you are running

Both images are signed at build time with
[cosign](https://docs.sigstore.dev/), keyless, and carry an SBOM and a build
provenance attestation. There is no signing key held in this repository: the
signature is bound to the identity of the workflow that produced the image
and is recorded in the public Rekor transparency log.

```sh
cosign verify ghcr.io/librechat-ai/starguard-bot:latest \
  --certificate-identity-regexp \
    '^https://github.com/LibreChat-AI/Starguard/.github/workflows/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The identity is the repository as GitHub spells it, capitals included, which
is not the same rule as the image path: a container reference has no
uppercase so the workflow lowercases the owner for `ghcr.io/...`, while the
certificate records the workflow's own URL unchanged. Running this under a
differently spelled owner means editing the regexp to match that spelling.

A successful verification establishes that the image came from a workflow in
this repository and names the commit it was built from. It says nothing about
whether that commit is free of defects; it rules out substitution between the
build and your pull, and nothing else.

The dependency chain underneath is pinned rather than trusted: both images
install from `requirements.lock` with `pip --require-hashes`, so a tampered
wheel or a compromised index fails the build, and both base images are pinned
by digest. CI additionally audits both lockfiles against the advisory
database and scans each built image with Trivy on every pull request.

## Reporting a Vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub Security Advisories: open the Security
tab on this repository and choose "Report a vulnerability". If that is not
available to you, contact the maintainer directly at
`<security contact email, to be filled in by the maintainer>`.

Include the affected component (Discord bot or OAuth server), reproduction
steps, and the potential impact. Expect an initial response within 5
business days, and a confirmation or resolution within 30 days of that.

## Known Historical Issue

Releases before the security hardening on this branch requested the OAuth
`repo` scope, which grants read and write access to every private repository
the verifying user owns, and stored the resulting GitHub access token in
MongoDB in cleartext. Neither is true of the current code: it now requests
`read:user` only, discards the access token once the request that needs it
completes, and purges any previously stored tokens at startup.

If you operated an earlier version of Starguard:

- Revoke the tokens. Which action actually does that is easy to get wrong,
  so it has its own section below.
- Upgrade to a version that includes the startup token purge so any
  cleartext tokens already in your database are removed. **Take a copy of
  the tokens before that first start** if you intend to revoke them through
  the API, because the purge deletes the only copy you hold.
- Rotate `SECRET_KEY` if you have reason to believe your database was
  read by someone else before the purge ran.

### Revoking the tokens

Every stored token was issued to **your** OAuth app on behalf of **one
member's** GitHub account, so revocation is per account. Three things that
look as though they would settle it at a stroke do not:

- <https://github.com/settings/connections/applications> lists only the apps
  the signed-in account has authorized. Opening it revokes your own grant and
  nobody else's, which is what earlier versions of this file told you to do.
- An organization's authorized OAuth apps page governs that app's access to
  the organization. It does not reach the personal tokens members hold.
- Resetting the client secret stops new authorization codes being exchanged,
  and breaks the API call below until you update `GITHUB_CLIENT_SECRET`.
  GitHub does not document it as invalidating tokens already issued, so do
  not treat it as a revocation.

GitHub gives an app owner no single action that revokes every token its app
issued. The REST API works one account at a time, through
[`DELETE /applications/{client_id}/grant`](https://docs.github.com/en/rest/apps/oauth-applications#delete-an-app-authorization),
which "will also delete all OAuth tokens associated with the application for
the user" but takes that user's access token as a required input. So there
are two ways to revoke and one way out:

1. **Revoke them yourself, from a copy of the tokens taken before the
   purge.** The only option that is both complete and under your control,
   and it stops being available the moment the purge runs. The commands are
   in [step 1 of the upgrade
   guide](docs/installation.md#1-revoke-the-oauth-tokens-the-old-version-stored).
2. **Ask every verified member to revoke, one by one.** If the purge has
   already run, this is the answer: you no longer hold the tokens, GitHub
   offers no way to revoke without them, and nobody but the member can act.
   Send each member
   `https://github.com/settings/connections/applications/<your client id>`,
   which opens your app's page inside their own account. You cannot do it for
   them and GitHub will not tell you who has.
3. **Delete the OAuth app.** The last resort, for when you cannot reach
   everyone: Settings, Developer settings, OAuth apps, your app, Advanced,
   Delete application. GitHub documents the steps but not what becomes of
   tokens already issued, so treat this as removing the app rather than as a
   guaranteed revocation. It costs every member a re-verification, because
   you then create a new app and put its `GITHUB_CLIENT_ID` and
   `GITHUB_CLIENT_SECRET` in `.env`.

The honest summary: unless you saved the tokens before upgrading, every
affected member has to revoke individually, and you can neither do it for
them nor confirm that they did.
