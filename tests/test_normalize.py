from __future__ import annotations

import pytest

from leadengine.normalize import is_role_email, normalize_domain, normalize_email


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme.example", "acme.example"),
        ("HTTPS://www.Acme.example/about?x=1", "acme.example"),
        ("http://ACME.EXAMPLE", "acme.example"),
        ("www.acme.example", "acme.example"),
        ("sales@acme.example", "acme.example"),
        ("acme.example:8080/path", "acme.example"),
        ("acme.example.", "acme.example"),
        ("  shop.acme.example  ", "shop.acme.example"),
    ],
)
def test_domain_variants_collapse(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "n/a", "localhost", "http://", "acme", "see sheet 2", "-bad-.example"])
def test_non_domains_are_rejected(raw: str | None) -> None:
    assert normalize_domain(raw) is None


def test_email_normalisation_and_role_detection() -> None:
    assert normalize_email("  Anna.Novak@Acme.Example ") == "anna.novak@acme.example"
    assert normalize_email("not-an-email") is None
    assert is_role_email("info@acme.example")
    assert not is_role_email("anna@acme.example")
