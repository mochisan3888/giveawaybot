import sys,unittest,sqlite3,asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock
sys.path.insert(0,str(Path(__file__).parent/'runtime'))
import discord
from giveaway.moderation import *
def err(cls,code,status=404):return cls(NS(status=status,reason='test'),{'code':code,'message':'test'})
class User:
 def __init__(self,uid,role=None):
  self.id=uid;self.send=AsyncMock(side_effect=err(discord.Forbidden,50007,403))
  if role is not None:self.top_role=role;self.guild_permissions=NS(ban_members=True,administrator=True)
 def __str__(self):return 'ResolvedUser'
class BanTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.actor=User(1,10);self.me=User(2,20);self.target=User(3);self.member=None
  self.guild=NS(id=9,owner_id=8,name='Test',ban=AsyncMock(),fetch_ban=AsyncMock(side_effect=err(discord.NotFound,10026)))
  async def fetch(uid):
   if uid==2:return self.me
   if self.member is not None:return self.member
   raise err(discord.NotFound,10007)
  self.guild.fetch_member=AsyncMock(side_effect=fetch)
  self.conn=sqlite3.connect(':memory:');self.bot=NS(user=self.me,store=NS(db=self.conn),fetch_user=AsyncMock(return_value=self.target))
  self.service=Moderation(self.bot);self.service.staff=AsyncMock(return_value=(self.guild,self.actor));self.service.configured=Mock(return_value={})
  self.i=NS(id=42,user=self.actor,guild_id=9,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
 async def asyncTearDown(self):self.conn.close()
 def test_input(self):
  for value in ('abc','-1','0','1.2','18446744073709551616'):
   with self.assertRaises(ReceiptError):ban_target_input(user_id=value)
  for kwargs in ({},{'user':self.target,'user_id':'3'}):
   with self.assertRaises(ReceiptError):ban_target_input(**kwargs)
  self.assertEqual(ban_target_input(user_id='3').id,3);self.assertIs(ban_target_input(user=self.target),self.target)
 async def test_absent_ban_and_failed_dm_preserves_case(self):
  await self.service.stage(self.i,discord.Object(id=3),'ban','reason')
  self.assertIn('ResolvedUser',self.i.followup.send.call_args.args[0]);self.assertIn('3',self.i.followup.send.call_args.args[0])
  await self.service.execute(self.i,3,'ban','reason',0,42)
  self.guild.ban.assert_awaited_once();c=self.service.db.cases(9,3)[0];self.assertEqual(c['state'],'applied');self.assertEqual(c['dm'],'failed')
 async def test_joined_high_role_after_confirmation_rejected(self):
  await self.service.stage(self.i,self.target,'ban','reason');self.member=User(3,11)
  with self.assertRaises(ReceiptError):await self.service.execute(self.i,3,'ban','reason',0,42)
  self.guild.ban.assert_not_awaited();self.assertEqual(self.service.db.cases(9),[])
 async def test_left_after_confirmation_supported(self):
  self.member=User(3,1);await self.service.stage(self.i,self.member,'ban','reason');self.member=None
  await self.service.execute(self.i,3,'ban','reason',0,42);self.guild.ban.assert_awaited_once()
 async def test_member_api_failures_not_absence(self):
  for error in (err(discord.Forbidden,50013,403),err(discord.HTTPException,0,500),err(discord.NotFound,10004)):
   self.guild.fetch_member=AsyncMock(side_effect=error)
   with self.assertRaises(discord.HTTPException):await resolve_ban_target(self.bot,self.guild,3)
  self.bot.fetch_user.assert_not_awaited()
 async def test_unknown_user_rejected(self):
  self.bot.fetch_user.side_effect=err(discord.NotFound,10013)
  with self.assertRaises(ReceiptError):await self.service.stage(self.i,self.target,'ban','reason')
 async def test_existing_ban_and_ban_lookup_error(self):
  self.guild.fetch_ban=AsyncMock(return_value=object())
  with self.assertRaises(ReceiptError):await self.service.execute(self.i,3,'ban','reason',0,42)
  self.guild.fetch_ban=AsyncMock(side_effect=err(discord.Forbidden,50013,403))
  with self.assertRaises(discord.Forbidden):await self.service.execute(self.i,3,'ban','reason',0,42)
  self.guild.ban.assert_not_awaited();self.assertEqual(self.service.db.cases(9),[])
 def test_protected_ids_and_permissions(self):
  for uid in (1,2,8):
   with self.assertRaises(ReceiptError):check_target(self.guild,self.actor,User(uid),self.me,'ban')
  self.actor.guild_permissions.ban_members=False
  with self.assertRaises(ReceiptError):check_target(self.guild,self.actor,self.target,self.me,'ban')
  self.actor.guild_permissions.ban_members=True;self.me.guild_permissions.ban_members=False
  with self.assertRaises(ReceiptError):check_target(self.guild,self.actor,self.target,self.me,'ban')
if __name__=='__main__':unittest.main()
