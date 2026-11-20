import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import discord
from giveaway.donation import Donation,DonationPanel,register_donation
from giveaway.donation_store import DonationStore
from giveaway.redemptions import ReceiptError

class DonationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.owner,self.role,self.everyone,self.me=[MagicMock() for _ in range(4)];self.role.id=30
        self.ch=MagicMock(spec=discord.TextChannel);self.ch.id=50;self.ch.topic='donation-ticket:999:1'
        self.ch.get_partial_message.return_value.edit=AsyncMock()
        self.guild=SimpleNamespace(id=10,default_role=self.everyone,me=self.me,fetch_member=AsyncMock(return_value=self.owner),
            fetch_channels=AsyncMock(return_value=[]),create_text_channel=AsyncMock(return_value=self.ch),get_role=lambda rid:self.role)
        self.bot=SimpleNamespace(user=SimpleNamespace(id=999),store=SimpleNamespace(db=self.conn),add_view=MagicMock(),
            get_guild=lambda gid:self.guild,get_channel=lambda cid:self.ch,http=SimpleNamespace(request=AsyncMock(return_value={'id':'123'})),
            fulfillment=SimpleNamespace(validate_policy=AsyncMock(return_value=(object(),[self.role])),db=SimpleNamespace(link=lambda uid,gid:None)))
        self.s=Donation(self.bot);self.s.db.set_config(10,policy={'category':20,'roles':[30],'minecraft':False})
        self.i=SimpleNamespace(guild=self.guild,guild_id=10,user=SimpleNamespace(id=100),channel_id=50,channel=self.ch)
    async def asyncTearDown(self):self.conn.close()
    async def test_concurrent_submissions_one_private_ticket_and_only_staff_mentions(self):
        a,b=await asyncio.gather(self.s.request(self.i,'10m @everyone','明日'),self.s.request(self.i,'10m',''))
        self.assertEqual(a['id'],b['id']);self.guild.create_text_channel.assert_awaited_once()
        acl=self.guild.create_text_channel.call_args.kwargs['overwrites']
        self.assertFalse(acl[self.everyone].view_channel);self.assertTrue(acl[self.owner].view_channel)
        payload=self.bot.http.request.call_args.kwargs['json']
        self.assertEqual(payload['allowed_mentions'],{'parse':[],'users':['100'],'roles':['30']})
        self.assertEqual(self.s.db.get(a['id'])['content'],'10m @everyone')
    async def test_policy_snapshot_and_restart_recovery(self):
        r=self.s.db.create(10,100,'スポナー5個','');r['creating']=True;self.s.db.save(r)
        self.s.db.set_config(10,policy={'category':99,'roles':[31],'minecraft':False})
        self.s.db=DonationStore(self.conn)
        self.assertEqual(self.s.db.get(r['id'])['policy']['category'],20)
        self.guild.fetch_channels.return_value=[self.ch]
        await self.s.open_ticket(self.s.db.get(r['id']));self.guild.create_text_channel.assert_not_awaited()
    async def test_staff_only_lifecycle_preserves_history(self):
        r=await self.s.request(self.i,'20m','')
        self.guild.fetch_member.return_value=SimpleNamespace(id=100,roles=[])
        with self.assertRaises(ReceiptError):await self.s.action(self.i,r['id'],'start')
        staff=SimpleNamespace(id=200,roles=[self.role]);self.i.user.id=200
        self.guild.fetch_member.side_effect=lambda uid:staff if uid==200 else self.owner
        await self.s.action(self.i,r['id'],'start');await self.s.action(self.i,r['id'],'done');await self.s.action(self.i,r['id'],'close')
        self.assertEqual(self.s.db.get(r['id'])['status'],'closed')
        self.assertFalse(self.ch.edit.call_args.kwargs['overwrites'][self.owner].send_messages)
        self.assertIsNone(self.s.db.active(10,100))
    async def test_persistent_panel_and_admin_configuration(self):
        self.assertTrue(DonationPanel(self.s).is_persistent())
        self.bot.tree=SimpleNamespace(add_command=MagicMock());self.bot.donation=self.s
        register_donation(self.bot);group=self.bot.tree.add_command.call_args.args[0]
        self.i.permissions=discord.Permissions.none()
        for name in ['setup','panel']:
            with self.assertRaises(discord.app_commands.MissingPermissions):
                for check in group.get_command(name).checks:check(self.i)
    async def test_blank_and_oversized_donations_rejected(self):
        for text in [' ', 'x'*1001]:
            with self.assertRaises(ReceiptError):self.s.db.create(10,100,text,'')
        self.assertIsNone(self.s.db.active(10,100))
