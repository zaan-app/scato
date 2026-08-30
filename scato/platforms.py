# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import abc
import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum

import aiohttp

from scato import __version__


class QueryError(Exception):
    pass


class UsernameQueryable(metaclass=abc.ABCMeta):
    """Abstract class for platforms that can query usernames."""

    @abc.abstractmethod
    async def check_username(self, username):
        raise NotImplementedError


class EmailQueryable(metaclass=abc.ABCMeta):
    """Abstract class for platforms that can query email addresses."""

    @abc.abstractmethod
    async def check_email(self, email):
        raise NotImplementedError


class PrerequestRequired(metaclass=abc.ABCMeta):
    """Abstract class for platforms that require a pre-request to retrieve a token,
    for use in the main query. This request is sent once and cached for future
    queries."""

    @abc.abstractmethod
    async def prerequest(self):
        raise NotImplementedError

    async def get_token(self):
        """
        Retrieve and return platform token using the `prerequest` method specified in the class

        The lock keeps concurrent queries to a single prerequest: the first caller sends it while
        the rest wait for its result, so a bulk query costs one extra HTTP request per platform
        rather than one per query. -c only moves that request to before the main queries.
        """
        async with self.prerequest_lock:
            if not self.prerequest_sent:
                self.token = await self.prerequest()
                self.prerequest_sent = True
                logging.debug(f"TOKEN {self.__class__.__name__}: {self.token}")
        if self.token is None:
            raise QueryError(BasePlatform.TOKEN_ERROR_MESSAGE)
        return self.token


class BasePlatform:
    # Default user agent taken from `odeialba/instagram-php-scraper`
    # https://github.com/odeialba/instagram-php-scraper/blob/39e8565e8446fa2c66dbcdee8807aa03fca2bbda/src/InstagramScraper/Instagram.php#L46
    DEFAULT_HEADERS = {
        "User-agent": "Mozilla/5.0 (Linux; Android 8.1.0; motorola one Build/OPKS28.63-18-3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/70.0.3538.80 Mobile Safari/537.36 Instagram 72.0.0.21.98 Android (27/8.1.0; 320dpi; 720x1362; motorola; motorola one; deen_sprout; qcom; pt_BR; 132081645)",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    }
    # Headers specific to a platform, merged over DEFAULT_HEADERS. Headers passed to an
    # individual request take precedence over both.
    HEADERS = {}
    UNEXPECTED_CONTENT_TYPE_ERROR_MESSAGE = "Unexpected content type {} (HTTP {}). You might be sending too many requests. Use a proxy or wait before trying again."
    TOKEN_ERROR_MESSAGE = "Could not retrieve token. You might be sending too many requests. Use a proxy or wait before trying again."
    TOO_MANY_REQUEST_ERROR_MESSAGE = "Requests denied by platform due to excessive requests. Use a proxy or wait before trying again."
    # Statuses returned when a platform is throttling or blocking us outright instead of
    # answering the query
    RATE_LIMITED_STATUSES = (403, 406, 429)
    CONNECT_TIMEOUT_DURATION = 10
    TIMEOUT_DURATION = 20

    client_timeout = aiohttp.ClientTimeout(connect=CONNECT_TIMEOUT_DURATION, total=TIMEOUT_DURATION)

    # 1: Be as explicit as possible in handling all cases
    # 2: Do not include any queries that will lead to side-effects on users (e.g. submitting sign up forms)
    # OK to omit checks for whether a key exists when parsing the JSON response. KeyError is handled by the parent coroutine.

    def response_failure(self, query, *, message="Failure"):
        return PlatformResponse(
            platform=Platforms(self.__class__),
            query=query,
            available=False,
            valid=False,
            success=False,
            message=message,
            link=None,
        )

    def response_available(self, query, *, message="Available"):
        return PlatformResponse(
            platform=Platforms(self.__class__),
            query=query,
            available=True,
            valid=True,
            success=True,
            message=message,
            link=None,
        )

    def response_unavailable(self, query, *, message="Unavailable", link=None):
        return PlatformResponse(
            platform=Platforms(self.__class__),
            query=query,
            available=False,
            valid=True,
            success=True,
            message=message,
            link=link,
        )

    def response_invalid(self, query, *, message="Invalid"):
        return PlatformResponse(
            platform=Platforms(self.__class__),
            query=query,
            available=False,
            valid=False,
            success=True,
            message=message,
            link=None,
        )

    def response_unavailable_or_invalid(self, query, *, message, unavailable_messages, link=None):
        if any(x in message for x in unavailable_messages):
            return self.response_unavailable(query, message=message, link=link)
        else:
            return self.response_invalid(query, message=message)

    def _request(self, method, url, **kwargs):
        proxy = (
            self.proxy_list[self.request_count % len(self.proxy_list)] if self.proxy_list else None
        )
        self.request_count += 1
        headers = {**BasePlatform.DEFAULT_HEADERS, **self.HEADERS, **kwargs.pop("headers", {})}
        return self.session.request(
            method, url, headers=headers, timeout=self.client_timeout, proxy=proxy, **kwargs
        )

    def post(self, url, **kwargs):
        logging.debug(f"POST {url}")
        return self._request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        logging.debug(f"GET {url}")
        return self._request("GET", url, **kwargs)

    def head(self, url, **kwargs):
        logging.debug(f"HEAD {url}")
        return self._request("HEAD", url, **kwargs)

    @staticmethod
    async def get_json(request):
        # Platforms answer with an HTML error page or an empty body instead of JSON when they
        # throttle us, and may omit the Content-Type header entirely
        content_type = request.headers.get("Content-Type", "")
        if not content_type.startswith("application/json"):
            if request.status in BasePlatform.RATE_LIMITED_STATUSES:
                raise QueryError(BasePlatform.TOO_MANY_REQUEST_ERROR_MESSAGE)
            raise QueryError(
                BasePlatform.UNEXPECTED_CONTENT_TYPE_ERROR_MESSAGE.format(
                    content_type or "none", request.status
                )
            )
        else:
            json = await request.json()
            logging.debug(f"JSON {request.url} {request.status}: {json}")
            return json

    @staticmethod
    async def get_text(request):
        text = await request.text()
        logging.debug(f"TEXT {request.url} {request.status}: {text}")
        return text

    def __init__(self, session, proxy_list=None):
        self.session = session
        self.proxy_list = proxy_list or []
        self.request_count = 0
        self.prerequest_sent = False
        self.prerequest_lock = asyncio.Lock()
        self.token = None


