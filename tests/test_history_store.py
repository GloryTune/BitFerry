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

    def test_device_identity_wins_over_reused_ip(self):
        """IP 被新机器拿走后，带设备标识的老记录不能跟着过继给新机器。"""
        store = HistoryStore(self.path)
        old = dict(message(1), dev='uid:old', mine=False, name='旧机器')
        new = dict(message(2), dev='uid:new')
        legacy = dict(message(3))                      # 升级前的记录，没有设备标识
        legacy.pop('dev', None)
        for rec in (old, new, legacy):
            store.append('192.168.1.2', rec)
        store.save()
        total, _, records = store.page(['192.168.1.2'], devs=['uid:new'])
        self.assertEqual([r['payload'] for r in records], ['消息 2', '消息 3'])
        self.assertEqual(total, 2)
        self.assertEqual(store.page([], devs=['uid:old'])[0], 1)
        self.assertEqual(store.page(['192.168.1.2'])[0], 1)        # 只剩老记录按 IP 兜底

    def test_delete_device_keeps_other_devices_sharing_the_ip(self):
        store = HistoryStore(self.path)
        store.append('ip', dict(message(1), dev='uid:a'))
        store.append('ip', dict(message(2), dev='uid:b'))
        store.save()
        store.delete_device(['ip'], ['uid:a'])
        self.assertEqual(store.page([], devs=['uid:a'])[0], 0)
        self.assertEqual(store.page([], devs=['uid:b'])[0], 1)
        self.assertEqual([rec['dev'] for ip, rec in store._cache.values()], ['uid:b'])

    def test_backfill_claims_old_records_by_sender_of_the_day(self):
        def record(n, day, name, mine):
            return {'kind': 'text', 'payload': f'消息 {n}', 'mine': mine, 'ts': '12:00',
                    'day': day, 'seq': n, 'name': name, 'msg_id': str(n)}
        legacy = {'192.168.1.2': [
            record(1, '2026-10-15', '我', True),        # 当天只和 2号机 说话 -> 整天归 2号机
            record(2, '2026-10-15', '2号机', False),
            record(3, '2026-10-16', '我', True),        # 当天没有来信 -> 留空, 继续按 IP 兜底
            record(4, '2026-10-17', '2号机', False),    # 当天两台设备都发过 -> 逐条认领
            record(5, '2026-10-17', '新机器', False),
            record(6, '2026-10-17', '我', True),
        ]}
        names = {'2号机': 'uid:old', '新机器': 'uid:new'}
        store = HistoryStore(self.path, legacy, names)
        everything = store.page(['192.168.1.2'], devs=['uid:old', 'uid:new'])[2]
        claimed = {r['payload']: r.get('dev') for r in everything}
        self.assertEqual(claimed, {'消息 1': 'uid:old', '消息 2': 'uid:old', '消息 3': None,
                                   '消息 4': 'uid:old', '消息 5': 'uid:new', '消息 6': None})
        self.assertEqual(store.page([], devs=['uid:new'])[0], 1)
        self.assertEqual(store.page(['192.168.1.2'], devs=['uid:new'])[0], 3)   # 含两条兜底老记录
        HistoryStore(self.path, None, dict(names, 我='uid:self'))     # 二次启动不再重新认领
        left = HistoryStore(self.path).page(['192.168.1.2'])[2]
        self.assertEqual([(r['payload'], r.get('dev')) for r in left],
                         [('消息 3', None), ('消息 6', None)])
