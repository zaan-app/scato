# scato

[![PyPI version](https://img.shields.io/pypi/v/scato.svg)](https://pypi.org/project/scato/)
[![Python versions](https://img.shields.io/pypi/pyversions/scato.svg)](https://pypi.org/project/scato/)
[![MPL 2.0 license](https://img.shields.io/badge/License-MPL%202.0-blue.svg)](https://www.mozilla.org/en-US/MPL/2.0/)
[![CI](https://github.com/ameerfayiz/scato/actions/workflows/ci.yml/badge.svg)](https://github.com/ameerfayiz/scato/actions/workflows/ci.yml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**scato** offers accurate and fast checks for email address and username usage on online platforms.

Given an email address or username, it tells you whether it is **available**, **taken** or **invalid** on a range of online platforms.

> ### About this fork
>
> This is a maintained continuation of [`iojw/socialscan`](https://github.com/iojw/socialscan), which was archived by its author. By then most of its platform checks had stopped working, because the sign-up endpoints they relied on had been changed, retired or put behind bot protection.
>
> This fork repairs those checks, replaces the ones that could not be repaired, and drops the ones the platforms no longer expose at all. See [What changed in this fork](#what-changed-in-this-fork).
>
> The import name is `scato`, not `socialscan`, so calls into the library need updating: `from socialscan.util import ...` becomes `from scato.util import ...`. In exchange, nothing collides with the original package any more — the two can be installed side by side.

## Features

1. **Accuracy**: scato queries the platforms' registration endpoints directly, retrieving the appropriate CSRF tokens, headers and cookies. This avoids the false positives and negatives that come from guessing at profile pages. See [Accuracy](#accuracy) for the two platforms where this is no longer possible.

2. **Speed**: all queries run concurrently with [asyncio](https://docs.python.org/3/library/asyncio.html) and [aiohttp](https://aiohttp.readthedocs.io/en/stable/), so bulk checks stay fast. A 24-query run against every supported platform completes in about 1.2 seconds.

3. **Library / CLI**: use it from the command line, or import it into existing code.

4. **Email support**: both email addresses and usernames can be queried.

## Supported platforms

|           | Username | Email | Method |
|:---------:|:--------:|:-----:|:-------|
| Instagram |    ✔️     |  ✔️   | Registration endpoint |
| Twitter   |    ✔️     |  ✔️   | Registration endpoint |
| Lastfm    |    ✔️     |       | Registration endpoint |
| GitLab    |    ✔️     |       | Registration endpoint |
| Reddit    |    ✔️     |       | Registration endpoint |
| Firefox   |          |  ✔️   | Registration endpoint |
| GitHub    |    ✔️     |       | Account page (see [Accuracy](#accuracy)) |
| Tumblr    |    ✔️     |       | Account page (see [Accuracy](#accuracy)) |

## Installation

### pip
```
> pip install scato
```

### Install from source
```
> git clone https://github.com/ameerfayiz/scato.git
> cd scato
> pip install .
```

## Usage

The CLI is installed as `scato`.

```
usage: scato [list of usernames/email addresses to check]

positional arguments:
  query                 one or more usernames/email addresses to query (email addresses
                        are automatically queried if they match the format)

options:
  -h, --help            show this help message and exit
  --platforms [platform ...], -p [platform ...]
                        list of platforms to query (default: all platforms)
  --view-by {platform,query}
                        view results sorted by platform or by query (default: query)
  --available-only, -a  only print usernames/email addresses that are available and not
                        in use
  --cache-tokens, -c    cache tokens for platforms requiring more than one HTTP request
                        (Instagram & Lastfm), reducing total number of requests sent
  --input input.txt, -i input.txt
                        file containing list of queries to execute
  --proxy-list proxy_list.txt
                        file containing list of HTTP proxy servers to execute queries
                        with
  --verbose, -v         show query responses as they are received
  --show-urls           display profile URLs for usernames on supported platforms
                        (profiles may not exist if usernames are reserved or belong to
                        deleted/banned accounts)
  --json json.txt       output results in JSON format to the specified file
  --debug               output debug messages
  --version             show program's version number and exit
```

Example:

```
> scato social jsndiwimw --show-urls

----------------------------------------
               jsndiwimw
----------------------------------------
GitHub
GitLab
Lastfm
Tumblr
Twitter
----------------------------------------
                 social
----------------------------------------
GitHub - https://github.com/social
GitLab - https://gitlab.com/social
Lastfm - https://www.last.fm/user/social
Tumblr - https://social.tumblr.com
Twitter - https://twitter.com/social

Available, Taken/Reserved, Invalid, Error
```

## As a library

scato can be imported into existing code and used as a library.

The async method `execute_queries` and its synchronous wrapper `sync_execute_queries` take a list of queries and an optional list of platforms and proxies, execute all queries concurrently, and return the results in the same order.

```python
from scato.platforms import Platforms
from scato.util import sync_execute_queries

queries = ["jsndiwimw", "social", "admin@mozilla.com"]
platforms = [Platforms.GITHUB, Platforms.LASTFM, Platforms.FIREFOX]
results = sync_execute_queries(queries, platforms)
for result in results:
    print(f"{result.query} on {result.platform}: {result.message} "
          f"(Success: {result.success}, Valid: {result.valid}, Available: {result.available})")
```

Output:
```
jsndiwimw on GitHub: Available (Success: True, Valid: True, Available: True)
jsndiwimw on Lastfm: Ok, that username can be yours! (Success: True, Valid: True, Available: True)
social on GitHub: Unavailable (Success: True, Valid: True, Available: False)
social on Lastfm: Sorry, this username isn't available. (Success: True, Valid: True, Available: False)
admin@mozilla.com on Firefox: Unavailable (Success: True, Valid: True, Available: False)
```

A query is only sent to platforms that support its type, so a username is skipped for email-only platforms and vice versa. Each result is a `PlatformResponse` with `platform`, `query`, `available`, `valid`, `success`, `message` and `link` fields.

### Interpreting a result

| | Meaning |
|:--|:--|
| `success=False` | The platform did not answer the question. `message` says why — usually rate limiting. Retry later or use `--proxy-list`. |
| `valid=False` | The query is not a legal username/email on that platform. |
| `available=True` | Free to register. |
| `available=False` | Taken or reserved. |

Always check `success` before trusting `available`.

## Text file input

For bulk queries with the `--input` option, place one username/email on each line:
```
username1
email2@mail.com
username3
```

## Accuracy

Most tools check username availability by requesting the profile page of the username in question and reading the HTTP status code or error text. This is a naive approach that fails in two ways:

- **Reserved keywords**: platforms reserve a set of names that cannot be registered even though no profile page exists for them (try checking `admin`, `home` or `root` against other services).
- **Deleted/banned accounts**: these usernames stay unavailable even when the profile page is gone.

scato avoids this by querying registration endpoints directly, which is what makes its answers reliable.

**Two platforms are exceptions.** GitHub's sign-up form is behind a bot-protection challenge, and `tumblr.com` refuses connections from non-browser clients, so neither registration endpoint is reachable any more. For these two, scato validates the username format locally and then checks the account page. Reserved keywords are still reported correctly on both, since each serves or redirects those paths rather than returning a 404 (of 26 reserved GitHub words tested, 24 report correctly). A username freed by a deleted account, however, will read as available.

### Rate limiting

These are third-party endpoints with their own limits, and some block datacenter IP ranges outright. When a platform throttles or blocks a query, the result comes back with `success=False` and a message saying so, rather than a wrong answer. Use `--proxy-list` or wait before retrying.

## What changed in this fork

| Platform | Change |
|:--|:--|
| Firefox | Fixed — the API rejects browser user agents with `406`, so a plain one is now sent |
| Lastfm | Fixed and re-enabled — same user-agent issue; was present but unregistered upstream |
| GitHub | Username check moved to the account page; email check removed (sign-up form is bot-protected) |
| Tumblr | Username check moved to the blog subdomain; email check removed (`tumblr.com` refuses connections) |
| Reddit | Switched to the `username_available.json` endpoint; `check_username.json` is retired |
| Instagram | Token now read from the sign-up page, which does not reject the user agent |
| Pinterest | **Removed** — `EmailExistsResource` no longer exists and the replacement requires authentication |
| Snapchat, Yahoo | Remain unregistered and non-functional; their endpoints are gone |

Also in this release:

- Per-platform HTTP headers, so one platform's requirements no longer override another's.
- Concurrent queries against a platform now share a single token prerequest instead of sending one each — a bulk run costs one extra request per platform, not one per query.
- Rate limiting and blocking are reported distinctly from wrong answers.
- Added request timeouts, and widened error handling to cover timeouts and malformed responses.
- Fixed the licence declared in package metadata, which said MIT while the project is MPL-2.0.
- Dropped Python 3.6/3.7/3.8 support (all end-of-life); the minimum is now 3.9.

## Development

```
> pip install -e ".[dev]"
> pytest -m "not network"   # offline tests
> pytest                    # includes live platform queries
```

The live tests query real platforms. When a platform blocks or throttles the runner, those tests **skip** rather than fail, so a genuine regression stays distinguishable from a platform-side block.

## Contributing

Errors, suggestions, or want a platform added? [Submit an issue](https://github.com/ameerfayiz/scato/issues). PRs are welcome.

## Credits

Originally written by [Isaac Ong](https://github.com/iojw) as [socialscan](https://github.com/iojw/socialscan). This fork continues that work; all credit for the original design and implementation is his.

## License

[Mozilla Public License 2.0](LICENSE), unchanged from the original project.
