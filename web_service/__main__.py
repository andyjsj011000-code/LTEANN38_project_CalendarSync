"""Visible local preview and explicit school test-server launcher."""
import argparse
import json
import os
from pathlib import Path
import webbrowser
import threading
import socket
import time


def load_env(path):
    # No shell expansion or command execution. Only explicit WEB_* assignments.
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'): continue
        name,separator,value=line.partition('=')
        if not separator or not name.startswith('WEB_') or not name.replace('_','').isalnum(): raise ValueError('Invalid environment file')
        os.environ[name]=value.strip().strip('"').strip("'")


def open_when_ready(url,port):
    def wait_and_open():
        for _ in range(50):
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=0.2):
                    webbrowser.open(url); return
            except OSError: time.sleep(0.2)
    threading.Thread(target=wait_and_open,daemon=True).start()


def main():
    parser=argparse.ArgumentParser(description='CalendarSync test server')
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--demo',action='store_true',help='Loopback-only synthetic visual preview')
    group.add_argument('--school',action='store_true',help='School account test server')
    group.add_argument('--generate-keys',type=Path,metavar='NEW_FILE',help='Generate a new local environment key file; never overwrite')
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--port',type=int)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    if args.generate_keys:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from .push import b64
        key=ec.generate_private_key(ec.SECP256R1())
        private=b64(key.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        with args.generate_keys.open('x',encoding='utf-8') as file:
            file.write('WEB_DATA_KEY='+Fernet.generate_key().decode()+'\nWEB_VAPID_PRIVATE_KEY='+private+'\n')
        args.generate_keys.chmod(0o600)
        print('Created local keys. Keep this file private and reuse its values.'); return
    if args.env_file: load_env(args.env_file)
    import uvicorn
    if args.demo:
        port=args.port or 8093; os.environ['CALENDARSYNC_PREVIEW_PORT']=str(port)
        if not args.no_browser: open_when_ready(f'http://127.0.0.1:{port}',port)
        print(f'Synthetic preview only: http://127.0.0.1:{port}')
        uvicorn.run('web_service.preview:create_preview',factory=True,host='127.0.0.1',port=port,access_log=False)
    else:
        from .config import Settings
        config=Settings.from_env(); port=args.port or 8080
        if not config.auth_configured: parser.error('School Entra settings are required')
        if not args.no_browser: open_when_ready(config.origin,port)
        uvicorn.run('web_service.server:create_test_server',factory=True,host='127.0.0.1',port=port,workers=1,access_log=False,proxy_headers=True,forwarded_allow_ips='127.0.0.1')

if __name__=='__main__': main()
