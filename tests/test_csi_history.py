"""CSI history tests; database cases use a disposable isolated schema."""
import os
import queue
import threading
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import psycopg2
from psycopg2 import sql

from csi_history import CsiEventStore, connect, initialize_schema


def event(motion=True, **extra):
    return {"id": str(uuid.uuid4()), "device_id": "test-csi", "room": "Room 1",
            "motion": motion, "score": .08, "threshold_on": .05, "threshold_off": .0375,
            "recorded_at": datetime.now(timezone.utc).isoformat(), **extra}


class HistoryTests(unittest.TestCase):
    def test_db_timeout_preserves_existing_configuration(self):
        with patch('csi_history.psycopg2.connect') as driver:
            connect({'database':'test','options':'-c search_path=isolated'})
        args = driver.call_args.kwargs
        self.assertEqual(args['connect_timeout'],1)
        self.assertIn('search_path=isolated',args['options'])
        self.assertIn('statement_timeout=750',args['options'])

    def test_invalid_history_window_never_connects(self):
        store=CsiEventStore({})
        with patch('csi_history.connect') as driver:
            for hours,limit in ((0,20),(169,20),(24,0),(24,101),(True,20)):
                with self.assertRaises(ValueError): store.read('test',hours,limit)
            driver.assert_not_called()

    def test_read_failure_is_isolated_and_logged_once(self):
        store=CsiEventStore({})
        with patch('csi_history.connect',side_effect=psycopg2.OperationalError('private-test-value')), \
             self.assertLogs('csi_history',level='WARNING') as logs:
            for _ in range(2):
                result=store.read('test')
                self.assertFalse(result['history_available'])
                self.assertEqual(result['events'],[])
        self.assertEqual(len(logs.output),1)
        self.assertNotIn('private-test-value',str(logs.output))

    def test_queue_is_nonblocking_bounded_and_uses_room_configuration(self):
        store=CsiEventStore({},room='Office')
        store.submit(event())
        self.assertEqual(store._queue.get_nowait()['room'],'Office')
        with patch.object(store._queue,'put_nowait',side_effect=queue.Full), \
             self.assertLogs('csi_history',level='WARNING') as logs:
            store.submit(event()); store.submit(event())
        self.assertEqual(len(logs.output),1)

    def test_writer_retries_same_event_id_without_blocking_ingress(self):
        store=CsiEventStore({})
        done=threading.Event()
        attempts=[]
        def write(value):
            attempts.append(dict(value))
            if len(attempts)==1: raise psycopg2.OperationalError('private-test-value')
            done.set()
        with patch.object(store,'_write',side_effect=write), self.assertLogs('csi_history',level='WARNING'):
            store.start()
            try:
                store.submit(event())
                self.assertTrue(done.wait(3))
            finally: store.stop()
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[0]['id'],attempts[1]['id'])
        self.assertFalse(store._write_failed)


@unittest.skipUnless(os.getenv('RUN_MOTION_DB_TESTS')=='1','Opt-in PostgreSQL tests use an isolated schema')
class HistoryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema='csi_test_'+uuid.uuid4().hex
        cls.base={'host':os.getenv('DB_HOST','localhost'),'port':os.getenv('DB_PORT','5432'),
                  'database':os.environ['DB_NAME'],'user':os.environ['DB_USER'],'password':os.environ['DB_PASSWORD']}
        with closing(psycopg2.connect(**cls.base)) as conn,conn,conn.cursor() as cur:
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        cls.config=dict(cls.base,options=f'-c search_path={cls.schema} -c timezone=Europe/Kyiv')
        cls.addClassCleanup(cls.remove_schema)
        initialize_schema(cls.config)

    @classmethod
    def remove_schema(cls):
        with closing(psycopg2.connect(**cls.base)) as conn,conn,conn.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.device=uuid.uuid4().hex
        self.store=CsiEventStore(self.config)

    def write(self,motion=True,**extra):
        value=event(motion,device_id=self.device,**extra)
        self.store._write(value)
        return value

    def test_repeatable_migration_preserves_events(self):
        self.write()
        initialize_schema(self.config)
        self.assertEqual(len(self.store.read(self.device)['events']),1)

    def test_retry_idempotency_and_restart_persistence(self):
        value=self.write()
        self.store._write(value)
        self.write(False)
        fresh_store=CsiEventStore(self.config)
        data=fresh_store.read(self.device)
        self.assertTrue(data['history_available'])
        self.assertEqual([row['motion'] for row in data['events']],[False,True])
        self.assertIsNotNone(data['history_last_motion_at'])
        self.assertEqual(data['events'][1]['id'],value['id'])

    def test_device_window_order_and_limit(self):
        now=datetime.now(timezone.utc)
        self.write(recorded_at=(now-timedelta(hours=26)).isoformat())
        self.write(recorded_at=(now-timedelta(seconds=2)).isoformat())
        latest=self.write(False,recorded_at=(now-timedelta(seconds=1)).isoformat())
        self.store._write(event(device_id='other-device'))
        data=self.store.read(self.device,24,1)
        self.assertEqual(len(data['events']),1)
        self.assertEqual(data['events'][0]['id'],latest['id'])
        self.assertEqual(len(self.store.read(self.device,24,20)['events']),2)
        self.assertEqual(len(self.store.read(self.device,48,20)['events']),3)

    def test_empty_device_has_no_fabricated_events(self):
        data=self.store.read(self.device)
        self.assertEqual(data['events'],[])
        self.assertIsNone(data['history_last_motion_at'])