class Snapchat(BasePlatform, UsernameQueryable, PrerequestRequired):
    URL = "https://accounts.snapchat.com/accounts/login"
    ENDPOINT = "https://accounts.snapchat.com/accounts/get_username_suggestions"
    USERNAME_TAKEN_MSGS = ["is already taken", "is currently unavailable"]

    async def prerequest(self):
        async with self.get(Snapchat.URL) as r:
            """
            See: https://github.com/aio-libs/aiohttp/issues/3002
            Snapchat sends multiple Set-Cookie headers in its response setting the value of 'xsrf-token',
            causing the original value of 'xsrf-token' to be overwritten in aiohttp
            Need to analyse the header response to extract the required value
            """
            cookies = r.headers.getall("Set-Cookie")
            for cookie in cookies:
                match = re.search(r"xsrf_token=([\w-]*);", cookie)
                if match:
                    token = match.group(1)
                    return token

    async def check_username(self, username):
        token = await self.get_token()
        async with self.post(
            Snapchat.ENDPOINT,
            data={"requested_username": username, "xsrf_token": token},
            cookies={"xsrf_token": token},
        ) as r:
            # Non-JSON received if too many requests
            json_body = await self.get_json(r)
            if "error_message" in json_body["value"]:
                return self.response_unavailable_or_invalid(
                    username,
                    message=json_body["value"]["error_message"],
                    unavailable_messages=Snapchat.USERNAME_TAKEN_MSGS,
                )
            elif json_body["value"]["status_code"] == "OK":
                return self.response_available(username)

    # Email: Snapchat doesn't associate email addresses with accounts


