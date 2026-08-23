import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

import {
    describeSupportedNode,
    explainUnsupportedNode,
    isSupportedNodeVersion,
    parseNodeVersion,
} from '../bin/runtime-support.js';

test('Node version parsing accepts runtime and plain semver strings', () => {
    assert.deepEqual(parseNodeVersion('v20.19.0'), { major: 20, minor: 19, patch: 0 });
    assert.deepEqual(parseNodeVersion('22.12.1'), { major: 22, minor: 12, patch: 1 });
    assert.equal(parseNodeVersion('unknown'), null);
});

test('LimeBot rejects runtimes below the frontend toolchain minimum', () => {
    assert.equal(isSupportedNodeVersion('v18.20.8'), false);
    assert.equal(isSupportedNodeVersion('v20.18.9'), false);
    assert.equal(isSupportedNodeVersion('v20.19.0'), false);
    assert.equal(isSupportedNodeVersion('v22.18.0'), false);
    assert.equal(isSupportedNodeVersion('v22.19.0'), true);
    assert.equal(isSupportedNodeVersion('v24.0.0'), true);
    assert.match(describeSupportedNode(), /22\.19\.0/);
});

test('unsupported Node text tells a non-technical user what to install next', () => {
    const text = explainUnsupportedNode('v22.14.0');
    assert.match(text, /app called Node\.js/);
    assert.match(text, /nodejs\.org/);
    assert.match(text, /v22\.14\.0/);
    assert.doesNotMatch(text, /stack/i);
});

test('CLI rejects start on an old Node runtime with a plain-language next step', () => {
    const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    const cliUrl = pathToFileURL(path.join(rootDir, 'bin', 'cli.js')).href;
    const script = [
        "Object.defineProperty(process, 'version', { value: 'v18.20.8' });",
        "process.argv = [process.execPath, 'bin/cli.js', 'start'];",
        `await import(${JSON.stringify(cliUrl)});`,
    ].join('');
    const result = spawnSync(process.execPath, ['--input-type=module', '--eval', script], {
        cwd: rootDir,
        encoding: 'utf8',
    });

    assert.equal(result.status, 1);
    assert.match(result.stdout, /app called Node\.js/);
    assert.match(result.stdout, /nodejs\.org/);
    assert.doesNotMatch(result.stdout, /Commands:/);
});
