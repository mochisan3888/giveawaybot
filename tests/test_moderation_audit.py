import datetime
import json
import sqlite3
import unittest
from types import SimpleNamespace as NS
import discord
from giveaway.moderation_store import ModerationStore
from giveaway.moderation_audit import ModerationAudit, punishment

class AuditTests(unittest.TestCase):
    def entry(self, action=discord.AuditLogAction.ban, **kwargs):
        d=dict(id=100, user_id=20, target=NS(id=30), action=action, reason='test',
               created_at=datetime.datetime.now(datetime.timezone.utc), after=NS())
        d.update(kwargs)
        return NS(**d)

    def test_actions(self):
        for native, expected in [(discord.AuditLogAction.ban,'ban'),(discord.AuditLogAction.unban,'unban'),(discord.AuditLogAction.kick,'kick')]:
            self.assertEqual(punishment(self.entry(native))['action'],expected)
        self.assertIsNone(punishment(self.entry(discord.AuditLogAction.member_update)))
        self.assertIsNone(punishment(self.entry(discord.AuditLogAction.role_update)))

    def test_timeout(self):
        date=datetime.datetime.now(datetime.timezone.utc)
        data=punishment(self.entry(discord.AuditLogAction.member_update,after=NS(timed_out_until=date)))
        self.assertEqual(data['action'],'timeout')
        self.assertIn('解除予定',data['text'])
        self.assertEqual(punishment(self.entry(discord.AuditLogAction.member_update,after=NS(timed_out_until=None)))['action'],'untimeout')

    def test_durable_dedup_and_self_exclusion(self):
        db=sqlite3.connect(':memory:')
        store=ModerationStore(db)
        service=NS(db=store,bot=NS(user=NS(id=10)))
        audit=ModerationAudit(service)
        audit.record(1,self.entry(),2)
        ModerationAudit(service).record(1,self.entry(),2)
        audit.record(1,self.entry(id=101,user_id=10),2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM moderation_events').fetchone()[0],1)
        self.assertEqual(db.execute('SELECT cursor FROM moderation_audit_cursor').fetchone()[0],101)
        self.assertEqual(store.cases(1),[])
        data=json.loads(db.execute('SELECT data FROM moderation_events').fetchone()[0])
        self.assertEqual(data['audit_id'],'100')
