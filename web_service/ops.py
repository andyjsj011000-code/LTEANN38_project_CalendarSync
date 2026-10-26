"""Local operator commands. No public administrator endpoint is exposed."""
import argparse,json
from pathlib import Path
from .__main__ import load_env
from .config import Settings
from .test_storage import TestDatabase

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('action',choices=['feedback','revoke-user'])
    parser.add_argument('--user-id')
    args=parser.parse_args(); load_env(args.env_file); settings=Settings.from_env()
    if not settings.database_path.is_file(): parser.error('Existing test database is required')
    db=TestDatabase(settings.database_path,settings.data_key)
    if args.action=='feedback':
        with db.connect() as c: rows=c.execute('SELECT id,created_at,payload_blob FROM web_feedback ORDER BY created_at DESC LIMIT 100').fetchall()
        for row in rows: print(json.dumps({'id':row[0],'created_at':row[1],**db.unseal(row[2])},ensure_ascii=False))
    else:
        if not args.user_id or args.user_id.count(':')!=1: parser.error('Use the exact tenant:object-id principal')
        db.revoke_all(args.user_id); print('Revoked the selected user connections and subscriptions.')
if __name__=='__main__': main()
