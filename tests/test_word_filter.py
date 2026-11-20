import sqlite3
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from giveaway.word_filter import WordFilter, normalize, terms, register_word_filter
from giveaway.redemptions import ReceiptError
import discord

class Rules(unittest.TestCase):
    def setUp(self):
        self.bot=NS(store=NS(db=sqlite3.connect(':memory:')),intents=NS(message_content=True))
        self.s=WordFilter(self.bot)
    def test_normalization(self):
        self.assertEqual(normalize(' ＡbＣ\u200b カ タカナ '),'abcかたかな')
        self.assertEqual(normalize('ﾊﾞｶ'),normalize('ばか'))
        self.assertEqual(terms(['テスト','てすと','ＴＥＳＴ']),['テスト','ＴＥＳＴ'])
    def test_guild_isolation(self):
        rid=self.s.add(1,['テスト','test','試験'])
        self.assertEqual(self.s.match(1,'これは試験です'),(rid,'試験'))
        self.assertEqual(self.s.match(1,'Ｔ E S T'),(rid,'test'))
        self.assertIsNone(self.s.match(2,'test'))
        self.assertFalse(self.s.config(1)['enabled'])
        self.s.configure(1,enabled=True)
        self.assertFalse(self.s.config(2)['enabled'])
    def test_bounds(self):
        for words in [[],['   '],['x'*81]]:
            with self.assertRaises(ReceiptError):self.s.add(1,words)
    def test_commands(self):
        self.bot.word_filter=self.s
        self.bot.tree=NS(add_command=lambda g:setattr(self,'group',g))
        register_word_filter(self.bot)
        self.assertEqual(len(self.group.commands),6)
        self.assertTrue(self.group.default_permissions.manage_guild)

class DeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot=NS(store=NS(db=sqlite3.connect(':memory:')),intents=NS(message_content=True))
        self.s=WordFilter(self.bot)
        self.s.configure(1,enabled=True,log=9)
        self.s.add(1,['bad'])
        self.member=NS(roles=[])
        self.guild=NS(id=1,fetch_member=AsyncMock(return_value=self.member))
        self.channel=NS(id=2,parent_id=None,fetch_message=AsyncMock())
        self.msg=NS(id=3,guild=self.guild,channel=self.channel,content='bad',author=NS(id=4,bot=False),webhook_id=None,delete=AsyncMock())
        self.channel.fetch_message.return_value=self.msg
        self.log=NS(send=AsyncMock())
        self.p=patch('giveaway.word_filter.private_destination',AsyncMock(return_value=self.log));self.p.start()
    async def asyncTearDown(self):self.p.stop()
    async def test_delete_and_log(self):
        self.assertTrue(await self.s.check(self.msg))
        self.msg.delete.assert_awaited_once()
        self.log.send.assert_awaited_once()
        self.assertEqual(self.log.send.call_args.kwargs['allowed_mentions'].to_dict()['parse'],[])
    async def test_exclusions_and_bots(self):
        for changes in [dict(channels=[2]),dict(channels=[7])]:
            self.channel.parent_id=7
            self.s.configure(1,**changes)
            self.assertFalse(await self.s.check(self.msg))
        self.s.configure(1,channels=[],roles=[8]);self.member.roles=[NS(id=8)]
        self.assertFalse(await self.s.check(self.msg))
        self.member.roles=[];self.msg.author.bot=True
        self.assertFalse(await self.s.check(self.msg))
        self.msg.delete.assert_not_awaited()
    async def test_fresh_text_prevents_stale_delete(self):
        fresh=NS(**vars(self.msg));fresh.content='good'
        self.channel.fetch_message.return_value=fresh
        self.assertFalse(await self.s.check(self.msg))
        self.msg.delete.assert_not_awaited()
    async def test_log_failure_does_not_repeat_deletion(self):
        self.log.send.side_effect=RuntimeError()
        self.assertTrue(await self.s.check(self.msg))
        self.assertIn('ログ送信失敗',self.s.errors[1])
    async def test_edit_without_cached_member(self):
        self.bot.get_channel=lambda _:self.channel
        payload=NS(guild_id=1,channel_id=2,message_id=3,data={'content':'bad'})
        self.assertTrue(await self.s.edited(payload))
        self.msg.delete.assert_awaited_once()
