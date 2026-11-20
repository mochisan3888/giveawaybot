import asyncio
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from giveaway.fulfillment import Fulfillment, ClaimView, MinecraftPanelView, ticket_overwrites
from giveaway.redemptions import ReceiptError

class FulfillmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.bot=SimpleNamespace(store=SimpleNamespace(db=self.conn),user=SimpleNamespace(id=999),
            http=SimpleNamespace(request=AsyncMock(return_value={'id':'123'})),add_view=MagicMock())
        self.s=Fulfillment(self.bot)
        game=dict(id=1,guild=10,channel=11,message=12,prize='prize',
            receipt_policy=dict(roles=[30],category=20,minutes=60,minecraft=False),
            receipt_events=[dict(round=1,winners=[100],at=time.time(),deadline=time.time()+3600)])
        self.s.db.seed(game)
        self.r=self.s.db.get(1)
    async def asyncTearDown(self):self.conn.close()
    def interaction(self,uid=100):
        return SimpleNamespace(guild_id=10,user=SimpleNamespace(id=uid),response=SimpleNamespace(
            send_message=AsyncMock(),defer=AsyncMock(),send_modal=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
    async def test_panel_unlinked_user_opens_id_modal(self):
        self.s.suffix='verify.example.com';self.s.secret='s'*40
        panel=MinecraftPanelView(self.s)
        self.assertTrue(panel.is_persistent())
        i=self.interaction()
        await panel.children[0].callback(i)
        view=i.response.send_message.call_args.kwargs['view']
        self.assertEqual([b.label for b in view.children],['Java版','統合版'])
        await view.children[0].callback(i)
        i.response.send_modal.assert_awaited_once()
        self.assertIsNone(i.response.send_modal.call_args.args[0].rid)

    async def test_panel_linked_user_gets_private_status(self):
        with self.conn:self.conn.execute("insert into guild_minecraft_links(guild,user,uuid,name,verified,original_name) values (10,?,?,?,?, '元の名前')",(100,'a'*32,'Steve',time.time()))
        i=self.interaction()
        await MinecraftPanelView(self.s).children[0].callback(i)
        i.response.send_modal.assert_not_awaited()
        i.response.send_message.assert_awaited_once()
        self.assertTrue(i.response.send_message.call_args.kwargs['ephemeral'])
        self.assertIn('Steve',i.response.send_message.call_args.args[0])

    async def test_unrelated_user_cannot_claim(self):
        with self.assertRaises(ReceiptError):await self.s.begin(self.interaction(200),1)
    async def test_linked_user_skips_id_form_and_opens(self):
        self.r['policy']['minecraft']=True;self.s.db.save(self.r)
        with self.conn:self.conn.execute("insert into guild_minecraft_links(guild,user,uuid,name,verified,original_name) values (10,?,?,?,?, '元の名前')",(100,'a'*32,'Steve',time.time()))
        async def opened(r):r['channel']=50;self.s.db.save(r)
        self.s.open_ticket=AsyncMock(side_effect=opened)
        i=self.interaction();await self.s.begin(i,1)
        i.response.send_modal.assert_not_awaited()
        self.s.open_ticket.assert_awaited_once()
    async def test_unlinked_required_user_gets_private_form(self):
        self.r['policy']['minecraft']=True;self.s.db.save(self.r)
        self.s.suffix='verify.example.com';self.s.secret='s'*40
        i=self.interaction();await self.s.begin(i,1)
        view=i.response.send_message.call_args.kwargs['view']
        self.assertEqual([b.label for b in view.children],['Java版','統合版'])
        await view.children[0].callback(i)
        i.response.send_modal.assert_awaited_once()
        self.assertFalse(self.s.db.get(1)['requested'])
    async def test_expired_user_cannot_open_or_launch_link(self):
        self.r['deadline']=0;self.s.db.save(self.r)
        i=self.interaction()
        with self.assertRaises(ReceiptError):await self.s.begin(i,1)
        i.response.send_modal.assert_not_awaited()
    async def test_existing_ticket_is_returned_without_recreating(self):
        self.r.update(channel=50,status='closed',deadline=0);self.s.db.save(self.r)
        self.s.open_ticket=AsyncMock()
        i=self.interaction();await self.s.begin(i,1)
        self.s.open_ticket.assert_not_awaited()
        self.assertIn('<#50>',i.response.send_message.call_args.args[0])
    async def test_dm_blocked_is_saved_and_claim_button_remains_usable(self):
        user=SimpleNamespace(create_dm=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='blocked'),{'code':50007,'message':'blocked'})))
        self.bot.get_user=lambda uid:user
        await self.s.deliver_dm(self.r)
        self.assertEqual(self.s.db.get(1)['dm'],'blocked')
        self.assertEqual(ClaimView(self.s,1).children[0].custom_id,'receipt:1:claim')
    async def test_dm_sent_once_even_after_reload(self):
        user=SimpleNamespace(create_dm=AsyncMock(return_value=SimpleNamespace(id=321)))
        self.bot.get_user=lambda uid:user
        await self.s.deliver_dm(self.r)
        await self.s.deliver_dm(self.s.db.get(1))
        self.bot.http.request.assert_awaited_once()
        payload=self.bot.http.request.call_args.kwargs['json']
        self.assertIn('当選しました',payload['content'])
        self.assertIn('components',payload)
    async def test_open_ticket_exact_deadline_blocks_channel_creation(self):
        guild=SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(id=100)))
        self.bot.get_guild=lambda gid:guild
        self.s.validate_policy=AsyncMock(return_value=(object(),[]))
        self.r['deadline']=0
        with self.assertRaises(ReceiptError):await self.s.open_ticket(self.r)
        self.assertEqual(self.s.db.get(1)['status'],'expired')
    def ticket_guild(self):
        channel=MagicMock(spec=discord.TextChannel)
        channel.id=50
        channel.topic='giveaway-receipt:999:1'
        channel.created_at=SimpleNamespace(timestamp=lambda:time.time())
        guild=SimpleNamespace(default_role=object(),me=object(),fetch_member=AsyncMock(return_value=object()),
            create_text_channel=AsyncMock(return_value=channel),fetch_channels=AsyncMock(return_value=[]))
        self.bot.get_guild=lambda gid:guild
        self.s.validate_policy=AsyncMock(return_value=(object(),[object()]))
        self.s.update_panel=AsyncMock()
        return guild,channel
    async def test_concurrent_claims_create_only_one_channel(self):
        guild,channel=self.ticket_guild()
        await asyncio.gather(self.s.begin(self.interaction(),1),self.s.begin(self.interaction(),1))
        guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.s.db.get(1)['channel'],50)
    async def test_channel_created_before_crash_is_recovered_after_deadline(self):
        guild,channel=self.ticket_guild()
        self.r.update(creating=True,deadline=time.time()-10)
        channel.created_at=SimpleNamespace(timestamp=lambda:time.time()-20)
        guild.fetch_channels.return_value=[channel]
        await self.s.open_ticket(self.r)
        guild.create_text_channel.assert_not_awaited()
        self.assertEqual(self.s.db.get(1)['channel'],50)
    async def test_creation_response_after_deadline_removes_late_ticket(self):
        guild,channel=self.ticket_guild()
        channel.created_at=SimpleNamespace(timestamp=lambda:self.r['deadline']+1)
        channel.delete=AsyncMock()
        with self.assertRaises(ReceiptError):await self.s.open_ticket(self.r)
        channel.delete.assert_awaited_once()
        self.assertEqual(self.s.db.get(1)['status'],'expired')
    async def test_panel_mentions_only_winner_and_configured_roles(self):
        self.r.update(channel=50,status='open')
        self.bot.get_channel=lambda cid:SimpleNamespace(id=50)
        await self.s.update_panel(self.r)
        payload=self.bot.http.request.call_args.kwargs['json']
        self.assertEqual(payload['allowed_mentions'],{'parse':[],'roles':['30'],'users':['100']})
        self.assertIn('対応開始',str(payload['components']))

    async def test_staff_only_action_and_audit(self):
        self.r.update(channel=50,status='open');self.s.db.save(self.r)
        i=SimpleNamespace(guild=SimpleNamespace(id=10,fetch_member=AsyncMock(return_value=SimpleNamespace(id=200,roles=[]))),channel_id=50,user=SimpleNamespace(id=200))
        with self.assertRaises(ReceiptError):await self.s.action(i,1,'start')
        i.guild.fetch_member.return_value=SimpleNamespace(id=200,roles=[SimpleNamespace(id=30)])
        self.s.update_panel=AsyncMock()
        await self.s.action(i,1,'start')
        r=self.s.db.get(1)
        self.assertEqual(r['assignee'],200)
        self.assertEqual(r['history'][0]['action'],'start')
        await self.s.action(i,1,'done')
        self.assertEqual(self.s.db.get(1)['status'],'completed')
    async def test_authenticated_callback_only_and_token_replay(self):
        self.s.secret='s'*40
        token,_=self.s.db.challenge(100,'a'*32,'Steve',guild=10,original_name='元の名前')
        app=web.Application();app.router.add_post('/verify',self.s.verified)
        async with TestClient(TestServer(app)) as client:
            body=dict(token=token,uuid='a'*32,name='Steve')
            response=await client.post('/verify',json=body)
            self.assertEqual(response.status,401)
            response=await client.post('/verify',json=body,headers={'Authorization':'Bearer '+self.s.secret})
            self.assertEqual(response.status,200)
            response=await client.post('/verify',json=body,headers={'Authorization':'Bearer '+self.s.secret})
            self.assertEqual(response.status,400)

class OverwriteTests(unittest.TestCase):
    def test_private_acl_and_closed_winner_cannot_post(self):
        everyone,winner,role1,role2,bot=[object() for _ in range(5)]
        guild=SimpleNamespace(default_role=everyone)
        acl=ticket_overwrites(guild,winner,[role1,role2],bot)
        self.assertFalse(acl[everyone].view_channel)
        for x in [winner,role1,role2,bot]:self.assertTrue(acl[x].view_channel)
        closed=ticket_overwrites(guild,winner,[role1,role2],bot,closed=True)
        self.assertFalse(closed[winner].send_messages)
        self.assertFalse(closed[winner].send_messages_in_threads)
        self.assertTrue(closed[role1].read_message_history)
