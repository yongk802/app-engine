"""Invitation email: owner-only, metered, safe addresses, and a draft when there is no SMTP."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app_engine.mailer import MailBudget, Mailer, SmtpSettings, clean_address, invitation_email, mailto_link
from tests.test_public_origin import LAUNCHER, PUBLIC, load_engine, make_app, owner_client, public_client


def test_addresses_are_plain_and_cannot_smuggle_headers():
    assert clean_address(" Rook@Example.com ") == "Rook@Example.com"
    for bad in ("rook", "rook@", "@example.com", "Rook <rook@example.com>", "a@b.c,d@e.f", "a@b.co\nBcc: x@y.z", "x" * 250 + "@a.bc", None, 3):
        assert clean_address(bad) is None, bad


def test_the_email_says_what_a_closed_beta_player_needs():
    subject, body = invitation_email(server_name="Hostinger VPS", origin="https://play.example", username="rook", link="https://play.example/join/abc", game="Night City Table", inviter="Yong")
    assert subject == "Yong invited you to play Night City Table online"
    for needle in ("closed beta", "nothing to install", "rook", "https://play.example/join/abc", "Hostinger VPS", "Find a random opponent", "expires in 7 days", "https://play.example/sign-in", "saved to your account"):
        assert needle in body, needle
    link = mailto_link("rook@example.com", subject, body)
    assert link.startswith("mailto:rook%40example.com?subject=Yong%20invited") and "join/abc" in link


def test_budget_refuses_floods_per_server_and_per_recipient():
    budget = MailBudget(per_hour=3, per_day=4, per_recipient=2)
    for _ in range(2):
        assert budget.reason_to_refuse("a@x.y") is None
        budget.record("A@x.y")
    assert "already received 2" in budget.reason_to_refuse("a@x.y")
    budget.record("b@x.y")
    assert "an hour" in budget.reason_to_refuse("c@x.y")


def test_smtp_settings_parse_or_refuse():
    s = SmtpSettings.from_env("smtp://user%40x.y:p%40ss@mail.example:587", "invites@example.com")
    assert (s.host, s.port, s.username, s.password, s.starttls, s.tls) == ("mail.example", 587, "user@x.y", "p@ss", True, False)
    assert SmtpSettings.from_env("smtps://mail.example", "invites@example.com").port == 465
    assert SmtpSettings.from_env("", "") is None
    with pytest.raises(ValueError):
        SmtpSettings.from_env("http://mail.example", "invites@example.com")
    with pytest.raises(ValueError):
        SmtpSettings.from_env("smtp://mail.example", "not an address")


def test_mailer_sends_within_budget_only():
    sent = []
    mailer = Mailer(SmtpSettings.from_env("smtp://mail.example", "invites@example.com"), MailBudget(per_hour=1, per_day=1, per_recipient=1), transport=lambda *a: sent.append(a))
    asyncio.run(mailer.send("rook@example.com", "s", "b", "Server"))
    assert sent == [("rook@example.com", "s", "b", "Server")]
    with pytest.raises(PermissionError):
        asyncio.run(mailer.send("other@example.com", "s", "b", "Server"))
    assert len(sent) == 1


def test_route_drafts_without_smtp_sends_with_it_and_is_owner_only(tmp_path, monkeypatch):
    make_app(tmp_path / "apps", "cards")
    module = load_engine(tmp_path, monkeypatch, APP_ENGINE_PUBLIC_ORIGIN=PUBLIC, APP_ENGINE_ADMIN_SECRET="hunter2-but-longer", APP_ENGINE_SERVER_NAME="Yong's table")
    owner, admin = owner_client(module)
    try:
        created = owner.post(f"{LAUNCHER}/admin/players", json={"username": "rook", "apps": ["cards"]}, headers=admin).json()
        pid, first_link = created["player"]["id"], created["invite"]
        assert owner.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "not an address"}, headers=admin).status_code == 400
        assert owner.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "rook@example.com"}).status_code == 403, "the launcher capability is required"
        draft = owner.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "rook@example.com", "from_name": "Yong"}, headers=admin)
        assert draft.status_code == 200, draft.text
        body = draft.json()
        assert body["sent"] is False and body["mailto"].startswith("mailto:rook%40example.com") and "Yong's table" in body["body"] and "cards" in body["body"]
        new_link = body["body"].split("expires in 7 days):\n   ")[1].split("\n")[0]
        assert new_link != first_link and new_link.startswith(f"{LAUNCHER}/join/")
        with public_client(module) as guest:
            assert guest.get(first_link).status_code == 404, "emailing minted a fresh link and retired the old one"
            assert guest.get(new_link).status_code == 200
            guest.post(new_link, data={"password": "correct horse battery", "confirm": "correct horse battery"}, follow_redirects=False)
            assert guest.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "x@y.zz"}, headers=admin).status_code == 404, "players have no mail surface"

        # With SMTP configured the server sends, within its budget.
        sent = []
        module._mailer = module.Mailer(SmtpSettings.from_env("smtp://mail.example", "invites@example.com"), MailBudget(per_hour=2, per_day=2, per_recipient=1), transport=lambda *a: sent.append(a))
        ok = owner.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "rook@example.com"}, headers=admin)
        assert ok.status_code == 200 and ok.json()["sent"] is True and sent[0][0] == "rook@example.com" and sent[0][3] == "Yong's table"
        again = owner.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "rook@example.com"}, headers=admin)
        assert again.status_code == 429 and "already received" in again.json()["detail"] and again.headers["retry-after"]
        assert owner.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "second@example.com"}, headers=admin).status_code == 200
        assert owner.post(f"{LAUNCHER}/admin/players/{pid}/email", json={"to": "third@example.com"}, headers=admin).status_code == 429
        events = [json.loads(l)["event"] for l in (tmp_path / "state" / "audit.log").read_text().splitlines()]
        assert events.count("invitation.email.sent") == 2 and "invitation.email.refused" in events and "invitation.email.drafted" in events
    finally:
        owner.close()
