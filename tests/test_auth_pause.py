import unittest,os,sys
from unittest.mock import patch,AsyncMock,Mock
from types import SimpleNamespace as NS
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'runtime'))
from giveaway import access_policy as p
from giveaway.fulfillment import Fulfillment,ReceiptError
class AuthPauseTests(unittest.IsolatedAsyncioTestCase):
 async def test_callbacks_block_without_mutation(self):
  with patch.dict(os.environ,{'MC_AUTO_AUTH_PAUSED':'1','BOT_RUNTIME_MODE':'full'}):
   for method in (Fulfillment.verified,Fulfillment.bedrock_issue):
    response=await method(None,None);self.assertEqual(response.status,503)
 async def test_new_bedrock_and_code_block(self):
  service=NS(db=NS(link=Mock(return_value=None)))
  i=NS(guild_id=1,user=NS(id=2))
  with patch.dict(os.environ,{'MC_AUTO_AUTH_PAUSED':'1','BOT_RUNTIME_MODE':'full'}):
   with self.assertRaises(ReceiptError):await Fulfillment.begin_bedrock(service,i,1)
   with self.assertRaises(ReceiptError):await Fulfillment.complete_bedrock(None,None,1,'code')
 async def test_existing_link_still_visible(self):
  service=NS(db=NS(link=Mock(return_value={'existing':True})))
  i=NS(guild_id=1,user=NS(id=2),response=NS(send_message=AsyncMock()))
  with patch.dict(os.environ,{'MC_AUTO_AUTH_PAUSED':'1','BOT_RUNTIME_MODE':'full'}),patch('giveaway.fulfillment.link_status',return_value='existing'):
   await Fulfillment.begin_link(service,i)
   i.response.send_message.assert_awaited_once()
 async def test_new_java_block(self):
  service=NS(db=NS(link=Mock(return_value=None)));i=NS(guild_id=1,user=NS(id=2))
  with patch.dict(os.environ,{'MC_AUTO_AUTH_PAUSED':'1','BOT_RUNTIME_MODE':'full'}):
   with self.assertRaises(ReceiptError):await Fulfillment.begin_link(service,i)
 def test_vps_default_and_permanent_guild_restriction(self):
  with patch.dict(os.environ,{'BOT_RUNTIME_MODE':'full'},clear=True):
   self.assertFalse(p.auto_auth_paused());self.assertTrue(p.restricted(900000000000000001))
   self.assertTrue(p.blocked(None,{'guild_id':'900000000000000001','type':2,'data':{'name':'minecraft'}}))
