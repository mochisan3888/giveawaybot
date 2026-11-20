"""Forced-SSH registry exchange and atomic cross-host usage limits (no message bodies)."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from .rotate_ai_key import atomic


def reserve(path,p,now=None):
    now=time.time() if now is None else now
    if not all(str(p.get(k,'')).isdigit() for k in ('request_id','guild','actor')):raise ValueError('Invalid IDs')
    day=datetime.fromtimestamp(now,ZoneInfo('Asia/Tokyo')).date().isoformat()
    with closing(sqlite3.connect(path,timeout=15)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS usage(request TEXT PRIMARY KEY,guild TEXT,actor TEXT,at REAL,day TEXT)')
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT guild,actor FROM usage WHERE request=?',(p['request_id'],)).fetchone()
        if row:return dict(ok=tuple(row)==(p['guild'],p['actor']))
        if db.execute('SELECT COUNT(*) FROM usage WHERE day=?',(day,)).fetchone()[0]>=100:
            return dict(ok=False,error='両BOT共通の1日100回制限に達しました。')
        last=db.execute('SELECT MAX(at) FROM usage WHERE actor=?',(p['actor'],)).fetchone()[0]
        if last and now-last<60:return dict(ok=False,error='AIの再利用は60秒待ってください。')
        db.execute('INSERT INTO usage VALUES(?,?,?,?,?)',(p['request_id'],p['guild'],p['actor'],now,day))
    return dict(ok=True)


def validate(s):
    if s.get('version')!=1 or not -30<=time.time()-s.get('updated_at',0)<=180:raise ValueError('Stale snapshot')
    if not isinstance(s.get('tickets'),list) or len(s['tickets'])>10000:raise ValueError('Invalid tickets')
    result=dict(version=1,updated_at=s['updated_at'],tickets=[],settings={})
    for r in s['tickets']:
        if not all(isinstance(r.get(k),str) and r[k].isdigit() for k in ('guild','channel')):raise ValueError('Invalid IDs')
        if not isinstance(r.get('roles'),list) or len(r['roles'])>100 or not all(isinstance(x,str) and x.isdigit() and x!=r['guild'] for x in r['roles']):raise ValueError('Invalid roles')
        result['tickets'].append(dict(guild=r['guild'],channel=r['channel'],roles=r['roles'],kind=str(r.get('kind',''))[:80],status='closed' if r.get('status')=='closed' else 'open'))
    for g,c in s.get('settings',{}).items():
        if not g.isdigit() or type(c.get('enabled')) is not bool or not isinstance(c.get('rules',''),str) or len(c.get('rules',''))>3000:raise ValueError('Invalid settings')
        result['settings'][g]=dict(enabled=c['enabled'],rules=c.get('rules',''))
    return result


def exchange(root,p):
    root=Path(root)
    if p.get('origin')!='tormo':raise ValueError('Unsupported origin')
    tormo=validate(p['snapshot'])
    giveaway={}
    file=root/'ticket-bridge-giveaway.json'
    if file.exists():giveaway=json.loads(file.read_text())
    if p.get('giveaway_snapshot'):
        new=validate(p['giveaway_snapshot'])
        if new['updated_at']>giveaway.get('updated_at',0):giveaway=new;atomic(file,giveaway,10001)
    merged=dict(version=1,tormo=tormo,giveaway=giveaway)
    atomic(root/'ticket-bridge.json',merged,10001)
    return merged
