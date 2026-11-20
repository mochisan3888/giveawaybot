import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from giveaway.ticket_ai import TicketAI, scrub
from giveaway.redemptions import ReceiptError
from giveaway.rotate_ai_key import rotate


class AssistTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.ai = TicketAI(SimpleNamespace(store=SimpleNamespace(db=self.db)))

    def test_usage_limit_survives_service_recreation(self):
        for n in range(100):self.ai.reserve(1,n,1700000000)
        other = TicketAI(SimpleNamespace(store=SimpleNamespace(db=self.db)))
        with self.assertRaises(ReceiptError):other.reserve(2,101,1700000000)

    def test_ticket_is_scoped_to_guild_and_channel(self):
        self.db.execute('CREATE TABLE receipts(data TEXT)')
        self.db.execute('INSERT INTO receipts VALUES (?)',(json.dumps({'guild':1,'channel':2}),))
        self.assertEqual(self.ai.record(1,2)['guild'],1)
        with self.assertRaises(ReceiptError):self.ai.record(9,2)

    def test_secret_redaction(self):
        self.assertNotIn('sk-',scrub('token sk-'+'a'*40))


class RotationTests(unittest.TestCase):
    def test_failed_probe_preserves_old_key_and_retries_pending(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            (p/'admin').write_text('test-admin')
            (p/'runtime').write_text(json.dumps({'id':'old','value':'old-value'}))
            cfg=dict(runtime_key_file=str(p/'runtime'),state_file=str(p/'state'),admin_key_file=str(p/'admin'),project_id='test',service_account_id='service')
            calls=[]
            def fail(method,path,key,data=None):
                calls.append((method,path))
                if path=='responses':raise TimeoutError()
                return {'id':'new','value':'new-value'}
            with self.assertRaises(TimeoutError):rotate(cfg,fail,1700000000)
            self.assertEqual(json.loads((p/'runtime').read_text())['id'],'old')
            def success(method,path,key,data=None):
                calls.append((method,path));return {}
            self.assertEqual(rotate(cfg,success,1700000001),'rotated')
            self.assertEqual(json.loads((p/'runtime').read_text())['id'],'new')
            self.assertEqual(sum(path.endswith('/api_keys') for _,path in calls),1)
            self.assertFalse(any(method=='DELETE' for method,_ in calls))
            self.assertEqual(rotate(cfg,success,1700003601),'not_due')
            self.assertIn(('DELETE','organization/projects/test/api_keys/old'),calls)
