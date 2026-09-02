"""Trusted proxy client IP resolution and rate-limit isolation tests.

Tests that:
- get_client_ip uses X-Forwarded-For when the direct peer is trusted.
- get_client_ip ignores X-Forwarded-For when the direct peer is NOT trusted.
- Different clients get distinct rate-limit keys.
- Arbitrary public X-Forwarded-For cannot spoof the effective IP.
"""

from __future__ import annotations

import pytest
from fastapi import Request
from starlette.datastructures import Scope


def _make_request(
    client_host: str = "127.0.0.1",
    xff: str | None = None,
) -> Request:
    """Create a minimal Request with the given client host and XFF header."""
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "client": (client_host, 12345),
        "server": ("testserver", 80),
    }
    if xff is not None:
        scope["headers"] = [(b"x-forwarded-for", xff.encode())]
    return Request(scope)


class TestClientIPResolution:
    """Test get_client_ip with trusted and untrusted proxies."""

    def test_no_trusted_proxies_uses_direct_peer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With empty FORWARDED_ALLOW_IPS, the direct peer IP is used."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        request = _make_request(client_host="192.168.1.1", xff="10.0.0.1")
        assert get_client_ip(request) == "192.168.1.1"

    def test_trusted_proxy_xff_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the direct peer is trusted, XFF is used."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        request = _make_request(client_host="127.0.0.1", xff="203.0.113.5")
        assert get_client_ip(request) == "203.0.113.5"

    def test_untrusted_proxy_xff_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the direct peer is NOT trusted, XFF is ignored."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        # Direct peer is NOT in the trusted list.
        request = _make_request(client_host="198.51.100.1", xff="203.0.113.5")
        assert get_client_ip(request) == "198.51.100.1"

    def test_trusted_cidr_xff_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CIDR notation in FORWARDED_ALLOW_IPS works."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.16.0.0/12")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        # 172.17.0.1 is in 172.16.0.0/12.
        request = _make_request(client_host="172.17.0.1", xff="203.0.113.5")
        assert get_client_ip(request) == "203.0.113.5"

    def test_trusted_cidr_outside_xff_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """IP outside the trusted CIDR is not trusted."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.16.0.0/12")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        # 192.168.1.1 is NOT in 172.16.0.0/12.
        request = _make_request(client_host="192.168.1.1", xff="203.0.113.5")
        assert get_client_ip(request) == "192.168.1.1"

    def test_no_xff_returns_direct_peer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Trusted proxy but no XFF header → direct peer."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        request = _make_request(client_host="127.0.0.1", xff=None)
        assert get_client_ip(request) == "127.0.0.1"

    def test_chained_proxies_leftmost_non_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With chained proxies, the leftmost non-trusted IP is the client."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1,10.0.0.1")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        # XFF: client=203.0.113.5, proxy1=10.0.0.1, proxy2=127.0.0.1
        request = _make_request(
            client_host="127.0.0.1",
            xff="203.0.113.5, 10.0.0.1, 127.0.0.1",
        )
        assert get_client_ip(request) == "203.0.113.5"

    def test_spoofed_xff_from_untrusted_peer_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A direct public request with spoofed XFF cannot inject a victim IP."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        # Attacker connects directly (not through trusted proxy).
        request = _make_request(
            client_host="198.51.100.99",
            xff="victim-ip-203.0.113.1",
        )
        # The spoofed XFF is ignored — the attacker's own IP is used.
        assert get_client_ip(request) == "198.51.100.99"


class TestRateLimitIsolation:
    """Test that different clients get distinct rate-limit keys."""

    def test_two_clients_distinct_ips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Client A and Client B get different IPs and thus different rate-limit keys."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        req_a = _make_request(client_host="127.0.0.1", xff="203.0.113.1")
        req_b = _make_request(client_host="127.0.0.1", xff="203.0.113.2")

        ip_a = get_client_ip(req_a)
        ip_b = get_client_ip(req_b)

        assert ip_a != ip_b
        assert ip_a == "203.0.113.1"
        assert ip_b == "203.0.113.2"

    def test_spoofed_victim_not_affected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Attacker cannot cause victim's IP to be used for rate limiting."""
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
        get_settings.cache_clear()

        from app.dependencies import get_client_ip

        # Attacker connects directly with victim's IP in XFF.
        attacker_req = _make_request(
            client_host="198.51.100.99",
            xff="203.0.113.1",  # victim
        )
        # Victim connects through trusted proxy.
        victim_req = _make_request(
            client_host="127.0.0.1",
            xff="203.0.113.1",
        )

        attacker_ip = get_client_ip(attacker_req)
        victim_ip = get_client_ip(victim_req)

        # Attacker's IP is their own, NOT the victim's.
        assert attacker_ip == "198.51.100.99"
        # Victim's IP is correctly resolved.
        assert victim_ip == "203.0.113.1"
        assert attacker_ip != victim_ip
