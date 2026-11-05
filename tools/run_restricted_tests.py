"""Reduced-risk local tests, not an OS sandbox or a PostgreSQL/live test.

Run with `python -I -S tools/run_restricted_tests.py`. Python-level external
connections and child processes are denied before importing pytest. Native
extensions and the tool host remain outside this restriction's guarantee.
"""
import ipaddress
import os
from pathlib import Path
import sys
import sysconfig
import uuid


if not sys.flags.isolated or not sys.flags.no_site:
    raise SystemExit('Use python -I -S to avoid startup customization')

root = Path(__file__).resolve().parents[1]
blocked = [0]


def loopback(value):
    if value == 'localhost':
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except (ValueError, TypeError):
        return False


def audit(event, args):
    deny = event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.startfile'}
    if event in {'socket.connect', 'socket.connect_ex', 'socket.bind'}:
        address = args[1]
        deny = not (isinstance(address, tuple) and loopback(address[0]))
    elif event == 'socket.getaddrinfo':
        deny = not loopback(args[0])
    elif event in {'socket.sendto', 'socket.sendmsg'}:
        # These tests do not need datagram transport.
        deny = True
    if deny:
        blocked[0] += 1
        raise PermissionError('Restricted test runner denied an operation')


sys.addaudithook(audit)
sys.dont_write_bytecode = True
for key in list(os.environ):
    if key.startswith('WEB_') or key in {
        'DATABASE_URL', 'RENDER', 'CALENDARSYNC_DISPOSABLE_POSTGRES_DSN',
        'CALENDARSYNC_ALLOW_TEMP_SCHEMA', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS',
        'PYTHONPATH', 'COVERAGE_PROCESS_START',
    }:
        del os.environ[key]
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
sys.path.insert(0, str(root))
for key in ('purelib', 'platlib'):
    path = sysconfig.get_path(key)
    if path and path not in sys.path:
        sys.path.append(path)

import pytest

result = pytest.main([
    str(root / 'tests_web'), '-q', '-p', 'no:cacheprovider',
    '-c', str(root / 'tools' / 'restricted-pytest.ini'),
    '--ignore=' + str(root / 'tests_web' / 'test_beta_package.py'),
    '--basetemp=' + str(root.parent / ('restricted-pytest-' + uuid.uuid4().hex)),
])
print('Denied operations:', blocked[0])
raise SystemExit(int(result) if not blocked[0] else 1)
