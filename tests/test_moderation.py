import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import discord
from giveaway.moderation import Moderation, check_target, AppealButtons, ModPanel
from giveaway.moderation_store import ModerationStore, validate_template, render_template
from giveaway.redemptions import ReceiptError


def member(uid,rank=1,admin=False,permissions=True):
    return NS(id=uid,top_role=rank,roles=[NS(id=50)],guild_permissions=NS(administrator=admin,moderate_members=permissions,kick_members=permissions,ban_members=permissions),send=AsyncMock(return_value=NS(id=555)),timeout=AsyncMock(),kick=AsyncMock())

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.addCleanup(self.conn.close);self.db=ModerationStore(self.conn)
    def test_guild_scope_count_revoke_and_idempotency(self):
        self.db.configure(1,roles=[50],log=20,category=30)
        c,fresh=self.db.create(1,7,9,'warn','reason','req');self.assertTrue(fresh)
        duplicate,fresh=self.db.create(1,7,9,'warn','reason','req');self.assertFalse(fresh);self.assertEqual(c['id'],duplicate['id'])
        self.assertEqual(self.db.warnings(1,7),0)
        c['state']='applied';self.db.save(c)
        self.assertEqual(self.db.warnings(1,7),1);self.assertEqual(self.db.warnings(2,7),0)
        self.assertIsNone(self.db.get(c['id'],2))
        with self.assertRaises(ReceiptError):self.db.revoke(2,c['id'],9,'wrong guild')
        self.db.revoke(1,c['id'],9,'mistake')
        self.assertEqual(self.db.warnings(1,7),0);self.assertEqual(len(self.db.cases(1)),1)
        with self.assertRaises(ReceiptError):self.db.revoke(1,c['id'],9,'again')
        reloaded=ModerationStore(self.conn)
        self.assertTrue(reloaded.get(c['id'])['revoked']);self.assertEqual(reloaded.config(2),{})
    def test_template_rejects_attribute_access_conversion_and_formats(self):
        for value in ('{user.__class__}','{user!r}','{user:>1000000}','{bad}','{', ''):
            with self.assertRaises(ReceiptError):validate_template(value)
        self.assertEqual(render_template('{server}: {reason} {{}}',dict(server='Salt',reason='test')),'Salt: test {}')
    def test_log_destination_snapshot(self):
        self.db.configure(1,log=20)
        c,_=self.db.create(1,7,9,'warn','r',1)
        self.db.event(c,'first',9)
        self.db.configure(1,log=30)
        self.db.event(c,'second',9)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT channel FROM moderation_events ORDER BY id')],[20,30])

class PermissionTests(unittest.TestCase):
    def test_hierarchy_native_permissions_and_admin_timeout(self):
        guild=NS(owner_id=100);actor=member(9,5);target=member(7,1);me=member(8,10)
        check_target(guild,actor,target,me,'timeout')
        for bad in (actor,me,member(100),member(7,5)):
            with self.assertRaises(ReceiptError):check_target(guild,actor,bad,me,'ban')
        with self.assertRaises(ReceiptError):check_target(guild,actor,member(7,1,True),me,'timeout')
        with self.assertRaises(ReceiptError):check_target(guild,member(9,5,permissions=False),target,me,'ban')
        with self.assertRaises(ReceiptError):check_target(guild,actor,target,member(8,0),'kick')
        check_target(guild,member(100,0),target,me,'ban')

class ServiceFixtures:
    async def asyncSetUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.actor=member(9,5);self.target=member(7);self.me=member(8,10)
        self.channel=Mock(spec=discord.TextChannel);self.channel.id=40;self.channel.guild=NS(id=1);self.channel.send=AsyncMock(return_value=NS(id=99))
        async def fetch(uid):return {9:self.actor,7:self.target,8:self.me}[uid]
        self.guild=NS(id=1,name='Salt',owner_id=100,fetch_member=AsyncMock(side_effect=fetch),ban=AsyncMock(),unban=AsyncMock(),fetch_ban=AsyncMock(),fetch_channel=AsyncMock(return_value=self.channel))
        self.bot=NS(user=self.me,store=NS(db=self.conn),get_guild=lambda gid:self.guild if gid==1 else None,fetch_user=AsyncMock(return_value=self.target))
        self.s=Moderation(self.bot);self.s.db.configure(1,roles=[50],log=20,category=30)
        self.i=NS(guild_id=1,user=self.actor,channel_id=40,channel=self.channel,guild=self.guild)
    async def asyncTearDown(self):self.conn.close()
    def case(self,action='timeout'):
        c,_=self.s.db.create(1,7,9,action,'why',str(len(self.s.db.cases(1))+100))
        c.update(state='applied',channel=40,appeal='open');self.s.db.save(c);return c
