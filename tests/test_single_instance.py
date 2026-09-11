"""Single-instance and receive-service lifecycle regression tests."""
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_reliability import app


class InstanceTests(unittest.TestCase):
    def test_windows_new_mutex_keeps_handle_until_release(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 0x123456789
        guard = app._SingleInstanceGuard()
        with patch.object(app.platform, 'system', return_value='Windows'), \
                patch.object(app, '_windows_mutex_api', return_value=(kernel, lambda: 0)):
            self.assertTrue(guard.acquire())
        kernel.CreateMutexW.assert_called_once_with(None, False, 'Global\\BitFerry-single-instance')
        kernel.CloseHandle.assert_not_called()
        guard.release()
        guard.release()
        kernel.CloseHandle.assert_called_once_with(0x123456789)

    def test_windows_existing_mutex_prevents_second_instance(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 42
        guard = app._SingleInstanceGuard()
        with patch.object(app.platform, 'system', return_value='Windows'), \
                patch.object(app, '_windows_mutex_api', return_value=(kernel, lambda: 183)):
            self.assertFalse(guard.acquire())
        kernel.CloseHandle.assert_called_once_with(42)

    def test_windows_permission_error_cannot_bypass_guard(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = None
        with patch.object(app.platform, 'system', return_value='Windows'), \
                patch.object(app, '_windows_mutex_api', return_value=(kernel, lambda: 5)):
            with self.assertRaises(RuntimeError):
                app._SingleInstanceGuard().acquire()

    def test_file_lock_is_exclusive_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as td, patch.object(app, 'APP_DATA', Path(td)), \
                patch.object(app.platform, 'system', return_value='Darwin'):
            first, second = app._SingleInstanceGuard(), app._SingleInstanceGuard()
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.release()
                self.assertTrue(second.acquire())
            finally:
                first.release()
                second.release()

    def test_activation_requires_response_and_accepts_split_response(self):
        conn = Mock()
        conn.waitForConnected.return_value = True
        conn.waitForReadyRead.return_value = True
        conn.readAll.side_effect = [b'ra', b'ised']
        with patch.object(app, 'QLocalSocket', return_value=conn):
            self.assertEqual(app._activate_existing_instance(), (True, True))
        conn.write.assert_called_once_with(b'raise')
        conn.abort.assert_called_once()

    def test_unresponsive_instance_is_not_reported_as_activated(self):
        conn = Mock()
        conn.waitForConnected.return_value = True
        conn.waitForReadyRead.return_value = False
        conn.readAll.return_value = b''
        with patch.object(app, 'QLocalSocket', return_value=conn):
            self.assertEqual(app._activate_existing_instance(), (True, False))

    def test_second_launch_never_creates_window_or_network_services(self):
        for responding in (True, False):
            with self.subTest(responding=responding), patch.object(app, 'QApplication'), \
                    patch.object(app, '_SingleInstanceGuard') as guard, \
                    patch.object(app, '_activate_existing_instance', return_value=(True, responding)), \
                    patch.object(app, 'MainWindow') as window, patch.object(app, 'QLocalServer') as server, \
                    patch.object(app.QMessageBox, 'information') as message:
                guard.return_value.acquire.return_value = False
                app.main()
                window.assert_not_called()
                server.assert_not_called()
                self.assertEqual(message.call_count, 0 if responding else 1)

    def test_local_server_failure_does_not_start_window(self):
        with patch.object(app, 'QApplication'), patch.object(app, '_SingleInstanceGuard') as guard, \
                patch.object(app, '_activate_existing_instance', return_value=(False, False)), \
                patch.object(app, 'MainWindow') as window, patch.object(app, 'QLocalServer') as server, \
                patch.object(app.QMessageBox, 'warning'):
            guard.return_value.acquire.return_value = True
            server.return_value.listen.return_value = False
            app.main()
            window.assert_not_called()
            guard.return_value.release.assert_called_once()


class ListenerTests(unittest.TestCase):
    def receiver(self, states):
        return app.Receiver(Mock(), Mock(), Mock(), on_status=lambda *args: states.append(args))

    def test_windows_uses_exclusive_bind_without_reuseaddr(self):
        sock = Mock()
        with patch.object(app.platform, 'system', return_value='Windows'), \
                patch.object(app.socket, 'SO_EXCLUSIVEADDRUSE', -5, create=True):
            app._configure_receive_socket(sock)
        sock.setsockopt.assert_called_once_with(app.socket.SOL_SOCKET, -5, 1)

    def test_bind_failure_reports_reason_closes_socket_and_can_retry(self):
        states = []
        receiver = self.receiver(states)
        bad, good = Mock(), Mock()
        bad.bind.side_effect = OSError('address already in use')
        def accepted():
            receiver.running = False
            raise OSError('test shutdown')
        good.accept.side_effect = accepted
        with patch.object(app.socket, 'socket', side_effect=[bad, good]):
            receiver.start()
            receiver._thread.join(2)
            self.assertFalse(receiver._thread.is_alive())
            self.assertFalse(states[-1][0])
            self.assertIn('address already in use', states[-1][1])
            bad.close.assert_called()
            receiver.start()
            receiver._thread.join(2)
        self.assertIn((True, ''), states)
        self.assertFalse(receiver._serve_lock.locked())

    def test_repeated_start_does_not_create_multiple_listener_threads(self):
        states, gate = [], threading.Event()
        receiver = self.receiver(states)
        sock = Mock()
        def accept():
            gate.wait(2)
            receiver.running = False
            raise OSError('shutdown')
        sock.accept.side_effect = accept
        with patch.object(app.socket, 'socket', return_value=sock) as factory:
            receiver.start()
            thread = receiver._thread
            receiver.start()
            self.assertIs(receiver._thread, thread)
            gate.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            factory.assert_called_once()

    def test_discovery_does_not_announce_before_receiver_ready(self):
        discovery = app.Discovery.__new__(app.Discovery)
        discovery.advertising = threading.Event()
        with patch.object(app.socket, 'socket') as factory:
            discovery.force_broadcast()
            discovery._unicast_reply('127.0.0.2')
            factory.assert_not_called()

    def test_status_updates_advertising_and_retry_button(self):
        w = SimpleNamespace(discovery=Mock(), recv_status_lbl=Mock(), btn_retry_receiver=Mock(), known={'ip': {}})
        app.MainWindow._on_receiver_status(w, False, 'port occupied')
        w.discovery.set_receiving.assert_called_with(False)
        w.btn_retry_receiver.setVisible.assert_called_with(True)
        w.recv_status_lbl.setToolTip.assert_called_with('port occupied')
        app.MainWindow._on_receiver_status(w, True, '')
        w.discovery.set_receiving.assert_called_with(True)
        w.btn_retry_receiver.setVisible.assert_called_with(False)
        w.discovery.ping_known.assert_called_once_with(['ip'])


class LocalChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = app.QApplication.instance() or app.QApplication([])

    def test_actual_local_channel_acknowledges_activation(self):
        import uuid
        server = app.QLocalServer()
        name = 'bitferry-test-' + uuid.uuid4().hex
        self.assertTrue(server.listen(name), server.errorString())
        activations = []
        def accept():
            conn = server.nextPendingConnection()
            activations.append(True)
            conn.write(b'raised')
            conn.flush()
            conn.disconnectFromServer()
        server.newConnection.connect(accept)
        results = []
        try:
            with patch.object(app, '_SINGLE_INSTANCE_KEY', name):
                thread = threading.Thread(target=lambda: results.append(app._activate_existing_instance(1000)))
                thread.start()
                deadline = time.monotonic() + 2
                while thread.is_alive() and time.monotonic() < deadline:
                    self.qt.processEvents()
                    time.sleep(0.005)
                thread.join(1)
                self.assertFalse(thread.is_alive())
            self.assertEqual(results, [(True, True)])
            self.assertEqual(activations, [True])
        finally:
            server.close()


if __name__ == '__main__':
    unittest.main()
