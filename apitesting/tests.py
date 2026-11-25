import os
from unittest import mock

from django.test import TestCase

from . import collection_runner as engine
from .models import ApiCollection, CollectionRun


class WorkerQueueTests(TestCase):
    def setUp(self):
        self.collection = ApiCollection.objects.create(name='c')

    def _run(self):
        return CollectionRun.objects.create(collection=self.collection)

    def test_worker_mode_leaves_run_queued(self):
        run = self._run()
        with mock.patch.dict(os.environ, {'RUN_EXECUTION': 'worker'}), \
                mock.patch.object(engine.threading, 'Thread') as thread:
            engine.start_run(run)
        thread.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, 'queued')

    def test_thread_mode_is_default(self):
        run = self._run()
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(engine.threading, 'Thread') as thread:
            os.environ.pop('RUN_EXECUTION', None)
            engine.start_run(run)
        thread.assert_called_once()

    def test_claim_takes_oldest_once(self):
        first, second = self._run(), self._run()
        self.assertEqual(engine.claim_next().pk, first.pk)
        self.assertEqual(engine.claim_next().pk, second.pk)
        self.assertIsNone(engine.claim_next())
        first.refresh_from_db()
        self.assertEqual(first.status, 'running')

    def test_worker_executes_queued_run_and_fails_orphans(self):
        orphan = self._run()
        orphan.status = 'running'
        orphan.save()
        queued = self._run()
        with mock.patch.object(engine, 'execute') as execute, mock.patch.object(engine, 'tick'):
            engine.worker_loop(once=True)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, 'error')
        execute.assert_called_once()
        self.assertEqual(execute.call_args[0][0].pk, queued.pk)


class MappingSourceTests(TestCase):
    def _scopes(self):
        from types import SimpleNamespace
        return SimpleNamespace(collection={}, local={}, environment={}, project={}, globals={},
                               substitute=lambda text: text.replace('{{aid}}', '77'))

    def test_value_from_response_and_from_request_payload(self):
        from .workflows import _apply_mappings
        scopes, captured = self._scopes(), {}
        result = {'body': '{"data": {"id": "g1"}}'}
        request = {'body': '{"airport": {"id": "{{aid}}"}, "name": "x"}'}
        notes = _apply_mappings([
            {'from_path': 'data.id', 'to_var': 'id'},
            {'from_path': 'airport.id', 'to_var': 'airport_id', 'from': 'request'},
            {'from_path': 'missing', 'to_var': 'nope', 'from': 'request'},
        ], result, scopes, captured, request)
        self.assertEqual(captured['id'], 'g1')
        self.assertEqual(captured['airport_id'], '77')          # the sent request value, variables filled in
        self.assertFalse(notes[2]['found'])
