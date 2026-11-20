import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from giveaway.serverinfo import ServerInfo, channel_names, info_overwrites

class ServerInfoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=sqlite3.connect(':memory:')
        self.price={'buy':'1.25','sell':'1'}
        self.channels=[]
        self.guild=SimpleNamespace(id=10,default_role=object(),me=object(),fetch_channels=AsyncMock(side_effect=lambda:self.channels))
        self.bot=SimpleNamespace(store=SimpleNamespace(db=self.db),skelly=SimpleNamespace(db=SimpleNamespace(config=lambda gid:self.price)),fetch_guild=AsyncMock(return_value=SimpleNamespace(approximate_member_count=123)))
        self.s=ServerInfo(self.bot)
    async def asyncTearDown(self):self.db.close()
    async def test_prices_count_refresh_and_restart_cooldown(self):
        c={'category':20,'channels':{},'next_update':0}
        for i,key in enumerate(['members','buy','sell'],100):
            ch=MagicMock(spec=discord.VoiceChannel);ch.id=i;ch.name='old';ch.edit=AsyncMock()
            self.channels.append(ch);c['channels'][key]={'id':i,'last_edit':0}
        self.s.save(10,c)
        with patch('giveaway.serverinfo.time.time',return_value=1000):await self.s.refresh(self.guild)
        self.assertEqual(self.channels[0].edit.call_args.kwargs['name'],'👥｜メンバー：約123人')
        self.assertEqual(self.channels[1].edit.call_args.kwargs['name'],'🟢｜Skelly Buy：1.25m / 個')
        self.price['buy']='2'
        restarted=ServerInfo(self.bot)
        with patch('giveaway.serverinfo.time.time',return_value=1100):await restarted.refresh(self.guild)
        self.assertEqual(self.channels[1].edit.await_count,1)
        with patch('giveaway.serverinfo.time.time',return_value=1700):await restarted.refresh(self.guild)
        self.assertEqual(self.channels[1].edit.call_args.kwargs['name'],'🟢｜Skelly Buy：2m / 個')
    async def test_unchanged_and_deleted_channels_are_not_edited_or_recreated(self):
        names=channel_names(123,self.price)
        ch=MagicMock(spec=discord.VoiceChannel);ch.id=100;ch.name=names['members'];ch.edit=AsyncMock();self.channels.append(ch)
        self.s.save(10,{'category':20,'channels':{'members':{'id':100,'last_edit':0},'buy':{'id':101,'last_edit':0}},'next_update':0})
        await self.s.refresh(self.guild);ch.edit.assert_not_awaited()
    def test_separate_status_tracks_reception(self):
        self.assertEqual(channel_names(7,{'accepting':False})['status'],'🔴｜Skelly：受付停止中')
        self.assertEqual(channel_names(7,{})['status'],'🟢｜Skelly：受付中')
    def test_side_stop_hides_only_stopped_price(self):
        names=channel_names(7,{'buy':'1','sell':'2','buy_accepting':False,'sell_accepting':True})
        self.assertEqual(names['buy'],'🟢｜Skelly Buy：STOP')
        self.assertEqual(names['sell'],'🔵｜Skelly Sell：2m / 個')
        self.assertIn('Sellのみ受付中',names['status'])
        names=channel_names(7,{'accepting':False})
        self.assertIn('STOP',names['buy']);self.assertIn('STOP',names['sell'])

    def test_missing_prices_are_not_invented(self):
        self.assertEqual(channel_names(None,{})['buy'],'🟢｜Skelly Buy：未設定')
    def test_channels_visible_but_locked_and_no_chat(self):
        perms=info_overwrites(self.guild)[self.guild.default_role]
        self.assertTrue(perms.view_channel);self.assertFalse(perms.connect);self.assertFalse(perms.send_messages)

    async def test_manual_refresh_bypasses_both_timers(self):
        ch=MagicMock(spec=discord.VoiceChannel);ch.id=100;ch.name='old';ch.edit=AsyncMock();self.channels.append(ch)
        self.s.save(10,{'category':20,'channels':{'members':{'id':100,'last_edit':999}},'next_update':1600})
        with patch('giveaway.serverinfo.time.time',return_value=1000):
            result=await self.s.refresh(self.guild,force=True)
        self.assertEqual(result,{'updated':1,'failed':0,'missing':0})
        ch.edit.assert_awaited_once()
        self.assertEqual(self.s.get(10)['next_update'],1600)

    async def test_manual_missing_setup(self):
        from giveaway.redemptions import ReceiptError
        with self.assertRaises(ReceiptError):await self.s.refresh(self.guild,force=True)

    async def test_manual_reports_failure(self):
        ch=MagicMock(spec=discord.VoiceChannel);ch.id=100;ch.name='old'
        ch.edit=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'No permission'))
        self.channels.append(ch)
        self.s.save(10,{'category':20,'channels':{'members':{'id':100,'last_edit':0}},'next_update':0})
        result=await self.s.refresh(self.guild,force=True)
        self.assertEqual(result['failed'],1)
        self.assertEqual(result['updated'],0)

    async def test_unknown_unrelated_channel_type_does_not_block_refresh(self):
        ch=MagicMock(spec=discord.VoiceChannel);ch.id=100;ch.name='old';ch.edit=AsyncMock()
        self.guild.fetch_channels.side_effect=discord.InvalidData('Unknown channel type 20')
        self.guild.fetch_channel=AsyncMock(return_value=ch)
        self.s.save(10,{'category':20,'channels':{'members':{'id':100,'last_edit':0}},'next_update':0})
        result=await self.s.refresh(self.guild,force=True)
        self.assertEqual(result,{'updated':1,'failed':0,'missing':0})
        self.guild.fetch_channel.assert_awaited_once_with(100)
        ch.edit.assert_awaited_once()
