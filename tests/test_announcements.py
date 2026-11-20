import tempfile
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from giveaway.store import Store

class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        os.environ['DATABASE_PATH']=self.temp.name+'/unused-bot-db'
        self.store=Store(self.temp.name+'/db')
        self.game=self.store.create(dict(guild=1,channel=2,message=3,id=1,prize='1~100m @everyone',status='done',count=1,end=0,role=None,days=0,rules={},rounds=[[42]],announcement={'round':1,'state':'pending'}))
        self.fake=SimpleNamespace(store=self.store,http=SimpleNamespace(request=AsyncMock(return_value={'id':'99'})))
    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()
    async def test_japanese_winner_and_only_winner_ping_once(self):
        from giveaway.bot import GiveawayBot
        await GiveawayBot.announce(self.fake,self.game)
        payload=self.fake.http.request.call_args.kwargs['json']
        self.assertIn('おめでとう <@42>',payload['content'])
        self.assertIn('当選しました',payload['content'])
        self.assertEqual(payload['allowed_mentions'],{'parse':[],'users':['42']})
        self.assertTrue(payload['enforce_nonce'])
        await GiveawayBot.announce(self.fake,self.store.get(self.game['id']))
        self.assertEqual(self.fake.http.request.await_count,1)
    async def test_retry_keeps_nonce_and_result(self):
        from giveaway.bot import GiveawayBot
        self.fake.http.request.side_effect=[OSError('network'),{'id':'99'}]
        with self.assertRaises(OSError):await GiveawayBot.announce(self.fake,self.game)
        await GiveawayBot.announce(self.fake,self.store.get(self.game['id']))
        calls=self.fake.http.request.call_args_list
        self.assertEqual(calls[0].kwargs['json']['nonce'],calls[1].kwargs['json']['nonce'])
        self.assertEqual(self.store.get(self.game['id'])['rounds'],[[42]])
    async def test_old_completed_games_are_not_announced(self):
        from giveaway.bot import GiveawayBot
        del self.game['announcement']
        await GiveawayBot.announce(self.fake,self.game)
        self.fake.http.request.assert_not_awaited()
    async def test_empty_winners_and_reroll(self):
        from giveaway.bot import GiveawayBot
        self.game['rounds'].append([])
        self.game['announcement']['round']=2
        await GiveawayBot.announce(self.fake,self.game)
        p=self.fake.http.request.call_args.kwargs['json']
        self.assertIn('【再抽選】',p['content'])
        self.assertIn('対象者はいません',p['content'])
        self.assertEqual(p['allowed_mentions']['users'],[])
    async def test_entry_count_uses_unique_entries_and_cancellations(self):
        from giveaway.bot import GiveawayBot
        channel=SimpleNamespace(get_partial_message=lambda mid:message)
        message=SimpleNamespace(edit=AsyncMock())
        self.fake.get_channel=lambda cid:channel
        self.store.enter(self.game['id'],42)
        self.store.enter(self.game['id'],42)
        self.store.enter(self.game['id'],43)
        await GiveawayBot.publish(self.fake,self.game)
        fields=message.edit.call_args.kwargs['embed'].fields
        self.assertEqual(next(f.value for f in fields if f.name=='応募人数'),'2人')
        self.store.leave(self.game['id'],42)
        await GiveawayBot.publish(self.fake,self.game)
        self.assertEqual(self.game['entry_count'],1)
