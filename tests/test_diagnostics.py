import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from diagnostics import DailyLog, SerialWriter, collect_paths, cache_inventory, delete_cache_candidates


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_daily_logs_retention_and_token_redaction(self):
        folder = self.root / 'log'; folder.mkdir()
        today = datetime.now()
        for n in range(10):
            (folder / f'{today-timedelta(days=n):%Y-%m-%d}.log').write_text('old\n')
        unrelated = folder / 'keep.txt'; unrelated.write_text('keep')
        log = DailyLog(self.root)
        log.record('error', 'GET http://localhost/?k=secret&token=private failed')
        log.close()
        files = list(folder.glob('*.log'))
        self.assertEqual(len(files), 7)
        text = (folder / f'{today:%Y-%m-%d}.log').read_text()
        self.assertIn('[ERROR]', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('private', text)
        self.assertTrue(unrelated.exists())

    def test_cache_keeps_referenced_folders_layers_and_recent_files(self):
        images = self.root / 'images'; temp = self.root / 'temp'
        images.mkdir(); (temp / 'pending').mkdir(parents=True)
        paths = [images / 'used.png', images / 'used.base.png', images / 'used.layers.json',
                 temp / 'pending/child', images / 'orphan.png']
        old = time.time() - 3 * 86400
        for file in paths:
            file.write_text('keep'); os.utime(file, (old, old))
        recent = images / 'recent.png'; recent.write_text('new')
        refs = collect_paths([{'path': str(images / 'used.png')}, {'path': str(temp / 'pending')}])
        total, candidates = cache_inventory(images, temp, refs)
        self.assertEqual([Path(x[0]).name for x in candidates], ['orphan.png'])
        delete_cache_candidates(candidates)
        self.assertTrue(all(file.exists() for file in paths[:-1]))
        self.assertTrue(recent.exists())

    def test_background_writer_preserves_save_order(self):
        path = self.root / 'draft.json'
        def write(path, value):
            path.write_text(str(value)); return True
        writer = SerialWriter(write, self.fail)
        for n in range(20):
            writer.submit(path, n)
        writer.close()
        self.assertEqual(path.read_text(), '19')
