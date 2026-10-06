# Security

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for anything that could be exploited before it is fixed.

Include the pg_debug and PostgreSQL versions, the access mode (`unrestricted`
or `restricted`), the command or API call, and what happened. A fix or a
response explaining why none is needed follows within two weeks.

## What counts

pg_debug connects to a database with whatever privileges its connection URL
carries, and in the default `unrestricted` mode runs any statement it is
given. That is by design and not a vulnerability. These are:

- `restricted` mode running a statement that is not read-only, or escaping the
  read-only transaction and statement timeout it promises.
- Any way for input pg_debug analyses (query text, plan output,
  `pg_stat_statements` entries, migration files) to make it run SQL that was
  not asked for.
- Credentials from the connection URL appearing in output, logs or error
  messages. The driver redacts the password; a path that bypasses it is a bug.

## Supported versions

Only the latest release on PyPI receives fixes.
