import sqlite3
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,AsyncMock
import discord
from giveaway.staffroom import RoomStore,StaffRooms,RoomControls,room_overwrites
from giveaway.redemptions import ReceiptError

class RoomStoreTests(unittest.TestCase):
    def test_scope_snapshot_and_idempotency(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close);s=RoomStore(c)
        r,new=s.create(1,9,[7,8,7],'相談',3,[50],123)
        self.assertTrue(new);self.assertEqual(r['participants'],[7,8])
        dup,new=s.create(1,9,[7],'相談',3,[50],123)
        self.assertFalse(new);self.assertEqual(dup['id'],r['id'])
        with self.assertRaises(ReceiptError):s.get(2,r['id'])
        s.configure(1,3);s.configure(2,4)
        self.assertEqual(s.config(1),3);self.assertEqual(s.config(2),4)
        self.assertEqual(RoomStore(c).get(1,r['id'])['subject'],'相談')
    def test_overwrites_are_private_and_close_disables_writes(self):
        def target(uid):m=Mock();m.id=uid;return m
        everyone,me,creator,user,role=[target(x) for x in (1,2,3,4,5)]
        guild=NS(id=1,default_role=everyone)
        opened=room_overwrites(guild,me,creator,[user],[role])
        self.assertFalse(opened[everyone].view_channel)
        for m in (creator,user,role):self.assertTrue(opened[m].view_channel);self.assertTrue(opened[m].send_messages)
        closed=room_overwrites(guild,me,creator,[user],[role],True)
        for m in (creator,user,role):self.assertTrue(closed[m].view_channel);self.assertFalse(closed[m].send_messages);self.assertFalse(closed[m].send_messages_in_threads)
        self.assertTrue(closed[me].send_messages)

class RoomOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.user=NS(id=7,bot=False,guild_permissions=NS(administrator=False),roles=[])
        self.actor=NS(id=9)
        self.guild=NS(id=1,fetch_member=AsyncMock(return_value=self.user))
        self.channel=NS(id=40,edit=AsyncMock(),send=AsyncMock())
        self.mod=NS(staff=AsyncMock(return_value=(self.guild,self.actor)),db=NS(config=lambda gid:dict(roles=[50],category=3)))
        self.bot=NS(store=NS(db=self.conn),moderation=self.mod)
        self.s=StaffRooms(self.bot)
        self.s.overwrites=AsyncMock(return_value={})
        self.r,_=self.s.db.create(1,9,[7],'相談',3,[50],123)
        self.r.update(channel=40,status='open');self.s.db.save(self.r)
        self.i=NS(guild_id=1,channel_id=40,channel=self.channel)
    async def asyncTearDown(self):self.conn.close()
    async def test_remove_close_and_persistent_control(self):
        r=await self.s.change(self.i,'remove',self.user)
        self.assertEqual(r['participants'],[])
        r=await self.s.change(self.i,'close')
        self.assertEqual(r['status'],'closed')
        self.assertFalse(r['sync_pending'])
        with self.assertRaises(ReceiptError):await self.s.change(self.i,'add',self.user)
        self.assertTrue(RoomControls(self.s).is_persistent())
    async def test_unauthorized_and_wrong_channel_do_not_edit(self):
        self.i.channel_id=41
        with self.assertRaises(ReceiptError):await self.s.change(self.i,'close')
        self.i.channel_id=40;self.mod.staff.side_effect=ReceiptError('denied')
        with self.assertRaises(ReceiptError):await self.s.change(self.i,'close')
        self.channel.edit.assert_not_awaited()
    async def test_uncertain_edit_is_recoverable_and_not_logged_as_success(self):
        self.channel.edit.side_effect=TimeoutError()
        with self.assertRaises(ReceiptError):await self.s.change(self.i,'close')
        r=self.s.db.get(1,self.r['id']);self.assertTrue(r['sync_pending'])
        self.assertEqual(r['history'][-1]['action'],'close_request')
        self.channel.edit.side_effect=None
        r=await self.s.change(self.i,'sync');self.assertFalse(r['sync_pending'])
        self.assertEqual(r['status'],'closed')
    async def test_cannot_remove_staff_or_owner(self):
        self.user.roles=[NS(id=50)]
        with self.assertRaises(ReceiptError):await self.s.change(self.i,'remove',self.user)
        self.channel.edit.assert_not_awaited()
    async def test_creation_and_duplicate_request_do_not_create_second_channel(self):
        self.s.category=AsyncMock(return_value=NS(id=3))
        self.s.welcome=AsyncMock()
        self.guild.create_text_channel=AsyncMock(return_value=self.channel)
        r=await self.s.create(self.i,[7],'相談',None,124)
        again=await self.s.create(self.i,[7],'相談',None,124)
        self.assertEqual(r['id'],again['id']);self.assertEqual(r['channel'],40)
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.guild.create_text_channel.call_args.kwargs['topic'],f"staff-room:{r['id']}")
    async def test_creation_timeout_retains_record_for_recovery(self):
        self.s.category=AsyncMock(return_value=NS(id=3))
        self.guild.create_text_channel=AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(ReceiptError):await self.s.create(self.i,[7],'相談',None,124)
        with self.assertRaises(ReceiptError):await self.s.create(self.i,[7],'相談',None,124)
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.s.db.all(1)[0]['status'],'creating')
