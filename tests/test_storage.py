import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from codex_usage.models import UsageSample
from codex_usage.storage import Store, export_samples

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'nested'/'usage.sqlite3'
        self.row=UsageSample(1800000000,25,banked_reset_count=1)
    def tearDown(self): self.temp.cleanup()
    def test_persists_across_restart(self):
        with Store(self.path) as db: db.add([self.row])
        with Store(self.path) as db: self.assertEqual(db.load(),[self.row])
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_deduplicates_and_filters(self):
        with Store(self.path) as db:
            db.add([self.row,self.row,replace(self.row,window='secondary')])
            self.assertEqual(len(db.load()),2)
            self.assertEqual(db.load(window='primary'),[self.row])
            self.assertEqual(db.load(limit_id='absent'),[])
    def test_sorted_and_since_filter(self):
        with Store(self.path) as db:
            later=replace(self.row,timestamp=self.row.timestamp+30)
            db.add([later,self.row])
            self.assertEqual(db.load(since=self.row.timestamp+1),[later])
    def test_json_and_csv_exports_unknowns(self):
        out=io.StringIO();export_samples([self.row],out,'json')
        item=json.loads(out.getvalue())[0]
        self.assertEqual(item['remaining_percent'],75)
        self.assertIsNone(item['model'])
        self.assertEqual(item['banked_reset_count'],1)
        out=io.StringIO();export_samples([self.row],out,'csv')
        item=list(csv.DictReader(io.StringIO(out.getvalue())))[0]
        self.assertEqual(item['model'],'')
    def test_empty_export_has_csv_header(self):
        out=io.StringIO();export_samples([],out,'csv')
        self.assertIn('used_percent',out.getvalue())

if __name__=='__main__': unittest.main()
