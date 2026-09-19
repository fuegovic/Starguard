# Security Policy

## Supported Versions

Starguard does not yet publish tagged releases. Security fixes land on
`main` and ship in the images built from it, so running the latest `main` (or
the latest `:latest` / commit-SHA tagged image) is what stays supported. Once
tagged releases begin, this section will list which of them still receive
fixes.

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

- Revoke the OAuth token Starguard was issued, from
  https://github.com/settings/connections/applications (or your
  organization's authorized OAuth apps page if the repository it verifies
  against is organization owned).
- Upgrade to a version that includes the startup token purge so any
  cleartext tokens already in your database are removed.
- Rotate `SECRET_KEY` if you have reason to believe your database was
  read by someone else before the purge ran.