class Instagram(BasePlatform, UsernameQueryable, EmailQueryable, PrerequestRequired):
    # The sign-up page sets the CSRF token cookie and, unlike the landing_info endpoint it
    # replaces, answers 200 rather than 400 'useragent mismatch' for the user agent we send
    URL = "https://www.instagram.com/accounts/emailsignup/"
    ENDPOINT = "https://www.instagram.com/accounts/web_create_ajax/attempt/"
    USERNAME_TAKEN_MSGS = [
        "This username isn't available.",
        "A user with that username already exists.",
    ]
    USERNAME_LINK_FORMAT = "https://www.instagram.com/{}"
    # Sent by the web sign-up form alongside the CSRF token
    HEADERS = {
        "X-Requested-With": "XMLHttpRequest",
        "X-IG-App-ID": "936619743392459",
        "Referer": URL,
    }

    async def prerequest(self):
        async with self.get(Instagram.URL) as r:
            if "csrftoken" in r.cookies:
                token = r.cookies["csrftoken"].value
                return token

    async def check_username(self, username):
        token = await self.get_token()
        async with self.post(
            Instagram.ENDPOINT, data={"username": username}, headers={"x-csrftoken": token}
        ) as r:
            json_body = await self.get_json(r)
            # Too many requests
            if json_body["status"] == "fail":
                return self.response_failure(username, message=json_body["message"])
            if "username" in json_body["errors"]:
                return self.response_unavailable_or_invalid(
                    username,
                    message=json_body["errors"]["username"][0]["message"],
                    unavailable_messages=Instagram.USERNAME_TAKEN_MSGS,
                    link=Instagram.USERNAME_LINK_FORMAT.format(username),
                )
            else:
                return self.response_available(username)

    async def check_email(self, email):
        token = await self.get_token()
        async with self.post(
            Instagram.ENDPOINT, data={"email": email}, headers={"x-csrftoken": token}
        ) as r:
            json_body = await self.get_json(r)
            # Too many requests
            if json_body["status"] == "fail":
                return self.response_failure(email, message=json_body["message"])
            if "email" not in json_body["errors"]:
                return self.response_available(email)
            else:
                message = json_body["errors"]["email"][0]["message"]
                if json_body["errors"]["email"][0]["code"] == "invalid_email":
                    return self.response_invalid(email, message=message)
                else:
                    return self.response_unavailable(email, message=message)


class GitHub(BasePlatform, UsernameQueryable):
    URL = "https://github.com"
    USERNAME_LINK_FORMAT = "https://github.com/{}"

    # GitHub's sign-up form sits behind a bot-protection challenge that answers 403 to every
    # request made here, so availability is read off the account page instead. Reserved names
    # are still reported correctly because GitHub serves or redirects their paths rather than
    # 404ing them, but a username freed by a deleted account reads as available.
    # Names are alphanumeric with single hyphens, up to 39 characters, and cannot start or
    # end with a hyphen.
    username_regex = re.compile(r"[A-Za-z\d](?:[A-Za-z\d]|-(?=[A-Za-z\d])){0,38}")

    async def check_username(self, username):
        # Custom matching required as GitHub validates the name in the browser before querying
        if not self.username_regex.fullmatch(username):
            return self.response_invalid(
                username,
                message="Username may only contain alphanumeric characters or single hyphens, "
                "cannot begin or end with a hyphen and is limited to 39 characters.",
            )
        link = GitHub.USERNAME_LINK_FORMAT.format(username)
        async with self.head(link, allow_redirects=False) as r:
            if r.status == 404:
                return self.response_available(username)
            elif r.status < 400:
                # 2xx for an account page, 3xx for a name GitHub reserves for its own pages
                return self.response_unavailable(username, link=link)
            elif r.status in BasePlatform.RATE_LIMITED_STATUSES:
                return self.response_failure(
                    username, message=BasePlatform.TOO_MANY_REQUEST_ERROR_MESSAGE
                )
            else:
                return self.response_failure(username, message=f"Unexpected status {r.status}")

    # Email: GitHub only discloses whether an address is in use through its sign-up form, which
    # is behind the same bot-protection challenge


