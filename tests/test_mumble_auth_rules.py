"""Reguły logowania do Mumble kontem OTS (wtyczka TakLab, decyzja z 23 XI 2026).

Nazwa w Mumble to "<znak> (<konto>)". Hasło to token rejestracji albo hasło
konta. Wspólne hasło środowiska przechodzi tylko dalej do Murmura (połączenia
techniczne w klastrze), a telefony go nie dostają.
"""

from types import SimpleNamespace

from opentakserver.mumble.auth_rules import decide, parse_display_name, session_id

SHARED = "wspolne-haslo"


def users(**accounts):
    return lambda name: accounts.get(name)


def account(name="pluton1", active=True, password="haslo"):
    return SimpleNamespace(username=name, active=active, password=password)


def tokens(valid):
    """valid: set of (token, username) pairs."""
    return lambda token, username: (token, username) in valid


def verify(password, stored):
    return password == stored


def run(name, password, accounts=None, valid_tokens=(), shared=SHARED):
    accounts = accounts if accounts is not None else {"pluton1": account()}
    return decide(name, password, users(**accounts), tokens(set(valid_tokens)), verify, shared)


def test_parse_display_name():
    assert parse_display_name("Alfa 1 (pluton1)") == ("Alfa 1", "pluton1")
    assert parse_display_name("Alfa (1) (pluton1)") == ("Alfa (1)", "pluton1")
    assert parse_display_name("Żbik (łączność.2)") == ("Żbik", "łączność.2")
    assert parse_display_name("pluton1") is None
    assert parse_display_name(" (pluton1)") is None
    assert parse_display_name("Alfa (plu ton)") is None
    assert parse_display_name("Alfa (pluton1) x") is None


def test_two_callsigns_one_account_get_different_ids():
    a = session_id("pluton1", "Alfa 1")
    b = session_id("pluton1", "Alfa 2")
    assert a != b
    assert 1_000_000_000 <= a < 2_000_000_000


def test_same_pair_same_id():
    assert session_id("pluton1", "Alfa 1") == session_id("pluton1", "Alfa 1")


def test_token_accepted():
    assert run("Alfa 1 (pluton1)", "TOK", valid_tokens={("TOK", "pluton1")}) == (
        session_id("pluton1", "Alfa 1"), "Alfa 1 (pluton1)")


def test_password_of_account_accepted():
    result = run("Alfa 1 (pluton1)", "haslo")
    assert result == (session_id("pluton1", "Alfa 1"), "Alfa 1 (pluton1)")


def test_token_of_other_account_rejected():
    assert run("Alfa 1 (pluton1)", "TOK", valid_tokens={("TOK", "pluton2")}) == (-1, None)


def test_inactive_account_rejected():
    accounts = {"pluton1": account(active=False)}
    assert run("Alfa 1 (pluton1)", "TOK", accounts, {("TOK", "pluton1")}) == (-1, None)
    assert run("Alfa 1 (pluton1)", "haslo", accounts) == (-1, None)


def test_unknown_account_rejected():
    assert run("Alfa 1 (nikt)", "haslo") == (-1, None)


def test_no_parenthesis_rejected():
    assert run("pluton1", "haslo") == (-1, None)


def test_shared_password_falls_through():
    assert run("cokolwiek-CloudTAK", SHARED) == (-2, None)


def test_empty_shared_password_never_falls_through():
    assert run("cokolwiek", "", shared="") == (-1, None)


def test_superuser_falls_through():
    assert run("SuperUser", "x") == (-2, None)
