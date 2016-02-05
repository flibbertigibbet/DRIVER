from datetime import datetime, timedelta
import uuid
import pytz

from ashlar.models import RecordSchema, RecordType, Record
from data.models import DedupeJob, RecordDuplicate
from data.tasks import deduplicate_records as task

from django.test import TestCase
from django.test.utils import override_settings

from django.db.models import Q


class DedupeTaskTestCase(TestCase):
    @override_settings(CELERY_EAGER_PROPAGATES_EXCEPTIONS=True,
                       CELERY_ALWAYS_EAGER=True,
                       BROKER_BACKEND='memory')
    def setUp(self):
        super(DedupeTaskTestCase, self).setUp()
        self.start = datetime.now(pytz.timezone('Asia/Manila'))
        self.then = self.start - timedelta(days=10)
        self.beforeThen = self.then - timedelta(days=1)
        self.afterThen = self.then + timedelta(days=1)
        self.beforeNow = self.start - timedelta(days=1)
        self.afterNow = self.start + timedelta(days=1)

        self.tod = self.start.hour
        self.dow = self.start.isoweekday() + 1  # 1 added here to handle differences in indexing

        self.record_type = RecordType.objects.create(label='foo', plural_label='foos')
        self.schema = RecordSchema.objects.create(
            schema={"type": "object"},
            version=1,
            record_type=self.record_type
        )
        # 3 identical records to test dedupe
        self.record1 = Record.objects.create(
            occurred_from=self.start,
            occurred_to=self.start,
            geom='POINT (0 0)',
            location_text='Equator1',
            schema=self.schema
        )
        self.record2 = Record.objects.create(
            occurred_from=self.start,
            occurred_to=self.start,
            geom='POINT (0 0.00001)',
            location_text='Equator2',
            schema=self.schema
        )
        self.record3 = Record.objects.create(
            occurred_from=self.start,
            occurred_to=self.start,
            geom='POINT (0 0.0002)',
            location_text='Equator3',
            schema=self.schema
        )
        # and one that shouldn't be considered a duplicate
        self.record4 = Record.objects.create(
            occurred_from=self.start,
            occurred_to=self.start,
            geom='POINT (0 5)',
            location_text='somewhere else1',
            schema=self.schema
        )
        self.stop = datetime.now(pytz.timezone('Asia/Manila'))

    @override_settings(CELERY_EAGER_PROPAGATES_EXCEPTIONS=True,
                       CELERY_ALWAYS_EAGER=True,
                       BROKER_BACKEND='memory')
    def test_find_duplicate_records(self):
        self.assertEqual(DedupeJob.objects.count(), 0)
        self.assertEqual(RecordDuplicate.objects.count(), 0)

        # find all duplicates
        result = task.find_duplicate_records.delay().get()

        self.assertEqual(DedupeJob.objects.count(), 1)
        self.assertEqual(RecordDuplicate.objects.count(), 3)
        self.assertEqual(
            RecordDuplicate.objects.filter(
                Q(record=self.record4) | Q(duplicate_record=self.record4)
            ).count(),
            0
        )
        self.assertIsNotNone(DedupeJob.objects.latest().celery_task)

        # test incremental dedupe task
        now = datetime.now().replace(tzinfo=pytz.timezone('Asia/Manila'))
        newrecord = Record.objects.create(
            occurred_from=now,
            occurred_to=now,
            geom='POINT (0 5)',
            location_text='somewhere else2',
            schema=self.schema
        )
        result = task.find_duplicate_records.delay().get()
        self.assertEqual(DedupeJob.objects.count(), 2)
        self.assertEqual(
            RecordDuplicate.objects.filter(
                Q(record=newrecord) | Q(duplicate_record=newrecord)
            ).count(),
            1
        )
        self.assertEqual(RecordDuplicate.objects.count(), 4)

    @override_settings(CELERY_EAGER_PROPAGATES_EXCEPTIONS=True,
                       CELERY_ALWAYS_EAGER=True,
                       BROKER_BACKEND='memory')
    def test_get_dedupe_ids(self):
        self.assertEqual(DedupeJob.objects.count(), 0)
        self.assertEqual(RecordDuplicate.objects.count(), 0)

        result = task.find_duplicate_records.delay().get()
        job1 = DedupeJob.objects.latest()

        now = datetime.now().replace(tzinfo=pytz.timezone('Asia/Manila'))
        newrecord = Record.objects.create(
            occurred_from=now,
            occurred_to=now,
            geom='POINT (0 5)',
            location_text='somewhere else2',
            schema=self.schema
        )
        result = task.find_duplicate_records.delay().get()

        job2 = DedupeJob.objects.latest()

        self.assertNotEqual(job1, job2)
        self.assertEqual(
            len(task.get_dedupe_ids(job1)),
            RecordDuplicate.objects.filter(job=job1).count())

    @override_settings(CELERY_EAGER_PROPAGATES_EXCEPTIONS=True,
                       CELERY_ALWAYS_EAGER=True,
                       BROKER_BACKEND='memory')
    def test_get_dedupe_set(self):
        self.assertEqual(DedupeJob.objects.count(), 0)
        self.assertEqual(RecordDuplicate.objects.count(), 0)
        start2 = datetime.utcnow().replace(tzinfo=pytz.timezone('UTC'))
        newrecord = Record.objects.create(
            occurred_from=self.start,
            occurred_to=self.start,
            geom='POINT (0 5)',
            location_text='somewhere else2',
            schema=self.schema
        )
        stop2 = datetime.utcnow().replace(tzinfo=pytz.timezone('UTC'))

        for record in Record.objects.filter(occurred_from=start2):
            print "created=%s, occurred_from=%s" % (record.created, record.occurred_from)

        set, queryset = task.get_dedupe_set({'start_time': self.start, 'end_time': self.stop})
        self.assertEqual(len(set), 4)
        set, queryset = task.get_dedupe_set({'start_time': start2, 'end_time': stop2})
        self.assertEqual(len(set), 1)
