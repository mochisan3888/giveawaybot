import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from giveaway.redemptions import Redemptions, ReceiptError
from giveaway.fulfillment import Fulfillment, minecraft_nickname, link_status

class GuildLinkTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.db=Redemptions(self.conn)
    def tearDown(self):self.conn.close()
    def issue(self,guild,user=100,uuid='a'*32,receipt=None):
        return self.db.challenge(user,uuid,'Steve',receipt,now=100,guild=guild,original_name='元の名前')[0]
    def test_independent_challenges_and_links_per_server(self):
        a=self.issue(10);b=self.issue(20)
        self.db.verify(a,'a'*32,'Steve',now=101)
        self.assertIsNone(self.db.link(100,20))
        self.db.verify(b,'a'*32,'Steve',now=102)
        self.assertEqual(self.db.link(100,10)['name'],'Steve')
        self.assertEqual(self.db.link(100,20)['name'],'Steve')
        self.assertEqual({j['guild'] for j in self.db.nickname_jobs(103)},{10,20})
    def test_uuid_uniqueness_is_only_within_server(self):
        a=self.issue(10);b=self.issue(20,user=200);other=self.issue(10,user=200)
        self.db.verify(a,'a'*32,'Steve',now=101)
        self.db.verify(b,'a'*32,'Steve',now=101)
        with self.assertRaises(ReceiptError):self.db.verify(other,'a'*32,'Steve',now=101)
    def test_legacy_global_links_are_preserved_but_not_auto_granted(self):
        self.conn.execute('INSERT INTO minecraft_links VALUES (?,?,?,?)',(100,'a'*32,'Steve',99));self.conn.commit()
        self.db=Redemptions(self.conn)
        self.assertIsNone(self.db.link(100,10))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM minecraft_links').fetchone()[0],1)
        self.db.verify(self.issue(10),'a'*32,'Steve',now=101)
        self.assertIsNotNone(self.db.link(100,10))
    def test_cannot_attach_another_server_receipt(self):
        self.db.seed(dict(id=1,guild=10,channel=11,message=12,prize='P',receipt_policy={'minecraft':True},
            receipt_events=[dict(round=1,winners=[100],at=90,deadline=1000)]))
        with self.assertRaises(ReceiptError):self.issue(20,receipt=1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM guild_link_challenges').fetchone()[0],0)
    def test_retry_state_persists_without_changing_original_name(self):
        self.db.verify(self.issue(10),'a'*32,'Steve',now=101)
        self.db.nickname_result(10,100,'forbidden',300)
        self.assertEqual(self.db.nickname_jobs(200),[])
        self.db=Redemptions(self.conn)
        self.assertEqual(self.db.nickname_jobs(301)[0]['original_name'],'元の名前')
    def test_nickname_limit_preserves_minecraft_id(self):
        self.assertEqual(minecraft_nickname('Steve','元の名前'),'Steve / 元の名前')
        nick=minecraft_nickname('LongMinecraftID1','😀'*32)
        self.assertTrue(nick.startswith('LongMinecraftID1 / '))
        self.assertLessEqual(len(nick.encode('utf-16-le'))//2,32)

class NicknameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.member=SimpleNamespace(nick='古い名前',edit=AsyncMock())
        self.guild=SimpleNamespace(fetch_member=AsyncMock(return_value=self.member))
        self.requested_guilds=[]
        def get_guild(gid):self.requested_guilds.append(gid);return self.guild
        self.service=Fulfillment(SimpleNamespace(store=SimpleNamespace(db=self.conn),get_guild=get_guild))
        t,_=self.service.db.challenge(100,'a'*32,'Steve',guild=10,original_name='元の名前')
        self.service.db.verify(t,'a'*32,'Steve')
    async def asyncTearDown(self):self.conn.close()
    async def test_only_verified_server_nickname_changes_once(self):
        await self.service.update_nicknames();await self.service.update_nicknames()
        self.assertEqual(self.requested_guilds,[10])
        self.member.edit.assert_awaited_once_with(nick='Steve / 元の名前',reason='このサーバーでMinecraft認証が完了')
        self.assertEqual(self.service.db.link(100,10)['nickname_state'],'done')
    async def test_permission_failure_does_not_undo_identity(self):
        self.member.edit.side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),{'code':50013,'message':'Missing Permissions'})
        await self.service.update_nicknames()
        link=self.service.db.link(100,10)
        self.assertEqual(link['name'],'Steve');self.assertEqual(link['nickname_state'],'forbidden')
        self.assertIn('ロール順位',link_status(link))
        self.assertEqual(self.service.db.nickname_jobs(),[])
    async def test_recovery_does_not_prefix_nickname_twice(self):
        self.member.nick='Steve / 元の名前'
        await self.service.update_nicknames()
        self.member.edit.assert_not_awaited()
        self.assertEqual(self.service.db.link(100,10)['nickname_state'],'done')
