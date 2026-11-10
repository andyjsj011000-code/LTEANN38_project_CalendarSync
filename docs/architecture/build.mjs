import {existsSync, mkdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const directory = path.dirname(fileURLToPath(import.meta.url));
const repository = path.resolve(directory, '../..');
const archifyHome = process.argv[2] || process.env.ARCHIFY_HOME;
if (!archifyHome) {
  throw new Error('Pass the Archify directory as an argument or set ARCHIFY_HOME');
}
const cli = path.resolve(archifyHome, 'bin/archify.mjs');
if (!existsSync(cli)) throw new Error('Archify CLI not found: ' + cli);

const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const reports = path.join(directory, '.checks', 'build-' + stamp);
mkdirSync(reports, {recursive: true});
const bootstrap = pathToFileURL(path.join(directory, 'renderer/font-bootstrap.mjs')).href;
// NODE_OPTIONS also reaches Archify's renderer subprocesses.
const nodeOptions = [process.env.NODE_OPTIONS, '--import=' + bootstrap].filter(Boolean).join(' ');
const result = spawnSync(process.execPath, [
  cli, 'finalize', 'explainer',
  path.join(directory, 'source/oracle-guide.archify.md'),
  path.join(directory, 'oracle-guide.html'),
  '--repo-root', repository, '--out-dir', reports, '--json',
], {
  cwd: repository,
  env: {...process.env, NODE_OPTIONS: nodeOptions},
  stdio: 'inherit',
  windowsHide: true,
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
