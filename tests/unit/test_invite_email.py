from datetime import datetime, timezone
from typing import Any

from core.email import Invite, render_invite


def invite(**overrides) -> Invite:
    values: dict[str, Any] = dict(
        to_email="asha@candidates.test",
        candidate_first_name="Asha",
        company_name="Acme Fintech",
        job_title="Senior Backend Engineer",
        join_url="http://localhost:8000/join/tok123",
        expires_at=datetime(2026, 11, 20, 6, 30, tzinfo=timezone.utc),
        max_minutes=12,
    )
    values.update(overrides)
    return Invite(**values)


def parts(message):
    text = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    return text, html


def test_invite_is_addressed_and_carries_the_link_in_both_parts():
    message = render_invite(invite())
    text, html = parts(message)

    assert message["To"] == "asha@candidates.test"
    assert "Senior Backend Engineer" in message["Subject"]
    assert "http://localhost:8000/join/tok123" in text
    assert 'href="http://localhost:8000/join/tok123"' in html


def test_invite_states_it_is_an_ai_and_shows_expiry_in_ist():
    text, _ = parts(render_invite(invite()))

    assert "AI" in text
    assert "20 Nov 2026, 12:00 PM IST" in text


def test_html_part_escapes_candidate_supplied_values():
    _, html = parts(render_invite(invite(candidate_first_name="<script>x</script>")))

    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_first_invite_is_not_labelled_a_reminder():
    message = render_invite(invite())
    text, _ = parts(message)

    assert not message["Subject"].startswith("Reminder")
    assert text.startswith("Hi Asha,\n\nThanks for applying for the Senior Backend Engineer role at Acme Fintech.\n\nThe next step")


def test_a_reinvite_is_a_reminder_that_says_earlier_links_still_work():
    message = render_invite(invite(reminder=True))
    text, html = parts(message)

    assert message["Subject"] == "Reminder: Acme Fintech: screening call for Senior Backend Engineer"
    assert "Any earlier link we sent you will also take you to this call." in text
    assert "Any earlier link we sent you will also take you to this call." in " ".join(html.split())
    assert "http://localhost:8000/join/tok123" in text
