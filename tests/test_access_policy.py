import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from giveaway.access_policy import blocked,GIVEAWAY_ONLY
class AccessTests(unittest.TestCase):
 def setUp(self):
  self.gid=next(iter(GIVEAWAY_ONLY))
  db=SimpleNamespace(get=lambda id:{'guild':self.gid})
  self.bot=SimpleNamespace(moderation=SimpleNamespace(db=db),fulfillment=SimpleNamespace(db=db))
 def test_commands_and_modals(self):
  for t in (2,4):
   for name in ('minecraft','moderation','vip','vipgiveaway','staffroom','skelly','donation','serverinfo','warnstats','ticketlog'):
    self.assertTrue(blocked(self.bot,dict(type=t,guild_id=str(self.gid),data={'name':name})))
   self.assertFalse(blocked(self.bot,dict(type=t,guild_id=str(self.gid),data={'name':'giveaway'})))
  self.assertTrue(blocked(self.bot,dict(type=5,guild_id=str(self.gid),data={'custom_id':'random-modal'})))
 def test_components_and_other_guild(self):
  for custom in ('giveaway:join:v1','giveaway:leave:v1','giveaway:status:v1'):
   self.assertFalse(blocked(self.bot,dict(type=3,guild_id=self.gid,data={'custom_id':custom})))
  for custom in ('giveaway:claim:v1','minecraft:link:v1','moderation:panel:list','vip:new:v1'):
   self.assertTrue(blocked(self.bot,dict(type=3,guild_id=self.gid,data={'custom_id':custom})))
   self.assertFalse(blocked(self.bot,dict(type=3,guild_id=123,data={'custom_id':custom})))
 def test_dm_records(self):
  for custom in ('receipt:10:claim','moderation:10:appeal'):
   self.assertTrue(blocked(self.bot,dict(type=3,data={'custom_id':custom})))

class RuntimeTests(unittest.TestCase):
 def test_runtime_keeps_receipts_and_blocks_moderation(self):
  from unittest.mock import patch
  with patch.dict('os.environ',{'BOT_RUNTIME_MODE':'giveaway'}):
   for name in ('giveaway','vipgiveaway','ticket'):
    self.assertFalse(blocked(None,dict(type=2,guild_id=900000000000000003,data={'name':name})))
   for name in ('minecraft','moderation','staffroom','skelly','vip'):
    self.assertTrue(blocked(None,dict(type=2,guild_id=900000000000000003,data={'name':name})))
   self.assertFalse(blocked(None,dict(type=3,guild_id=900000000000000003,data={'custom_id':'receipt:3:close'})))
   self.assertTrue(blocked(None,dict(type=2,guild_id=900000000000000001,data={'name':'ticket'})))
