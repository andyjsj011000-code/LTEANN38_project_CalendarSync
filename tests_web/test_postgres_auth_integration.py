"""Opt-in; the fixture only accepts an explicitly disposable PostgreSQL DB."""
import json
from concurrent.futures import ThreadPoolExecutor

from cryptography.fernet import Fernet

from test_postgres_personal_tasks_integration import postgres_tasks
from web_service.postgres_auth_store import PostgresAuthStore
from web_service.storage import Missing


def test_oauth_flow_has_one_winner_when_callbacks_race(postgres_tasks):
    _, connect = postgres_tasks
    cipher = Fernet(Fernet.generate_key())
    def seal(payload): return cipher.encrypt(json.dumps(payload).encode()).decode()
    def unseal(blob): return json.loads(cipher.decrypt(blob.encode()))
    store = PostgresAuthStore(connect, seal, unseal, lambda: 1000)
    raw = store.store_flow({'nonce': 'test-nonce'})
    def consume(_):
        try:
            return store.consume_flow(raw)
        except Missing:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))
    assert sum(result is not None for result in results) == 1
    assert next(result for result in results if result is not None)['nonce'] == 'test-nonce'
