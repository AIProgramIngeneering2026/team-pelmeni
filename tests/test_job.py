import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pelmeni.job import Job, JobError, JobState


class JobTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 9, tzinfo=timezone.utc)
        self.start = self.now + timedelta(hours=1)
        self.end = self.start + timedelta(hours=1)
        self.owner = uuid4()
        self.job = self.make_job()

    def make_job(self, **overrides):
        fields = dict(owner_id=self.owner, start_at=self.start, end_at=self.end, now=self.now)
        fields.update(overrides)
        return Job(**fields)

    def command(self, name, *, job=None, now=None, actor=None):
        job = self.job if job is None else job
        kwargs = {"now": self.now if now is None else now}
        if name in ("confirm", "cancel"):
            kwargs["actor_id"] = self.owner if actor is None else actor
        getattr(job, name)(**kwargs)

    def rejected(self, code, name, **kwargs):
        before = (self.job.id, self.job.owner_id, self.job.start_at, self.job.end_at, self.job.state)
        with self.assertRaises(JobError) as caught:
            self.command(name, **kwargs)
        self.assertEqual(code, caught.exception.code)
        self.assertTrue(str(caught.exception))
        self.assertEqual(before, (self.job.id, self.job.owner_id, self.job.start_at, self.job.end_at, self.job.state))

    def test_normal_lifecycle(self):
        identity = self.job.id
        self.assertEqual(JobState.PENDING_CONFIRMATION, self.job.state)
        self.command("confirm")
        self.assertEqual(JobState.CONFIRMED, self.job.state)
        self.command("complete", now=self.end)
        self.assertEqual(JobState.COMPLETED, self.job.state)
        self.assertEqual(identity, self.job.id)

    def test_author_can_cancel_pending(self):
        self.command("cancel")
        self.assertEqual(JobState.CANCELLED, self.job.state)

    def test_author_can_cancel_confirmed_before_start(self):
        self.command("confirm")
        self.command("cancel", now=self.start - timedelta(microseconds=1))
        self.assertEqual(JobState.CANCELLED, self.job.state)

    def test_pending_expires_at_start_or_later(self):
        for moment in (self.start, self.end + timedelta(days=1)):
            with self.subTest(moment=moment):
                job = self.make_job()
                job.expire(now=moment)
                self.assertEqual(JobState.CANCELLED, job.state)

    def test_cannot_complete_without_confirmation(self):
        self.rejected("INVALID_STATE", "complete", now=self.end)

    def test_cannot_confirm_twice(self):
        self.command("confirm")
        self.rejected("INVALID_STATE", "confirm")

    def test_cannot_confirm_at_or_after_start(self):
        for moment in (self.start, self.end):
            with self.subTest(moment=moment):
                self.rejected("CONFIRMATION_EXPIRED", "confirm", now=moment)

    def test_cannot_cancel_started_confirmed_meeting(self):
        self.command("confirm")
        for moment in (self.start, self.end):
            with self.subTest(moment=moment):
                self.rejected("MEETING_STARTED", "cancel", now=moment)

    def test_cannot_expire_early(self):
        self.rejected("INVALID_STATE", "expire", now=self.start - timedelta(microseconds=1))

    def test_cannot_expire_confirmed_booking(self):
        self.command("confirm")
        self.rejected("INVALID_STATE", "expire", now=self.start)

    def test_cannot_complete_early(self):
        self.command("confirm")
        self.rejected("INVALID_STATE", "complete", now=self.end - timedelta(microseconds=1))

    def test_cannot_resurrect_cancelled_booking(self):
        self.command("cancel")
        for name in ("confirm", "expire", "complete"):
            with self.subTest(command=name):
                self.rejected("INVALID_STATE", name, now=self.end)

    def test_cannot_cancel_twice(self):
        self.command("cancel")
        self.rejected("ALREADY_CANCELLED", "cancel")

    def test_completed_booking_rejects_every_command(self):
        self.command("confirm")
        self.command("complete", now=self.end)
        for name in ("confirm", "cancel", "expire", "complete"):
            with self.subTest(command=name):
                self.rejected("INVALID_STATE", name, now=self.end)

    def test_other_actor_cannot_confirm(self):
        self.rejected("FORBIDDEN", "confirm", actor=uuid4())

    def test_other_actor_cannot_cancel(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                if confirmed:
                    self.command("confirm")
                self.rejected("FORBIDDEN", "cancel", actor=uuid4())

    def test_public_fields_are_read_only(self):
        for field, value in (("state", JobState.COMPLETED), ("id", uuid4()),
                             ("owner_id", uuid4()), ("start_at", self.now), ("end_at", self.now)):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    setattr(self.job, field, value)

    def test_constructor_does_not_accept_state(self):
        with self.assertRaises(TypeError):
            self.make_job(state=JobState.COMPLETED)

    def test_invalid_interval(self):
        for end in (self.start, self.start - timedelta(seconds=1)):
            with self.subTest(end=end), self.assertRaises(JobError) as caught:
                self.make_job(end_at=end)
            self.assertEqual("INVALID_INTERVAL", caught.exception.code)
            self.assertEqual("end_at", caught.exception.field)

    def test_creation_in_past(self):
        with self.assertRaises(JobError) as caught:
            self.make_job(start_at=self.now - timedelta(seconds=1))
        self.assertEqual("START_IN_PAST", caught.exception.code)

    def test_creation_at_current_time_is_allowed_but_confirmation_is_expired(self):
        self.job = self.make_job(start_at=self.now)
        self.rejected("CONFIRMATION_EXPIRED", "confirm")
        self.command("expire")
        self.assertEqual(JobState.CANCELLED, self.job.state)

    def test_naive_time_is_rejected_on_creation(self):
        for field in ("start_at", "end_at", "now"):
            with self.subTest(field=field), self.assertRaises(JobError) as caught:
                self.make_job(**{field: self.now.replace(tzinfo=None)})
            self.assertEqual("INVALID_PARAMETERS", caught.exception.code)
            self.assertEqual(field, caught.exception.field)

    def test_naive_time_is_rejected_by_commands_without_mutation(self):
        for name in ("confirm", "cancel", "expire"):
            with self.subTest(command=name):
                self.rejected("INVALID_PARAMETERS", name, now=self.now.replace(tzinfo=None))
        self.command("confirm")
        self.rejected("INVALID_PARAMETERS", "complete", now=self.end.replace(tzinfo=None))

    def test_timezones_are_compared_as_instants(self):
        yekaterinburg = timezone(timedelta(hours=5))
        self.rejected("CONFIRMATION_EXPIRED", "confirm", now=self.start.astimezone(yekaterinburg))


if __name__ == "__main__":
    unittest.main()
