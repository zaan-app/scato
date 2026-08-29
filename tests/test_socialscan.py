import asyncio
import logging

import pytest

from socialscan.platforms import BasePlatform, PlatformResponse, Platforms, PrerequestRequired
from socialscan.util import sync_execute_queries

TIMEOUT_DURATION = 30  # in seconds

AVAILABLE_USERNAMES = ["jsndiwimw"]
UNAVAILABLE_USERNAMES = ["social"]
INVALID_USERNAMES = ["*"]

UNUSED_EMAILS = ["unused@notanemail.com"]
# An address registered on the platform. The default is not a Firefox account, so platforms
# that disagree with it list their own.
USED_EMAILS = {None: ["fire@gmail.com"], Platforms.FIREFOX: ["admin@mozilla.com"]}

# A platform answering with one of these is refusing to serve us rather than answering the
# query, which says nothing about whether socialscan reads its responses correctly
BLOCKED_MESSAGES = (
    BasePlatform.TOO_MANY_REQUEST_ERROR_MESSAGE,
    BasePlatform.TOKEN_ERROR_MESSAGE,
)

USERNAME_PLATFORMS = [p for p in Platforms if hasattr(p.value, "check_username")]
EMAIL_PLATFORMS = [p for p in Platforms if hasattr(p.value, "check_email")]

logging.basicConfig(level=logging.DEBUG)


def used_emails(platform):
    return USED_EMAILS.get(platform, USED_EMAILS[None])


def query_one(query, platform) -> PlatformResponse:
    return sync_execute_queries([query], [platform])[0]


def skip_if_blocked(response: PlatformResponse):
    """Report a platform that is blocking or throttling us as a skip, so that it stays
    distinguishable from a platform that answered with the wrong result."""
    if not response.success and any(m in response.message for m in BLOCKED_MESSAGES):
        pytest.skip(f"{response.platform} is not answering queries: {response.message}")


def assert_available(response: PlatformResponse):
    skip_if_blocked(response)
    assert response.available
    assert response.valid
    assert response.success


def assert_unavailable(response: PlatformResponse):
    skip_if_blocked(response)
    assert not response.available
    assert response.valid
    assert response.success


def assert_invalid(response: PlatformResponse):
    skip_if_blocked(response)
    assert not response.available
    assert not response.valid
    assert response.success


@pytest.mark.parametrize("platform", USERNAME_PLATFORMS)
@pytest.mark.parametrize(
    "usernames, assert_function",
    [
        (AVAILABLE_USERNAMES, assert_available),
        (UNAVAILABLE_USERNAMES, assert_unavailable),
        (INVALID_USERNAMES, assert_invalid),
    ],
)
@pytest.mark.network
@pytest.mark.timeout(TIMEOUT_DURATION)
def test_usernames(platform, usernames, assert_function):
    for username in usernames:
        assert_function(query_one(username, platform))


@pytest.mark.parametrize("platform", EMAIL_PLATFORMS)
@pytest.mark.network
@pytest.mark.timeout(TIMEOUT_DURATION)
def test_unused_emails(platform):
    for email in UNUSED_EMAILS:
        assert_available(query_one(email, platform))


@pytest.mark.parametrize("platform", EMAIL_PLATFORMS)
@pytest.mark.network
@pytest.mark.timeout(TIMEOUT_DURATION)
def test_used_emails(platform):
    for email in used_emails(platform):
        assert_unavailable(query_one(email, platform))


def test_concurrent_queries_share_one_prerequest():
    """Queries running concurrently against a platform should send its prerequest once
    between them, not once each."""

    class CountingPlatform(BasePlatform, PrerequestRequired):
        def __init__(self):
            super().__init__(session=None)
            self.prerequest_count = 0

        async def prerequest(self):
            self.prerequest_count += 1
            await asyncio.sleep(0.05)
            return "token"

    async def run():
        checker = CountingPlatform()
        tokens = await asyncio.gather(*(checker.get_token() for _ in range(10)))
        return checker.prerequest_count, tokens

    prerequest_count, tokens = asyncio.run(run())
    assert prerequest_count == 1
    assert tokens == ["token"] * 10
