import unittest
from types import SimpleNamespace
from giveaway.store import Store
from giveaway.notifications import notification_payload
from giveaway.redemptions import ReceiptError

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:')
        self.role=SimpleNamespace(id=42,mention='<@&42>',mentionable=False,is_default=lambda:False)
        self.g=SimpleNamespace(id=10,me=object(),get_role=lambda rid:self.role if rid==42 else None)
        self.p=SimpleNamespace(mention_everyone=True)
        self.c=SimpleNamespace(permissions_for=lambda me:self.p)
    def tearDown(self):self.s.db.close()
    def test_explicit_targets_only(self):
        self.s.set_notification_role(10,42)
        content,m,r=notification_payload(self.s,self.g,self.c,'role')
        self.assertEqual(content,'<@&42>');self.assertEqual(m.to_dict()['roles'],[42]);self.assertEqual(m.to_dict()['parse'],[])
        content,m,r=notification_payload(self.s,self.g,self.c,'everyone')
        self.assertEqual(content,'@everyone');self.assertEqual(m.to_dict()['parse'],['everyone'])
        content,m,r=notification_payload(self.s,self.g,self.c,'none')
        self.assertIsNone(content);self.assertEqual(m.to_dict()['parse'],[])
    def test_unset_deleted_and_permissions(self):
        with self.assertRaises(ReceiptError):notification_payload(self.s,self.g,self.c,'role')
        self.s.set_notification_role(10,99)
        with self.assertRaises(ReceiptError):notification_payload(self.s,self.g,self.c,'role')
        self.s.set_notification_role(10,42);self.p.mention_everyone=False
        with self.assertRaises(ReceiptError):notification_payload(self.s,self.g,self.c,'role')
        with self.assertRaises(ReceiptError):notification_payload(self.s,self.g,self.c,'everyone')
        self.role.mentionable=True
        self.assertEqual(notification_payload(self.s,self.g,self.c,'role')[2],42)
    def test_guild_scope_and_clear(self):
        self.s.set_notification_role(10,42)
        self.assertIsNone(self.s.notification_role(11))
        self.s.set_notification_role(10,None)
        self.assertIsNone(self.s.notification_role(10))
