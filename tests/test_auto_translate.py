import json
import sqlite3
import time
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
import discord
from giveaway.auto_translate import AutoTranslate,extract_text,candidate,translate_text,register_auto_translate

class TranslateTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.db=sqlite3.connect(':memory:')
  self.guild=NS(id=1,fetch_member=AsyncMock(return_value=NS()))
  perms=NS(view_channel=True,read_message_history=True,send_messages=True,embed_links=True)
  self.channel=NS(id=2,guild=self.guild,permissions_for=lambda m:perms,send=AsyncMock(return_value=NS(id=100)))
  self.message=NS(id=3,guild=self.guild,channel=self.channel,author=NS(id=4,bot=True),webhook_id=5,
      flags=NS(is_crossposted=True),created_at=datetime.now(timezone.utc),content='The server will restart in ten minutes.',embeds=[],jump_url='https://discord.com/channels/1/2/3')
  self.channel.fetch_message=AsyncMock(return_value=self.message)
  self.bot=NS(user=NS(id=99),store=NS(db=self.db),get_channel=lambda cid:self.channel,fetch_channel=AsyncMock(),intents=NS(message_content=True),tree=discord.app_commands.CommandTree(NS(http=NS(),_connection=NS(_command_tree=None))))
  self.service=AutoTranslate(self.bot)
  self.db.execute('INSERT INTO translation_config VALUES(?,?,?,?,?,?)',(2,1,'a',time.time()-5,1,50));self.db.commit()
 async def asyncTearDown(self):self.db.close()
 def row(self):return self.db.execute('SELECT message,channel,guild,generation,at FROM translation_jobs').fetchone()
 def state(self):return self.db.execute('SELECT state FROM translation_jobs').fetchone()[0]
 async def test_followed_webhook_and_dedupe(self):
  await self.service.enqueue(self.message);await self.service.enqueue(self.message)
  self.assertEqual(self.db.execute('SELECT COUNT(*) FROM translation_jobs').fetchone()[0],1)
  self.assertNotIn('restart',str(list(self.db.execute('SELECT * FROM translation_jobs'))))
 async def test_filters_and_general_opt_in(self):
  self.message.flags.is_crossposted=False
  await self.service.enqueue(self.message);self.assertIsNone(self.row())
  self.db.execute('UPDATE translation_config SET followed_only=0')
  await self.service.enqueue(self.message);self.assertIsNotNone(self.row())
 async def test_self_and_old_messages_excluded(self):
  self.message.author.id=99
  await self.service.enqueue(self.message);self.assertIsNone(self.row())
  self.message.author.id=4;self.message.created_at=datetime.fromtimestamp(time.time()-90000,timezone.utc)
  await self.service.enqueue(self.message);self.assertIsNone(self.row())
 async def test_only_japanese_ignored_and_embed_supported(self):
  self.message.content='サーバーを再起動します'
  await self.service.enqueue(self.message);self.assertIsNone(self.row())
  self.message.embeds=[discord.Embed(title='Update',description='New features are available.')]
  await self.service.enqueue(self.message);self.assertIsNotNone(self.row())
 async def test_source_length_bound(self):
  self.message.content='English '*2000
  await self.service.enqueue(self.message);self.assertEqual(self.state(),'too_long')
 async def test_success_no_mentions_with_source_link(self):
  await self.service.enqueue(self.message)
  with patch('giveaway.auto_translate.translate_text',AsyncMock(return_value='10分後に再起動します。')):
   await self.service.process(self.row())
  self.assertEqual(self.state(),'sent');kw=self.channel.send.call_args.kwargs
  self.assertEqual(kw['allowed_mentions'].to_dict()['parse'],[])
  self.assertIn(self.message.jump_url,kw['embed'].fields[0].value)
 async def test_not_english_no_post(self):
  await self.service.enqueue(self.message)
  with patch('giveaway.auto_translate.translate_text',AsyncMock(return_value=None)):
   await self.service.process(self.row())
  self.channel.send.assert_not_awaited();self.assertEqual(self.state(),'skipped')
 async def test_disable_during_api_prevents_send(self):
  await self.service.enqueue(self.message)
  async def translate(t):self.db.execute('DELETE FROM translation_config');return '訳'
  with patch('giveaway.auto_translate.translate_text',translate):await self.service.process(self.row())
  self.channel.send.assert_not_awaited();self.assertEqual(self.state(),'cancelled')
 async def test_edited_during_api_prevents_stale_translation(self):
  await self.service.enqueue(self.message)
  async def translate(t):self.message.content='Changed announcement';return '訳'
  with patch('giveaway.auto_translate.translate_text',translate):await self.service.process(self.row())
  self.channel.send.assert_not_awaited();self.assertEqual(self.state(),'changed')
 async def test_daily_limit(self):
  self.assertTrue(self.service.reserve(1,1));self.assertFalse(self.service.reserve(1,1))
  await self.service.enqueue(self.message);self.db.execute('UPDATE translation_config SET daily_limit=1')
  with patch('giveaway.auto_translate.translate_text',AsyncMock()) as api:await self.service.process(self.row());api.assert_not_awaited()
  self.assertEqual(self.state(),'limit')
 async def test_global_limit(self):
  for gid in range(200):self.assertTrue(self.service.reserve(gid,1))
  self.assertFalse(self.service.reserve(201,50))
 async def test_restart_does_not_resend_unknown(self):
  await self.service.enqueue(self.message);self.service.finish(3,'processing')
  AutoTranslate(self.bot);self.assertEqual(self.state(),'uncertain')
 async def test_generation_change_cancels_queued(self):
  await self.service.enqueue(self.message);self.db.execute("UPDATE translation_config SET generation='b'")
  with patch('giveaway.auto_translate.translate_text',AsyncMock()) as api:await self.service.process(self.row());api.assert_not_awaited()
  self.assertEqual(self.state(),'cancelled')
 def test_filters_urls_and_extracts_fields(self):
  self.assertFalse(candidate('https://example.com'))
  self.message.embeds=[discord.Embed().add_field(name='Notice',value='Maintenance is complete.')]
  self.assertIn('Maintenance',extract_text(self.message))
 def test_schema(self):
  self.bot.auto_translate=self.service;register_auto_translate(self.bot)
  self.assertEqual([c.name for c in self.bot.tree.get_command('autotranslate').commands],['setup','disable','status'])

class APIContractTests(unittest.IsolatedAsyncioTestCase):
 async def test_payload_and_strict_output(self):
  response=NS(status=200,json=AsyncMock(return_value={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'is_english':True,'translation':'こんにちは'})}]}]}))
  ctx=AsyncMock();ctx.__aenter__.return_value=response
  session=Mock();session.post.return_value=ctx
  client=AsyncMock();client.__aenter__.return_value=session
  with patch('giveaway.auto_translate.api_key',return_value='test-only'),patch('giveaway.auto_translate.aiohttp.ClientSession',return_value=client):
   self.assertEqual(await translate_text('Hello everyone'),'こんにちは')
  p=session.post.call_args.kwargs['json'];self.assertFalse(p['store']);self.assertNotIn('tools',p)
  self.assertTrue(p['text']['format']['strict'])
