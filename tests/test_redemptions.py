import sqlite3
import tempfile
import time
import unittest
from giveaway.redemptions import Redemptions, ReceiptError

class ReceiptStateTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.r=Redemptions(self.db)
        self.policy=dict(roles=[30,40],category=20,minutes=60,minecraft=True)
        self.game=dict(id=1,guild=10,channel=11,message=12,prize='prize',receipt_policy=self.policy,
                       receipt_events=[dict(round=1,winners=[100],at=1000,deadline=4600)])
        self.r.seed(self.game)
    def tearDown(self):self.db.close()
    def test_seed_is_unique_and_snapshot_survives_setting_changes(self):
        self.r.seed(self.game)
        self.r.set_profile(10,11,dict(self.policy,minutes=5))
        self.assertEqual(len(self.r.all()),1)
        self.assertEqual(self.r.all()[0]['policy']['minutes'],60)
    def test_foreign_user_and_exact_deadline_rejected(self):
        receipt=self.r.all()[0]
        with self.assertRaises(ReceiptError):self.r.authorize(receipt,101,2000)
        self.r.authorize(receipt,100,4599)
        with self.assertRaises(ReceiptError):self.r.authorize(receipt,100,4600)
        receipt['channel']=22
        self.r.authorize(receipt,100,9000)
        with self.assertRaises(ReceiptError):self.r.authorize(receipt,101,9000)
    def test_link_requires_pinned_uuid_and_is_one_time(self):
        token,_=self.r.challenge(100,'a'*32,'Steve',receipt=1,now=2000,guild=10,original_name='元の名前')
        with self.assertRaises(ReceiptError):self.r.verify(token,'b'*32,'Other',now=2001)
        self.assertIsNone(self.r.link(100,10))
        self.r.verify(token,'a'*32,'Steve',now=2002)
        self.assertEqual(self.r.link(100,10)['uuid'],'a'*32)
        self.assertTrue(self.r.get(1)['requested'])
        with self.assertRaises(ReceiptError):self.r.verify(token,'a'*32,'Steve',now=2003)
    def test_uuid_cannot_link_two_discord_users(self):
        t1,_=self.r.challenge(100,'a'*32,'Steve',now=100,guild=10,original_name='元の名前')
        t2,_=self.r.challenge(101,'a'*32,'Steve',now=100,guild=10,original_name='元の名前')
        self.r.verify(t1,'a'*32,'Steve',now=101)
        with self.assertRaises(ReceiptError):self.r.verify(t2,'a'*32,'Steve',now=102)
    def test_challenge_expiry_cannot_bypass_receipt_deadline(self):
        token,end=self.r.challenge(100,'a'*32,'Steve',receipt=1,now=4500,guild=10,original_name='元の名前')
        self.assertEqual(end,4600)
        with self.assertRaises(ReceiptError):self.r.verify(token,'a'*32,'Steve',now=4600)
    def test_old_games_do_not_send_historical_dms(self):
        self.r.seed(dict(self.game,id=2,receipt_events=[]))
        self.assertEqual(len(self.r.all()),1)
    def test_reissue_invalidates_old_address(self):
        t1,_=self.r.challenge(100,'a'*32,'Steve',now=100,guild=10,original_name='元の名前')
        t2,_=self.r.challenge(100,'a'*32,'Steve',now=140,guild=10,original_name='元の名前')
        with self.assertRaises(ReceiptError):self.r.verify(t1,'a'*32,'Steve',now=141)
        self.r.verify(t2,'a'*32,'Steve',now=141)
    def test_restart_preserves_link_and_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(tmp+'/db');r=Redemptions(db);r.seed(self.game)
            token,_=r.challenge(100,'a'*32,'Steve',now=100,guild=10,original_name='元の名前')
            r.verify(token,'a'*32,'Steve',now=101)
            receipt=r.get(1);receipt.update(channel=88,status='working',assignee=77);r.save(receipt);db.close()
            db=sqlite3.connect(tmp+'/db');r=Redemptions(db)
            self.assertEqual(r.link(100,10)['name'],'Steve')
            self.assertEqual(r.get(1)['assignee'],77);db.close()
    def test_everyone_staff_role_rejected(self):
        with self.assertRaises(ReceiptError):self.r.set_profile(10,11,dict(self.policy,roles=[10]))
    def test_slow_dm_save_cannot_erase_verified_claim_request(self):
        stale=self.r.get(1)
        token,_=self.r.challenge(100,'a'*32,'Steve',receipt=1,now=2000,guild=10,original_name='元の名前')
        self.r.verify(token,'a'*32,'Steve',now=2001)
        stale['dm']='sent'
        self.r.save(stale)
        self.assertTrue(self.r.get(1)['requested'])
