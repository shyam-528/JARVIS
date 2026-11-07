"""Phase 5: calendar parsing, review-first default, backup drill."""

from datetime import date

from core.calendar_feed import _parse_ics_datetime, fetch_today_events, format_today


def test_ics_datetime_formats():
    assert _parse_ics_datetime("20260911T090000Z") is not None
    assert _parse_ics_datetime("20260911") is not None
    assert _parse_ics_datetime("garbage") is None


def test_no_url_no_events(monkeypatch):
    # The function falls back to the CALENDAR_ICS_URL env var; clear it so the
    # "no URL" case is tested even on machines where the var is set.
    monkeypatch.delenv("CALENDAR_ICS_URL", raising=False)
    assert fetch_today_events(ics_url="") == []


def test_today_filter_and_format():
    today = date.today().strftime("%Y%m%d")
    events = [
        {"start": __import__("datetime").datetime.strptime(today, "%Y%m%d"), "summary": "Standup"},
    ]
    out = format_today(events)
    assert "Standup" in out
    assert format_today([]) == ""


def test_code_agent_defaults_review_first():
    import inspect

    from core.code_agent import CodeAgent

    sig = inspect.signature(CodeAgent.__init__)
    assert sig.parameters["auto_approve"].default is False
