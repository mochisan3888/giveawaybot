import datetime
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
import discord
from giveaway.clear_messages import delete_selected,register_clear

class ClearTests(unittest.IsolatedAsyncioTestCase):
 def message(self,days):return N(created_at=discord.utils.utcnow()-datetime.timedelta(days=days),delete=AsyncMock())
 async def test_recent_bulk_old_single(self):
  channel=N(delete_messages=AsyncMock());messages=[self.message(0),self.message(1),self.message(20)]
  self.assertEqual(await delete_selected(channel,messages,'reason'),(3,None))
  channel.delete_messages.assert_awaited_once_with(messages[:2],reason='reason')
  messages[2].delete.assert_awaited_once()
 async def test_single_and_empty(self):
  channel=N(delete_messages=AsyncMock());msg=self.message(0)
  self.assertEqual(await delete_selected(channel,[msg],'reason'),(1,None));msg.delete.assert_awaited_once()
  self.assertEqual(await delete_selected(channel,[],'reason'),(0,None));channel.delete_messages.assert_not_awaited()
 async def test_failed_batch_never_counted(self):
  channel=N(delete_messages=AsyncMock(side_effect=TimeoutError()))
  messages=[self.message(0),self.message(1),self.message(20)]
  self.assertEqual(await delete_selected(channel,messages,'reason'),(0,'TimeoutError'))
  messages[2].delete.assert_not_awaited()
 async def test_partial_old_deletion(self):
  messages=[self.message(20),self.message(30)];messages[1].delete.side_effect=TimeoutError()
  self.assertEqual(await delete_selected(N(),messages,'reason'),(1,'TimeoutError'))
 def test_command_bounds(self):
  commands=[]
  def command(**kwargs):
   def wrap(fn):
    cmd=discord.app_commands.Command(callback=fn,**kwargs);commands.append(cmd);return cmd
   return wrap
  register_clear(N(tree=N(command=command)))
  self.assertTrue(commands[0].default_permissions.manage_messages)
  p=commands[0].parameters[0]
  self.assertEqual((p.min_value,p.max_value),(1,1000))

class LargeClearTests(unittest.IsolatedAsyncioTestCase):
 async def test_thousand_messages_split_into_ten_batches(self):
  channel=N(delete_messages=AsyncMock())
  messages=[N(created_at=discord.utils.utcnow(),delete=AsyncMock()) for _ in range(1000)]
  self.assertEqual(await delete_selected(channel,messages,'reason'),(1000,None))
  self.assertEqual(channel.delete_messages.await_count,10)
  self.assertEqual([m for call in channel.delete_messages.call_args_list for m in call.args[0]],messages)
  self.assertTrue(all(len(call.args[0])==100 for call in channel.delete_messages.call_args_list))

class DiscordSignatureTests(unittest.IsolatedAsyncioTestCase):
 async def test_real_partial_message_delete_signature(self):
  http=N(delete_message=AsyncMock())
  channel=N(id=123,type=discord.ChannelType.text,_state=N(http=http))
  message=discord.PartialMessage(channel=channel,id=100000000000000000)
  self.assertEqual(await delete_selected(channel,[message],'audit reason'),(1,None))
  http.delete_message.assert_awaited_once()
