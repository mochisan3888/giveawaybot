import json
import tempfile
import unittest
from pathlib import Path
from giveaway.community_template import rpc, mapped, manifest, SOURCE

class Templates(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.channel='900000000000000005';self.role='900000000000000008'
        self.source=dict(settings={'moderation_config':[{'guild':int(SOURCE),'data':json.dumps({'log':int(self.channel),'roles':[int(self.role)]})}]},panels=[],channels=[{'id':self.channel}],roles=[{'id':self.role}],warnings=[])
        (self.root/'community-template-source.json').write_text(json.dumps(self.source))
        self.payload=dict(action='apply',deployment_id='test',target_guild_id='900000000000000012',actor_id='822601651613466686',role_map={self.role:'900000000000000014'},channel_map={self.channel:'900000000000000013'})
    def tearDown(self):self.tmp.cleanup()
    def test_manifest_and_no_history(self):
        out=rpc(self.root,dict(action='export'))
        self.assertEqual(out['required_role_ids'],[self.role])
        self.assertNotIn('settings',out)
    def test_mapping(self):
        self.assertEqual(mapped({self.role:[int(self.channel)]},{self.role:'1',self.channel:'2'}),{'1':[2]})
        with self.assertRaises(ValueError):mapped(int(self.role),{})
    def test_idempotent_queue(self):
        a=rpc(self.root,self.payload);b=rpc(self.root,self.payload)
        self.assertEqual(a,b);self.assertEqual(a['status'],'queued')
        out=rpc(self.root,dict(action='status',deployment_id='test'))
        self.assertEqual(out,a)
    def test_source_protected_and_missing_map(self):
        with self.assertRaises(ValueError):rpc(self.root,{**self.payload,'target_guild_id':SOURCE})
        with self.assertRaises(ValueError):rpc(self.root,{**self.payload,'role_map':{}})
    def test_conflicting_replay_rejected(self):
        rpc(self.root,self.payload)
        with self.assertRaises(ValueError):rpc(self.root,{**self.payload,'actor_id':'123'})
    def test_deleted_reference_rejected(self):
        self.source['roles']=[]
        with self.assertRaises(ValueError):manifest(self.source)
