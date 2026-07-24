from unittest.mock import AsyncMock

import pytest

from app.services.webhooks import _validate_diagnostic_url, _redact_url


def test_redact_url_strips_credentials_and_brackets_ipv6():
    # userinfo + query are dropped (they can carry secrets); host[:port] + path kept.
    assert _redact_url("https://user:secret@example.com/path?token=abc") == "example.com/path"
    assert _redact_url("https://example.com:8080/p") == "example.com:8080/p"
    # IPv6 literal + port is bracketed so host:port stays unambiguous.
    assert _redact_url("http://[::1]:8080/p") == "[::1]:8080/p"
    assert _redact_url("http://[2001:db8::1]:443/x") == "[2001:db8::1]:443/x"
    # A non-numeric port must NOT collapse the whole URL to "<unparseable url>";
    # we still surface the safe host/path.
    assert _redact_url("https://example.com:abc/p") == "example.com/p"


def _addrinfo(ip):
    # Matches the (family, type, proto, canonname, sockaddr) shape returned by getaddrinfo.
    return [(None, None, None, None, (ip, 443))]


def _mock_resolution(mocker, ip):
    mock_loop = mocker.MagicMock()
    mock_loop.getaddrinfo = AsyncMock(return_value=_addrinfo(ip))
    mocker.patch("app.services.webhooks.asyncio.get_running_loop", return_value=mock_loop)


@pytest.mark.asyncio
async def test_ipv4_mapped_ipv6_loopback_is_blocked(mocker):
    # ::ffff:127.0.0.1 parses as IPv6, so the plain IPv4 blocklist alone misses it.
    _mock_resolution(mocker, "::ffff:127.0.0.1")
    with pytest.raises(ValueError, match="private or reserved"):
        await _validate_diagnostic_url("https://example.com/hook")


@pytest.mark.asyncio
async def test_ipv4_mapped_ipv6_private_is_blocked(mocker):
    _mock_resolution(mocker, "::ffff:169.254.169.254")
    with pytest.raises(ValueError, match="private or reserved"):
        await _validate_diagnostic_url("https://example.com/hook")


@pytest.mark.asyncio
async def test_ipv6_multicast_is_blocked(mocker):
    _mock_resolution(mocker, "ff02::1")
    with pytest.raises(ValueError, match="private or reserved"):
        await _validate_diagnostic_url("https://example.com/hook")


@pytest.mark.asyncio
async def test_public_address_is_allowed(mocker):
    _mock_resolution(mocker, "93.184.216.34")
    await _validate_diagnostic_url("https://example.com/hook")