class Tumblr(BasePlatform, UsernameQueryable):
    URL = "https://www.tumblr.com"
    USERNAME_LINK_FORMAT = "https://{}.tumblr.com"

    # tumblr.com drops connections from non-browser clients, taking its registration API with
    # it, so the blog subdomain - served separately and still answering - is queried instead.
    # Blog names are alphanumeric with hyphens, up to 32 characters, and cannot start with one.
    username_regex = re.compile(r"[A-Za-z\d][A-Za-z\d-]{0,31}")

    async def check_username(self, username):
        # Custom matching required as an invalid name is not a resolvable subdomain
        if not self.username_regex.fullmatch(username):
            return self.response_invalid(
                username,
                message="Blog names may only contain letters, numbers and hyphens, must begin "
                "with a letter or number and are limited to 32 characters.",
            )
        link = Tumblr.USERNAME_LINK_FORMAT.format(username)
        async with self.head(link, allow_redirects=False) as r:
            if r.status == 404:
                return self.response_available(username)
            elif r.status < 400:
                # 3xx for a blog that exists but requires a login to view
                return self.response_unavailable(username, link=link)
            elif r.status == 403:
                # Names Tumblr keeps for itself are refused rather than served
                return self.response_unavailable(username, message="Reserved", link=link)
            elif r.status in BasePlatform.RATE_LIMITED_STATUSES:
                return self.response_failure(
                    username, message=BasePlatform.TOO_MANY_REQUEST_ERROR_MESSAGE
                )
            else:
                return self.response_failure(username, message=f"Unexpected status {r.status}")

    # Email: the registration API that reported address usage is only reachable through
    # tumblr.com, which refuses our connections


class GitLab(BasePlatform, UsernameQueryable):
    URL = "https://gitlab.com/users/sign_in"
    ENDPOINT = "https://gitlab.com/users/{}/exists"
    USERNAME_LINK_FORMAT = "https://gitlab.com/{}"

    async def check_username(self, username):
        # Custom matching required as validation is implemented locally and not server-side by GitLab
        if not re.fullmatch(
            r"[a-zA-Z0-9_\.][a-zA-Z0-9_\-\.]*[a-zA-Z0-9_\-]|[a-zA-Z0-9_]", username
        ):
            return self.response_invalid(
                username, message="Please create a username with only alphanumeric characters."
            )
        async with self.get(
            GitLab.ENDPOINT.format(username), headers={"X-Requested-With": "XMLHttpRequest"}
        ) as r:
            # Special case for usernames
            if r.status == 401:
                return self.response_unavailable(
                    username, link=GitLab.USERNAME_LINK_FORMAT.format(username)
                )
            json_body = await self.get_json(r)
            if json_body["exists"]:
                return self.response_unavailable(
                    username, link=GitLab.USERNAME_LINK_FORMAT.format(username)
                )
            else:
                return self.response_available(username)

    # Email: GitLab requires a reCAPTCHA token to check email address usage which we cannot bypass


class Reddit(BasePlatform, UsernameQueryable):
    URL = "https://www.reddit.com"
    # Replaces check_username.json, which the sign-up form no longer uses
    ENDPOINT = "https://www.reddit.com/api/username_available.json"
    USERNAME_LINK_FORMAT = "https://www.reddit.com/u/{}"

    # Reddit rejects a malformed name in the browser rather than at this endpoint: 3-20
    # characters made up of letters, numbers, underscores and hyphens
    username_regex = re.compile(r"[A-Za-z\d_-]{3,20}")

    async def check_username(self, username):
        # Custom matching required as validation is implemented locally and not server-side
        if not self.username_regex.fullmatch(username):
            return self.response_invalid(
                username,
                message="Usernames must be between 3 and 20 characters and may only contain "
                "letters, numbers, underscores and hyphens.",
            )
        async with self.get(Reddit.ENDPOINT, params={"user": username}) as r:
            json_body = await self.get_json(r)
            # The endpoint answers with a bare boolean, and an object when it refuses the query
            if json_body is True:
                return self.response_available(username)
            elif json_body is False:
                return self.response_unavailable(
                    username, link=Reddit.USERNAME_LINK_FORMAT.format(username)
                )
            else:
                return self.response_failure(username, message=f"Unexpected response {json_body}")

    # Email: You can register multiple Reddit accounts under the same email address so not possible to check if an address is in use


