import json
import sqlite3
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
from giveaway.community_setup import CommunitySetup, blueprint, channel_payload, black_png, register_community
from giveaway.community_template import SOURCE

class Planning(unittest.TestCase):
 def test_exclusions(self):
  source={'excluded_channel_ids':['2'],'roles':[{'id':SOURCE,'name':'everyone'},{'id':'1','name':'Member'},{'id':'2','name':'bot','managed':True},{'id':'3','name':'警告1回'}], 'channels':[{'id':'1','type':0},{'id':'2','type':4},{'id':'3','type':0,'parent_id':'2'},{'id':'4','type':20}]}
  roles,chs=blueprint(source)
  self.assertEqual([r['id'] for r in roles],['1']);self.assertEqual([c['id'] for c in chs],['1'])
 def test_private_permissions_remain_private(self):
  c=dict(id='x',name='staff',type=0,parent_id='cat',permission_overwrites=[dict(id=SOURCE,type=0,allow='0',deny='1024'),dict(id='role',type=0,allow='1024',deny='0'),dict(id='user',type=1,allow='1024',deny='0')])
  p=channel_payload(c,{SOURCE:'target','role':'newrole'},{'cat':'newcat'},123)
  self.assertEqual(p['parent_id'],'newcat')
  self.assertEqual(p['permission_overwrites'][0]['deny'],'1024')
  self.assertEqual(p['permission_overwrites'][1]['id'],'newrole')
  self.assertNotIn('user',[o['id'] for o in p['permission_overwrites']])
 def test_black_png(self):
  import zlib,struct
  png=black_png();self.assertTrue(png.startswith(b'\x89PNG'))
  n=8;pixels=None
  while n<len(png):
   size=struct.unpack('!I',png[n:n+4])[0]
   if png[n+4:n+8]==b'IDAT':pixels=zlib.decompress(png[n+8:n+8+size])
   n+=size+12
  self.assertEqual(set(pixels),{0})
 def test_command_schema(self):
  groups=[];bot=N(community_setup=None,tree=N(add_command=groups.append))
  register_community(bot)
  self.assertEqual([c.name for c in groups[0].commands],['setup','status','resume','recover'])

class Resources(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.s=CommunitySetup(N(store=N(db=sqlite3.connect(':memory:'))))
  self.data={'pending':None,'roles':{},'state':'running'}
 async def test_checkpoint(self):
  call=AsyncMock(return_value={'id':'new'})
  await self.s.resource(1,self.data,'old','roles',call)
  self.assertEqual(self.s.get(1)['roles'],{'old':'new'});self.assertIsNone(self.s.get(1)['pending'])
 async def test_unknown_outcome_is_not_retried(self):
  call=AsyncMock(side_effect=TimeoutError())
  with self.assertRaises(TimeoutError):await self.s.resource(1,self.data,'old','roles',call)
  self.assertIsNotNone(self.s.get(1)['pending'])
  with self.assertRaises(ValueError):await self.s.resource(1,self.data,'old','roles',call)
  self.assertEqual(call.await_count,1)
