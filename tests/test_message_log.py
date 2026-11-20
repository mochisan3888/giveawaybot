import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME = ROOT.parent if (ROOT.parent / 'giveaway').is_dir() else ROOT.parent.parent / 'runtime'
RUNTIME = Path(os.environ.get('MESSAGELOG_TEST_RUNTIME', str(DEFAULT_RUNTIME)))
sys.path.insert(0, str(RUNTIME))
source = ROOT / 'message_log.py'
if source.exists():
    spec = importlib.util.spec_from_file_location('giveaway.message_log', source)
    ml = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ml
    spec.loader.exec_module(ml)
else:
    from giveaway import message_log as ml
import discord


def record(mid=1, gid=9, seen=None, body='before'):
    return dict(id=mid, guild=gid, channel=20, author=30, name='name', body=body,
                seen=time.time() if seen is None else seen, attachments=[])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.store = ml.MessageLogStore(self.db)
    def tearDown(self):
        self.db.close()
    def test_initial_off_disable_and_generation(self):
        self.assertIsNone(self.store.config(9))
        self.store.setup(9, 10)
        first = self.store.config(9)
        self.store.save(record())
        self.store.setup(9, 10)
        self.assertNotEqual(first, self.store.config(9))
        self.assertIsNone(self.store.get(9, 1))
        self.store.save(record())
        self.store.disable(9)
        self.assertIsNone(self.store.config(9))
        self.assertIsNone(self.store.get(9, 1))
    def test_ttl_on_read_before_cleanup(self):
        self.store.save(record())
        with patch.object(ml.time, 'time', return_value=time.time() + ml.TTL + 1):
            self.assertIsNone(self.store.get(9, 1))
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM message_log_cache').fetchone()[0], 1)
            self.store.cleanup()
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM message_log_cache').fetchone()[0], 0)
    def test_count_and_byte_limits(self):
        with patch.object(ml, 'GUILD_LIMIT', 2), patch.object(ml, 'TOTAL_LIMIT', 3):
            for mid in range(5): self.store.save(record(mid))
            self.assertIsNone(self.store.get(9, 2))
            self.store.save(record(10, 8))
            self.store.save(record(11, 8))
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM message_log_cache').fetchone()[0], 3)
        with patch.object(ml, 'BYTE_LIMIT', 1000):
            self.store.save(record(12, body='語' * 200))
            self.assertLessEqual(self.db.execute('SELECT SUM(bytes) FROM message_log_cache').fetchone()[0], 1000)
    def test_restart_persists_config_and_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite3'
            db = sqlite3.connect(path); store = ml.MessageLogStore(db)
            store.setup(9, 10); store.save(record()); db.close()
            db = sqlite3.connect(path); store = ml.MessageLogStore(db)
            self.assertEqual(store.config(9)[0], 10)
            self.assertEqual(store.get(9, 1)['body'], 'before')
            db.close()


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.bot = NS(store=NS(db=self.conn), intents=NS(message_content=True, guild_messages=True))
        self.service = ml.MessageLog(self.bot)
        self.service.db.setup(9, 10)
        self.env = patch.dict(os.environ, {'BOT_RUNTIME_MODE': 'full'})
        self.env.start()
    async def asyncTearDown(self):
        await self.service.stop()
        self.env.stop()
        self.conn.close()
    def message(self, gid=9, channel=20, bot=False, webhook=None):
        return NS(id=1, guild=NS(id=gid) if gid else None, channel=NS(id=channel),
                  author=NS(id=30, bot=bot), webhook_id=webhook, content='before', attachments=[])
    def payload(self, **data):
        return NS(guild_id=9, channel_id=20, message_id=1, data=data)
    async def test_create_exclusions_and_initial_off(self):
        for message in [self.message(gid=None), self.message(gid=8), self.message(bot=True),
                        self.message(webhook=1), self.message(channel=10), self.message(gid=900000000000000001)]:
            await self.service.on_message(message)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM message_log_cache').fetchone()[0], 0)
        await self.service.on_message(self.message())
        self.assertEqual(self.service.db.get(9, 1)['body'], 'before')
    async def test_partial_edit_preserves_fields_and_embeds_ignored(self):
        await self.service.on_message(self.message())
        await self.service.on_raw_message_edit(self.payload(embeds=[{}]))
        self.assertTrue(self.service.queue.empty())
        await self.service.on_raw_message_edit(self.payload(attachments=[dict(id=5, filename='a.txt', size=42, url='SECRET')]))
        item = self.service.queue.get_nowait(); self.service.queue.task_done()
        self.assertEqual(item[1]['body'], 'before')
        self.assertNotIn('url', item[1]['attachments'][0])
        await self.service.on_raw_message_edit(self.payload(content='after'))
        self.assertEqual(self.service.db.get(9, 1)['attachments'][0]['size'], 42)
        self.assertEqual(self.service.queue.get_nowait()[2]['body'], 'before')
        self.service.queue.task_done()
        await self.service.on_raw_message_edit(self.payload(content='after'))
        self.assertTrue(self.service.queue.empty())
    async def test_missing_old_and_unknown_author(self):
        await self.service.on_raw_message_edit(self.payload(content='new'))
        self.assertTrue(self.service.queue.empty())
        await self.service.on_raw_message_edit(self.payload(content='new', author={'id':'30','username':'human'}))
        item = self.service.queue.get_nowait(); self.service.queue.task_done()
        self.assertIsNone(item[2]); self.assertEqual(item[1]['body'], 'new')
    async def test_bot_edit_excluded(self):
        await self.service.on_raw_message_edit(self.payload(content='bot', author={'id':'30','bot':True,'username':'bot'}))
        self.assertTrue(self.service.queue.empty())
    async def test_delete_and_bulk_and_cache_miss(self):
        self.service.db.save(record(1)); self.service.db.save(record(2))
        await self.service.on_raw_bulk_message_delete(NS(guild_id=9, channel_id=20, message_ids={1, 2, 3}))
        self.assertEqual(self.service.queue.qsize(), 2)
        self.assertIsNone(self.service.db.get(9, 1))
        self.assertIn('復元できません', self.service.state[9])
    async def test_edit_does_not_extend_ttl(self):
        old = record(seen=time.time()-1000); self.service.db.save(old)
        await self.service.on_raw_message_edit(self.payload(content='after'))
        self.assertEqual(self.service.db.get(9, 1)['seen'], old['seen'])
    async def test_queue_bound_and_disable_generation(self):
        self.service.queue = asyncio.Queue(maxsize=2)
        cfg = self.service.db.config(9)
        for x in range(5): self.service.enqueue(cfg, record(x), None, '削除')
        self.assertEqual(self.service.queue.qsize(), 2)
        item = self.service.queue.get_nowait(); self.service.queue.task_done()
        self.service.db.disable(9); self.service.clear_pending(9)
        with patch.object(ml, 'private_destination', AsyncMock()) as dest:
            await self.service.deliver_one(item); dest.assert_not_awaited()
        self.assertTrue(self.service.queue.empty())
    async def test_send_none_mentions_truncation_and_privacy_rechecked(self):
        channel = NS(send=AsyncMock())
        cfg = self.service.db.config(9)
        item = (cfg, record(body='@everyone '+('語'*4000)), None, '編集')
        with patch.object(ml, 'private_destination', AsyncMock(return_value=channel)) as dest:
            await self.service.deliver_one(item)
            self.assertEqual(dest.await_count, 1)
            kwargs = channel.send.call_args.kwargs
            self.assertFalse(kwargs['allowed_mentions'].everyone)
            self.assertFalse(kwargs['allowed_mentions'].users)
            self.assertFalse(kwargs['allowed_mentions'].roles)
            self.assertIn('編集前不明', kwargs['embed'].fields[0].value)
            self.assertIn('省略', kwargs['embed'].fields[1].value)
            self.assertLessEqual(len(kwargs['embed'].fields[1].value), 1000)
            dest.side_effect = ml.ReceiptError('public')
            with self.assertRaises(ml.ReceiptError): await self.service.deliver_one(item)
            self.assertEqual(channel.send.await_count, 1)
    async def test_expired_queued_item_never_sends(self):
        with patch.object(ml, 'private_destination', AsyncMock()) as dest:
            await self.service.deliver_one((self.service.db.config(9), record(seen=time.time()-ml.TTL), None, '削除'))
            dest.assert_not_awaited()
    async def test_worker_fail_closed_continues_and_stops(self):
        with patch.object(ml, 'private_destination', AsyncMock(side_effect=RuntimeError('private'))):
            await self.service.start()
            self.service.enqueue(self.service.db.config(9), record(), None, '削除')
            await asyncio.wait_for(self.service.queue.join(), 2)
            self.assertIn('送信停止', self.service.state[9])
            self.assertFalse(self.service.worker.done())
            await self.service.stop()
            self.assertIsNone(self.service.worker)


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE moderation_config(guild INTEGER PRIMARY KEY,data TEXT)')
        self.db.execute('INSERT INTO moderation_config VALUES(?,?)', (9,json.dumps({'roles':[12]})))
        def role(rid, **perms): return NS(id=rid, permissions=discord.Permissions(**perms), tags=None)
        self.everyone = role(9, view_channel=True)
        self.staff = role(11, manage_guild=True)
        self.configured = role(12)
        self.ordinary = role(13)
        self.guild = NS(id=9, owner_id=100, roles=[self.everyone,self.staff,self.configured,self.ordinary], default_role=self.everyone)
        self.guild.fetch_member = AsyncMock(return_value=NS(id=30,roles=[self.everyone,self.staff]))
        self.data = dict(id='10',guild_id='9',type=0,permission_overwrites=[dict(id='9',type=0,allow='0',deny='1024'),dict(id='11',type=0,allow='1024',deny='0')])
        self.bot = NS(user=NS(id=99),_connection=None,store=NS(db=self.db),fetch_guild=AsyncMock(return_value=self.guild),http=NS(get_channel=AsyncMock(return_value=self.data)))
        self.permissions = discord.Permissions(view_channel=True,send_messages=True,embed_links=True)
        self.channel = NS(permissions_for=lambda _:self.permissions)
        self.mock = patch.object(ml.discord,'TextChannel',return_value=self.channel); self.mock.start()
    async def asyncTearDown(self):
        self.mock.stop(); self.db.close()
    async def test_private_staff_role_and_configured_role_allowed(self):
        self.assertIs(await ml.private_destination(self.bot,9,10), self.channel)
        self.data['permission_overwrites'].append(dict(id='12',type=0,allow='1024',deny='0'))
        await ml.private_destination(self.bot,9,10)
    async def test_everyone_not_explicitly_denied_rejected(self):
        self.data['permission_overwrites'][0]['deny']='0'
        with self.assertRaises(ml.ReceiptError): await ml.private_destination(self.bot,9,10)
    async def test_ordinary_role_allow_rejected_even_with_deny_elsewhere(self):
        self.data['permission_overwrites'].append(dict(id='13',type=0,allow='1024',deny='0'))
        with self.assertRaises(ml.ReceiptError): await ml.private_destination(self.bot,9,10)
    async def test_uncached_individual_allow_rejected(self):
        self.data['permission_overwrites'].append(dict(id='30',type=1,allow='1024',deny='0'))
        self.guild.fetch_member.return_value=NS(id=30,roles=[self.everyone,self.ordinary])
        with self.assertRaises(ml.ReceiptError): await ml.private_destination(self.bot,9,10)
        self.guild.fetch_member.assert_awaited_once_with(30)
    async def test_staff_member_allow_and_failed_fetch_closed(self):
        self.data['permission_overwrites'].append(dict(id='30',type=1,allow='1024',deny='0'))
        await ml.private_destination(self.bot,9,10)
        self.guild.fetch_member.side_effect=RuntimeError('REST failed')
        with self.assertRaises(RuntimeError): await ml.private_destination(self.bot,9,10)
    async def test_wrong_guild_threads_missing_permissions_rejected(self):
        self.data['guild_id']='8'
        with self.assertRaises(ml.ReceiptError): await ml.private_destination(self.bot,9,10)
        self.data['guild_id']='9'; self.data['type']=11
        with self.assertRaises(ml.ReceiptError): await ml.private_destination(self.bot,9,10)
        self.data['type']=0; self.permissions.embed_links=False
        with self.assertRaises(ml.ReceiptError): await ml.private_destination(self.bot,9,10)
    async def test_everyone_admin_rejected(self):
        self.everyone.permissions.administrator=True
        with self.assertRaises(ml.ReceiptError): await ml.private_destination(self.bot,9,10)


if __name__ == '__main__':
    unittest.main()
