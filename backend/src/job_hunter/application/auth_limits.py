from collections.abc import Callable
from datetime import datetime, timedelta
from ipaddress import IPv6Address, ip_address, ip_network
from typing import Protocol

from job_hunter.domain.accounts import (
    AUTH_POLICIES,
    AuthAction,
    AuthLimitExceeded,
    InvalidRegistration,
    RateBucket,
    normalize_email,
)


class AuthCounter(Protocol):
    def consume(
        self, buckets: tuple[RateBucket, ...], now: datetime, window: timedelta
    ) -> int: ...


class AuthLimits:
    def __init__(self, counter: AuthCounter, now: Callable[[], datetime]) -> None:
        self.counter, self.now = counter, now

    def check(self, action: AuthAction, peer: str, email: str | None = None) -> None:
        policy = AUTH_POLICIES[action]
        try:
            address = ip_address(peer)
            if isinstance(address, IPv6Address):
                address = address.ipv4_mapped or address
            network = (
                str(ip_network(f"{address}/64", strict=False))
                if isinstance(address, IPv6Address)
                else str(address)
            )
        except ValueError:
            network = "unknown"
        buckets = [RateBucket(f"{action}:ip:{network}", policy.ip_limit)]
        if email is not None and policy.account_limit:
            try:
                normalized = normalize_email(email)
            except InvalidRegistration:
                pass
            else:
                buckets.append(
                    RateBucket(f"{action}:account:{normalized}", policy.account_limit)
                )
        retry = self.counter.consume(
            tuple(buckets), self.now(), timedelta(seconds=policy.seconds)
        )
        if retry:
            raise AuthLimitExceeded(retry)