class ServiceTests(ServiceFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_warn_idempotent_and_dm_failure_not_false_failure(self):
        self.target.send.side_effect=discord.Forbidden(NS(status=403,reason='no'),{'code':50007,'message':'Cannot DM'})
        result=await self.s.execute(self.i,7,'warn','why',0,123)
        self.assertIn('送信できません',result);self.assertEqual(self.s.db.warnings(1,7),1)
        await self.s.execute(self.i,7,'warn','why',0,123)
        self.assertEqual(len(self.s.db.cases(1)),1);self.assertEqual(self.target.send.await_count,1)
    async def test_timeout_and_release(self):
        await self.s.execute(self.i,7,'timeout','why',60,1)
        until=self.target.timeout.call_args.args[0]
        self.assertIsNotNone(until.tzinfo)
        await self.s.execute(self.i,7,'untimeout','why',0,2)
        self.assertIsNone(self.target.timeout.call_args.args[0])
    async def test_kick_notification_precedes_action(self):
        steps=[]
        async def send(**kwargs):steps.append('dm');return NS(id=1)
        async def kick(**kwargs):steps.append('kick')
        self.target.send.side_effect=send;self.target.kick.side_effect=kick
        await self.s.execute(self.i,7,'kick','why',0,1)
        self.assertEqual(steps,['dm','kick'])
    async def test_ban_retains_messages_and_unban(self):
        self.guild.fetch_ban.side_effect = discord.NotFound(NS(status=404, reason='Not Found'), {'code': 10026, 'message': 'Unknown Ban'})
        await self.s.execute(self.i,7,'ban','why',0,1)
        self.assertEqual(self.guild.ban.call_args.kwargs['delete_message_seconds'],0)
        self.guild.fetch_ban.side_effect = None
        await self.s.execute(self.i,7,'unban','why',0,2)
        self.guild.unban.assert_awaited_once()
    async def test_api_failure_not_counted_as_applied(self):
        self.target.timeout.side_effect=discord.Forbidden(NS(status=403,reason='no'),'no')
        await self.s.execute(self.i,7,'timeout','why',10,1)
        self.assertEqual(self.s.db.cases(1)[0]['state'],'failed')
        self.target.timeout.side_effect=TimeoutError()
        await self.s.execute(self.i,7,'timeout','why',10,2)
        self.assertEqual(self.s.db.cases(1)[0]['state'],'uncertain')
    async def test_staff_access_revoked_before_execution(self):
        self.actor.roles=[]
        with self.assertRaises(ReceiptError):await self.s.execute(self.i,7,'warn','why',0,1)
        self.assertEqual(self.s.db.cases(1),[])
    async def test_appeal_impersonation_and_cross_guild(self):
        c=self.case()
        for i in (NS(guild_id=None,user=NS(id=99)),NS(guild_id=2,user=NS(id=7))):
            with self.assertRaises(ReceiptError):await self.s.relay(i,c['id'],'hello')
        with self.assertRaises(ReceiptError):await self.s.relay(NS(guild_id=2,user=self.actor),c['id'],'hello',True)
        self.channel.send.assert_not_awaited()
    async def test_user_dm_and_staff_reply_with_no_message_intent(self):
        c=self.case();i=NS(guild_id=None,user=self.target)
        self.assertIn('送信しました',await self.s.relay(i,c['id'],'appeal'))
        self.assertEqual(self.channel.send.call_args.kwargs['embed'].description,'appeal')
        with self.assertRaises(ReceiptError):await self.s.relay(i,c['id'],'spam')
        self.assertIn('送信しました',await self.s.relay(self.i,c['id'],'response',True))
        self.assertEqual(self.target.send.call_args.kwargs['embed'].description,'response')
    async def test_done_and_closed_reject_further_messages(self):
        c=self.case();await self.s.appeal_action(self.i,c['id'],'done')
        with self.assertRaises(ReceiptError):await self.s.relay(NS(guild_id=None,user=self.target),c['id'],'hello')
        await self.s.appeal_action(self.i,c['id'],'close')
        with self.assertRaises(ReceiptError):await self.s.appeal_action(self.i,c['id'],'start')
    async def test_views_persist_and_history_is_guild_scoped(self):
        c=self.case('warn')
        self.assertTrue(AppealButtons(self.s,c['id']).is_persistent())
        self.assertTrue(AppealButtons(self.s,c['id'],True).is_persistent())
        self.assertTrue(ModPanel(self.s).is_persistent())
        self.assertEqual(len(self.s.history(2,7,1).fields),0)
        self.assertEqual(len(self.s.people(1,1).fields),1)
    async def test_pending_or_failed_action_has_no_appeal(self):
        c=self.case();c['state']='failed';self.s.db.save(c)
        with self.assertRaises(ReceiptError):await self.s.relay(NS(guild_id=None,user=self.target),c['id'],'hello')
    async def test_dm_failure_is_reported_in_appeal(self):
        c=self.case();self.target.send.side_effect=discord.Forbidden(NS(status=403,reason='no'),'no')
        self.assertIn('送信を確認できません',await self.s.relay(self.i,c['id'],'reply',True))

class DeliveryTests(ServiceFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_log_delivery_preserves_transcript_and_suppresses_mentions(self):
        self.bot.get_channel=lambda cid:self.channel
        self.bot.http=NS(request=AsyncMock(return_value={'id':'123'}))
        c=self.case();self.s.db.event(c,'本人から異議申し立て',7,'@everyone help')
        await self.s.flush();await self.s.flush()
        self.bot.http.request.assert_awaited_once()
        payload=self.bot.http.request.call_args.kwargs['json']
        self.assertEqual(payload['allowed_mentions'],{'parse':[]})
        self.assertTrue(payload['enforce_nonce'])
        self.assertEqual(payload['embeds'][0]['description'],'@everyone help')
    async def test_uncertain_log_stops_after_nonce_window(self):
        import time
        self.bot.get_channel=lambda cid:self.channel
        self.bot.http=NS(request=AsyncMock(side_effect=TimeoutError()))
        c=self.case();self.s.db.event(c,'event',9)
        await self.s.flush()
        self.conn.execute('UPDATE moderation_events SET sent_at=?',(time.time()-130,))
        await self.s.flush()
        self.assertEqual(self.conn.execute('SELECT state FROM moderation_events').fetchone()[0],'uncertain')
        self.bot.http.request.assert_awaited_once()
    async def test_private_ticket_overwrites_and_unknown_create_does_not_duplicate(self):
        category=Mock(spec=discord.CategoryChannel);category.id=30
        channel=Mock(spec=discord.TextChannel);channel.id=40;channel.topic='moderation-appeal:1'
        role=Mock(spec=discord.Role);role.id=50
        everyone=Mock(spec=discord.Role);everyone.id=1
        me=Mock(spec=discord.Member);me.id=8
        self.guild.default_role=everyone
        self.guild.get_role=lambda rid:role
        self.guild.fetch_channels=AsyncMock(return_value=[])
        self.guild.fetch_channel=AsyncMock(return_value=category)
        self.guild.fetch_member=AsyncMock(return_value=me)
        self.guild.create_text_channel=AsyncMock(side_effect=TimeoutError())
        self.s.validate_config=AsyncMock()
        c=self.case();c['channel']=None;self.s.db.save(c)
        with self.assertRaises(TimeoutError):await self.s.appeal_channel(c,self.guild)
        overwrites=self.guild.create_text_channel.call_args.kwargs['overwrites']
        self.assertFalse(overwrites[everyone].view_channel)
        self.assertTrue(overwrites[role].view_channel)
        self.assertTrue(overwrites[me].send_messages)
        self.assertNotIn(self.target.id,[getattr(x,'id',None) for x in overwrites])
        with self.assertRaises(ReceiptError):await self.s.appeal_channel(c,self.guild)
        self.guild.create_text_channel.assert_awaited_once()
        self.guild.fetch_channels.return_value=[channel]
        self.assertEqual((await self.s.appeal_channel(c,self.guild)).id,40)
        self.guild.create_text_channel.assert_awaited_once()

class PublicStatsTests(ServiceFixtures,unittest.IsolatedAsyncioTestCase):
    async def test_caution_is_recorded_and_not_in_stats(self):
        await self.s.execute(self.i,7,'caution','PRIVATE_CAUTION',0,1)
        self.assertEqual(self.s.db.warnings(1,7),0)
        self.target.send.assert_awaited_once()
        self.assertIn('PRIVATE_CAUTION',str(self.s.history(1,7,1).to_dict()))
        self.assertNotIn('PRIVATE_CAUTION',str(self.s.public_stats(1,7,1).to_dict()))
    async def test_public_stats_excludes_failed_and_other_guild_and_appeal_content(self):
        c=self.case('warn');self.s.db.event(c,'本人から異議申し立て',7,'PRIVATE_APPEAL')
        self.s.db.revoke(1,c['id'],9,'cancel')
        failed,_=self.s.db.create(1,7,9,'ban','FAILED_BAN','failed');failed['state']='failed';self.s.db.save(failed)
        other,_=self.s.db.create(2,7,9,'warn','OTHER_GUILD','other');other['state']='applied';self.s.db.save(other)
        result=str(self.s.public_stats(1,7,1).to_dict())
        self.assertIn('取り消し済み',result)
        for private in ('PRIVATE_APPEAL','FAILED_BAN','OTHER_GUILD'):self.assertNotIn(private,result)
        self.assertEqual(self.s.db.warnings(1,7),0)
        self.assertEqual(self.s.db.warnings(2,7),1)
