import itertools
import json
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import update
from test_profiles import HEADERS, NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners

from job_hunter.app import create_app
from job_hunter.application.sessions import Sessions, csrf_token
from job_hunter.domain.searches import (
    COUNTRIES,
    LANGUAGES,
    SearchDefinition,
    compile_search,
)
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.settings import ConfigurationError, Settings, load_settings

owners = profile_owners
BASE = "/api/v1/searches/preview"
ALLOWED = ("jobs.example.org", "careers.example.net")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"groups": [["Python"]]}, '("Python")'),
        (
            {"groups": [["Python", "Go"], ["remote", "Paris"]]},
            '("Python" OR "Go") AND ("remote" OR "Paris")',
        ),
        (
            {"groups": [["data engineer", "C++", "C#", ".NET", "CI/CD"]]},
            '("data engineer" OR "C++" OR "C#" OR ".NET" OR "CI/CD")',
        ),
        (
            {"groups": [["développeur", "工程师", "Cafe\u0301", "Ｐｙｔｈｏｎ"]]},
            '("développeur" OR "工程师" OR "Café" OR "Python")',
        ),
        (
            {"groups": [[" Python  ", "Python", "python", "Rock 'n' roll"]]},
            '("Python" OR "python" OR "Rock \'n\' roll")',
        ),
        (
            {"groups": [["Python"], ["Python"]], "exclusions": ["junior", "junior"]},
            '("Python") AND ("Python") -"junior"',
        ),
        (
            {
                "groups": [["Python"]],
                "exclusions": ["recruitment agency", "intern"],
                "sites": [
                    "JOBS.EXAMPLE.ORG",
                    "careers.example.net",
                    "jobs.example.org",
                ],
            },
            '("Python") AND (site:jobs.example.org OR site:careers.example.net) '
            '-"recruitment agency" -"intern"',
        ),
    ],
)
def test_query_examples(body: dict[str, object], expected: str) -> None:
    definition = SearchDefinition.model_validate(body)
    original = definition.model_dump_json()
    result = compile_search(definition, ALLOWED)
    assert result.query == expected and result.provider_parameters == {}
    assert definition.model_dump_json() == original
    assert "not hard filters" in result.hints[0]


def test_grouping_and_round_trip_properties() -> None:
    for terms in itertools.product(
        ("Python", "C++", "data engineer", "开发"), repeat=3
    ):
        input_groups = [list(terms[:2]), [terms[2]]]
        body = {"groups": input_groups, "exclusions": ["intern"]}
        definition = SearchDefinition.model_validate(body)
        result = compile_search(definition, ())
        groups = result.query.removesuffix(' -"intern"').split(" AND ")
        for group, expected in zip(groups, input_groups, strict=True):
            assert group.startswith("(") and group.endswith(")")
            assert group[1:-1].split(" OR ") == [
                f'"{term}"' for term in dict.fromkeys(expected)
            ]
        restored = SearchDefinition.model_validate_json(definition.model_dump_json())
        assert (
            compile_search(restored, ()).model_dump_json() == result.model_dump_json()
        )


