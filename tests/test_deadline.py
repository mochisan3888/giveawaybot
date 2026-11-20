import os
import tempfile
import unittest

class DeadlineTests(unittest.TestCase):
    def test_finished_deadline_does_not_keep_counting(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ.setdefault('DATABASE_PATH', d + '/db')
            from giveaway.bot import embed_for
            game=dict(id=1, prize='test', status='done', count=1, end=100, role=None, days=0, rules={}, rounds=[[1]])
            field=next(f for f in embed_for(game).fields if f.name == '締切')
            self.assertNotIn(':R>',field.value)
            self.assertIn('終了',field.value)

    def test_host_mention_and_legacy_fallback(self):
        from giveaway.bot import embed_for
        game=dict(id=1, prize='test', status='done', count=1, end=100, role=None, days=0, rules={}, rounds=[[1]], host=42)
        self.assertEqual(next(f.value for f in embed_for(game).fields if f.name == 'ホスト'), '<@42>')
        del game['host']
        self.assertIn('記録なし', next(f.value for f in embed_for(game).fields if f.name == 'ホスト'))
