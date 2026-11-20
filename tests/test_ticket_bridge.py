import tempfile
import time
import unittest
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from giveaway.ticket_bridge_rpc import reserve,validate
from giveaway.ticket_ai import TicketAI
from giveaway.redemptions import ReceiptError


class BridgeTests(unittest.TestCase):
    def test_common_limit_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'limits.db'
            p=dict(request_id='1',guild='5',actor='7')
            self.assertTrue(reserve(path,p,1700000000)['ok'])
            self.assertTrue(reserve(path,p,1700000001)['ok'])
            self.assertFalse(reserve(path,dict(request_id='2',guild='5',actor='7'),1700000001)['ok'])
            with ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(lambda n:reserve(path,dict(request_id=str(n),guild='5',actor=str(n)),1700000001),range(100,220)))
            self.assertEqual(sum(r['ok'] for r in results),99)

    def test_rejects_everyone_and_stale_snapshot(self):
        s=dict(version=1,updated_at=time.time(),settings={},tickets=[dict(guild='123',channel='456',roles=['123'])])
        with self.assertRaises(ValueError):validate(s)
        s['tickets']=[];s['updated_at']=time.time()-181
        with self.assertRaises(ValueError):validate(s)

    def test_drops_unexpected_content(self):
        s=dict(version=1,updated_at=time.time(),settings={},tickets=[dict(guild='123',channel='456',roles=['789'],body='private content')])
        self.assertNotIn('body',validate(s)['tickets'][0])


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_tormo_priority_stops_before_budget_or_api(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'ticket-bridge.json').write_text(json.dumps(dict(version=1,tormo=dict(version=1,updated_at=time.time(),settings={'1':{'enabled':True}},tickets=[]))))
            db=sqlite3.connect(':memory:')
            try:
                ai=TicketAI(SimpleNamespace(store=SimpleNamespace(db=db)))
                with patch.dict('os.environ',{'TICKET_BRIDGE_DIR':d}):
                    with self.assertRaisesRegex(ReceiptError,'Tormo'):
                        await ai.assist(SimpleNamespace(guild_id=1))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ticket_ai_usage').fetchone()[0],0)
            finally:db.close()
