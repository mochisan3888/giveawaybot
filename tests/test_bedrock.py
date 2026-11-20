import sqlite3,unittest,time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from giveaway.redemptions import Redemptions,ReceiptError
from giveaway.bedrock_links import BedrockLinks
from giveaway.fulfillment import Fulfillment,EditionChoice,minecraft_nickname,register_commands
import discord

class BedrockTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.r=Redemptions(self.db);self.s=BedrockLinks(self.r)
    def tearDown(self):self.db.close()
    def test_one_use_expiry_and_server_scope(self):
        code=self.s.issue('123456','Player One',now=100)
        linked=self.s.consume(code,10,20,'元の名前',now=200)
        self.assertEqual(linked['uuid'],'bedrock:123456');self.assertEqual(linked['name'],'Player One')
        self.assertIsNone(self.r.link(20,11))
        with self.assertRaises(ReceiptError):self.s.consume(code,11,20,'x',now=201)
        code=self.s.issue('123456','Player One',now=1000)
        with self.assertRaises(ReceiptError):self.s.consume(code,11,20,'x',now=1600)
    def test_duplicates_manual_audit_and_no_overwrite(self):
        self.s.register_manual(10,20,'Player One','old',actor=99)
        with self.assertRaises(ReceiptError):self.s.register_manual(10,21,'player one','x',actor=99)
        code=self.s.issue('123456','Player One')
        with self.assertRaises(ReceiptError):self.s.consume(code,10,22,'x')
        with self.assertRaises(ReceiptError):self.s.register_manual(10,20,'Other','x',actor=99)
        self.assertEqual(self.db.execute('SELECT actor,method FROM minecraft_link_audit').fetchone(),(99,'admin_manual'))
        self.s.register_manual(11,21,'player one','x',actor=98)
        self.assertEqual(self.r.link(20,10)['name'],'Player One')
    def test_receipt_authorization_and_transaction(self):
        now=time.time()
        game=dict(id=1,guild=10,channel=11,message=12,prize='prize',receipt_policy=dict(roles=[30],category=20,minutes=60,minecraft=True),receipt_events=[dict(round=1,winners=[20],at=now,deadline=now+3600)])
        self.r.seed(game)
        code=self.s.issue('123','BedrockUser')
        with self.assertRaises(ReceiptError):self.s.consume(code,10,21,'x',receipt=1)
        self.assertIsNone(self.r.link(21,10))
        self.s.consume(code,10,20,'x',receipt=1)
        self.assertEqual(self.db.execute('SELECT receipt FROM receipt_requests').fetchone()[0],1)
    def test_bad_identity_and_nickname_length(self):
        for x in ['0','-1','abc','18446744073709551616']:
            with self.assertRaises(ReceiptError):self.s.issue(x,'Name')
        for n in ['', '\n','@everyone','x'*33]:
            with self.assertRaises(ReceiptError):self.s.issue('123',n)
        self.assertLessEqual(len(minecraft_nickname('😀'*32,'original').encode('utf-16-le'))//2,32)

class BedrockUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_choice_is_bound_to_initiator(self):
        service=SimpleNamespace(begin_bedrock=AsyncMock())
        view=EditionChoice(service,20,10)
        with self.assertRaises(ReceiptError):await view.children[1].callback(SimpleNamespace(user=SimpleNamespace(id=21),guild_id=10))
        service.begin_bedrock.assert_not_awaited()
    async def test_admin_register_requires_administrator(self):
        db=sqlite3.connect(':memory:')
        bot=SimpleNamespace(store=SimpleNamespace(db=db),tree=discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none())))
        bot.fulfillment=Fulfillment(bot)
        group=discord.app_commands.Group(name='giveaway',description='test')
        register_commands(bot,group)
        command=bot.tree.get_command('minecraft').get_command('admin_register')
        with self.assertRaises(discord.app_commands.MissingPermissions):
            for check in command.checks:check(SimpleNamespace(permissions=discord.Permissions(manage_guild=True)))
        db.close()

class AdminLinkTests(unittest.TestCase):
    setUp=BedrockTests.setUp
    tearDown=BedrockTests.tearDown
    def test_replace_failure_is_atomic_and_scope_preserved(self):
        self.s.admin_link(10,20,'java','Steve','original',99,'a'*32)
        self.s.admin_link(10,21,'java','Alex','other',99,'b'*32)
        self.s.admin_link(11,20,'java','Steve','original2',99,'a'*32)
        with self.assertRaises(ReceiptError):self.s.admin_link(10,20,'java','Alex','wrong',99,'b'*32,True)
        self.assertEqual(self.r.link(20,10)['uuid'],'a'*32)
        self.s.admin_link(10,20,'bedrock','Bedrock Name','wrong',99,replace=True)
        self.assertEqual(self.r.link(20,10)['original_name'],'original')
        self.s.admin_unlink(10,20,99)
        self.assertIsNone(self.r.link(20,10));self.assertIsNotNone(self.r.link(20,11))
        self.assertEqual(self.db.execute('SELECT method FROM minecraft_link_audit ORDER BY id DESC LIMIT 1').fetchone()[0],'admin_unlink')
    def test_unlink_revokes_old_java_challenge(self):
        token,_=self.r.challenge(20,'a'*32,'Steve',guild=10,original_name='x')
        self.s.admin_unlink(10,20,99)
        with self.assertRaises(ReceiptError):self.r.verify(token,'a'*32,'Steve')
