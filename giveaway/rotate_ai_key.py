"""Run under a separate OS account/scheduler. Admin credential never enters BOT env.

Run hourly; swaps at most once per 24 hours. Failed operations are retried from
persisted pending state. Exit code 1 must be alerted by the deployment scheduler.
"""
import json
import os
import sys
import time
import tempfile
import urllib.request
from pathlib import Path
from contextlib import contextmanager


def atomic(path, data, group=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('symlink target refused')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        if group is not None:
            os.fchown(fd, -1, int(group))
            os.fchmod(fd, 0o640)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):os.unlink(name)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        if os.name == 'nt':
            import msvcrt
            f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def request(method, path, key, data=None):
    req = urllib.request.Request('https://api.openai.com/v1/'+path,method=method,
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},
        data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def rotate(config, call=request, now=None):
    now = time.time() if now is None else now
    runtime = Path(config['runtime_key_file'])
    state_path = Path(config['state_file'])
    with locked(state_path.with_suffix('.lock')):
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        base = 'organization/projects/'+config['project_id']
        admin = Path(config['admin_key_file']).read_text().strip()
        # Only IDs created and recorded by this rotator are ever revoked.
        for old in list(state.get('retire', [])):
            try:
                call('DELETE',base+'/api_keys/'+old,admin)
            except Exception as exc:
                if getattr(exc,'code',None) != 404:raise
            state['retire'].remove(old)
            atomic(state_path,state)
        if not state.get('pending') and now-state.get('switched',0) < 86400:
            return 'not_due'
        pending = state.get('pending')
        if pending and pending.get('expires_at') and pending['expires_at'] <= now:
            try:
                call('DELETE',base+'/api_keys/'+pending['id'],admin)
            except Exception as exc:
                if getattr(exc,'code',None) != 404:raise
            del state['pending']
            atomic(state_path,state)
        if not state.get('pending'):
            # 48h expiry provides a retry window around daily rotation.
            # Creation is not blindly retried in this invocation if response is lost.
            pending = call('POST',base+'/service_accounts/'+config['service_account_id']+'/api_keys',admin,
                {'name':'GiveAwayBot daily '+str(int(now)), 'expires_in_seconds':172800,
                 'scopes':['api.responses.write']})
            if not pending.get('value') or not pending.get('id'):
                raise ValueError('invalid key response')
            state['pending'] = pending
            state['previous'] = (json.loads(runtime.read_text()).get('id') if runtime.exists() else None)
            atomic(state_path,state)
        pending = state['pending']
        # Exercise the actual permission/model before touching the active key.
        call('POST','responses',pending['value'],{'model':config.get('model','gpt-4.1-mini'),
             'input':'Reply OK.', 'max_output_tokens':16, 'store':False})
        # Atomic key reload by BOT on its next request; existing calls can finish.
        atomic(runtime,pending,config.get('runtime_group'))
        if state.get('previous') and state['previous'] != pending['id']:
            state.setdefault('retire',[]).append(state['previous'])
        state.pop('previous',None)
        state.update(switched=now)
        del state['pending']
        atomic(state_path,state)
        # Old key is retired on next hourly run, allowing in-flight requests to finish.
        return 'rotated'


if __name__ == '__main__':
    try:
        print(rotate(json.loads(Path(sys.argv[1]).read_text())))
    except Exception as exc:
        # No exception messages/HTTP bodies: they may contain credential material.
        print('API key rotation failed: '+type(exc).__name__,file=sys.stderr)
        sys.exit(1)
