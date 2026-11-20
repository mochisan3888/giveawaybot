"""Entry point invoked only by the existing forced SSH command."""
import json
import os
import sys
from pathlib import Path
from .community_template import rpc

def main():
    try:
        raw=sys.stdin.buffer.read(1048577)
        if len(raw)>1048576:raise ValueError('Request too large')
        result=rpc(Path(os.environ['DATABASE_PATH']).parent,json.loads(raw))
    except Exception as error:
        result=dict(ok=False,error=str(error)[:500] if isinstance(error,ValueError) else type(error).__name__)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
