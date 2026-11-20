"""Guild-scoped moderation cases, immutable events, and durable log delivery."""
import json
import string
import time
from .redemptions import ReceiptError

LABELS = {'caution':'注意','warn':'警告','timeout':'タイムアウト','kick':'Kick','ban':'Ban','untimeout':'タイムアウト解除','unban':'Ban解除'}
DEFAULTS = {
    'caution':'{server}からの注意のお知らせです。\n内容：{reason}\nこれは警告ではなく、警告数や公開のWarnStatsには掲載されません。',
    'warn':'{server}で警告を受けました。\n理由：{reason}',
    'timeout':'{server}でタイムアウトの処分通知です。\n理由：{reason}\n解除予定：{until}',
    'kick':'{server}からのKick処分通知です。\n理由：{reason}',
    'ban':'{server}からのBan処分通知です。\n理由：{reason}',
    'untimeout':'{server}でタイムアウトが解除されました。\n理由：{reason}',
    'unban':'{server}でBanが解除されました。\n理由：{reason}',
    'panel':'警告・処分の履歴を確認できます。ボタンは担当スタッフ専用で、一覧は操作した本人だけに表示されます。',
}
FIELDS = {'server','user','reason','until','case','warnings','action'}

def validate_template(text):
    if not text.strip() or len(text)>1400:raise ReceiptError('文面は1～1400文字で入力してください。')
    try:
        for _, field, spec, conversion in string.Formatter().parse(text):
            if field is not None and (field not in FIELDS or spec or conversion):
                raise ValueError()
    except ValueError:raise ReceiptError('使用できる変数：'+', '.join('{'+f+'}' for f in sorted(FIELDS)))
    return text

def render_template(text, values):
    validate_template(text)
    return text.format_map(values)[:3500]

class ModerationStore:
    def __init__(self, db):
        self.db=db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS moderation_templates(guild INTEGER NOT NULL,name TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(guild,name));
        CREATE TABLE IF NOT EXISTS moderation_config(guild INTEGER PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS moderation_cases(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,user INTEGER NOT NULL,request TEXT UNIQUE NOT NULL,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS moderation_cases_guild ON moderation_cases(guild,user,id);
        CREATE TABLE IF NOT EXISTS moderation_events(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,case_id INTEGER NOT NULL,channel INTEGER,data TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',sent_at REAL NOT NULL DEFAULT 0);
        ''')
    def config(self,guild):
        r=self.db.execute('SELECT data FROM moderation_config WHERE guild=?',(guild,)).fetchone()
        return json.loads(r[0]) if r else {}
    def configure(self,guild,**fields):
        cfg=self.config(guild);cfg.update(fields)
        with self.db:self.db.execute('INSERT OR REPLACE INTO moderation_config VALUES(?,?)',(guild,json.dumps(cfg)))
        return cfg
    def get(self,cid,guild=None):
        r=self.db.execute('SELECT data FROM moderation_cases WHERE id=?',(cid,)).fetchone()
        c=json.loads(r[0]) if r else None
        return c if c and (guild is None or c['guild']==guild) else None
    def create(self,guild,user,actor,action,reason,request,until=None):
        with self.db:
            old=self.db.execute('SELECT data FROM moderation_cases WHERE request=?',(str(request),)).fetchone()
            if old:return json.loads(old[0]),False
            c=dict(guild=guild,user=user,actor=actor,action=action,reason=reason,until=until,created=time.time(),state='pending',dm='pending',appeal='none',channel=None,revoked=False)
            cur=self.db.execute('INSERT INTO moderation_cases(guild,user,request,data) VALUES(?,?,?,?)',(guild,user,str(request),'{}'))
            c['id']=cur.lastrowid;self.save(c)
            return c,True
    def save(self,c):
        with self.db:self.db.execute('UPDATE moderation_cases SET data=? WHERE id=?',(json.dumps(c),c['id']))
    def cases(self,guild,user=None):
        sql='SELECT data FROM moderation_cases WHERE guild=?';args=[guild]
        if user is not None:sql+=' AND user=?';args.append(user)
        return [json.loads(r[0]) for r in self.db.execute(sql+' ORDER BY id DESC',args)]
    def warnings(self,guild,user):
        return sum(c['action']=='warn' and c['state']=='applied' and not c['revoked'] for c in self.cases(guild,user))
    def event(self,c,kind,actor,text=''):
        cfg=self.config(c['guild']);channel=cfg.get('log')
        data=dict(kind=kind,actor=actor,user=c['user'],action=c['action'],at=time.time(),text=text[:1500])
        with self.db:self.db.execute('INSERT INTO moderation_events(guild,case_id,channel,data,state) VALUES(?,?,?,?,?)',(c['guild'],c['id'],channel,json.dumps(data),'pending' if channel else 'no_destination'))
    def revoke(self,guild,cid,actor,reason):
        c=self.get(cid,guild)
        if not c or c['action']!='warn' or c['state']!='applied' or c['revoked']:raise ReceiptError('有効な警告のケース番号を指定してください。')
        c.update(revoked=True,revoked_by=actor,revoked_reason=reason)
        self.save(c);self.event(c,'警告取り消し',actor,reason)
        return c

    def templates(self,guild):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM moderation_templates WHERE guild=? ORDER BY name',(guild,))]

    def template(self,guild,name):
        row=self.db.execute('SELECT data FROM moderation_templates WHERE guild=? AND name=?',(guild,name.strip())).fetchone()
        if not row:raise ReceiptError('このサーバーにそのテンプレートはありません。')
        return json.loads(row[0])

    def save_template(self,guild,name,action,reason,minutes=0,editing=False):
        name,reason=name.strip(),reason.strip()
        if not 1<=len(name)<=80 or not 1<=len(reason)<=1000:raise ReceiptError('名前は1～80文字、理由は1～1000文字で入力してください。')
        if action not in ('caution','warn','timeout','kick','ban'):raise ReceiptError('注意・警告・タイムアウト・Kick・Banから選択してください。')
        if action=='timeout':
            if not 1<=minutes<=40320:raise ReceiptError('タイムアウトの期間は1～40320分です。')
        elif minutes:raise ReceiptError('期間を指定できるのはタイムアウトだけです。')
        old=self.db.execute('SELECT 1 FROM moderation_templates WHERE guild=? AND name=?',(guild,name)).fetchone()
        if editing and not old:raise ReceiptError('編集対象がありません。追加コマンドを使用してください。')
        if not editing and old:raise ReceiptError('同じ名前があります。編集コマンドを使用してください。')
        if not editing and len(self.templates(guild))>=100:raise ReceiptError('テンプレートはサーバーごとに100件までです。')
        data=dict(name=name,action=action,reason=reason,minutes=minutes)
        with self.db:self.db.execute('INSERT OR REPLACE INTO moderation_templates VALUES(?,?,?)',(guild,name,json.dumps(data)))
        return data

    def delete_template(self,guild,name):
        old=self.template(guild,name)
        with self.db:self.db.execute('DELETE FROM moderation_templates WHERE guild=? AND name=?',(guild,old['name']))
        return old
