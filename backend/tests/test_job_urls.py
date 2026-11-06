import pytest

from job_hunter.domain.jobs import CANONICAL_VERSION, canonical_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "HTTPS://Example.ORG:443/jobs/42?utm_source=search#details",
            "https://example.org/jobs/42",
        ),
        ("http://EXAMPLE.org:80", "http://example.org/"),
        ("https://example.org:8443/Job/", "https://example.org:8443/Job/"),
        ("https://bücher.example/emploi", "https://xn--bcher-kva.example/emploi"),
        ("https://faß.example/", "https://xn--fa-hia.example/"),
        ("http://[2001:0DB8:0:0:0:0:0:1]:80/a", "http://[2001:db8::1]/a"),
        (
            "https://example.org/?a=1&UTM_SOURCE=x&a=2&b=&gclid=x",
            "https://example.org/?a=1&a=2&b=",
        ),
        (
            "https://example.org/job?%75tm_source=x&jobId=42",
            "https://example.org/job?jobId=42",
        ),
        (
            "https://example.org/a%2Fb?x=a+b&x=a%20b&&ref=vacancy#section",
            "https://example.org/a%2Fb?x=a+b&x=a%20b&&ref=vacancy",
        ),
        ("https://example.org/?", "https://example.org/?"),
        ("https://example.org/é", "https://example.org/é"),
    ],
)
def test_conservative_canonicalization_is_idempotent(raw: str, expected: str) -> None:
    assert CANONICAL_VERSION == 1
    assert canonical_url(raw) == expected
    assert canonical_url(expected) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("?job=1", "?job=2"),
        ("?a=1&a=2", "?a=2&a=1"),
        ("?flag", "?flag="),
        ("", "?"),
        ("/Job", "/job"),
        ("/a%2Fb", "/a/b"),
        ("/a/../b", "/b"),
        ("/a/", "/a"),
        ("?utm_custom=1", "?utm_custom=2"),
    ],
)
def test_meaningful_variants_do_not_merge(left: str, right: str) -> None:
    assert canonical_url("https://example.org" + left) != canonical_url(
        "https://example.org" + right
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "file:///tmp/job",
        "javascript:alert(1)",
        "//example.org/job",
        "/job",
        "https:///job",
        "https://user:private@example.org/",
        "https://@example.org/",
        "https://example.org:0/",
        "https://example.org:65536/",
        "https://example.org:/",
        "https://example.org:bad/",
        "https://[bad]/",
        "https://exa_mple.org/",
        "https://%65xample.org/",
        "https://example.org/\\job",
        "https://example.org/%xy",
        "https://example.org/with space",
        "\nhttps://example.org/",
        "https://example.org/\x00",
        "https://example.org/?access_token=private",
        "https://example.org/#api_key=private",
        "https://example.org/?%74oken=private",
        "https://example.org/\ud800",
        "https://example.org/" + "é" * 1024,
    ],
)
def test_invalid_or_credential_bearing_urls_fail_without_echo(raw: str) -> None:
    with pytest.raises(ValueError) as error:
        canonical_url(raw)
    assert "private" not in str(error.value)


def test_byte_bound_is_applied_to_input_and_normalized_url() -> None:
    exact = "https://example.org/" + "x" * (2048 - len("https://example.org/"))
    assert canonical_url(exact) == exact
    with pytest.raises(ValueError):
        canonical_url(exact + "x")
    expanded_host = "https://é.example/"
    with pytest.raises(ValueError):
        canonical_url(expanded_host + "x" * (2048 - len(expanded_host.encode())))