class X(BasePlatform, UsernameQueryable, EmailQueryable):
    URL = "https://x.com/signup"
    USERNAME_ENDPOINT = "https://api.x.com/i/users/username_available.json"
    EMAIL_ENDPOINT = "https://api.x.com/i/users/email_available.json"
    # [account in use, account suspended]
    USERNAME_TAKEN_MSGS = ["That username has been taken", "unavailable"]
    USERNAME_LINK_FORMAT = "https://x.com/{}"

    async def check_username(self, username):
        async with self.get(X.USERNAME_ENDPOINT, params={"username": username}) as r:
            json_body = await self.get_json(r)
            message = json_body["desc"]
            if json_body["valid"]:
                return self.response_available(username, message=message)
            else:
                return self.response_unavailable_or_invalid(
                    username,
                    message=message,
                    unavailable_messages=X.USERNAME_TAKEN_MSGS,
                    link=X.USERNAME_LINK_FORMAT.format(username),
                )

    async def check_email(self, email):
        async with self.get(X.EMAIL_ENDPOINT, params={"email": email}) as r:
            json_body = await self.get_json(r)
            message = json_body["msg"]
            if not json_body["valid"] and not json_body["taken"]:
                return self.response_invalid(email, message=message)

            if json_body["taken"]:
                return self.response_unavailable(email, message=message)
            else:
                return self.response_available(email, message=message)


class Lastfm(BasePlatform, UsernameQueryable, PrerequestRequired):
    URL = "https://www.last.fm/join"
    ENDPOINT = "https://www.last.fm/join/partial/validate"
    USERNAME_TAKEN_MSGS = ["Sorry, this username isn't available."]
    USERNAME_LINK_FORMAT = "https://www.last.fm/user/{}"
    # Last.fm rate limits browser user agents hard enough to answer 406 to the join page
    HEADERS = {"User-agent": f"scato/{__version__}"}

    tag_regex = re.compile(r"<[^>]+>")

    async def prerequest(self):
        async with self.get(Lastfm.URL) as r:
            if "csrftoken" in r.cookies:
                token = r.cookies["csrftoken"].value
                return token

    async def check_username(self, username):
        token = await self.get_token()
        data = {"csrfmiddlewaretoken": token, "userName": username, "email": ""}
        headers = {
            "Accept": "*/*",
            "Referer": Lastfm.URL,
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": f"csrftoken={token}",
        }
        async with self.post(Lastfm.ENDPOINT, data=data, headers=headers) as r:
            json_body = await self.get_json(r)
            if json_body["userName"]["valid"]:
                return self.response_available(
                    username, message=json_body["userName"]["success_message"]
                )
            else:
                return self.response_unavailable_or_invalid(
                    username,
                    message=self.tag_regex.sub("", json_body["userName"]["error_messages"][0]),
                    unavailable_messages=Lastfm.USERNAME_TAKEN_MSGS,
                    link=Lastfm.USERNAME_LINK_FORMAT.format(username),
                )

    # Email: the sign-up form only validates the format of an address, reporting any well-formed
    # address as valid whether or not an account already uses it


