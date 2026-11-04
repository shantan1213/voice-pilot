"""Focused tests: spoken meeting lines stay TTS-friendly, voice helpers exact.

Speech never reads ISO dates ("2026-11-06" -> "two oh two six") or clock
times ("10:00 AM" -> "ten oh oh ay em"). The clickable slot cards show the
full details visually instead.
"""

import unittest
from datetime import datetime

from app.agent import meetings as meeting_core


def _day() -> datetime:
    return datetime(2026, 11, 6, 0, 0)


class SpokenFormTests(unittest.TestCase):
    def test_spoken_date_has_no_digits(self):
        self.assertEqual(meeting_core.spoken_date(_day()), "Friday, November 6")

    def test_spoken_time_has_no_zeros(self):
        day = _day()
        self.assertEqual(meeting_core.spoken_time(day.replace(hour=10)), "10 AM")
        self.assertEqual(meeting_core.spoken_time(day.replace(hour=12)), "12 PM")
        self.assertEqual(meeting_core.spoken_time(day.replace(hour=18)), "6 PM")
        self.assertEqual(
            meeting_core.spoken_time(day.replace(hour=10, minute=30)), "10:30 AM"
        )

    def test_availability_does_not_list_times(self):
        day = _day()
        free = meeting_core.free_slots(day, [])
        self.assertEqual(len(free), 10)
        speech = meeting_core.availability_speech(day, free, "India Standard Time")
        self.assertIn("10 open times", speech)
        self.assertIn("Friday, November 6", speech)
        self.assertIn("India Standard Time", speech)
        self.assertNotIn("2026", speech)
        self.assertNotIn("AM", speech)
        self.assertNotIn("PM", speech)

    def test_availability_singular_and_full(self):
        day = _day()
        free = meeting_core.free_slots(day, [])
        one = meeting_core.availability_speech(day, free[:1], "UTC")
        self.assertIn("1 open time", one)
        self.assertNotIn("open times", one)
        busy = [(start, end) for start, end in meeting_core.day_slot_bounds(day)]
        self.assertEqual(meeting_core.free_slots(day, busy), [])
        full = meeting_core.availability_speech(day, [], "India Standard Time")
        self.assertIn("fully booked", full)
        self.assertIn("Friday, November 6", full)
        self.assertNotIn("2026", full)


class VoiceHelperTests(unittest.TestCase):
    def test_parse_meeting_type(self):
        self.assertEqual(meeting_core.parse_meeting_type("a video call"), "video call")
        self.assertEqual(meeting_core.parse_meeting_type("zoom meeting"), "video call")
        self.assertEqual(meeting_core.parse_meeting_type("phone call"), "phone call")
        self.assertEqual(meeting_core.parse_meeting_type("in person"), "in-person")
        self.assertIsNone(meeting_core.parse_meeting_type("quarterly review"))

    def test_match_slot(self):
        day = _day()
        free = meeting_core.free_slots(day, [])
        self.assertEqual(
            meeting_core.match_slot("Book the 10:00 AM slot on 2026-11-06.", free)[0].hour, 10
        )
        self.assertEqual(
            meeting_core.match_slot("Book the 9:00 AM slot on November 6, 2026.", free)[0].hour, 9
        )
        self.assertEqual(meeting_core.match_slot("2pm", free)[0].hour, 14)
        self.assertEqual(meeting_core.match_slot("12", free)[0].hour, 12)
        self.assertEqual(meeting_core.match_slot("1", free)[0].hour, 13)
        self.assertEqual(meeting_core.match_slot("9", free)[0].hour, 9)
        self.assertIsNone(meeting_core.match_slot("eight", free))

    def test_confirm_summary_format(self):
        day = _day()
        details = {
            "topic": "pricing",
            "meeting_type": "video call",
            "name": "John",
            "email": "john@example.com",
        }
        summary = meeting_core.confirm_summary(
            details, day, day.replace(hour=10), "India Standard Time"
        )
        self.assertEqual(
            summary,
            "Please confirm: a video call about pricing on Friday, November 6 at "
            "10 AM in India Standard Time, under the name John, using "
            "john@example.com. Should I book it?",
        )

    def test_alternatives_capped_at_three(self):
        day = _day()
        free = meeting_core.free_slots(day, [])
        suggestion = meeting_core.alternative_suggestion(free)
        self.assertIn("9 AM", suggestion)
        self.assertIn("10 AM", suggestion)
        self.assertIn("11 AM", suggestion)
        self.assertNotIn("12 PM", suggestion)
        self.assertNotIn("2026", suggestion)