def test_provider_parameters_are_separate_and_bounded() -> None:
    definition = SearchDefinition(
        groups=[["engineer"]],
        language="fr",
        country="fr",
        location="Paris, Ile-de-France, France",
        recency_days=30,
    )
    preview = compile_search(definition, ())
    assert preview.query == '("engineer")'
    assert preview.provider_parameters == {
        "lr": "lang_fr",
        "gl": "fr",
        "location": "Paris, Ile-de-France, France",
        "as_qdr": "d30",
    }
    for country in COUNTRIES:
        result = compile_search(SearchDefinition(groups=[["x"]], country=country), ())
        assert result.provider_parameters == {"gl": country}
    for language in LANGUAGES:
        result = compile_search(SearchDefinition(groups=[["x"]], language=language), ())
        assert result.provider_parameters == {"lr": "lang_" + language}
    for days in (1, 365):
        result = compile_search(SearchDefinition(groups=[["x"]], recency_days=days), ())
        assert result.provider_parameters == {"as_qdr": f"d{days}"}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "x" * 81,
        "\ufdfa" * 10,
        "\x00",
        "line\nbreak",
        "\ud800",
        '"engineer"',
        'x" OR site:evil.test "',
        "＂escape＂",
        "“engineer”",
        "engineer\\",
        "site:evil.test",
        "filetype:pdf",
        "x | y",
        "(Python OR Go)",
        "https://evil.test/path",
        "www.evil.test",
        "WWW.evil.test",
        "//evil.test",
        "-junior",
        "x&api_key=secret",
        "%22OR%22",
        "x?gl=ru",
        "x\u202ey",
        "*",
        "...",
    ],
)
def test_unsafe_or_empty_literals_are_rejected(value: str) -> None:
    for field in ("groups", "exclusions", "location"):
        body: dict[str, object] = {"groups": [["engineer"]]}
        body[field] = (
            [[value]]
            if field == "groups"
            else [value]
            if field == "exclusions"
            else value
        )
        if field == "location" and value == "x" * 81:
            continue
        with pytest.raises(ValidationError):
            SearchDefinition.model_validate(body)


