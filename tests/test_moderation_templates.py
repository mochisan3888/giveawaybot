import sqlite3
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import discord
from discord import app_commands
from giveaway.moderation_store import ModerationStore
from giveaway.moderation import register_moderation
from giveaway.redemptions import ReceiptError

class TemplatesTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.addCleanup(self.conn.close);self.db=ModerationStore(self.conn)
    def test_crud_is_guild_scoped_and_snapshots_survive_changes(self):
        original=self.db.save_template(1,'連投','timeout','連投をやめてください',10)
        self.db.save_template(2,'連投','warn','別サーバー')
        self.db.save_template(1,'連投','timeout','更新後',20,editing=True)
        self.assertEqual(original['minutes'],10)
        self.assertEqual(self.db.template(2,'連投')['action'],'warn')
        self.db.delete_template(1,'連投')
        with self.assertRaises(ReceiptError):self.db.template(1,'連投')
        self.assertEqual(len(ModerationStore(self.conn).templates(2)),1)
    def test_validation_prevents_silent_overwrite_and_invalid_duration(self):
        self.db.save_template(1,'注意','caution','理由')
        with self.assertRaises(ReceiptError):self.db.save_template(1,'注意','ban','理由')
        with self.assertRaises(ReceiptError):self.db.save_template(2,'注意','ban','理由',editing=True)
        for action,minutes in [('timeout',0),('timeout',40321),('warn',10),('invalid',0)]:
            with self.assertRaises(ReceiptError):self.db.save_template(1,'test',action,'reason',minutes)
        with self.assertRaises(ReceiptError):self.db.save_template(1,'  ','warn','reason')

class TemplateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_use_runs_existing_confirmation_and_does_not_modify_template(self):
        conn=sqlite3.connect(':memory:');self.addCleanup(conn.close)
        db=ModerationStore(conn);db.save_template(1,'連投','timeout','元の理由',10)
        client=discord.Client(intents=discord.Intents.none())
        service=NS(db=db,stage=AsyncMock())
        bot=NS(moderation=service,tree=app_commands.CommandTree(client))
        register_moderation(bot)
        group=bot.tree.get_command('moderation');cmd=group.get_command('template_use')
        i=NS(guild_id=1);user=NS(id=8)
        await cmd.callback(i,'連投',user,'今回の理由',30)
        service.stage.assert_awaited_once_with(i,user,'timeout','今回の理由',30)
        self.assertEqual(db.template(1,'連投')['minutes'],10)
        with self.assertRaises(ReceiptError):await cmd.callback(NS(guild_id=2),'連投',user)
        self.assertEqual(service.stage.await_count,1)
        for name in ('template_add','template_edit','template_delete'):
            command=group.get_command(name)
            with self.assertRaises(app_commands.MissingPermissions):
                await command.checks[0](NS(permissions=discord.Permissions.none()))
        self.assertLessEqual(len(group.to_dict(bot.tree)['options']),25)