class Yahoo(BasePlatform, UsernameQueryable, PrerequestRequired):
    URL = "https://login.yahoo.com/account/create"
    USERNAME_ENDPOINT = "https://login.yahoo.com/account/module/create?validateField=yid"

    # Modified from Yahoo source
    error_messages = {
        "IDENTIFIER_EXISTS": "A Yahoo account already exists with this email address. REPLACE_SIGNIN_LINK.",
        "DANGLING_IDENTIFIER_EXISTS": "A Yahoo account already exists with this email address.",
        "IDENTIFIER_NOT_AVAILABLE": "This email address is not available for sign up, try something else",
        "EMAIL_DOMAIN_NOT_ALLOWED": "You cannot use this email address. Instead try creating Yahoo email address",
        "RESERVED_WORD_PRESENT": "A Yahoo account already exists with this email address.",
        "SOME_SPECIAL_CHARACTERS_NOT_ALLOWED": "You can only use letters, numbers, periods (‘.’), and underscores (‘_’) in your username.",
        "SOME_SPECIAL_CHARACTERS_NOT_ALLOWED_IN_EMAIL": "Make sure you use your full email address, including an “@” sign and a domain.",
        "INVALID_IDENTIFIER": "Error: Invalid identifier.",
        "CANNOT_END_WITH_SPECIAL_CHARACTER": "Your username has to end with a letter or a number.",
        "CANNOT_HAVE_MORE_THAN_ONE_PERIOD": "You can’t have more than one ‘.’ in your username.",
        "NEED_AT_LEAST_ONE_ALPHA": "Please use at least one letter in your username.",
        "CANNOT_START_WITH_SPECIAL_CHARACTER_OR_NUMBER": "Your username has to start with a letter.",
        "CONSECUTIVE_SPECIAL_CHARACTERS_NOT_ALLOWED": "You can’t have more than one ‘.’ or ‘_’ in a row.",
        "INVALID_NAME_LENGTH": "That name is too long.",
        "LENGTH_TOO_SHORT": "That email address is too short, please use a longer one.",
        "LENGTH_TOO_LONG": "That email address is too long, please use a shorter one.",
        "NAME_CONTAINS_URL": "You can't use this name",
        "ELECTION_SPECIFIC_WORD_PRESENT": "Not available, try something else.",
    }

    regex = re.compile(r"v=1&s=([^\s]*)")

    async def prerequest(self):
        async with self.get(Yahoo.URL) as r:
            if "AS" in r.cookies:
                match = self.regex.search(r.cookies["AS"].value)
                if match:
                    return match.group(1)

    async def check_username(self, username):
        token = await self.get_token()
        async with self.post(
            Yahoo.USERNAME_ENDPOINT,
            data={"specId": "yidReg", "acrumb": token, "yid": username},
            headers={"X-Requested-With": "XMLHttpRequest"},
        ) as r:
            json_body = await self.get_json(r)
            if json_body["errors"][2]["name"] != "yid":
                return self.response_available(username)
            else:
                error = json_body["errors"][2]["error"]
                error_pretty = self.error_messages.get(error, error.replace("_", " ").capitalize())
                if error in (
                    "IDENTIFIER_EXISTS",
                    "RESERVED_WORD_PRESENT",
                    "IDENTIFIER_NOT_AVAILABLE",
                    "DANGLING_IDENTIFIER_EXISTS",
                ):
                    return self.response_unavailable(username, message=error_pretty)
                else:
                    return self.response_invalid(username, message=error_pretty)


class Firefox(BasePlatform, EmailQueryable):
    URL = "https://accounts.firefox.com/signup"
    EMAIL_ENDPOINT = "https://api.accounts.firefox.com/v1/account/status"
    # The cache in front of the API answers 406 to any request carrying a browser user agent
    HEADERS = {"User-agent": f"scato/{__version__}"}

    async def check_email(self, email):
        async with self.post(Firefox.EMAIL_ENDPOINT, json={"email": email}) as r:
            json_body = await self.get_json(r)
            if r.status == 400:
                # The API rejects an address it will not accept at sign up
                return self.response_invalid(email, message=json_body["message"])
            elif "error" in json_body:
                return self.response_failure(email, message=json_body["message"])
            elif json_body["exists"]:
                return self.response_unavailable(email)
            else:
                return self.response_available(email)


class Platforms(Enum):
    GITHUB = GitHub
    GITLAB = GitLab
    INSTAGRAM = Instagram
    LASTFM = Lastfm
    REDDIT = Reddit
    TUMBLR = Tumblr
    X = X
    FIREFOX = Firefox

    def __str__(self):
        return self.value.__name__

    def __len__(self):
        return len(self.value.__name__)


@dataclass(frozen=True)
class PlatformResponse:
    platform: Platforms
    query: str
    available: bool
    valid: bool
    success: bool
    message: str
    link: str
