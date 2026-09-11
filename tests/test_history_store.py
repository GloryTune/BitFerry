import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from history_store import HistoryStore


def message(n):
    return {'kind': 'text', 'payload': f'消息 {n}', 'mine': True, 'ts': '12:00',
            'day': '2026-09-11', 'seq': n, 'name': 'Peer', 'msg_id': str(n)}


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'history.sqlite3'

    def test_migration_once_and_real_database_pagination(self):
        store = HistoryStore(self.path, {'ip': [message(i) for i in range(123)]})
        self.assertEqual(store._cache, {})
        total, page, records = store.page(['ip'], page=-1)
        self.assertEqual((total, page, len(records)), (123, 2, 23))
        self.assertEqual(records[0]['payload'], '消息 100')
        self.assertEqual(store.page(['ip'], '消息 12')[0], 4)
        with patch('builtins.open', side_effect=AssertionError('must not reread JSON')):
            again = HistoryStore(self.path, lambda: self.fail('migration repeated'))
        self.assertEqual(again.page(['ip'])[0], 123)
        self.assertEqual(again._cache, {})

    def test_incremental_update_and_restart_recovery(self):
        store = HistoryStore(self.path, {'ip': [message(i) for i in range(20)]})
        ip, rec = store.find('5')
        rec['delivery'] = 'sending'
        with patch.object(store, '_write', wraps=store._write) as write:
            store.save()
            self.assertEqual(write.call_count, 1)
        again = HistoryStore(self.path)
        again.recover()
        self.assertEqual(again.find('5')[1]['delivery'], 'unconfirmed')
        self.assertEqual(again.page(['ip'])[0], 20)

    def test_failed_transaction_rolls_back_all_rows_and_can_retry(self):
        store = HistoryStore(self.path)
        store.append('ip', message(1)); store.append('ip', message(2))
        original = store._write
        calls = []
        def fail(db, rid, ip, rec):
            calls.append(rid)
            if len(calls) == 2:
                raise OSError('disk full')
            original(db, rid, ip, rec)
        with patch.object(store, '_write', side_effect=fail):
            with self.assertRaises(OSError):
                store.save()
        self.assertEqual(HistoryStore(self.path).page(['ip'])[0], 0)
        store.save()
        self.assertEqual(HistoryStore(self.path).page(['ip'])[0], 2)

    def test_failed_migration_can_restart_without_duplicates(self):
        original = HistoryStore._write
        def fail(db, rid, ip, rec):
            original(db, rid, ip, rec)
            raise OSError('interrupted')
        with patch.object(HistoryStore, '_write', side_effect=fail):
            with self.assertRaises(OSError):
                HistoryStore(self.path, {'ip': [message(1)]})
        store = HistoryStore(self.path, {'ip': [message(1)]})
        self.assertEqual(store.page(['ip'])[0], 1)