@pytest.mark.parametrize(
    "extra",
    [
        {"groups": []},
        {"groups": [[]]},
        {"groups": [["x"]] * 7},
        {"groups": [["x"] * 9]},
        {"groups": [["x"] * 8] * 5},
        {"groups": [[1]]},
        {"groups": "Python"},
        {"groups": [["中" * 79 + str(i) for i in range(4)]] * 3},
        {
            "groups": [["x" * 79 + str(i) for i in range(8)]] * 4,
        },
        {"exclusions": ["x"] * 9},
        {"exclusions": None},
        {"sites": ["x.org"] * 5},
        {"country": "zz"},
        {"country": "USA"},
        {"language": "zz"},
        {"language": "fr|en"},
        {"recency_days": 0},
        {"recency_days": 366},
        {"recency_days": True},
        {"recency_days": "7"},
        {"location": "x" * 121},
        {"owner_id": "private-canary"},
        {"q": "private-canary"},
        {"provider_parameters": {"api_key": "private-canary"}},
    ],
)
def test_schema_rejects_excessive_or_untyped_input(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        SearchDefinition.model_validate({"groups": [["engineer"]], **extra})
    assert "private-canary" not in str(error.value)


@pytest.mark.parametrize(
    "site",
    [
        "https://jobs.example.org",
        "jobs.example.org/path",
        "jobs.example.org:443",
        "*.example.org",
        "user@jobs.example.org",
        "example.org?x=y",
        "example.org#x",
        "127.0.0.1",
        "example.org.",
        "example..org",
        "-example.org",
        "a" * 64 + ".org",
    ],
)
def test_domains_reject_urls_and_raw_operators(site: str) -> None:
    with pytest.raises(ValidationError):
        SearchDefinition(groups=[["x"]], sites=[site])
    with pytest.raises(ValidationError):
        Settings(search_allowed_domains=(site,))


def test_allowlist_is_exact_and_mutated_values_cannot_bypass_validation() -> None:
    for site in ("evil.test", "jobs.example.org.evil.test", "sub.jobs.example.org"):
        with pytest.raises(ValueError):
            compile_search(SearchDefinition(groups=[["x"]], sites=[site]), ALLOWED)
    definition = SearchDefinition(groups=[["x"]], sites=[ALLOWED[0]])
    with pytest.raises(ValueError):
        compile_search(definition, ())
    definition.groups[0].append('" OR site:evil.test')
    with pytest.raises(ValidationError):
        compile_search(definition, ALLOWED)


def test_allowlist_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_HUNTER_SEARCH_ALLOWED_DOMAINS", '["JOBS.EXAMPLE.ORG"]')
    assert load_settings().search_allowed_domains == ("jobs.example.org",)
    monkeypatch.setenv("JOB_HUNTER_SEARCH_ALLOWED_DOMAINS", "[]")
    assert load_settings().search_allowed_domains == ()
    for value in (
        '["https://private-canary"]',
        '["x.org"]'[:-1],
        json.dumps(["x.org"] * 101),
    ):
        monkeypatch.setenv("JOB_HUNTER_SEARCH_ALLOWED_DOMAINS", value)
        with pytest.raises(ConfigurationError) as error:
            load_settings()
        assert "private-canary" not in str(error.value)


def test_query_byte_limit_and_maximum_collection_sizes() -> None:
    terms = [f"{i:02}" + "x" * (56 if i < 25 else 55) for i in range(32)]
    groups = [terms[i : i + 8] for i in range(0, 32, 8)]
    definition = SearchDefinition(groups=groups)
    assert len(compile_search(definition, ()).query.encode("utf-8")) == 2048
    groups[0][0] += "x"
    with pytest.raises(ValidationError):
        SearchDefinition(groups=groups)
    groups[0][0] = groups[0][0][:-2] + "中"
    with pytest.raises(ValidationError):
        SearchDefinition(groups=groups)
    domains = tuple(f"site{i}.example.org" for i in range(4))
    definition = SearchDefinition(
        groups=[[str(i) for i in range(n)] for n in (8, 8, 8, 6, 1, 1)],
        exclusions=[f"excluded{i}" for i in range(8)],
        sites=list(domains),
    )
    result = compile_search(definition, domains)
    assert result.query.count(' -"') == 8
    assert result.query.count("site:") == 4


def test_authenticated_preview_round_trips_and_browser_boundary(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        Settings(allowed_origins=(ORIGIN,), search_allowed_domains=ALLOWED)
    )
    cast(Sessions, app.state.sessions).now = lambda: NOW
    body = {
        "groups": [["engineer", "data engineer"], ["remote"]],
        "sites": [ALLOWED[0]],
    }
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post(BASE, json=body, headers=HEADERS).status_code == 401
        assert client.get("/api/v1/searches/hints").status_code == 401
        client.cookies.set("job_hunter_session", TOKEN)
        hints = client.get("/api/v1/searches/hints")
        assert hints.status_code == 200
        assert hints.headers["cache-control"] == "no-store"
        assert hints.json() == {
            "allowed_domains": list(ALLOWED),
            "language_codes": sorted(LANGUAGES),
            "country_codes": sorted(COUNTRIES),
        }
        app.state.search_allowed_domains = ()
        assert client.get("/api/v1/searches/hints").json()["allowed_domains"] == []
        app.state.search_allowed_domains = ALLOWED
        assert client.post(BASE, json=body).status_code == 403
        assert (
            client.post(BASE, json=body, headers={"Origin": ORIGIN}).status_code == 403
        )
        assert (
            client.post(
                BASE, json=body, headers={**HEADERS, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        first = client.post(BASE, json=body, headers=HEADERS)
        assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
        expected = compile_search(
            SearchDefinition.model_validate(body), ALLOWED
        ).model_dump(mode="json")
        assert first.json() == expected
        assert (
            client.post(BASE, json=json.loads(json.dumps(body)), headers=HEADERS).json()
            == expected
        )
        for invalid in (
            {"groups": [['private-canary" OR evil']]},
            {**body, "sites": ["private-canary.example.org"]},
            {**body, "owner_id": str(owners[1])},
        ):
            response = client.post(BASE, json=invalid, headers=HEADERS)
            assert response.status_code == 422
            assert response.json()["code"] == "validation_error"
            assert "private-canary" not in response.text + caplog.text
        with isolated_database.transaction() as session:
            session.execute(
                update(UserRecord)
                .where(UserRecord.id == owners[1])
                .values(email_verified_at=None)
            )
        client.cookies.set("job_hunter_session", "b" * 43)
        assert client.get("/api/v1/searches/hints").json() == hints.json()
        # Free, stateless preview also permits authenticated unverified accounts.
        other = client.post(
            BASE,
            json={"groups": [["second owner"]]},
            headers={**HEADERS, "X-CSRF-Token": csrf_token("b" * 43)},
        )
        assert other.status_code == 200 and other.json()["query"] == '("second owner")'
        assert "engineer" not in other.text
