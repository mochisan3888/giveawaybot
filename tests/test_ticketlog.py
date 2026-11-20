import json,sqlite3,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from giveaway.ticketlog import TicketLog,TABLES

class TicketLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=sqlite3.connect(':memory:')
        for table in TABLES:self.db.execute(f'CREATE TABLE {table}(data TEXT)')
        self.bot=SimpleNamespace(store=SimpleNamespace(db=self.db),get_channel=lambda cid:SimpleNamespace(guild=SimpleNamespace(id=10)),http=SimpleNamespace(request=AsyncMock(return_value={'id':'90'})))
        self.s=TicketLog(self.bot)
    async def asyncTearDown(self):self.db.close()
    def add(self,table,guild=10):
        r={'id':1,'guild':guild,'channel':50,'user':100,'created':1000,'history':[{'action':'start','by':200,'at':1001},{'action':'done','by':200,'at':1002},{'action':'close','by':200,'at':1003}]}
        with self.db:self.db.execute(f'INSERT INTO {table} VALUES (?)',(json.dumps(r),))
    async def test_all_ticket_types_and_dedup_after_restart(self):
        self.s.configure(10,77)
        for table in TABLES:self.add(table)
        await self.s.flush()
        self.assertEqual(self.bot.http.request.await_count,4*len(TABLES))
        for call in self.bot.http.request.call_args_list:
            self.assertEqual(call.kwargs['json']['allowed_mentions'],{'parse':[]})
        await TicketLog(self.bot).flush()
        self.assertEqual(self.bot.http.request.await_count,4*len(TABLES))
    async def test_baseline_and_other_guild_excluded(self):
        self.add('receipts');self.s.configure(10,77)
        self.add('vip_tickets',11)
        await self.s.flush();self.bot.http.request.assert_not_awaited()
    async def test_error_does_not_lose_event(self):
        self.s.configure(10,77);self.add('vip_tickets')
        self.bot.http.request.side_effect=OSError('connection')
        await self.s.flush()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ticketlog_events WHERE state='pending'").fetchone()[0],4)
        self.bot.http.request.side_effect=None
        await TicketLog(self.bot).flush()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ticketlog_events WHERE state='sent'").fetchone()[0],4)
