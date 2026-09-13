"""Product flows using real Qt widgets with isolated storage and no LAN services."""
import json
import threading
import time
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from test_reliability import app


class ProductTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = app.QApplication.instance() or app.QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='bitferry-product-test-'))
        self.root = Path(self.tmp)
        names = {'APP_DATA': self.root, 'IMG_DIR': self.root / 'images',
                 'RECV_ROOT': self.root / 'recv', 'DEFAULT_RECV_ROOT': self.root / 'recv',
                 'TEMP_RECV_DIR': self.root / 'temp', 'STAGED_FILE': self.root / 'staged.json',
                 'HISTORY_FILE': self.root / 'history.json', 'DEVICES_FILE': self.root / 'devices.json',
                 'SETTINGS_FILE': self.root / 'settings.json', 'OFFLINE_QUEUE_FILE': self.root / 'queue.json'}
        for name, path in names.items():
            self.stack.enter_context(patch.object(app, name, path))
        for cls, method in [(app.Discovery, 'start'), (app.Discovery, 'ping_known'),
                            (app.Receiver, 'start'), (app.WebBridge, 'start'),
                            (app.MainWindow, '_install_shortcut'), (app.MainWindow, '_build_tray'),
                            (app.MainWindow, 'action_check_update'), (app.MainWindow, '_bring_to_front')]:
            self.stack.enter_context(patch.object(cls, method))
        self.stack.enter_context(patch.object(app, 'get_local_ip', return_value='192.168.1.1'))
        self.w = app.MainWindow()
        self.w.known = {'192.168.1.2': {'name': 'Peer', 'uid': 'peer'}}
        self.w.current_ip = '192.168.1.2'
        self.w._draft_key = 'uid:peer'
        self.addCleanup(self.dispose)

    def dispose(self):
        self.w._draft_timer.stop()
        self.w._draft_writer.close()
        self.w._diagnostics.close()
        for _, cancel in self.w._folder_scans.values():
            cancel.set()
        self.w.deleteLater()
        self.qt.processEvents()

    def pump(self, until):
        deadline = time.monotonic() + 3
        while not until() and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(.005)
        self.assertTrue(until())

    def make_image(self):
        path = self.root / 'image.png'
        pix = app.QPixmap(64, 32)
        pix.fill(app.QColor('red'))
        pix.save(str(path))
        return path

    def test_ip_reuse_does_not_send_old_queue_to_new_device(self):
        ip = self.w.current_ip
        self.w.input.insertPlainText('private')
        self.w.action_send()
        self.w.online = {ip: {'name': 'Other', 'uid': 'other', 'online': True}}
        with patch.object(self.w, '_send_text_unit') as send:
            self.w._flush_offline_queue(ip)
            send.assert_not_called()
        self.w.online['192.168.1.9'] = {'name': 'Peer', 'uid': 'peer', 'online': True}
        with patch.object(self.w, '_send_text_unit') as send:
            self.w._flush_offline_queue(ip)
            self.assertEqual(send.call_args.kwargs['target_ip'], '192.168.1.9')

    def test_failed_cancellation_keeps_task_and_does_not_claim_cancelled(self):
        ip = self.w.current_ip
        self.w.input.insertPlainText('keep')
        self.w.action_send()
        qid = self.w.offline_queue[ip][0]['msg_id']
        with patch.object(app, 'save_offline_queue', return_value=False), patch.object(app.QMessageBox, 'warning') as warning:
            self.w._cancel_queue_component(ip, qid, 'text')
            warning.assert_called_once()
        self.assertEqual(len(self.w.offline_queue[ip]), 1)
        self.assertNotEqual(self.w.history[ip][-1]['delivery'], 'cancelled')
        self.assertEqual(len(app.load_offline_queue()[ip]), 1)

    def test_history_atomic_replace_failure_keeps_original(self):
        app.HISTORY_FILE.write_text('{"old": []}')
        self.w.history = {'new': []}
        original = app.os.replace
        def replace(src, dst):
            if Path(dst) == app.HISTORY_FILE:
                raise OSError('disk failure')
            return original(src, dst)
        with patch.object(app.os, 'replace', side_effect=replace):
            self.assertFalse(self.w._save_history())
        self.assertEqual(json.loads(app.HISTORY_FILE.read_text()), {'old': []})
        app.HISTORY_FILE.write_text('broken')
        self.assertEqual(self.w._read_legacy_history(), {'old': []})

    def test_accept_folder_runs_in_background_and_ignores_duplicate_click(self):
        import shutil
        path = self.stage()
        path.unlink(); path.mkdir(); (path / 'child').write_text('keep')
        entered, release = threading.Event(), threading.Event()
        original = shutil.copytree
        def slow(*args, **kwargs):
            entered.set(); release.wait(2)
            return original(*args, **kwargs)
        with patch.object(shutil, 'copytree', side_effect=slow):
            self.w._resolve_staged(self.w.current_ip, {str(path)})
            self.assertTrue(entered.wait(1))
            self.w._resolve_staged(self.w.current_ip, {str(path)})
            self.assertEqual(len(self.w._staged_processing), 1)
            self.qt.processEvents()
            release.set()
            self.pump(lambda: not self.w._staged_processing)
        self.assertEqual((app.RECV_ROOT / 'Peer/a.txt/child').read_text(), 'keep')
        self.assertEqual(len(self.w.history[self.w.current_ip]), 1)

    def test_concurrent_phone_uploads_keep_both_same_named_files(self):
        import io
        from types import SimpleNamespace
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(2)
        reserve = app._reserve_recv_path
        def synchronize(*args, **kwargs):
            dest = reserve(*args, **kwargs)
            barrier.wait(2)
            return dest
        def upload(data):
            handler = SimpleNamespace(headers={'Content-Length': str(len(data))},
                rfile=io.BytesIO(data), bridge=Mock(), _reply_json=Mock())
            app._WebHandler._upload(handler, 'cid', 'Phone', {'filename': 'same.txt'})
            handler._reply_json.assert_called_once_with({'ok': True})
        with patch.object(app, '_reserve_recv_path', side_effect=synchronize), ThreadPoolExecutor(2) as pool:
            list(pool.map(upload, [b'first', b'second']))
        self.assertEqual({p.read_bytes() for p in (app.RECV_ROOT / 'Phone').iterdir()}, {b'first', b'second'})

    def test_persistent_drafts_restore_text_images_files_and_cursor(self):
        image = self.make_image()
        file = self.root / 'attachment.txt'; file.write_text('file')
        self.w.input.insertPlainText('draft one')
        self.w.input.add_inline_image(str(image))
        self.w.pending = [{'type': 'file', 'path': str(file), 'name': file.name}]
        expected = self.w.input.extract_content()
        self.w._switch_draft('uid:other')
        self.w.input.insertPlainText('draft two')
        self.w._save_drafts()
        self.w._draft_writer.flush()
        other = app.MainWindow()
        try:
            other._switch_draft('uid:peer')
            self.assertEqual(other.input.extract_content(), expected)
            self.assertEqual(other.pending[0]['path'], str(file))
            other._switch_draft('uid:other')
            self.assertEqual(other.input.toPlainText(), 'draft two')
        finally:
            other._draft_timer.stop()
            other._draft_writer.close()
            other._diagnostics.close()
            other.deleteLater()

    def test_modified_retry_requires_confirmation_and_missing_file_blocks(self):
        file = self.root / 'original.txt'; file.write_text('old')
        self.w._attach_paths([str(file)]); self.w.action_send()
        ip = self.w.current_ip
        entry = self.w.offline_queue[ip][0]
        unit = entry['file_units'][0]; qid = entry['msg_id']; uid = unit['transfer_id']
        original = unit['send_items'][0]['source_version']
        file.write_text('new content')
        with patch.object(app.QMessageBox, 'question', return_value=app.QMessageBox.StandardButton.No), patch.object(self.w, '_flush_offline_queue') as flush:
            self.assertFalse(self.w._retry_queue_component(ip, qid, uid))
            flush.assert_not_called()
        self.assertEqual(unit['send_items'][0]['source_version'], original)
        with patch.object(app.QMessageBox, 'question', return_value=app.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.w._retry_queue_component(ip, qid, uid))
        self.assertNotEqual(unit['send_items'][0]['source_version'], original)
        file.unlink()
        with patch.object(app.QMessageBox, 'warning') as warning:
            self.assertFalse(self.w._retry_queue_component(ip, qid, uid))
            warning.assert_called_once()

    def test_delete_device_can_cancel_or_keep_tasks_and_handles_save_failure(self):
        ip = self.w.current_ip
        self.w.input.insertPlainText('queued'); self.w.action_send()
        box = Mock(); discard, keep = object(), object()
        box.addButton.side_effect = [discard, keep, object()]
        box.clickedButton.return_value = discard
        with patch.object(app, 'QMessageBox', return_value=box) as cls:
            cls.ButtonRole.DestructiveRole = 0; cls.ButtonRole.ActionRole = 1
            cls.StandardButton.Cancel = 2
            with patch.object(app, 'save_offline_queue', return_value=False):
                self.w._delete_peer(ip)
        self.assertIn(ip, self.w.known)
        self.assertEqual(len(self.w.offline_queue[ip]), 1)
        box.addButton.side_effect = [discard, keep, object()]
        with patch.object(app, 'QMessageBox', return_value=box):
            self.w._delete_peer(ip)
        self.assertNotIn(ip, self.w.known)
        self.assertEqual(self.w.offline_queue[ip], [])
        self.assertEqual(app.load_offline_queue()[ip], [])

    def test_phone_upload_enters_pending_confirmation(self):
        import io
        from types import SimpleNamespace
        app.set_setting('recv_confirm', True)
        chat = Mock()
        bridge = app.WebBridge('Host', chat, Mock(), Mock())
        handler = SimpleNamespace(headers={'Content-Length': '4'}, rfile=io.BytesIO(b'data'),
                                  bridge=bridge, _reply_json=Mock())
        app._WebHandler._upload(handler, 'phone', 'Phone', {'filename': 'test.txt'})
        args = chat.call_args.args
        self.assertEqual(args[2], 'batch_staged')
        part = json.loads(args[3])[0]
        self.assertTrue(app._safe_staged_path(part['path']))
        self.assertFalse((app.RECV_ROOT / 'Phone/test.txt').exists())
        self.w._on_chat_in(*args)
        self.assertTrue(app._load_staged_receives())
        bridge.stop()

    def test_closed_history_dialog_is_destroyed(self):
        from PyQt6.QtCore import QCoreApplication, QEvent
        for _ in range(5):
            dlg = app.HistoryDialog(self.w, 'Peer', [])
            dlg.show(); dlg.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.w.findChildren(app.HistoryDialog), [])

    def test_edit_annotation_text_preserves_position_color_font_and_cancel(self):
        from PyQt6.QtWidgets import QInputDialog
        canvas = app._EditorCanvas(app.QPixmap(200, 100))
        ann = app._Annotation('text', app.QColor('red'), 4)
        ann.text = 'before'; ann.p1 = app.QPoint(10, 20); ann.font = app.QFont()
        ann.font.setPointSize(24); canvas._annotations.append(ann)
        with patch.object(QInputDialog, 'getMultiLineText', return_value=('after', True)):
            self.assertTrue(app._edit_annotation_text(canvas, 0))
        self.assertEqual((ann.text, ann.p1, ann.font.pointSize(), ann.color.name()),
                         ('after', app.QPoint(10, 20), 24, '#ff0000'))
        with patch.object(QInputDialog, 'getMultiLineText', return_value=('discarded', False)):
            app._edit_annotation_text(canvas, 0)
        self.assertEqual(ann.text, 'after')
        canvas.deleteLater()

    def test_history_dialog_queries_database_page_without_loading_full_history(self):
        store = self.w.history
        for i in range(125):
            store.append(self.w.current_ip, {'kind': 'text', 'payload': f'历史 {i}', 'mine': True,
                'ts': '12:00', 'name': 'Me', 'seq': i, 'day': '2026-09-11'})
        store.save()
        dlg = app.HistoryDialog(self.w, 'Peer', [], store=store, ips=[self.w.current_ip])
        try:
            self.pump(lambda: '125' in dlg.page_label.text())
            self.assertEqual(len(dlg.scroll.widget().findChildren(app.Bubble)), 25)
            dlg.search.setText('历史 124')
            self.pump(lambda: '· 1 条' in dlg.page_label.text())
            self.assertEqual(len(dlg.scroll.widget().findChildren(app.Bubble)), 1)
        finally:
            dlg.close()

    def test_saved_screenshot_text_can_be_edited_after_reopening(self):
        from PyQt6.QtWidgets import QInputDialog
        app.IMG_DIR.mkdir(parents=True, exist_ok=True)
        base = app.QPixmap(200, 100); base.fill(app.QColor('white'))
        canvas = app._EditorCanvas(base)
        ann = app._Annotation('text', app.QColor('red'), 3)
        ann.p1 = app.QPoint(10, 10); ann.text = 'Original'; ann.font = app.QFont()
        ann.font.setPointSize(18); canvas._annotations.append(ann)
        path = app.IMG_DIR / 'editable.png'
        canvas.result_pixmap().save(str(path), 'PNG')
        app._save_editable_layers(path, base, canvas._annotations)
        editor = app.ImageEditorDialog(self.w, str(path))
        self.assertEqual(editor.canvas._annotations[0].text, 'Original')
        with patch.object(QInputDialog, 'getMultiLineText', return_value=('Revised', True)):
            app._edit_annotation_text(editor.canvas, 0)
        editor._confirm()
        again = app.ImageEditorDialog(self.w, editor.result_path)
        self.assertEqual(again.canvas._annotations[0].text, 'Revised')
        self.assertEqual(again.canvas._annotations[0].font.pointSize(), 18)
        editor.deleteLater(); again.deleteLater(); canvas.deleteLater()

    def test_retina_layer_coordinates_and_fonts_are_converted(self):
        app.IMG_DIR.mkdir(parents=True, exist_ok=True)
        base = app.QPixmap(200, 100); base.fill(app.QColor('white'))
        path = app.IMG_DIR / 'retina.png'; base.save(str(path))
        ann = app._Annotation('text', app.QColor('blue'), 3)
        ann.p1 = app.QPoint(30, 40); ann.text = 'Retina'; ann.font = app.QFont()
        ann.font.setPointSize(14)
        app._save_editable_layers(path, base, [ann], app.QPoint(20, 30), 2)
        loaded = app._load_editable_layers(path)[1][0]
        self.assertEqual(loaded.p1, app.QPoint(20, 20))
        self.assertEqual(loaded.font.pointSize(), 28)

    def test_delete_device_keep_choice_preserves_queue(self):
        ip = self.w.current_ip
        self.w.input.insertPlainText('queued'); self.w.action_send()
        box = Mock(); discard, keep = object(), object()
        box.addButton.side_effect = [discard, keep, object()]
        box.clickedButton.return_value = keep
        with patch.object(app, 'QMessageBox', return_value=box):
            self.w._delete_peer(ip)
        self.assertNotIn(ip, self.w.known)
        self.assertEqual(len(app.load_offline_queue()[ip]), 1)

    def test_web_enter_during_composition_does_not_send(self):
        import re, shutil, subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is only needed for the browser keyboard regression')
        script = Path(app.__file__).read_text(encoding='utf-8')
        body = re.search(r"inp.addEventListener\('keydown', function\(e\)\{(.*?)\n\}\);", script, re.S).group(1)
        harness = "let sent=0; function sendText(){sent++}; function key(e){" + body + "};" + """
          const normal={key:'Enter', shiftKey:false, isComposing:false, keyCode:13, preventDefault(){}};
          key({...normal, isComposing:true}); key({...normal,keyCode:229}); key({...normal,shiftKey:true});
          if(sent!==0) throw Error('composition or newline sent');
          key(normal); if(sent!==1) throw Error('ordinary Enter failed');
        """
        subprocess.run([node, '-e', harness], check=True, capture_output=True)

    def test_settings_name_and_screenshot_options_sync_with_main_controls(self):
        from PyQt6.QtWidgets import QInputDialog
        dlg = app.SettingsDialog(self.w)
        self.addCleanup(dlg.deleteLater)
        dlg.device_name_input.setText("  办公电脑  ")
        dlg.chk_shot_hide.setChecked(False)
        with patch.object(self.w.discovery, 'update_name') as broadcast:
            dlg._save_and_accept()
            broadcast.assert_called_once_with('办公电脑')
        self.assertEqual(self.w.hostname, '办公电脑')
        self.assertEqual(self.w.self_name_lbl.text(), '办公电脑')
        self.assertEqual(self.w.webbridge.hostname, '办公电脑')
        self.assertEqual(app.get_setting('device_name'), '办公电脑')
        self.assertFalse(app.load_shot_hide_window())
        self.assertFalse(self.w.act_shot_hide.isChecked())
        with patch.object(QInputDialog, 'getText', return_value=('前台改名', True)):
            self.w.action_rename_device()
        self.w.act_shot_hide.setChecked(True)
        reopened = app.SettingsDialog(self.w)
        self.addCleanup(reopened.deleteLater)
        self.assertEqual(reopened.device_name_input.text(), '前台改名')
        self.assertTrue(reopened.chk_shot_hide.isChecked())
        reopened.reject()

    def test_settings_cancel_and_blank_name_do_not_apply_changes(self):
        original_name = self.w.hostname
        original_hide = app.load_shot_hide_window()
        dlg = app.SettingsDialog(self.w)
        self.addCleanup(dlg.deleteLater)
        dlg.device_name_input.setText('未保存的名字')
        dlg.chk_shot_hide.setChecked(not original_hide)
        dlg.reject()
        self.assertEqual(self.w.hostname, original_name)
        self.assertEqual(app.load_shot_hide_window(), original_hide)
        dlg.device_name_input.setText('   ')
        with patch.object(app.QMessageBox, 'warning') as warning:
            dlg._save_and_accept()
            warning.assert_called_once()
        self.assertEqual(self.w.hostname, original_name)
        self.assertEqual(app.load_shot_hide_window(), original_hide)

    def test_dark_settings_labels_update_after_light_theme_preview(self):
        from PyQt6.QtGui import QPalette
        dlg = app.SettingsDialog(self.w)
        try:
            dlg._on_swatch_click('matcha')
            for theme in ('midnight', 'dusk', 'noir'):
                dlg._on_swatch_click(theme)
                dlg.ensurePolished()
                for widget in (dlg.radio_auto, dlg.radio_confirm, dlg.chk_focus_shot, dlg.chk_close_tray):
                    widget.ensurePolished()
                    color = widget.palette().color(QPalette.ColorRole.WindowText)
                    self.assertGreater(color.lightness(), 150)
        finally:
            dlg.close(); dlg.deleteLater()

    def test_main_chat_loads_recent_messages_and_bounded_older_pages(self):
        ip = self.w.current_ip
        self.w.session[ip] = [{'kind': 'text', 'payload': str(i), 'mine': True, 'name': 'Me', 'ts': '12:00', 'seq': i} for i in range(1200)]
        self.w._render_session(ip)
        self.assertEqual(self.w.chat_layout.count() - 1, 100)
        self.w._load_older_session()
        self.assertEqual(self.w.chat_layout.count() - 1, 200)
        for _ in range(6):
            self.w._load_older_session()
        self.assertLessEqual(self.w.chat_layout.count() - 1, 500)
        self.assertLess(self.w._session_start, 1000)

    def test_latest_button_only_appears_away_from_latest_messages(self):
        ip = self.w.current_ip
        self.w.right.setCurrentIndex(1)
        self.w.show()
        self.w._render_session(ip)
        self.pump(lambda: not self.w._rendering_session)
        self.assertTrue(self.w.btn_latest.isHidden())
        self.w.session[ip] = [
            {'kind': 'text', 'payload': str(i), 'mine': True,
             'name': 'Me', 'ts': '12:00', 'seq': i} for i in range(700)]
        self.w._render_session(ip)
        self.pump(lambda: not self.w._rendering_session)
        bar = self.w.chat_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertTrue(self.w.btn_latest.isHidden())
        bar.setValue(bar.maximum() // 2)
        self.assertFalse(self.w.btn_latest.isHidden())
        bar.setValue(bar.maximum())
        self.assertTrue(self.w.btn_latest.isHidden())
        for _ in range(5):
            self.w._load_older_session()
            self.pump(lambda: not self.w._rendering_session)
        self.assertLess(self.w._session_end, 700)
        bar.setValue(bar.maximum())
        self.assertFalse(self.w.btn_latest.isHidden())
        self.w.btn_latest.click()
        self.pump(lambda: not self.w._rendering_session)
        self.assertEqual(self.w._session_end, 700)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(self.w.btn_latest.isHidden())

    def test_mobile_restart_generates_new_ids(self):
        first = app.WebBridge('Host', Mock(), Mock(), Mock())
        second = app.WebBridge('Host', Mock(), Mock(), Mock())
        first.note_text('cid', 'Phone', ['a'])
        second.note_text('cid', 'Phone', ['b'])
        self.assertNotEqual(first.client_log('cid')[0]['id'], second.client_log('cid')[0]['id'])

    def test_large_attachment_validation_does_not_send_after_switching_device(self):
        files = []
        for n in range(65):
            file = self.root / f'{n}.bin'; file.write_bytes(b'x')
            files.append({'type': 'file', 'path': str(file), 'name': file.name})
        self.w.pending = files
        entered, release = threading.Event(), threading.Event()
        original = self.w._source_version
        def slow(path):
            entered.set(); release.wait(2); return original(path)
        with patch.object(self.w, '_source_version', side_effect=slow):
            self.w.action_send()
            self.assertTrue(entered.wait(1))
            self.w._switch_draft('uid:other')
            self.w.current_ip = '192.168.1.9'
            release.set()
            self.pump(lambda: not self.w._background_jobs)
        self.assertFalse(self.w.offline_queue)

    def test_queue_batch_cancel_only_selected_rows(self):
        from PyQt6.QtWidgets import QTableWidgetSelectionRange
        for text in ('first', 'second', 'third'):
            self.w.input.insertPlainText(text); self.w.action_send()
        dlg = app.SendQueueDialog(self.w)
        try:
            dlg.table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 4), True)
            dlg._batch(False)
            self.assertEqual(len(self.w._queue_rows()), 1)
            self.assertIn('third', self.w._queue_rows()[0][3])
        finally:
            dlg.close()

    def test_history_search_locates_original_context(self):
        store = self.w.history; ip = self.w.current_ip
        records = []
        for i in range(110):
            rec = {'kind': 'text', 'payload': f'message {i}', 'mine': True, 'ts': '12:00', 'day': '2026-09-11', 'seq': i, 'name': 'Me', 'msg_id': str(i)}
            records.append(rec); store.append(ip, rec)
        store.save()
        self.assertEqual(store.context_page([ip], records[74]), 1)
        dlg = app.HistoryDialog(self.w, 'Peer', [], store=store, ips=[ip])
        try:
            self.pump(lambda: '110' in dlg.page_label.text())
            dlg._locate_message(records[74])
            self.pump(lambda: dlg._page == 1)
            self.assertEqual(dlg.search.text(), '')
            self.assertEqual(len(dlg.scroll.widget().findChildren(app.Bubble)), 50)
        finally:
            dlg.close()

    def test_operation_log_excludes_chat_body(self):
        self.w._on_chat_in('Peer', self.w.current_ip, 'text', 'PRIVATE MESSAGE CONTENT')
        self.w._diagnostics.flush()
        lines = '\n'.join(self.w._diagnostics.recent)
        self.assertNotIn('PRIVATE MESSAGE CONTENT', lines)
        self.assertIn('收到消息', lines)

    def test_exit_waits_for_active_transfers_and_can_be_cancelled(self):
        from PyQt6.QtGui import QCloseEvent
        self.w._force_quit = True
        self.w._text_controls['active'] = app.TransferControl()
        event = QCloseEvent()
        with patch.object(app.QMessageBox, 'question', return_value=app.QMessageBox.StandardButton.Yes):
            self.w.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.w._waiting_exit)
        self.w._cancel_wait_exit()
        self.assertFalse(self.w._waiting_exit)
        self.w._text_controls.clear()

    def test_pending_exit_runs_only_after_transfer_finishes(self):
        self.w._waiting_exit = True
        self.w._text_controls['active'] = app.TransferControl()
        with patch.object(self.w, 'close', return_value=True) as close, patch.object(app.QApplication, 'quit') as quit_app:
            self.w._wait_for_exit()
            close.assert_not_called()
            self.w._text_controls.clear()
            self.w._wait_for_exit()
            close.assert_called_once()
            quit_app.assert_called_once()
        self.w._waiting_exit = False

    def test_phone_download_registration_survives_restart(self):
        file = self.root / 'download.txt'; file.write_text('download')
        first = app.WebBridge('Host', Mock(), Mock(), Mock())
        fid = first.register_download(str(file))
        second = app.WebBridge('Host', Mock(), Mock(), Mock())
        self.assertEqual(second.get_download(fid), str(file))

    def test_new_message_does_not_replace_older_chat_page(self):
        ip = self.w.current_ip
        self.w.session[ip] = [{'kind': 'text', 'payload': str(i), 'mine': True, 'name': 'Me', 'ts': '12:00', 'seq': i} for i in range(900)]
        self.w._render_session(ip)
        for _ in range(6):
            self.w._load_older_session()
        end = self.w._session_end
        count = self.w.chat_layout.count()
        rec = {'kind': 'text', 'payload': 'new', 'mine': False, 'name': 'Peer', 'ts': '12:01', 'seq': 901}
        self.w.session[ip].append(rec)
        self.w._add_bubble_widget(rec)
        self.assertEqual(self.w._session_end, end)
        self.assertEqual(self.w.chat_layout.count(), count)

    def test_drafts_preserve_text_images_attachments_and_cursor(self):
        image = self.make_image()
        self.w.input.insertPlainText('first draft')
        self.w.input.add_inline_image(str(image))
        self.w.pending = [{'type': 'file', 'name': 'a', 'path': '/a'}]
        original = self.w.input.extract_content()
        self.w._switch_draft('uid:other')
        self.assertEqual(self.w.input.extract_content(), [])
        self.assertEqual(self.w.pending, [])
        self.w.input.insertPlainText('second draft')
        self.w._switch_draft('uid:peer')
        self.assertEqual(self.w.input.extract_content(), original)
        self.assertEqual(self.w.pending[0]['name'], 'a')
        self.w._switch_draft('uid:other')
        self.assertEqual(self.w.input.toPlainText(), 'second draft')

    def test_reselect_same_device_does_not_reset_draft(self):
        self.w.input.insertPlainText('keep me')
        self.w._switch_draft('uid:peer')
        self.assertEqual(self.w.input.toPlainText(), 'keep me')

    def test_device_learning_uid_preserves_visible_draft(self):
        self.w._draft_key = 'name:Peer'
        self.w.input.insertPlainText('keep while identity updates')
        item = app.QListWidgetItem()
        item.setData(app.Qt.ItemDataRole.UserRole, self.w.current_ip)
        self.w._on_peer_selected(item, None)
        self.assertEqual(self.w.input.toPlainText(), 'keep while identity updates')
        self.assertEqual(self.w._draft_key, 'uid:peer')

    def test_folder_scan_runs_in_worker_and_result_stays_with_original_draft(self):
        folder = self.root / 'folder'
        folder.mkdir()
        (folder / 'a.txt').write_bytes(b'123')
        (folder / 'b.txt').write_bytes(b'4567')
        gate, entered = threading.Event(), threading.Event()
        original = app._scan_folder
        threads = []
        def blocked(*args):
            threads.append(threading.current_thread())
            entered.set()
            gate.wait(2)
            return original(*args)
        with patch.object(app, '_scan_folder', side_effect=blocked):
            self.w._attach_paths([str(folder)])
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.w.pending[0]['scan_state'], 'scanning')
            self.w._switch_draft('uid:other')
            gate.set()
            self.pump(lambda: not self.w._folder_scans)
        self.assertNotEqual(threads[0], threading.current_thread())
        self.assertEqual(self.w.pending, [])
        self.w._switch_draft('uid:peer')
        item = self.w.pending[0]
        self.assertEqual((item['count'], item['size'], item['scan_state']), (2, 7, 'ready'))
        with patch.object(Path, 'rglob', side_effect=AssertionError('rescanned on UI thread')):
            built = self.w._build_send_units()
        self.assertEqual(len(built[2][0]['send_items']), 2)
        self.assertEqual(built[2][0]['record']['size'], 7)

    def test_scan_failure_keeps_attachment_and_prevents_sending(self):
        folder = self.root / 'folder'
        folder.mkdir()
        with patch.object(app, '_scan_folder', side_effect=PermissionError('denied')):
            self.w._attach_paths([str(folder)])
            self.pump(lambda: not self.w._folder_scans)
        self.assertEqual(self.w.pending[0]['scan_state'], 'error')
        with patch.object(app.QMessageBox, 'information'):
            self.assertIsNone(self.w._build_send_units())

    def test_offline_queue_dialog_lists_and_cancels_individual_components(self):
        source = self.root / 'a.txt'
        source.write_text('file')
        self.w.input.insertPlainText('hello')
        self.w._attach_paths([str(source)])
        self.w.action_send()
        self.assertEqual(len(self.w._queue_rows()), 2)
        dialog = app.SendQueueDialog(self.w)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.table.rowCount(), 2)
        ip, qid, component, *_ = self.w._queue_rows()[0]
        dialog._cancel(ip, qid, component)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(self.w.history[ip][0]['delivery'], 'cancelled')
        self.assertEqual(len(json.loads(app.OFFLINE_QUEUE_FILE.read_text())[ip]), 1)

    def test_failed_text_retry_uses_same_content_and_message_id(self):
        ip = self.w.current_ip
        self.w.online[ip] = {'name': 'Peer', 'uid': 'peer', 'online': True, 'port': 1}
        self.w.input.insertPlainText('hello')
        calls = []
        def send(items, *args, **kwargs):
            calls.append((items, kwargs['transfer_id']))
            return 'error' if len(calls) == 1 else 'ok'
        with patch.object(app, 'send_batch', side_effect=send):
            self.w.action_send()
            self.pump(lambda: bool(self.w.history.get(ip)) and self.w.history[ip][0].get('delivery') == 'error')
            record = self.w.history[ip][0]
            self.assertEqual(self.w.input.toPlainText(), '')
            self.w._retry_message(record['msg_id'])
            self.pump(lambda: record['delivery'] == 'ok')
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(self.w.history[ip]), 1)
        self.assertEqual(self.w.offline_queue[ip], [])

    def test_failed_file_retry_updates_original_card(self):
        ip = self.w.current_ip
        self.w.online[ip] = {'name': 'Peer', 'uid': 'peer', 'online': True, 'port': 1}
        source = self.root / 'a.txt'
        source.write_text('file')
        self.w._attach_paths([str(source)])
        calls = []
        def send(items, *args, **kwargs):
            calls.append((items, kwargs['transfer_id']))
            return 'error' if len(calls) == 1 else 'ok'
        with patch.object(app, 'send_batch', side_effect=send):
            self.w.action_send()
            self.pump(lambda: self.w.history[ip][0].get('delivery') == 'error')
            record = self.w.history[ip][0]
            self.w._retry_message(record['msg_id'])
            self.pump(lambda: record['delivery'] == 'ok')
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(self.w.history[ip]), 1)

    def test_cancelled_message_can_be_restored_from_history(self):
        self.w.input.insertPlainText('restore')
        self.w.action_send()
        ip, qid, component, *_ = self.w._queue_rows()[0]
        self.w._cancel_queue_component(ip, qid, component)
        self.assertEqual(self.w.offline_queue[ip], [])
        self.w._retry_message(qid)
        self.assertEqual(self.w.offline_queue[ip][0]['text_items'][0]['text'], 'restore')
        self.assertEqual(self.w.history[ip][0]['delivery'], 'queued')

    def test_unknown_delivery_retry_requires_user_decision(self):
        self.w.input.insertPlainText('unknown')
        self.w.action_send()
        ip, qid, component, *_ = self.w._queue_rows()[0]
        entry = self.w.offline_queue[ip][0]
        entry['text_status'] = 'unconfirmed'
        with patch.object(app.QMessageBox, 'question', return_value=app.QMessageBox.StandardButton.No):
            self.w._retry_queue_component(ip, qid, component)
        self.assertEqual(entry['text_status'], 'unconfirmed')

    def test_retry_only_sends_selected_task(self):
        ip = self.w.current_ip
        for text in ('first', 'second'):
            self.w.input.insertPlainText(text)
            self.w.action_send()
        entries = self.w.offline_queue[ip]
        for entry in entries:
            entry['text_status'] = 'error'
        self.w.online[ip] = {'name': 'Peer', 'uid': 'peer', 'online': True, 'port': 1}
        with patch.object(app, 'send_batch', return_value='ok') as send:
            self.w._retry_queue_component(ip, entries[0]['msg_id'], 'text')
            self.pump(lambda: len(self.w.offline_queue[ip]) == 1)
            send.assert_called_once()
        self.assertEqual(self.w.offline_queue[ip][0]['text_items'][0]['text'], 'second')

    def test_missing_phone_file_stays_failed_in_queue(self):
        ip = 'web:phone'
        self.w.current_ip = ip
        self.w.known[ip] = {'name': 'Phone', 'uid': 'web-phone'}
        path = self.root / 'gone.txt'
        path.write_text('temp')
        self.w._attach_paths([str(path)])
        self.w.action_send()
        path.unlink()
        self.w.online[ip] = {'name': 'Phone', 'uid': 'web-phone', 'online': True, 'port': 1}
        self.w._flush_offline_queue(ip)
        self.assertEqual(self.w._queue_rows()[0][-1], 'error')
        self.assertFalse(self.w._offline_inflight)

    def test_recovery_replaces_stale_sending_label(self):
        record = {'msg_id': 'm', 'delivery': 'sending', 'pending': True}
        self.w.history = {'ip': [record]}
        self.w._recover_send_states()
        self.assertEqual(record['delivery'], 'unconfirmed')
        self.assertFalse(record['pending'])

    def test_cancel_interrupts_blocked_sender_socket(self):
        left, right = app.socket.socketpair()
        control = app.TransferControl()
        control.attach_socket(left)
        entered, finished = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            try:
                left.sendall(b'x' * (8 * 1024 * 1024))
            except OSError:
                pass
            finally:
                finished.set()
        thread = threading.Thread(target=blocked)
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            control.cancel()
            self.assertTrue(finished.wait(2))
            self.assertTrue(control.is_cancelled)
        finally:
            left.close(); right.close(); thread.join(2)

    def test_restart_marks_interrupted_sends_unconfirmed(self):
        app.save_offline_queue({'ip': [{'msg_id': 'm', 'text_status': 'sending', 'text_items': [],
                                       'file_units': [{'transfer_id': 'f', 'status': 'sending'}]}]})
        restored = app.load_offline_queue()['ip'][0]
        self.assertEqual(restored['text_status'], 'unconfirmed')
        self.assertEqual(restored['file_units'][0]['status'], 'unconfirmed')

    def stage(self):
        path = app.TEMP_RECV_DIR / 'Peer/a.txt'
        path.parent.mkdir(parents=True)
        path.write_text('keep')
        parts = [{'type': 'file', 'name': 'a.txt', 'path': str(path), 'size': 4,
                  'staged': True, 'final_dir': str(app.RECV_ROOT / 'Peer')}]
        self.w.staged_receives = {self.w.current_ip: [{'name': 'Peer', 'parts': parts}]}
        self.w._save_staged_receives()
        return path

    def test_pending_receives_restore_and_accept_after_restart(self):
        path = self.stage()
        self.w.staged_receives = app._load_staged_receives()
        self.assertEqual(self.w._staged_files(self.w.current_ip)[0][1]['path'], str(path))
        self.w._resolve_staged(self.w.current_ip, {str(path)})
        self.pump(lambda: not self.w._staged_processing)
        self.assertEqual((app.RECV_ROOT / 'Peer/a.txt').read_text(), 'keep')
        self.assertEqual(app._load_staged_receives(), {})
        self.assertFalse(path.exists())

    def test_failed_accept_keeps_file_and_persistent_pending_entry(self):
        path = self.stage()
        with patch.object(app, '_reserve_recv_path', side_effect=PermissionError('denied')):
            self.w._resolve_staged(self.w.current_ip, {str(path)})
            self.pump(lambda: not self.w._staged_processing)
        self.assertEqual(path.read_text(), 'keep')
        self.assertTrue(app._load_staged_receives())
        self.assertEqual(self.w.history, {})

    def test_history_search_and_pagination_limit_widgets(self):
        records = [{'kind': 'text', 'payload': f'message {i}', 'mine': True, 'ts': '12:00', 'name': 'Me',
                    'day': '2026-09-11'} for i in range(123)]
        records[0]['payload'] = 'special report.pdf'
        dialog = app.HistoryDialog(self.w, 'Peer', records)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(len(dialog.scroll.widget().findChildren(app.Bubble)), 23)
        dialog._show_page(0)
        self.assertEqual(len(dialog.scroll.widget().findChildren(app.Bubble)), 50)
        dialog.search.setText('REPORT.PDF')
        dialog._search()
        self.pump(lambda: len(dialog._matches) == 1)
        self.assertEqual(len(dialog.scroll.widget().findChildren(app.Bubble)), 1)
        self.assertFalse(dialog.next.isEnabled())
        self.assertFalse(dialog.previous.isEnabled())

    def test_history_ignores_stale_search_result(self):
        dialog = app.HistoryDialog(self.w, 'Peer', [])
        self.addCleanup(dialog.deleteLater)
        dialog._generation = 2
        dialog._apply_filter(1, [{'stale': True}])
        self.assertEqual(dialog._matches, [])

    def test_search_matches_escaped_legacy_chinese_filename(self):
        record = {'kind': 'batch', 'payload': json.dumps([{'type': 'file', 'name': '项目方案.pdf'}])}
        self.assertIn('项目方案', app._history_search_text(record))

    def test_editor_font_size_applies_to_new_and_selected_text(self):
        canvas = app._EditorCanvas(app.QPixmap(1600, 900))
        self.addCleanup(canvas.deleteLater)
        app._change_annotation_size(canvas, 24)
        canvas._begin_text(app.QPoint(10, 10), app.QPoint(10, 10))
        edit = canvas._text_edit
        self.assertEqual(edit.font().pointSize(), 24)
        canvas._commit_text('hello', edit, app.QPoint(10, 10), app.QColor('red'))
        self.assertAlmostEqual(canvas._annotations[0].font.pointSizeF() * canvas._scale, 24, places=4)
        app._change_annotation_size(canvas, 36)
        self.assertAlmostEqual(canvas._annotations[0].font.pointSizeF() * canvas._scale, 36, places=4)
        self.assertEqual(app._text_size_setting(), 36)

    def test_screenshot_size_buttons_switch_between_width_and_text_presets(self):
        overlay = app.ScreenshotOverlay(app.QPixmap(1000, 800), app.QRect(0, 0, 1000, 800), Mock())
        self.addCleanup(overlay.deleteLater)
        overlay._sel = app.QRect(10, 10, 500, 400)
        overlay._build_toolbar()
        self.assertFalse(hasattr(overlay, '_font_size_spin'))
        overlay._select_tool('rect')
        self.assertEqual([b.text() for b in overlay._size_btns], ['细', '中', '粗'])
        overlay._size_btns[2].click()
        self.assertEqual(overlay._width, 7)
        overlay._select_tool('text')
        self.assertEqual([b.text() for b in overlay._size_btns], ['小', '中', '大'])
        overlay._size_btns[2].click()
        self.assertEqual(overlay._text_size, 28)
        self.assertEqual(overlay._width, 7)
        self.assertTrue(overlay._size_btns[2].isChecked())
        overlay._begin_text(app.QPoint(50, 50))
        edit = overlay._text_edit
        overlay._commit_text('hello', edit, app.QPoint(50, 50), app.QColor('red'), edit.font())
        self.assertEqual(overlay._annotations[0].font.pointSize(), 28)
        overlay._size_btns[0].click()
        self.assertEqual(overlay._annotations[0].font.pointSize(), 14)

    def test_selected_text_syncs_size_buttons_without_changing_annotation(self):
        dialog = app.ImageEditorDialog(self.w, str(self.make_image()))
        self.addCleanup(dialog.deleteLater)
        canvas = dialog.canvas
        ann = app._Annotation('text', app.QColor('red'), 4)
        ann.font = app.QFont()
        ann.font.setPointSizeF(32 / canvas._scale)
        canvas._annotations = [ann]
        canvas._selected = 0
        app._sync_annotation_font(canvas)
        self.assertEqual([b.text() for b in canvas._size_btns], ['小', '中', '大'])
        self.assertEqual([b.isChecked() for b in canvas._size_btns], [False, False, True])
        self.assertAlmostEqual(ann.font.pointSizeF() * canvas._scale, 32)


if __name__ == '__main__':
    unittest.main()