class BehaviorContractTests(unittest.TestCase):
    def test_friendly_timezone(self):
        self.assertEqual(
            meeting_core.friendly_tz_label("IST"), "India Standard Time"
        )
        self.assertEqual(meeting_core.friendly_tz_label(""), "local time")
        self.assertEqual(meeting_core.friendly_tz_label("EST"), "EST")

    def test_explicit_time_mentioned(self):
        self.assertTrue(meeting_core.explicit_time_mentioned("Book the 10 AM slot"))
        self.assertTrue(meeting_core.explicit_time_mentioned("schedule at 10 tomorrow"))
        self.assertTrue(meeting_core.explicit_time_mentioned("meeting at 10:30"))
        self.assertTrue(meeting_core.explicit_time_mentioned("10 o'clock"))
        self.assertFalse(meeting_core.explicit_time_mentioned("schedule a meeting tomorrow"))
        self.assertFalse(meeting_core.explicit_time_mentioned("a 30 minute meeting for 2 people"))
        self.assertFalse(meeting_core.explicit_time_mentioned("what slots are free"))

    def test_testing_email_constant(self):
        self.assertEqual(meeting_core.TESTING_EMAIL, "pixelssunny@gmail.com")
        self.assertEqual(meeting_core.MEETING_TIMEZONE, "Asia/Kolkata")

    def test_cancel_frees_slot(self):
        import json
        import tempfile
        from datetime import timezone
        from pathlib import Path

        day = _day().replace(tzinfo=timezone.utc)
        start = day.replace(hour=10)
        end = day.replace(hour=11)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "bookings.jsonl").write_text(
                json.dumps({
                    "booking_id": "ABC12345",
                    "status": "confirmed",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                })
                + "\n",
                encoding="utf-8",
            )
            day_start, day_end = day.replace(hour=9), day.replace(hour=19)
            self.assertEqual(len(meeting_core.booked_intervals(base, day_start, day_end)), 1)
            updated = meeting_core.cancel_booking_record(base, "abc12345")
            self.assertIsNotNone(updated)
            self.assertTrue(str(updated["status"]).startswith("cancelled"))
            self.assertEqual(meeting_core.booked_intervals(base, day_start, day_end), [])
            self.assertEqual(len(meeting_core.free_slots(day, [])), 10)

    def test_purge_cancelled_keeps_active(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "bookings.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"booking_id": "AAA", "status": "confirmed"}),
                        json.dumps({"booking_id": "BBB", "status": "cancelled"}),
                        "not json",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(meeting_core.purge_cancelled_records(base), 1)
            rows = meeting_core.read_booking_records(base)
            self.assertEqual([r["booking_id"] for r in rows], ["AAA"])
            self.assertEqual(meeting_core.purge_cancelled_records(base), 0)

    def test_voice_field_labels_cover_collection_order(self):
        self.assertEqual(
            set(meeting_core.VOICE_FIELD_LABELS),
            set(meeting_core.VOICE_FIELD_ORDER),
        )
        for label in meeting_core.VOICE_FIELD_LABELS.values():
            self.assertTrue(label and "AM" not in label and "PM" not in label)


    def test_internal_bookings_block_slots(self):
        import json
        import tempfile
        from datetime import timezone
        from pathlib import Path

        day = _day().replace(tzinfo=timezone.utc)
        start = day.replace(hour=10)
        end = day.replace(hour=11)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(
                meeting_core.booked_intervals(
                    base, day.replace(hour=9), day.replace(hour=19)
                ),
                [],
            )
            (base / "bookings.jsonl").write_text(
                json.dumps({"start": start.isoformat(), "end": end.isoformat()})
                + "\nnot json\n",
                encoding="utf-8",
            )
            busy = meeting_core.booked_intervals(
                base, day.replace(hour=9), day.replace(hour=19)
            )
            self.assertEqual(len(busy), 1)
            free = meeting_core.free_slots(day, busy)
            self.assertEqual(len(free), 9)
            self.assertNotIn(start, [s for s, _ in free])


if __name__ == "__main__":
    unittest.main()
