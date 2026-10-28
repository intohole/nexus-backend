"""nexus.touch 触达诚实化原语单测。"""

from datetime import datetime, timedelta

from nexus.touch import TouchAction, decide_touch, fingerprint

NOW = datetime(2026, 10, 18, 8, 30, 0)


def test_fingerprint_normalizes_and_skips_empty():
    a = fingerprint("draft_nudge", "饮料", "no_date", 500000.0)
    b = fingerprint(" draft_nudge ", "饮料", "", None, "no_date", 500000.0)
    assert a == b and len(a) == 16
    assert fingerprint("x") != fingerprint("y")


def test_fingerprint_value_change_changes_identity():
    assert fingerprint("draft", 1, "no_date", 50.0) != fingerprint("draft", 2, "no_date", 50.0)
    assert fingerprint("draft", 1, "no_date", 50.0) != fingerprint("draft", 1, "no_date", 51.0)


def test_first_touch_sends():
    d = decide_touch(new_fingerprint="f1", now=NOW)
    assert d.action is TouchAction.SEND and d.reason == "first_touch" and d.allowed


def test_content_changed_sends_despite_sunset():
    d = decide_touch(
        new_fingerprint="f2",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=60),
        now=NOW,
    )
    assert d.action is TouchAction.SEND and d.reason == "content_changed"


def test_no_prior_fingerprint_releases_once():
    d = decide_touch(new_fingerprint="f1", last_fingerprint=None, last_sent_at=NOW - timedelta(days=2), now=NOW)
    assert d.action is TouchAction.SEND and d.reason == "no_prior_fingerprint"


def test_same_content_within_revive_window_silenced():
    d = decide_touch(
        new_fingerprint="f1",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=5),
        now=NOW,
    )
    assert d.action is TouchAction.SILENCE and not d.allowed


def test_same_content_over_revive_window_revives():
    d = decide_touch(
        new_fingerprint="f1",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=7),
        now=NOW,
    )
    assert d.action is TouchAction.REVIVE and d.allowed


def test_sunset_anchors_on_user_activity():
    late_active = NOW - timedelta(days=10)
    d_active = decide_touch(
        new_fingerprint="f1",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=40),
        user_last_active_at=late_active,
        now=NOW,
    )
    assert d_active.action is TouchAction.REVIVE

    d_inactive = decide_touch(
        new_fingerprint="f1",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=40),
        user_last_active_at=NOW - timedelta(days=45),
        now=NOW,
    )
    assert d_inactive.action is TouchAction.SUNSET


def test_sunset_only_for_same_content():
    d = decide_touch(
        new_fingerprint="f1",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=45),
        now=NOW,
    )
    assert d.action is TouchAction.SUNSET


def test_min_interval_blocks_even_new_content():
    d = decide_touch(
        new_fingerprint="f2",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(hours=2),
        min_interval=timedelta(hours=20),
        now=NOW,
    )
    assert d.action is TouchAction.SILENCE_TOO_SOON


def test_revive_then_silence_cycle():
    sent = NOW - timedelta(days=7)
    first = decide_touch(new_fingerprint="f1", last_fingerprint="f1", last_sent_at=sent, now=NOW)
    assert first.action is TouchAction.REVIVE
    revived_at = NOW
    second = decide_touch(new_fingerprint="f1", last_fingerprint="f1", last_sent_at=revived_at, now=NOW + timedelta(days=3))
    assert second.action is TouchAction.SILENCE


WEEK = timedelta(days=7)


def test_unread_backoff_blocks_new_content_within_schedule():
    d = decide_touch(
        new_fingerprint="f2",
        last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=8),
        min_interval=timedelta(days=6),
        now=NOW,
        unread_streak=1,
        backoff_schedule=(timedelta(days=14), timedelta(days=21)),
    )
    assert d.action is TouchAction.SILENCE_TOO_SOON
    assert d.reason == "within_min_interval_unread_backoff_1" and not d.allowed


def test_unread_backoff_picks_streak_tier_and_caps_at_last():
    base = dict(
        new_fingerprint="f2", last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=15), min_interval=timedelta(days=6), now=NOW,
        backoff_schedule=(timedelta(days=14), timedelta(days=21)),
    )
    streak1 = decide_touch(unread_streak=1, **base)
    streak5 = decide_touch(unread_streak=5, **base)
    assert streak1.action is TouchAction.SEND
    assert streak5.action is TouchAction.SILENCE_TOO_SOON
    assert streak5.reason == "within_min_interval_unread_backoff_5"


def test_unread_backoff_releases_after_tier_elapsed():
    d = decide_touch(
        new_fingerprint="f2", last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=14), min_interval=timedelta(days=6), now=NOW,
        unread_streak=1, backoff_schedule=(timedelta(days=14),),
    )
    assert d.action is TouchAction.SEND and d.reason == "content_changed" and d.allowed


def test_unread_backoff_amplifies_revive_gate_for_same_content():
    d = decide_touch(
        new_fingerprint="f1", last_fingerprint="f1",
        last_sent_at=NOW - WEEK, now=NOW,
        unread_streak=2, backoff_schedule=(timedelta(days=14), timedelta(days=21)),
    )
    assert d.action is TouchAction.SILENCE
    late = decide_touch(
        new_fingerprint="f1", last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=21), now=NOW,
        unread_streak=2, backoff_schedule=(timedelta(days=14), timedelta(days=21)),
    )
    assert late.action is TouchAction.REVIVE
    assert "unread_backoff_2" in late.reason


def test_no_streak_keeps_legacy_behavior():
    d = decide_touch(
        new_fingerprint="f2", last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=8), min_interval=timedelta(days=6), now=NOW,
        backoff_schedule=(timedelta(days=14),),
    )
    assert d.action is TouchAction.SEND and d.reason == "content_changed"


def test_streak_without_schedule_keeps_legacy_behavior():
    d = decide_touch(
        new_fingerprint="f2", last_fingerprint="f1",
        last_sent_at=NOW - timedelta(days=8), min_interval=timedelta(days=6), now=NOW,
        unread_streak=3,
    )
    assert d.action is TouchAction.SEND and d.reason == "content_changed"
