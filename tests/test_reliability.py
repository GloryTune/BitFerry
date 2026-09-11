"""Regression tests for receive isolation, receipts, offline state and macOS updates.

Run: QT_QPA_PLATFORM=offscreen python -B -m unittest discover -s tests -v
All application data and received files are redirected to temporary directories.
"""
import importlib.util
import io
import json
import os
import platform
import socket
import stat
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
_IMPORT_HOME = tempfile.TemporaryDirectory(prefix='bitferry-import-test-')
spec = importlib.util.spec_from_file_location('bitferry_test_app', Path(__file__).resolve().parents[1] / 'bitferry.py')
app = importlib.util.module_from_spec(spec)
with patch.object(Path, 'home', return_value=Path(_IMPORT_HOME.name)):
    spec.loader.exec_module(app)


class MemoryConnection:
    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.sent = b''

    def recv(self, n):
        return self.data.read(n)

    def sendall(self, data):
        self.sent += data


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bitferry-recv-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for key, path in {'RECV_ROOT': self.root / 'recv', 'TEMP_RECV_DIR': self.root / 'staged',
                          'IMG_DIR': self.root / 'images'}.items():
            p = patch.object(app, key, path)
            p.start()
            self.addCleanup(p.stop)
        self.events = []
        def record(kind):
            return lambda *args: self.events.append((kind, args))
        self.receiver = app.Receiver(record('file'), record('chat'), record('log'),
                                     on_recv_started=record('started'), on_recv_done=record('done'),
                                     on_recv_cancelled=record('cancelled'))

    def header(self, rel='Project/a.txt', ack=True, transfer_id=None):
        part = {'type': 'file', 'filename': 'a.txt', 'size': 3}
        if rel is not None:
            part['relpath'] = rel
        return {'parts': [part], 'ack': ack, 'transfer_id': transfer_id}

    def receive(self, header=None, data=b'NEW\xa5'):
        conn = MemoryConnection(data)
        self.receiver._handle_batch(conn, header or self.header(), 'peer', '127.0.0.2')
        return conn

    def existing(self):
        p = app.RECV_ROOT / 'peer/Project/a.txt'
        p.parent.mkdir(parents=True)
        p.write_bytes(b'OLD')
        return p

    def test_repeated_folder_keeps_old_and_new(self):
        old = self.existing()
        conn = self.receive()
        self.assertEqual(old.read_bytes(), b'OLD')
        self.assertEqual((old.parent.with_name('Project_1') / 'a.txt').read_bytes(), b'NEW')
        self.assertEqual(conn.sent, app.RECEIVED)
        parts = json.loads(next(args[3] for kind, args in self.events if kind == 'chat'))
        self.assertEqual(Path(parts[0]['path']).name, 'Project_1')

    def test_cancel_only_removes_new_folder(self):
        old = self.existing()
        with self.assertRaises(app._TransferAborted):
            self.receive(data=b'N')
        self.assertEqual(old.read_bytes(), b'OLD')
        self.assertFalse(old.parent.with_name('Project_1').exists())
        self.assertEqual([k for k, _ in self.events].count('cancelled'), 1)
        self.assertFalse(any(k == 'done' for k, _ in self.events))

    def test_disk_error_cleans_all_new_files_and_ends_progress(self):
        old = self.existing()
        def fail(conn, size, f, *args):
            f.write(b'N')
            raise OSError('disk full')
        with patch.object(self.receiver, '_stream_n', side_effect=fail):
            with self.assertRaises(OSError):
                self.receive()
        self.assertEqual(old.read_bytes(), b'OLD')
        self.assertFalse(old.parent.with_name('Project_1').exists())
        self.assertEqual([k for k, _ in self.events].count('cancelled'), 1)

    def test_directory_permission_error_ends_progress(self):
        with patch.object(app, '_reserve_recv_path', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.receive()
        self.assertEqual([k for k, _ in self.events].count('cancelled'), 1)

    def test_failure_on_second_file_rolls_back_entire_batch(self):
        header = self.header()
        header['parts'].append({'type': 'file', 'filename': 'b.txt', 'size': 3})
        original = self.receiver._stream_n
        def write(conn, n, f, *args):
            if str(f.name).endswith('b.txt'):
                raise OSError('disk full')
            return original(conn, n, f, *args)
        with patch.object(self.receiver, '_stream_n', side_effect=write), self.assertRaises(OSError):
            self.receive(header, b'NEWNEW\xa5')
        self.assertEqual(list(app.RECV_ROOT.rglob('*.txt')), [])
        self.assertFalse(any(k in ('chat', 'done') for k, _ in self.events))

    def test_invalid_paths_rejected_before_any_write(self):
        for rel in ('../a.txt', 'Project/../../a.txt', '/tmp/a.txt', 'C:\\outside\\a.txt'):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                self.receive(self.header(rel))
        self.assertEqual(self.events, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sender_directory_symlink_cannot_escape_root(self):
        outside = self.root / 'outside'
        outside.mkdir()
        app.RECV_ROOT.mkdir()
        (app.RECV_ROOT / 'peer').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.receive()
        self.assertEqual(list(outside.iterdir()), [])

    def test_same_name_concurrent_receives_use_different_folders(self):
        errors = []
        def run():
            try:
                self.receive()
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=run) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        files = list((app.RECV_ROOT / 'peer').rglob('a.txt'))
        self.assertEqual(len(files), 6)
        self.assertTrue(all(p.read_bytes() == b'NEW' for p in files))

    def test_single_segment_relpath_is_a_file_card(self):
        self.receive(self.header('a.txt'))
        parts = json.loads(next(args[3] for kind, args in self.events if kind == 'chat'))
        self.assertFalse(parts[0].get('is_folder'))
        self.assertEqual(Path(parts[0]['path']).read_bytes(), b'NEW')

    def test_duplicate_transfer_acknowledged_without_duplicate_file_or_message(self):
        header = self.header(transfer_id='stable-id')
        self.receive(header)
        chats = [x for x in self.events if x[0] == 'chat']
        conn = self.receive(header)
        self.assertEqual(conn.sent, app.RECEIVED)
        self.assertEqual([x for x in self.events if x[0] == 'chat'], chats)
        self.assertEqual(len(list(app.RECV_ROOT.rglob('a.txt'))), 1)

    def test_lost_ack_does_not_delete_committed_files(self):
        conn = MemoryConnection(b'NEW\xa5')
        conn.sendall = Mock(side_effect=BrokenPipeError())
        with self.assertRaises(BrokenPipeError):
            self.receiver._handle_batch(conn, self.header(transfer_id='retry'), 'peer', '127.0.0.2')
        self.assertEqual((app.RECV_ROOT / 'peer/Project/a.txt').read_bytes(), b'NEW')
        self.assertTrue(any(k == 'done' for k, _ in self.events))
        self.assertEqual(self.receive(self.header(transfer_id='retry')).sent, app.RECEIVED)
        self.assertEqual(len(list(app.RECV_ROOT.rglob('a.txt'))), 1)

    def test_zero_byte_file_finishes(self):
        h = self.header(None)
        h['parts'][0]['size'] = 0
        self.receive(h, app.COMMIT)
        self.assertEqual((app.RECV_ROOT / 'peer/a.txt').stat().st_size, 0)
        self.assertEqual([k for k, _ in self.events].count('done'), 1)

    def test_legacy_single_frame_no_commit_still_received(self):
        conn = MemoryConnection(b'NEW')
        self.receiver._handle_batch(conn, self.header(None, ack=False), 'peer', 'ip', require_commit=False)
        self.assertEqual((app.RECV_ROOT / 'peer/a.txt').read_bytes(), b'NEW')
        self.assertEqual(conn.sent, b'')

    def exchange(self, items):
        left, right = socket.socketpair()
        class Client:
            def connect(self, addr): pass
            def settimeout(self, t): left.settimeout(t)
            def sendall(self, data): left.sendall(data)
            def recv(self, n): return left.recv(n)
            def close(self): left.close()
        self.receiver._conn_sem.acquire()
        worker = threading.Thread(target=self.receiver._handle, args=(right, ('127.0.0.2', 1000)))
        worker.start()
        with patch.object(app.socket, 'socket', return_value=Client()), patch.object(app, '_my_uid', return_value='me'):
            status = app.send_batch(items, 'ip', 1, 'peer', lambda *a: None)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        return status

    def test_socket_pair_full_sender_receiver_protocol(self):
        self.assertEqual(self.exchange([{'type': 'text', 'text': 'hello'}]), 'ok')
        self.assertTrue(any(k == 'chat' for k, _ in self.events))

    def test_socket_pair_file_received_before_success(self):
        source = self.root / 'original.txt'
        source.write_bytes(b'hello file')
        self.assertEqual(self.exchange([{'type': 'file', 'path': str(source)}]), 'ok')
        self.assertEqual((app.RECV_ROOT / 'peer/original.txt').read_bytes(), b'hello file')

    def test_socket_pair_disk_failure_never_reports_success(self):
        source = self.root / 'original.txt'
        source.write_bytes(b'hello file')
        with patch.object(self.receiver, '_stream_n', side_effect=OSError('disk full')):
            self.assertIn(self.exchange([{'type': 'file', 'path': str(source)}]), ('error', 'unconfirmed'))
        self.assertEqual(list(app.RECV_ROOT.rglob('*.txt')), [])
        self.assertEqual([k for k, _ in self.events].count('cancelled'), 1)

    def test_legacy_staged_parent_card_cannot_delete_temp_root(self):
        target = app.TEMP_RECV_DIR / 'peer/..'
        sentinel = app.TEMP_RECV_DIR / 'other/a.txt'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('keep')
        w = SimpleNamespace(staged_receives={'ip': [{'name': 'peer', 'parts': [
            {'type': 'file', 'staged': True, 'path': str(target)}]}]},
            _append_log=Mock(), _update_staged_banner=Mock(), current_ip=None,
            _staged_keys=lambda ip: ['ip'], _save_staged_receives=Mock())
        app.MainWindow._resolve_staged(w, 'ip', set())
        self.assertEqual(sentinel.read_text(), 'keep')
        w._append_log.assert_called_once_with('已阻止对异常暂存路径的操作', 'error')


class SenderTests(unittest.TestCase):
    def test_missing_ack_is_not_success(self):
        for ack in (b'', b'wrong'):
            client = Mock()
            client.recv.return_value = ack
            with patch.object(app.socket, 'socket', return_value=client), patch.object(app, '_my_uid', return_value='me'):
                self.assertEqual(app.send_batch([{'type': 'text', 'text': 'hello'}], 'ip', 1, 'me', Mock()), 'unconfirmed')
            client.close.assert_called()

    def test_connection_failure_is_error(self):
        client = Mock()
        client.connect.side_effect = ConnectionRefusedError()
        with patch.object(app.socket, 'socket', return_value=client), patch.object(app, '_my_uid', return_value='me'):
            self.assertEqual(app.send_batch([{'type': 'text', 'text': 'hello'}], 'ip', 1, 'me', Mock()), 'error')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.real_save_queue = app.save_offline_queue
        self.entry = {'recipient': 'uid:peer', 'msg_id': 'm', 'text_items': [{'type': 'text', 'text': 'hello'}],
                      'file_units': [{'send_items': [], 'record': {}, 'label': 'a', 'transfer_id': 'f'}]}
        self.w = SimpleNamespace(offline_queue={'ip': [self.entry]}, _offline_inflight=set(),
                                 _waiting_exit=False, _prepare_source_check=lambda items, callback: True, _canonical_ip=lambda ip: ip, online={'ip': {'online': True}}, _device_key=lambda ip: 'uid:peer',
                                 _peer_online=lambda ip: True, _peer_port=lambda ip: 1,
                                 _send_text_unit=Mock(), _start_file_units=Mock(), _check_sources=lambda items: True,
                                 _append_log=Mock(), _rebuild_peer_list=Mock())
        self.saved = []
        p = patch.object(app, 'save_offline_queue', side_effect=lambda q: self.saved.append(json.loads(json.dumps(q))) or True)
        p.start()
        self.addCleanup(p.stop)

    def flush(self):
        app.MainWindow._flush_offline_queue(self.w, 'ip')

    def finish(self, component, status):
        app.MainWindow._finish_offline_component(self.w, 'ip', 'm', component, status)

    def test_not_removed_until_all_components_acknowledged(self):
        self.flush()
        self.assertEqual(len(self.w.offline_queue['ip']), 1)
        self.finish('text', 'ok')
        self.assertEqual(len(self.w.offline_queue['ip']), 1)
        self.finish('f', 'ok')
        self.assertEqual(self.w.offline_queue['ip'], [])
        self.assertEqual(self.saved[-1]['ip'], [])

    def test_failed_components_retained_and_only_they_retry(self):
        self.flush()
        self.finish('text', 'ok')
        self.finish('f', 'error')
        self.w._send_text_unit.reset_mock()
        self.w._start_file_units.reset_mock()
        self.flush()
        self.w._send_text_unit.assert_not_called()
        self.assertEqual(len(self.w._start_file_units.call_args.args[2]), 1)

    def test_duplicate_flush_does_not_start_same_work_twice(self):
        self.flush()
        self.flush()
        self.assertEqual(self.w._send_text_unit.call_count, 1)
        self.assertEqual(self.w._start_file_units.call_args.args[2], [])

    def test_missing_ack_is_retained_without_automatic_duplicate_send(self):
        self.flush()
        self.finish('text', 'unconfirmed')
        self.finish('f', 'unconfirmed')
        self.w._send_text_unit.reset_mock()
        self.flush()
        self.w._send_text_unit.assert_not_called()
        self.assertEqual(len(self.w.offline_queue['ip']), 1)

    def test_persistence_failure_does_not_start_send(self):
        with patch.object(app, 'save_offline_queue', return_value=False):
            self.flush()
        self.w._send_text_unit.assert_not_called()
        self.w._start_file_units.assert_not_called()

    def test_cancelled_offline_file_does_not_remain_queued(self):
        self.finish('text', 'ok')
        self.finish('f', 'cancelled')
        self.assertEqual(self.w.offline_queue['ip'], [])

    def test_queue_roundtrip_preserves_partial_delivery_and_ids(self):
        self.flush()
        self.finish('text', 'ok')
        self.finish('f', 'error')
        self.w.offline_queue = json.loads(json.dumps(self.saved[-1]))
        self.w._offline_inflight = set()
        self.w._send_text_unit.reset_mock()
        self.flush()
        self.w._send_text_unit.assert_not_called()
        self.assertEqual(self.w._start_file_units.call_args.args[2][0]['transfer_id'], 'f')

    def test_delivery_updates_same_record_and_clears_pending(self):
        rec = {'msg_id': 'm', 'pending': True}
        bubble = Mock()
        w = SimpleNamespace(history={'ip': [rec]}, session={'ip': [rec]},
                            _save_history=Mock(), _bubbles_by_id={'m': bubble}, signals=Mock())
        app.MainWindow._set_message_delivery(w, 'ip', 'm', 'ok')
        self.assertFalse(rec['pending'])
        self.assertEqual(rec['delivery'], 'ok')
        bubble.set_delivery.assert_called_once_with('ok', '')

    def test_atomic_queue_write_preserves_previous_file_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / 'offline_queue.json'
            file.write_text('{"old": []}')
            with patch.object(app, 'APP_DATA', Path(td)), patch.object(app, 'OFFLINE_QUEUE_FILE', file), \
                    patch.object(app.os, 'replace', side_effect=OSError('disk full')), patch.object(app, '_log_persist_error'):
                self.assertFalse(self.real_save_queue({'new': []}))
            self.assertEqual(json.loads(file.read_text()), {'old': []})
            self.assertEqual(list(Path(td).iterdir()), [file])


class UpdateTests(unittest.TestCase):
    @unittest.skipUnless(platform.system() == 'Darwin', 'ditto requires macOS')
    def test_macos_extract_retains_executable_and_framework_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / 'update.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                binary = zipfile.ZipInfo('BitFerry.app/Contents/MacOS/BitFerry')
                binary.create_system = 3
                binary.external_attr = (stat.S_IFREG | 0o755) << 16
                z.writestr(binary, b'#!/bin/sh\nexit 0\n')
                link = zipfile.ZipInfo('BitFerry.app/Contents/MacOS/current')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(link, 'BitFerry')
            dest = root / 'unpacked'
            app._extract_macos_update(archive, dest)
            executable = dest / 'BitFerry.app/Contents/MacOS/BitFerry'
            self.assertTrue(os.access(executable, os.X_OK))
            self.assertTrue(executable.with_name('current').is_symlink())

    def test_update_rejects_parent_traversal_before_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../outside', 'bad')
            with patch.object(app.subprocess, 'run') as run, self.assertRaises(ValueError):
                app._extract_macos_update(archive, Path(td) / 'dest')
            run.assert_not_called()

    def test_invalid_update_executable_does_not_start_installer(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / 'work'
            binary = work / 'BitFerry.app/Contents/MacOS/BitFerry'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'not executable')
            binary.chmod(0o644)
            with patch.object(app, '_current_app_bundle', return_value=Path(td) / 'current.app'), \
                    patch.object(app.tempfile, 'mkdtemp', return_value=str(work)), \
                    patch.object(app, '_extract_macos_update'), patch.object(app.subprocess, 'Popen') as launch:
                with self.assertRaises(RuntimeError):
                    app.apply_update_macos('unused.zip')
                launch.assert_not_called()


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = app.QApplication.instance() or app.QApplication([])

    def test_bubble_status_changes_without_creating_another_message(self):
        bubble = app.Bubble('text', 'hello', True, '12:00', 'me', pending=True, delivery='sending')
        self.addCleanup(bubble.deleteLater)
        self.assertIn('发送中', bubble._meta_label.text())
        bubble.set_delivery('error', 'connection refused')
        self.assertIn('发送失败', bubble._meta_label.text())
        self.assertEqual(bubble._meta_label.toolTip(), 'connection refused')
        bubble.set_delivery('ok')
        self.assertIn('对方已接收', bubble._meta_label.text())
        self.assertNotIn('离线', bubble._meta_label.text())

    def test_unsaved_offline_message_stays_in_input(self):
        w = SimpleNamespace(current_ip='ip', _source_check_cache={}, _source_key=lambda items: (), _prepare_source_check=lambda items, callback: True, _peer_name=lambda ip: 'Peer', _device_key=lambda ip: 'uid:peer', _peer_online=lambda ip: False,
                            _build_send_units=lambda: ([{'type': 'text', 'text': 'hello'}], [], []),
                            offline_queue={}, input=Mock(), pending=[{'type': 'file', 'path': 'attachment'}])
        with patch.object(app, 'save_offline_queue', return_value=False), patch.object(app.QMessageBox, 'warning'):
            app.MainWindow.action_send(w)
        w.input.reset.assert_not_called()
        self.assertEqual(w.pending, [{'type': 'file', 'path': 'attachment'}])
        self.assertEqual(w.offline_queue['ip'], [])


if __name__ == '__main__':
    unittest.main()
