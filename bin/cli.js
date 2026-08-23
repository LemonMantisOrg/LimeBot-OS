#!/usr/bin/env node

import { spawn, exec, execSync, execFile } from 'child_process';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import fs from 'fs';
import net from 'net';
import {
    buildNpmFingerprint,
    buildPythonFingerprint,
    buildFeatureFingerprint,
    clearFeatures,
    createDependencyState,
    evaluateDependencyState,
    loadDependencyState,
    isFeatureCurrent,
    recordFeatureInstall,
    recordSuccessfulInstall,
    writeDependencyStateAtomic,
} from './dependency-state.js';
import {
    FEATURE_DEFINITIONS,
    getCoreNpmInstallSpec,
    getDependencySpawnSpec,
    getFeatureInstallSpec,
    getVideoBinaryInstallInstructions,
    getVideoReadinessState,
    installRequestedFeatureSet,
    installFeatureThen,
    settleDependencyLanes,
    watchConfigFile,
} from './feature-install.js';
import { waitForBackendLiveness, waitForBackendReadiness } from './readiness-client.js';
import {
    ensureVenvExecutable,
    readRecentUpdateCache,
    runVenvPip,
    shouldDiscoverUpdates,
    startBackgroundUpdateDiscovery,
    startupWaitTarget,
} from './startup-flow.js';
import { describeSupportedNode, explainUnsupportedNode, isSupportedNodeVersion } from './runtime-support.js';
import { cleanupStoppedTaskState } from './task-state.js';
import { refreshWindowsProcessPath } from './windows-path.js';
import {
    applyUpdate,
    inspectWorktree,
    rollbackUpdate,
} from './updater.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');
const UPDATE_CHECK_TTL_MS = 1000 * 60 * 60 * 6;
const UPDATE_CHECK_TIMEOUT_MS = 2500;
const UPDATE_CHECK_CACHE_PATH = path.join(rootDir, 'data', 'cli-update-check.json');
const DEPENDENCY_STATE_PATH = path.join(rootDir, 'data', 'dependency-state.json');
const WIN_MAX_PATH_SAFE = 259;
const MIN_PYTHON_MAJOR = 3;
const MIN_PYTHON_MINOR = 11;
const MAX_SUPPORTED_PYTHON_MINOR = 14;
const WIN_PREFERRED_PYTHON_MINORS = [14, 13, 12, 11];
const WIN_VENV_PATH_PROBES = [
    path.join('Lib', 'site-packages'),
    path.join(
        'Lib',
        'site-packages',
        'litellm',
        'proxy',
        'guardrails',
        'guardrail_hooks',
        'litellm_content_filter',
        'guardrail_benchmarks',
        'results',
        'block_claims_prior_auth_gaming_-_contentfilter_(claims_prior_auth_gaming.yaml).json'
    ),
];
let cachedVenvLayout = null;

function hashPath(value) {
    let hash = 0x811c9dc5;
    for (let i = 0; i < value.length; i++) {
        hash ^= value.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(36);
}

function windowsSitePackagesPath(venvDir) {
    return path.join(path.resolve(venvDir), 'Lib', 'site-packages');
}

function windowsMaxProjectedPathLength(venvDir) {
    const base = path.resolve(venvDir);
    let maxLen = 0;
    for (const rel of WIN_VENV_PATH_PROBES) {
        const probeLen = path.join(base, rel).length;
        if (probeLen > maxLen) maxLen = probeLen;
    }
    return maxLen;
}

function windowsFallbackVenvDir() {
    const projectId = hashPath(rootDir);
    const localAppData = process.env.LOCALAPPDATA;
    if (localAppData) {
        return path.join(localAppData, 'LimeBot', 'venvs', projectId);
    }

    const userProfile = process.env.USERPROFILE;
    if (userProfile) {
        return path.join(userProfile, 'AppData', 'Local', 'LimeBot', 'venvs', projectId);
    }

    return path.join(path.parse(rootDir).root || 'C:\\', 'LimeBot', 'venvs', projectId);
}

function resolveVenvLayout() {
    if (cachedVenvLayout) return cachedVenvLayout;

    const defaultVenvDir = path.join(rootDir, '.venv');
    if (process.platform !== 'win32') {
        cachedVenvLayout = {
            venvDir: defaultVenvDir,
            usingFallback: false,
            projectedSitePackagesPath: null,
            defaultProjectedSitePackagesPath: null,
            projectedMaxPathLength: null,
            defaultProjectedMaxPathLength: null,
        };
        return cachedVenvLayout;
    }

    const projectedDefault = windowsSitePackagesPath(defaultVenvDir);
    const projectedDefaultMax = windowsMaxProjectedPathLength(defaultVenvDir);
    if (projectedDefaultMax < WIN_MAX_PATH_SAFE) {
        cachedVenvLayout = {
            venvDir: defaultVenvDir,
            usingFallback: false,
            projectedSitePackagesPath: projectedDefault,
            defaultProjectedSitePackagesPath: projectedDefault,
            projectedMaxPathLength: projectedDefaultMax,
            defaultProjectedMaxPathLength: projectedDefaultMax,
        };
        return cachedVenvLayout;
    }

    let fallbackDir = windowsFallbackVenvDir();
    let projectedFallback = windowsSitePackagesPath(fallbackDir);
    let projectedFallbackMax = windowsMaxProjectedPathLength(fallbackDir);
    if (projectedFallbackMax >= WIN_MAX_PATH_SAFE) {
        fallbackDir = path.join(path.parse(rootDir).root || 'C:\\', 'lbv', hashPath(rootDir));
        projectedFallback = windowsSitePackagesPath(fallbackDir);
        projectedFallbackMax = windowsMaxProjectedPathLength(fallbackDir);
    }

    cachedVenvLayout = {
        venvDir: fallbackDir,
        usingFallback: true,
        projectedSitePackagesPath: projectedFallback,
        defaultProjectedSitePackagesPath: projectedDefault,
        projectedMaxPathLength: projectedFallbackMax,
        defaultProjectedMaxPathLength: projectedDefaultMax,
    };
    return cachedVenvLayout;
}

function venvDirPath() {
    return resolveVenvLayout().venvDir;
}

// ── Logger ────────────────────────────────────────────────────────

const colors = {
    reset: "\x1b[0m", bright: "\x1b[1m", dim: "\x1b[2m",
    green: "\x1b[32m", yellow: "\x1b[33m", blue: "\x1b[34m",
    cyan: "\x1b[36m", gray: "\x1b[90m", red: "\x1b[31m",
    lime: "\x1b[38;5;154m",
};

const LOGO = `${colors.lime}
                                                                                                    
                                 ========                  ==++++**                                 
                               ========----              ---===++****                               
                              ============---          ---===++++*****                              
                              ==========    --       ---    +++++*****                              
                                             --======-==                                            
                                         ==================+                                        
                                      =====================+++                                      
                                    =====+%@@@@*====+%@@@@*++++*                                    
                                   =====#@@@@@@@#==+@@@@@@@%++++*                                   
                                  =====*@@@@@@@@%==#@@@@@@@@#+++**                                  
                                  =====*@@@@@@@@#==*@@@@@@@@%+++**                                   
                                   =====%@@@@@@%====%@@@@@@@++++*                                   
                                    ======#%@%+======+%@@%++++++                                    
                                      =====================+++                                      
                                        ===================+                                        
                                            =============                                           
                                                 ===                                                
                                                                                                    
               ===     ====                               ======                 ==                 
              =====    ===                               ==========             ====                
              =====    ==== ================    =======  ====  =====  =======  =======              
              =====    ==== ================= ========== ========== ==================              
              =====    ==== ====   ====  =============== ========== ====   ==== ====                
              =====    ==== ====   ====  =============== ====   ========   ==== ====                
              ======== ==== ====   ====  =============== =========== =========  ======              
               ======= ==== ====   ====   ===    =====   =========     =====      ====              

${colors.reset}`;

const log = (color, text) => console.log(`${color}${text}${colors.reset}`);
const success = (text) => log(colors.green, `  ✓ ${text}`);
const warning = (text) => log(colors.yellow, `  ⚠ ${text}`);
const error = (text) => log(colors.red, `  ✗ ${text}`);
const info = (text) => log(colors.blue, `  ${text}`);
const step = (text) => log(colors.lime, `  ${colors.bright}→ ${text}${colors.reset}`);

// ── Spinner Utility ────────────────────────────────────────────────

class Spinner {
    constructor(text) {
        this.text = text;
        this.frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
        this.frameIdx = 0;
        this.interval = null;
    }

    start() {
        process.stdout.write(`  ${colors.lime}${this.frames[0]}${colors.reset} ${this.text}`);
        this.interval = setInterval(() => {
            this.frameIdx = (this.frameIdx + 1) % this.frames.length;
            process.stdout.current_line = `  ${colors.lime}${this.frames[this.frameIdx]}${colors.reset} ${this.text}`;
            process.stdout.write(`\r${process.stdout.current_line}`);
        }, 80);
    }

    update(text) {
        this.text = text;
    }

    stop(msg, success = true) {
        if (this.interval) clearInterval(this.interval);
        process.stdout.write(`\r\x1b[K`); // Clear line
        if (success) {
            console.log(`  ${colors.green}✓${colors.reset} ${msg || this.text}`);
        } else {
            console.log(`  ${colors.red}✗${colors.reset} ${msg || this.text}`);
        }
    }
}

async function runWithSpinner(text, fn) {
    const s = new Spinner(text);
    s.start();
    try {
        const result = await fn();
        s.stop();
        return result;
    } catch (e) {
        s.stop(text, false);
        throw e;
    }
}

function appendBoundedOutput(current, chunk, maxChars = 8000) {
    const combined = current + String(chunk || '');
    return combined.length > maxChars ? combined.slice(-maxChars) : combined;
}

async function runDependencyCommand(command, args, { env, shell = false, label, retryCommand }) {
    return new Promise((resolve, reject) => {
        let output = '';
        let child;
        try {
            const spawnSpec = shell
                ? { command, args }
                : getDependencySpawnSpec(command, args);
            child = spawn(spawnSpec.command, spawnSpec.args, {
                cwd: rootDir,
                shell,
                stdio: ['ignore', 'pipe', 'pipe'],
                env,
            });
        } catch (err) {
            reject(new Error(`${label} could not start: ${err.message}\nRetry: ${retryCommand}`));
            return;
        }

        child.stdout?.on('data', (chunk) => {
            output = appendBoundedOutput(output, chunk);
        });
        child.stderr?.on('data', (chunk) => {
            output = appendBoundedOutput(output, chunk);
        });
        child.on('error', (err) => {
            reject(new Error(`${label} could not start: ${err.message}\nRetry: ${retryCommand}`));
        });
        child.on('close', (code) => {
            if (code === 0) {
                resolve();
                return;
            }
            const detail = output.trim();
            reject(new Error(
                `${label} failed with exit code ${code}.` +
                `${detail ? `\n${detail}` : ''}\nRetry: ${retryCommand}`
            ));
        });
    });
}

// ── Utilities ──────────────────────────────────────────────────────

async function isPortReachable(port) {
    const tryConnect = (host) => new Promise((resolve) => {
        const socket = new net.Socket();
        const onError = () => { socket.destroy(); resolve(false); };
        socket.setTimeout(500);
        socket.on('error', onError);
        socket.on('timeout', onError);
        socket.connect(port, host, () => { socket.end(); resolve(true); });
    });

    if (await tryConnect('127.0.0.1')) return true;
    if (await tryConnect('localhost')) return true;
    return false;
}

function openBrowser(url) {
    info(`Opening browser: ${url}`);
    const cmd = process.platform === 'darwin' ? `open "${url}"`
        : process.platform === 'win32' ? `start "" "${url}"`
            : `xdg-open "${url}"`;
    exec(cmd, (err) => {
        if (err) error(`Failed to open browser: ${err.message}`);
    });
}

async function waitForServer(port, maxAttempts = 60) {
    for (let i = 0; i < maxAttempts; i++) {
        if (await isPortReachable(port)) return true;
        await sleep(1000);
    }
    return false;
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function isTruthyEnv(value) {
    const normalized = String(value ?? '').trim().toLowerCase();
    return normalized === '1' || normalized === 'true' || normalized === 'yes' || normalized === 'on';
}

function shortSha(sha) {
    return typeof sha === 'string' ? sha.slice(0, 8) : '';
}

function readJsonSafe(filePath) {
    try {
        if (!fs.existsSync(filePath)) return null;
        return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    } catch {
        return null;
    }
}

function writeJsonSafe(filePath, payload) {
    try {
        fs.mkdirSync(path.dirname(filePath), { recursive: true });
        fs.writeFileSync(filePath, JSON.stringify(payload, null, 2), 'utf-8');
    } catch { }
}

async function runGit(args, timeoutMs = UPDATE_CHECK_TIMEOUT_MS) {
    return new Promise((resolve) => {
        let child;
        try {
            child = spawn('git', args, {
                cwd: rootDir,
                shell: false,
                stdio: ['ignore', 'pipe', 'pipe'],
            });
        } catch (err) {
            resolve({
                code: -1,
                stdout: '',
                stderr: err?.message || String(err),
                timedOut: false,
            });
            return;
        }
        let stdout = '';
        let stderr = '';
        let settled = false;
        let timer = null;

        const finish = (result) => {
            if (settled) return;
            settled = true;
            if (timer) clearTimeout(timer);
            resolve(result);
        };

        if (timeoutMs > 0) {
            timer = setTimeout(() => {
                try { child.kill(); } catch { }
                finish({ code: -1, stdout: stdout.trim(), stderr: stderr.trim(), timedOut: true });
            }, timeoutMs);
        }

        child.stdout.on('data', (d) => { stdout += d.toString(); });
        child.stderr.on('data', (d) => { stderr += d.toString(); });
        child.on('error', (err) => finish({ code: -1, stdout: '', stderr: err.message, timedOut: false }));
        child.on('close', (code) => finish({
            code: code ?? -1,
            stdout: stdout.trim(),
            stderr: stderr.trim(),
            timedOut: false,
        }));
    });
}

function readUpdateCheckCache() {
    const cached = readJsonSafe(UPDATE_CHECK_CACHE_PATH);
    if (!cached || typeof cached !== 'object') return null;
    return cached;
}

function writeUpdateCheckCache(status) {
    writeJsonSafe(UPDATE_CHECK_CACHE_PATH, status);
}

function readLocalPackageInfo() {
    const pkg = readJsonSafe(path.join(rootDir, 'package.json'));
    if (!pkg || typeof pkg !== 'object') {
        return { packageName: null, currentVersion: null };
    }
    return {
        packageName: typeof pkg.name === 'string' ? pkg.name.trim() : null,
        currentVersion: typeof pkg.version === 'string' ? pkg.version.trim() : null,
    };
}

function parseSemver(version) {
    const match = String(version || '').trim().match(/^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/i);
    if (!match) return null;
    return match.slice(1, 4).map((part) => Number.parseInt(part, 10));
}

function compareSemver(a, b) {
    const parsedA = parseSemver(a);
    const parsedB = parseSemver(b);
    if (!parsedA || !parsedB) return null;
    for (let i = 0; i < 3; i++) {
        if (parsedA[i] > parsedB[i]) return 1;
        if (parsedA[i] < parsedB[i]) return -1;
    }
    return 0;
}

function normalizeVersion(version) {
    const raw = String(version || '').trim();
    if (!raw) return null;
    const parsed = parseSemver(raw);
    if (!parsed) return raw;
    return `${parsed[0]}.${parsed[1]}.${parsed[2]}`;
}

function selectNewerVersion(current, candidate) {
    if (!candidate) return current;
    if (!current) return candidate;
    const cmp = compareSemver(candidate, current);
    if (cmp === null) return current;
    return cmp > 0 ? candidate : current;
}

async function getLocalGitSnapshot() {
    const inRepo = await runGit(['rev-parse', '--is-inside-work-tree']);
    if (inRepo.code !== 0 || inRepo.stdout !== 'true') return null;

    const head = await runGit(['rev-parse', 'HEAD']);
    if (head.code !== 0 || !head.stdout) return null;

    const branch = await runGit(['rev-parse', '--abbrev-ref', 'HEAD']);
    const branchName = branch.code === 0 ? branch.stdout.trim() : '';
    if (!branchName || branchName === 'HEAD') return null;

    const remote = await runGit(['config', '--get', `branch.${branchName}.remote`]);
    const remoteName = (remote.code === 0 && remote.stdout) ? remote.stdout.trim() : 'origin';

    const merge = await runGit(['config', '--get', `branch.${branchName}.merge`]);
    const mergeRef = (merge.code === 0 && merge.stdout) ? merge.stdout.trim() : `refs/heads/${branchName}`;
    const remoteBranch = mergeRef.startsWith('refs/heads/')
        ? mergeRef.slice('refs/heads/'.length)
        : mergeRef;

    return {
        localHead: head.stdout.toLowerCase(),
        branchName,
        remoteName,
        remoteBranch,
    };
}

async function getRemotePackageVersion(ref = 'FETCH_HEAD') {
    const result = await runGit(['show', `${ref}:package.json`]);
    if (result.code !== 0 || !result.stdout) return null;
    try {
        const pkg = JSON.parse(result.stdout);
        return normalizeVersion(pkg?.version);
    } catch {
        return null;
    }
}

async function getLatestGitTagVersion(remoteName) {
    const result = await runGit(['ls-remote', '--tags', '--refs', remoteName], UPDATE_CHECK_TIMEOUT_MS);
    if (result.code !== 0 || !result.stdout) return null;

    let latest = null;
    for (const line of result.stdout.split(/\r?\n/)) {
        const ref = line.trim().split(/\s+/)[1] || '';
        const tagName = ref.replace(/^refs\/tags\//, '');
        const version = normalizeVersion(tagName);
        if (!parseSemver(version)) continue;
        latest = selectNewerVersion(latest, version);
    }
    return latest;
}

function npmExecutable() {
    return process.platform === 'win32' ? 'npm.cmd' : 'npm';
}

async function getLatestNpmVersion(packageName) {
    if (!packageName || !await commandExists('npm')) return null;

    return new Promise((resolve) => {
        exec(
            `${npmExecutable()} view "${packageName}" version --json`,
            { cwd: rootDir, windowsHide: true, timeout: UPDATE_CHECK_TIMEOUT_MS },
            (err, stdout) => {
                if (err || !stdout) return resolve(null);

                try {
                    const parsed = JSON.parse(stdout);
                    if (typeof parsed === 'string') return resolve(normalizeVersion(parsed));
                    if (Array.isArray(parsed)) {
                        let latest = null;
                        for (const item of parsed) {
                            const version = normalizeVersion(item);
                            if (parseSemver(version)) latest = selectNewerVersion(latest, version);
                        }
                        return resolve(latest);
                    }
                } catch {
                    return resolve(normalizeVersion(stdout.trim()));
                }

                resolve(null);
            }
        );
    });
}

function buildLatestVersionSummary(currentVersion, sources) {
    let latestVersion = null;
    let latestVersionSource = null;

    for (const source of sources) {
        if (!source?.version || !parseSemver(source.version)) continue;
        const nextVersion = selectNewerVersion(latestVersion, source.version);
        if (nextVersion !== latestVersion) {
            latestVersion = nextVersion;
            latestVersionSource = source.label;
        }
    }

    const hasVersionUpdate = Boolean(
        latestVersion &&
        currentVersion &&
        compareSemver(latestVersion, currentVersion) > 0
    );

    return { latestVersion, latestVersionSource, hasVersionUpdate };
}

async function getUpdateStatus({ forceRefresh = false } = {}) {
    if (isTruthyEnv(process.env.LIMEBOT_DISABLE_UPDATE_CHECK)) return null;
    if (!fs.existsSync(path.join(rootDir, '.git'))) return null;
    if (!await commandExists('git')) return null;

    const snapshot = await getLocalGitSnapshot();
    if (!snapshot) return null;
    const worktree = await inspectWorktree({ runGit });
    const worktreeChanges = Array.isArray(worktree.changes) ? worktree.changes : [];
    const worktreeToken = worktree.available
        ? worktreeChanges.map((change) => change.raw).join('\n')
        : '';
    const packageInfo = readLocalPackageInfo();

    const now = Date.now();
    const cached = readUpdateCheckCache();
    if (
        !forceRefresh &&
        cached &&
        Number.isFinite(cached.checkedAt) &&
        cached.localHead === snapshot.localHead &&
        cached.branchName === snapshot.branchName &&
        cached.remoteName === snapshot.remoteName &&
        cached.remoteBranch === snapshot.remoteBranch &&
        cached.currentVersion === packageInfo.currentVersion &&
        cached.packageName === packageInfo.packageName &&
        cached.worktreeToken === worktreeToken &&
        (now - cached.checkedAt) < UPDATE_CHECK_TTL_MS
    ) {
        return cached;
    }

    const fetch = await runGit(['fetch', '--quiet', snapshot.remoteName, snapshot.remoteBranch]);
    if (fetch.code !== 0) return null;

    const remoteHeadRes = await runGit(['rev-parse', 'FETCH_HEAD']);
    if (remoteHeadRes.code !== 0 || !remoteHeadRes.stdout) return null;

    const counts = await runGit(['rev-list', '--left-right', '--count', 'HEAD...FETCH_HEAD']);
    if (counts.code !== 0 || !counts.stdout) return null;

    const [aheadRaw, behindRaw] = counts.stdout.split(/\s+/);
    const ahead = Number.parseInt(aheadRaw, 10);
    const behind = Number.parseInt(behindRaw, 10);
    if (!Number.isInteger(ahead) || !Number.isInteger(behind)) return null;

    const remotePackageVersion = await getRemotePackageVersion();
    const latestTagVersion = await getLatestGitTagVersion(snapshot.remoteName);
    const latestNpmVersion = await getLatestNpmVersion(packageInfo.packageName);
    const latestVersionSummary = buildLatestVersionSummary(packageInfo.currentVersion, [
        { label: 'remote package.json', version: remotePackageVersion },
        { label: 'git tag', version: latestTagVersion },
        { label: 'npm registry', version: latestNpmVersion },
    ]);

    const status = {
        checkedAt: now,
        localHead: snapshot.localHead,
        remoteHead: remoteHeadRes.stdout.toLowerCase(),
        branchName: snapshot.branchName,
        remoteName: snapshot.remoteName,
        remoteBranch: snapshot.remoteBranch,
        ahead,
        behind,
        packageName: packageInfo.packageName,
        currentVersion: packageInfo.currentVersion,
        remotePackageVersion,
        latestTagVersion,
        latestNpmVersion,
        latestVersion: latestVersionSummary.latestVersion,
        latestVersionSource: latestVersionSummary.latestVersionSource,
        hasVersionUpdate: latestVersionSummary.hasVersionUpdate,
        hasUpdate: behind > 0 || latestVersionSummary.hasVersionUpdate,
        worktreeKind: worktree.kind,
        worktreeToken,
        worktreeChanges: worktreeChanges.map((change) => change.path),
        codeDirty: Boolean(worktree.codeDirty),
        stateOnly: Boolean(worktree.stateOnly),
    };
    writeUpdateCheckCache(status);
    return status;
}

function printUpdateStatus(status, { alwaysShowSummary = false } = {}) {
    if (!status) {
        if (alwaysShowSummary) warning('Update status unavailable.');
        return;
    }

    const currentVersion = status.currentVersion || 'unknown';
    const latestVersion = status.latestVersion || status.remotePackageVersion || currentVersion;
    if (alwaysShowSummary) {
        info(`Current version: ${currentVersion}`);
        if (latestVersion && latestVersion !== 'unknown') {
            const sourceSuffix = status.latestVersionSource ? ` via ${status.latestVersionSource}` : '';
            info(`Latest version: ${latestVersion}${sourceSuffix}`);
        } else {
            info('Latest version: unavailable');
        }
        info(`Git state: ${status.branchName} @ ${shortSha(status.localHead)} (ahead ${status.ahead}, behind ${status.behind})`);
        if (status.worktreeKind === 'clean') {
            success('Working tree is clean.');
        } else if (status.worktreeKind === 'state-only') {
            warning('Local runtime state will be preserved during an update.');
        } else if (status.worktreeKind === 'code-dirty') {
            warning('Tracked source changes are present; automatic update is blocked.');
        }
    }

    if (status.hasVersionUpdate) {
        warning(`New version available: ${currentVersion} -> ${status.latestVersion}.`);
    } else if (alwaysShowSummary) {
        success(`Version is current at ${currentVersion}.`);
    }

    if (status.behind > 0) {
        const commitWord = status.behind === 1 ? 'commit' : 'commits';
        warning(`Git update available: ${status.behind} ${commitWord} behind ${status.remoteName}/${status.remoteBranch}.`);
        info(`Current ${shortSha(status.localHead)} -> latest ${shortSha(status.remoteHead)}.`);
    } else if (alwaysShowSummary) {
        success(`Git branch is up to date with ${status.remoteName}/${status.remoteBranch}.`);
    }

    if (status.behind > 0) {
        info(`Run 'git pull --ff-only ${status.remoteName} ${status.remoteBranch}' to update.`);
    } else if (status.hasVersionUpdate && status.latestVersionSource === 'npm registry' && status.packageName) {
        info(`Run 'npm install -g ${status.packageName}@latest' if you use the published package.`);
    } else if (status.hasVersionUpdate) {
        info('Pull the latest source or checkout the newest release tag to update.');
    }
}

function readEnvValue(key) {
    try {
        const envPath = path.join(rootDir, '.env');
        if (!fs.existsSync(envPath)) return null;
        const lines = fs.readFileSync(envPath, 'utf-8').split('\n');
        for (const rawLine of lines) {
            const line = rawLine.trim();
            if (!line || line.startsWith('#')) continue;
            const idx = line.indexOf('=');
            if (idx === -1) continue;
            const k = line.slice(0, idx).trim();
            if (k !== key) continue;
            let value = line.slice(idx + 1).trim();
            if (
                (value.startsWith('"') && value.endsWith('"')) ||
                (value.startsWith("'") && value.endsWith("'"))
            ) {
                value = value.slice(1, -1);
            }
            return value;
        }
    } catch { }
    return null;
}

function getConfiguredPort(key, fallback) {
    const raw = process.env[key] ?? readEnvValue(key);
    const parsed = Number.parseInt(String(raw ?? ''), 10);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) return fallback;
    return parsed;
}

function describePortBindFailure(result) {
    const code = result?.code ? `${result.code}: ` : '';
    const message = result?.message || 'unknown bind failure';
    if (result?.code === 'EACCES' || result?.code === 'EPERM') {
        const platformHint = process.platform === 'darwin'
            ? 'macOS denied permission to bind the port. Grant terminal/network permissions or run from a terminal with the required privileges.'
            : 'Permission denied while binding the port. Try a terminal with the required privileges or choose another port.';
        return `${code}${message}. ${platformHint}`;
    }
    if (result?.code === 'EADDRINUSE') {
        return `${code}${message}. Another process is already listening on this port.`;
    }
    return `${code}${message}`;
}

async function checkPortAvailability(port) {
    return new Promise((resolve) => {
        const server = net.createServer();
        const done = (result) => {
            try {
                server.removeAllListeners();
                if (server.listening) {
                    server.close(() => resolve(result));
                    return;
                }
            } catch { }
            resolve(result);
        };

        server.once('error', (err) => done({
            available: false,
            code: err?.code || '',
            message: err?.message || String(err),
        }));
        server.once('listening', () => done({ available: true }));
        server.listen({ port, host: '127.0.0.1', exclusive: true });
    });
}

async function isPortAvailable(port) {
    return (await checkPortAvailability(port)).available;
}

async function findAvailablePort(startPort, maxChecks = 100, reservedPorts = new Set()) {
    let port = startPort;
    let lastBlocked = null;
    for (let i = 0; i < maxChecks; i++, port++) {
        if (reservedPorts.has(port)) continue;
        const result = await checkPortAvailability(port);
        if (result.available) return port;
        lastBlocked = { port, ...result };
    }
    const detail = lastBlocked
        ? ` Last checked port ${lastBlocked.port}: ${describePortBindFailure(lastBlocked)}`
        : '';
    throw new Error(`Could not find available port near ${startPort} (checked ${maxChecks} ports).${detail}`);
}

function commandExists(cmd) {
    return new Promise((resolve) => {
        if (!cmd) return resolve(false);
        if (path.isAbsolute(cmd) || /[\\/]/.test(cmd)) {
            return resolve(fs.existsSync(cmd));
        }
        try {
            exec(
                process.platform === 'win32' ? `where ${cmd}` : `which ${cmd}`,
                (err) => resolve(!err)
            );
        } catch {
            resolve(false);
        }
    });
}

function pythonModuleAvailable(python, moduleName) {
    return new Promise((resolve) => {
        execFile(python, ['-c', `import ${moduleName}`], { windowsHide: true }, (err) => resolve(!err));
    });
}

async function getSystemPython() {
    const pinnedPython = String(process.env.LIMEBOT_PYTHON || '').trim();
    if (pinnedPython) return pinnedPython;

    let firstDetected = null;

    if (process.platform === 'win32' && await commandExists('py')) {
        const requestedVersion = String(process.env.LIMEBOT_PYTHON_VERSION || '').trim();
        if (requestedVersion) {
            const requested = await resolvePyLauncherPython(`-${requestedVersion}`);
            if (requested) return requested;
        }

        for (const minor of WIN_PREFERRED_PYTHON_MINORS) {
            const resolved = await resolvePyLauncherPython(`-3.${minor}`);
            if (resolved) return resolved;
        }

        const fallback = await resolvePyLauncherPython();
        if (fallback) firstDetected = fallback;
    }

    for (const candidate of ['python3', 'python']) {
        if (!await commandExists(candidate)) continue;
        const info = await getPythonRuntimeInfo(candidate);
        if (info.supported) return candidate;
        if (!firstDetected) firstDetected = candidate;
    }

    if (process.platform === 'win32') {
        const candidates = [];

        try {
            for (const entry of fs.readdirSync('C:\\')) {
                if (/^Python\d/i.test(entry)) {
                    candidates.push(path.join('C:\\', entry, 'python.exe'));
                }
            }
        } catch { }

        const localAppData = process.env.LOCALAPPDATA;
        if (localAppData) {
            const pyDir = path.join(localAppData, 'Programs', 'Python');
            try {
                for (const entry of fs.readdirSync(pyDir)) {
                    if (/^Python\d/i.test(entry)) {
                        candidates.push(path.join(pyDir, entry, 'python.exe'));
                    }
                }
            } catch { }
        }

        for (const c of candidates) {
            if (!fs.existsSync(c)) continue;
            const info = await getPythonRuntimeInfo(c);
            if (info.supported) return c;
            if (!firstDetected) firstDetected = c;
        }
    }

    return firstDetected || 'python';
}

function getVersion(cmd, args = ['--version']) {
    return new Promise((resolve) => {
        if (!cmd) return resolve(null);
        try {
            if (path.isAbsolute(cmd) || !cmd.includes(' ')) {
                const isWindowsShim = process.platform === 'win32'
                    && ['npm', 'npx', 'pnpm', 'yarn'].includes(cmd.toLowerCase());
                const executable = isWindowsShim ? (process.env.ComSpec || 'cmd.exe') : cmd;
                const commandArgs = isWindowsShim
                    ? ['/d', '/s', '/c', [cmd, ...args].join(' ')]
                    : args;
                execFile(executable, commandArgs, { windowsHide: true }, (err, stdout, stderr) => {
                    if (err) resolve(null);
                    else resolve((stdout || stderr).trim().split('\n')[0]);
                });
                return;
            }

            exec(`${cmd} ${args.join(' ')}`, (err, stdout, stderr) => {
                if (err) resolve(null);
                else resolve((stdout || stderr).trim().split('\n')[0]);
            });
        } catch {
            resolve(null);
        }
    });
}

function parsePythonVersion(versionText) {
    const raw = String(versionText || '').trim();
    if (!raw) return null;

    const match = raw.match(/Python\s+(\d+)\.(\d+)(?:\.(\d+))?/i)
        || raw.match(/^(\d+)\.(\d+)(?:\.(\d+))?$/);
    if (!match) return null;

    return {
        major: Number(match[1]),
        minor: Number(match[2]),
        patch: Number(match[3] || 0),
        raw,
    };
}

function describeSupportedPython() {
    return 'Python 3.11 to 3.14';
}

function isSupportedPythonVersion(version) {
    if (!version) return false;
    if (version.major !== MIN_PYTHON_MAJOR) return false;
    if (version.minor < MIN_PYTHON_MINOR) return false;
    if (version.minor > MAX_SUPPORTED_PYTHON_MINOR) {
        return false;
    }
    return true;
}

async function getPythonRuntimeInfo(cmd) {
    const versionText = await getVersion(cmd);
    const version = parsePythonVersion(versionText);
    return {
        command: cmd,
        versionText,
        version,
        supported: isSupportedPythonVersion(version),
    };
}

function unsupportedPythonMessage(info, { location = null, venvDir = null } = {}) {
    const versionText = info?.versionText || 'an unknown Python version';
    const target = location || `Python interpreter ${info?.command || '<unknown>'}`;

    let message = `${target} is using ${versionText}. LimeBot currently supports ${describeSupportedPython()}.`;
    if (process.platform === 'win32') {
        message += ` Recreate the venv with a supported version, for example: py -3.14 -m venv "${venvDir || venvDirPath()}"`;
    }
    return message;
}

async function ensureSupportedPython(cmd, purpose, venvDir = null) {
    const info = await getPythonRuntimeInfo(cmd);
    if (!info.version) {
        throw new Error(`${purpose} was found, but its version could not be determined (${cmd}).`);
    }
    if (!info.supported) {
        throw new Error(
            unsupportedPythonMessage(info, {
                location: purpose,
                venvDir,
            })
        );
    }
    return info;
}

function resolvePyLauncherPython(versionArg = null) {
    return new Promise((resolve) => {
        const args = [];
        if (versionArg) args.push(versionArg);
        args.push('-c', 'import sys; print(sys.executable)');

        try {
            execFile('py', args, { windowsHide: true }, (err, stdout) => {
                if (err) return resolve(null);
                const candidate = String(stdout || '').trim().split(/\r?\n/)[0];
                if (candidate && fs.existsSync(candidate)) resolve(candidate);
                else resolve(null);
            });
        } catch {
            resolve(null);
        }
    });
}


async function checkPlaywrightBrowsers() {
    const venvPython = venvPythonPath();
    const systemPython = await getSystemPython();
    const pythonCmd = fs.existsSync(venvPython) ? venvPython : systemPython;

    return new Promise((resolve) => {
        exec(
            `"${pythonCmd}" -c "from playwright.sync_api import sync_playwright; ` +
            `p = sync_playwright().start(); b = p.chromium.launch(); b.close(); p.stop(); print('OK')"`,
            { timeout: 30000 },
            (err, stdout) => resolve(!err && stdout.includes('OK'))
        );
    });
}

/** Resolve the venv Python binary path for the current platform. */
function venvPythonPath() {
    const isWin = process.platform === 'win32';
    const bin = isWin ? 'Scripts' : 'bin';
    const exe = isWin ? 'python.exe' : 'python';
    return path.join(venvDirPath(), bin, exe);
}


function buildChildEnv() {
    const venvDir = venvDirPath();
    const childEnv = { ...process.env };
    if (fs.existsSync(venvDir)) {
        const isWin = process.platform === 'win32';
        const venvBin = path.join(venvDir, isWin ? 'Scripts' : 'bin');
        childEnv.VIRTUAL_ENV = venvDir;
        childEnv.PATH = `${venvBin}${path.delimiter}${process.env.PATH}`;
        delete childEnv.PYTHONHOME;
    }
    return childEnv;
}


function isWhatsAppEnabled() {
    const raw = readEnvValue('ENABLE_WHATSAPP');
    return String(raw || '').toLowerCase() === 'true';
}


function killProc(proc) {
    if (!proc) return;
    try {
        if (process.platform === 'win32') {

            execSync(`taskkill /T /F /PID ${proc.pid}`, { stdio: 'ignore' });
        } else {

            proc.kill('SIGTERM');
        }
    } catch { /* already gone */ }
}


function killPort(port) {
    return new Promise((resolve) => {
        if (process.platform === 'win32') {
            exec(`netstat -ano | findstr :${port}`, (err, stdout) => {
                if (err || !stdout) return resolve(false);
                const pids = new Set();
                for (const line of stdout.split('\n')) {
                    const part = line.trim();
                    if (!part) continue;
                    const pid = part.split(/\s+/).pop();
                    if (pid && /^\d+$/.test(pid) && pid !== '0') pids.add(pid);
                }
                let pending = pids.size;
                if (pending === 0) return resolve(false);
                for (const pid of pids) {
                    exec(`taskkill /T /F /PID ${pid}`, () => { if (--pending === 0) resolve(true); });
                }
            });
        } else {
            exec(`lsof -ti:${port}`, (err, stdout) => {
                if (err || !stdout.trim()) return resolve(false);
                const pids = stdout.trim().split('\n').filter(Boolean);
                let pending = pids.length;
                if (pending === 0) return resolve(false);
                for (const pid of pids) {
                    exec(`kill -9 ${pid}`, () => { if (--pending === 0) resolve(true); });
                }
            });
        }
    });
}

// ── Commands ───────────────────────────────────────────────────────

async function cmdSetup(args = []) {
    const recommended = args.includes('--recommended') || args.includes('--browser');

    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Setup${colors.reset}\n`);
    info('This checks the apps LimeBot needs, writes a starter settings file,');
    info('and can install a real browser so chat can click and download files.');
    console.log('');

    let issues = 0;

    if (isSupportedNodeVersion(process.version)) {
        success(`Node.js is ready: ${process.version}`);
    } else {
        error(`Node.js is not ready (${process.version}).`);
        console.log(`\n${explainUnsupportedNode(process.version)}\n`);
        issues++;
    }

    const pythonCmd = await getSystemPython();
    if (await commandExists(pythonCmd)) {
        const pythonInfo = await getPythonRuntimeInfo(pythonCmd);
        if (pythonInfo.supported) {
            success(`Python is ready: ${pythonInfo.versionText}`);
        } else {
            error(`Python is the wrong version: ${pythonInfo.versionText}`);
            console.log(`\n${explainUnsupportedPython(pythonInfo)}\n`);
            issues++;
        }
    } else {
        error('Python is not installed (the app is named Python).');
        console.log(`\n${explainUnsupportedPython({ versionText: 'not installed' })}\n`);
        issues++;
    }

    const envPath = path.join(rootDir, '.env');
    const examplePath = path.join(rootDir, '.env.example');
    if (!fs.existsSync(envPath) && fs.existsSync(examplePath)) {
        fs.copyFileSync(examplePath, envPath);
        success('Created .env from the example. Open that file and paste your model key.');
    } else if (fs.existsSync(envPath)) {
        success('.env already exists and was left unchanged.');
    } else {
        warning('.env.example is missing, so a starter settings file was not created.');
    }

    if (recommended) {
        if (issues) {
            warning('Skipping browser install until Node.js and Python are ready.');
        } else {
            step('Installing the recommended browser extras (Playwright + Chromium)...');
            await ensureBrowserAndChromium();
        }
    } else {
        info('Want a real browser in one step? Run: npm run lime-bot setup -- --recommended');
    }

    console.log(`
  ${colors.bright}Next:${colors.reset}
  1. Open the .env file in Notepad, TextEdit, or the dashboard wizard.
  2. Paste your model API key next to the matching name (for example OPENAI_API_KEY).
  3. In this folder, run: npm start
  4. Open http://localhost:5173 if a browser window does not appear.
`);
    if (issues) {
        process.exitCode = 1;
    }
}

function explainUnsupportedPython(info = {}) {
    const found = String(info.versionText || 'an unknown version').trim();
    return [
        'LimeBot needs the app called Python, versions 3.11 through 3.14.',
        `This computer currently has ${found}.`,
        '',
        'Do this next (no command-line experience needed):',
        '1. Open https://www.python.org/downloads/ in your web browser.',
        '2. Download Python 3.12 or 3.13 and run the installer.',
        '3. On Windows, tick "Add python.exe to PATH" before you click Install.',
        '4. Close every terminal window, open a new one, and type: python --version',
        '5. Come back to the LimeBot folder and run: npm start',
        '',
        'LimeBot did not change any files yet.',
    ].join('\n');
}

async function cmdHelp() {
    process.stdout.write(LOGO);
    console.log(`${colors.lime}${colors.bright}
  🍋 LimeBot CLI
${colors.reset}
  ${colors.bright}Usage:${colors.reset} limebot <command> [options]

  ${colors.bright}Commands:${colors.reset}
    ${colors.cyan}start${colors.reset}            Start LimeBot (backend + frontend)
    ${colors.cyan}setup${colors.reset}            First-run helper: check apps, write .env, install browser
    ${colors.cyan}stop${colors.reset}             Stop all running LimeBot processes
    ${colors.cyan}status${colors.reset}           Check if LimeBot services are running
    ${colors.cyan}update${colors.reset}           Safely fast-forward source and preserve local state
    ${colors.cyan}update-check${colors.reset}     Check current version, latest version, and git update status
    ${colors.cyan}auth${colors.reset}             Manage CLI-only auth providers like Codex OAuth
    ${colors.cyan}skill${colors.reset}            Manage skills (install, uninstall, update, list)
    ${colors.cyan}plugin${colors.reset}           Install Cursor plugin packages (skills + MCP)
    ${colors.cyan}doctor${colors.reset}           Diagnose common issues + run tests
    ${colors.cyan}logs${colors.reset}             Show recent logs
    ${colors.cyan}review-diff${colors.reset}      Build a redacted, review-only diff artifact
    ${colors.cyan}install-browser${colors.reset}  Install Chromium for browser tool (optional)
    ${colors.cyan}feature${colors.reset}          Install optional feature dependencies
    ${colors.cyan}autorun${colors.reset}          Configure LimeBot to start automatically
    ${colors.cyan}help${colors.reset}             Show this help message

  ${colors.bright}Start Options:${colors.reset}
    ${colors.gray}--quick, -q${colors.reset}        Skip dependency checks
    ${colors.gray}--backend-only${colors.reset}     Start only the Python backend
    ${colors.gray}--frontend-only${colors.reset}    Start only the web frontend

  ${colors.bright}Autorun Commands:${colors.reset}
    ${colors.dim}limebot autorun enable${colors.reset}
    ${colors.dim}limebot autorun disable${colors.reset}

  ${colors.bright}Auth Commands:${colors.reset}
    ${colors.dim}limebot auth codex login${colors.reset}
    ${colors.dim}limebot auth codex import${colors.reset}
    ${colors.dim}limebot auth codex status${colors.reset}
    ${colors.dim}limebot auth codex logout${colors.reset}

  ${colors.bright}Skill Commands:${colors.reset}
    ${colors.dim}limebot skill list${colors.reset}
    ${colors.dim}limebot skill install <repo-url> [--ref v2.0]${colors.reset}
    ${colors.dim}limebot skill uninstall <name>${colors.reset}
    ${colors.dim}limebot skill update <name>${colors.reset}

  ${colors.bright}Plugin Commands:${colors.reset}
    ${colors.dim}limebot plugin list${colors.reset}
    ${colors.dim}limebot plugin install <local-path|cursor/plugins/github>${colors.reset}
    ${colors.dim}limebot plugin uninstall <name>${colors.reset}

  ${colors.bright}Feature Commands:${colors.reset}
    ${colors.dim}limebot feature install <browser|memory|documents|mcp|video|whatsapp|extension|all>${colors.reset}

  ${colors.bright}Review Options:${colors.reset}
    ${colors.dim}limebot review-diff --diff-file change.patch --output review.json${colors.reset}
    ${colors.gray}--invoke-model${colors.reset}     Call the configured LLM without tools
    ${colors.gray}--format markdown${colors.reset}  Write a Markdown artifact instead of JSON

  ${colors.bright}Examples:${colors.reset}
    ${colors.dim}limebot start${colors.reset}
    ${colors.dim}limebot update${colors.reset}
    ${colors.dim}limebot update --check${colors.reset}
    ${colors.dim}limebot update --rollback${colors.reset}
    ${colors.dim}limebot update-check${colors.reset}
    ${colors.dim}limebot auth codex login${colors.reset}
    ${colors.dim}limebot start --quick${colors.reset}
    ${colors.dim}limebot doctor${colors.reset}
    ${colors.dim}limebot doctor --skip-tests${colors.reset}
    ${colors.dim}limebot doctor --skip-perf${colors.reset}
    ${colors.dim}limebot review-diff --diff-file change.patch --output review.json${colors.reset}
    ${colors.dim}limebot install-browser${colors.reset}
`);
}

async function cmdReviewDiff(args = []) {
    const scriptPath = path.join(rootDir, 'scripts', 'review_diff.py');
    if (!fs.existsSync(scriptPath)) {
        throw new Error(`Review helper not found at ${scriptPath}`);
    }
    const venvPython = venvPythonPath();
    const systemPython = await getSystemPython();
    const pythonCmd = fs.existsSync(venvPython) ? venvPython : systemPython;

    return new Promise((resolve, reject) => {
        const child = spawn(pythonCmd, [scriptPath, ...args], {
            cwd: rootDir,
            stdio: 'inherit',
            env: buildChildEnv(),
        });
        child.on('error', reject);
        child.on('close', (code) => {
            if (code !== 0) process.exitCode = code || 1;
            resolve(code || 0);
        });
    });
}

async function loadCodexAuthHelper() {
    const helperPath = path.join(rootDir, 'scripts', 'codex-oauth.mjs');
    if (!fs.existsSync(helperPath)) {
        throw new Error(`Codex auth helper not found at ${helperPath}`);
    }
    return import(pathToFileURL(helperPath).href);
}

function printCodexAuthStatus(status) {
    if (!status?.configured) {
        warning('Codex OAuth is not configured.');
        info('Run `limebot auth codex login` to sign in with ChatGPT OAuth.');
        info('Run `limebot auth codex import` to import an existing Codex CLI login.');
        return;
    }

    success(`Codex OAuth is configured${status.email ? ` for ${status.email}` : ''}.`);
    info(`Store: ${status.storePath}`);
    if (status.displayName) info(`Display name: ${status.displayName}`);
    if (status.source) info(`Source: ${status.source}`);
    if (status.updatedAt) info(`Updated: ${status.updatedAt}`);
    if (status.importedFrom) info(`Imported from: ${status.importedFrom}`);
    if (status.expiresAt) {
        const expiryLabel = status.expired ? 'Expired' : 'Expires';
        info(`${expiryLabel}: ${status.expiresAt}`);
    } else {
        info('Expires: unknown');
    }
}

async function cmdAuth(args = []) {
    const provider = args[0]?.toLowerCase();
    const action = args[1]?.toLowerCase() || 'status';

    if (provider !== 'codex') {
        error('Usage: limebot auth codex <login|import|status|logout>');
        process.exit(1);
    }

    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Codex Auth${colors.reset}\n`);
    const helper = await loadCodexAuthHelper();

    switch (action) {
        case 'login': {
            const status = await helper.loginCodexAuth();
            success('Codex OAuth login complete.');
            printCodexAuthStatus(status);
            break;
        }
        case 'import': {
            const status = await helper.importCodexCliAuth();
            success('Imported Codex CLI login.');
            printCodexAuthStatus(status);
            break;
        }
        case 'status': {
            const status = await helper.getCodexAuthStatus();
            printCodexAuthStatus(status);
            break;
        }
        case 'logout': {
            const removed = await helper.logoutCodexAuth();
            if (removed) {
                success('Removed stored Codex OAuth profile.');
            } else {
                info('No stored Codex OAuth profile was present.');
            }
            break;
        }
        default:
            error(`Unknown auth action '${action}'.`);
            info('Usage: limebot auth codex <login|import|status|logout>');
            process.exit(1);
    }

    console.log('');
}

async function cmdUpdateCheck(args = []) {
    const forceRefresh = !args.includes('--cached');

    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Update Check${colors.reset}\n`);
    const status = await getUpdateStatus({ forceRefresh });
    printUpdateStatus(status, { alwaysShowSummary: true });
    console.log('');
}

async function cmdDoctor(args = []) {
    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Doctor${colors.reset}\n`);
    let issues = 0;

    const runTests = !args.includes('--skip-tests');
    const skipPerf = args.includes('--skip-perf');

    const pythonCmd = await getSystemPython();
    if (await commandExists(pythonCmd)) {
        const pythonInfo = await getPythonRuntimeInfo(pythonCmd);
        if (pythonInfo.supported) {
            success(`Python installed: ${pythonInfo.versionText} (${pythonCmd})`);
        } else {
            error(`Unsupported Python installed: ${pythonInfo.versionText} (${pythonCmd})`);
            console.log(`\n${explainUnsupportedPython(pythonInfo)}\n`);
            issues++;
        }
    } else {
        error('Python not found in PATH'); issues++;
    }

    if (await commandExists('node')) {
        const nodeVersion = await getVersion('node', ['-v']);
        if (isSupportedNodeVersion(nodeVersion)) {
            success(`Node.js installed: ${nodeVersion}`);
        } else {
            error(`Unsupported Node.js installed: ${nodeVersion || 'unknown version'}`);
            console.log(`\n${explainUnsupportedNode(nodeVersion)}\n`);
            issues++;
        }
    } else {
        error('Node.js is not installed (the app is named Node.js).');
        console.log(`\n${explainUnsupportedNode('not installed')}\n`);
        issues++;
    }

    if (await commandExists('npm')) {
        const npmVersion = await getVersion('npm', ['-v']);
        if (npmVersion) {
            success(`npm installed: v${npmVersion.replace(/^v/, '')}`);
        } else {
            error('npm was found but its version could not be read.');
            issues++;
        }
    } else {
        error('npm not found in PATH'); issues++;
    }

    fs.existsSync(path.join(rootDir, '.env'))
        ? success('.env file exists')
        : warning('.env file not found (run setup first)');

    const venvLayout = resolveVenvLayout();
    fs.existsSync(venvLayout.venvDir)
        ? success(`Python virtual environment exists${venvLayout.usingFallback ? ` (${venvLayout.venvDir})` : ''}`)
        : warning(`Python virtual environment not created (will be created on first start${venvLayout.usingFallback ? ` at ${venvLayout.venvDir}` : ''})`);

    const venvPython = venvPythonPath();
    if (fs.existsSync(venvPython)) {
        const venvInfo = await getPythonRuntimeInfo(venvPython);
        if (venvInfo.supported) {
            success(`Venv Python: ${venvInfo.versionText} (${venvPython})`);
        } else {
            error(`Unsupported venv Python: ${venvInfo.versionText} (${venvPython})`);
            info(
                unsupportedPythonMessage(venvInfo, {
                    location: `The virtual environment at ${venvLayout.venvDir}`,
                    venvDir: venvLayout.venvDir,
                })
            );
            issues++;
        }
    }

    const npmInstallReady = fs.existsSync(path.join(rootDir, 'node_modules'))
        && fs.existsSync(path.join(rootDir, 'node_modules', '.package-lock.json'));
    npmInstallReady
        ? success('Core NPM dependencies installed (root + web)')
        : warning('Core NPM dependencies not installed (will be installed on first start)');

    const dependencyState = loadDependencyState(DEPENDENCY_STATE_PATH) || createDependencyState();
    const installedFeatures = Object.keys(dependencyState.features || {}).sort();
    installedFeatures.length > 0
        ? info(`Recorded optional features: ${installedFeatures.join(', ')}`)
        : info('Recorded optional features: none');
    if (fs.existsSync(venvPython) && !(await pythonModuleAvailable(venvPython, 'pytest'))) {
        warning('Development/test dependencies are not installed.');
        info(`Install them with: "${venvPython}" -m pip install -r requirements-dev.txt`);
    }

    console.log('');
    info('Checking ports...');
    const backendPort = getConfiguredPort('WEB_PORT', 8000);
    const frontendPort = getConfiguredPort('FRONTEND_PORT', 5173);
    for (const [port, label] of [[backendPort, 'backend'], [3000, 'WhatsApp bridge'], [frontendPort, 'frontend']]) {
        (await isPortReachable(port))
            ? warning(`Port ${port} is in use (${label} may already be running)`)
            : success(`Port ${port} is available`);
    }

    console.log('');
    info('Checking optional features...');
    (await checkPlaywrightBrowsers())
        ? success('Browser tool: Chromium installed')
        : info(`Browser tool: Not installed ${colors.dim}(run ${colors.cyan}limebot install-browser${colors.dim} to enable)${colors.reset}`);
    const videoPythonReady = fs.existsSync(venvPython)
        && await pythonModuleAvailable(venvPython, 'yt_dlp')
        && await pythonModuleAvailable(venvPython, 'PIL');
    const videoBinariesReady = (await missingVideoBinaries()).length === 0;
    const videoState = getVideoReadinessState({
        pythonDependenciesReady: videoPythonReady,
        binariesReady: videoBinariesReady,
    });
    if (videoState === 'python-dependencies-missing') {
        info(`Video analysis: Python dependencies missing ${colors.dim}(run ${colors.cyan}limebot feature install video${colors.dim})${colors.reset}`);
    } else if (videoState === 'ffmpeg-missing') {
        warning('Video analysis: FFmpeg/ffprobe missing');
        info(`Install them with: ${getVideoBinaryInstallInstructions()}`);
    } else {
        success('Video analysis: ready');
    }

    if (runTests) {
        console.log('');
        info('Running tests...');
        const venvPython = venvPythonPath();
        const systemPython = await getSystemPython();
        const py = fs.existsSync(venvPython) ? venvPython : systemPython;
        const env = { ...process.env };
        if (skipPerf) env.LIMEBOT_SKIP_PERF = '1';

        const testExit = await new Promise((resolve) => {
            const proc = spawn(py, ['-m', 'unittest', 'discover', '-s', 'tests'], {
                cwd: rootDir,
                stdio: 'inherit',
                env,
            });
            proc.on('close', (code) => resolve(code ?? 1));
            proc.on('error', () => resolve(1));
        });

        if (testExit === 0) {
            success('Tests passed');
        } else {
            error('Tests failed');
            issues++;
        }
    } else {
        info(`Skipping tests ${colors.dim}(--skip-tests)${colors.reset}`);
    }

    console.log('');
    issues === 0
        ? log(colors.green, `  ${colors.bright}All checks passed!${colors.reset} Run ${colors.cyan}limebot start${colors.reset} to launch.`)
        : log(colors.yellow, `  ${colors.bright}${issues} issue(s) found.${colors.reset} Please resolve before starting.`);
    process.exitCode = issues === 0 ? 0 : 1;
    console.log('');
}

async function ensureBrowserAndChromium() {
    await installOptionalFeature('browser');
    info('Checking if Chromium is already installed...');
    if (await checkPlaywrightBrowsers()) {
        success('Chromium is already installed and ready.');
        return;
    }

    info('Installing Chromium via Playwright...');
    console.log(`  ${colors.dim}This may take a few minutes...${colors.reset}\n`);

    const pythonCmd = venvPythonPath();

    const exitCode = await new Promise((resolve) => {
        const proc = spawn(pythonCmd, ['-m', 'playwright', 'install', 'chromium'], {
            cwd: rootDir, stdio: 'inherit',
        });
        proc.on('close', (code) => {
            console.log('');
            resolve(code ?? 1);
        });
        proc.on('error', () => resolve(1));
    });
    if (exitCode !== 0 || !(await checkPlaywrightBrowsers())) {
        throw new Error(
            `Chromium installation or launch verification failed (exit ${exitCode}). ` +
            `On Linux, install the packages Playwright lists, then retry; LimeBot never runs sudo automatically.`
        );
    }
    success('Chromium installed and launch-verified successfully!');
}

async function refreshDependenciesAfterUpdate() {
    const childEnv = buildChildEnv();
    const npmLock = path.join(rootDir, 'package-lock.json');
    if (fs.existsSync(npmLock)) {
        await runDependencyCommand(
            npmExecutable(),
            ['ci', '--ignore-scripts'],
            {
                env: childEnv,
                label: 'NPM dependency refresh',
                retryCommand: `${npmExecutable()} ci --ignore-scripts`,
            },
        );
    }

    const venvPython = venvPythonPath();
    const requirements = path.join(rootDir, 'requirements.txt');
    if (fs.existsSync(venvPython) && fs.existsSync(requirements)) {
        await runDependencyCommand(
            venvPython,
            ['-m', 'pip', 'install', '-r', 'requirements.txt', '--quiet'],
            {
                env: childEnv,
                label: 'Python dependency refresh',
                retryCommand: `"${venvPython}" -m pip install -r requirements.txt`,
            },
        );
    }
}

async function cmdUpdate(args = []) {
    const checkOnly = args.includes('--check') || args.includes('--cached');
    const rollback = args.includes('--rollback');
    const refreshDeps = !args.includes('--no-deps');

    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Update${colors.reset}\n`);
    if (rollback) {
        const result = await rollbackUpdate({ runGit, rootDir, fsImpl: fs });
        if (!result.ok) {
            error(result.message || 'Rollback was not applied.');
            process.exitCode = 1;
        } else {
            success('Rolled back the last guarded update.');
            info('Run `limebot start` to restart LimeBot on the restored source.');
        }
        console.log('');
        return;
    }

    if (checkOnly) {
        const status = await getUpdateStatus({ forceRefresh: !args.includes('--cached') });
        printUpdateStatus(status, { alwaysShowSummary: true });
        console.log('');
        return;
    }

    const result = await applyUpdate({ runGit, rootDir, fsImpl: fs });
    if (!result.ok) {
        error(result.message || `Update was not applied (${result.reason}).`);
        if (result.reason === 'code-dirty') {
            info('Use `limebot update --check` to review the files, then commit or copy source changes aside.');
        } else if (result.reason === 'diverged') {
            info('Automatic updates only fast-forward. Reinstall from a fresh checkout if you need to discard local commits.');
        }
        process.exitCode = 1;
        console.log('');
        return;
    }
    if (!result.updated) {
        success('LimeBot is already up to date.');
        if (result.worktree?.stateOnly) info('Local runtime state is preserved.');
        console.log('');
        return;
    }

    success(`Updated ${shortSha(result.record.previousHead)} -> ${shortSha(result.record.newHead)}.`);
    info(`Runtime backup: ${result.backup.backupDir}`);
    if (refreshDeps) {
        try {
            await refreshDependenciesAfterUpdate();
            success('Dependencies refreshed.');
        } catch (err) {
            warning(`Dependency refresh deferred: ${err.message}`);
            info('Run `limebot start` to retry dependency installation.');
        }
    }
    info('Restart LimeBot with `limebot start` to load the new source.');
    console.log('');
}

async function missingVideoBinaries(required = ['ffmpeg', 'ffprobe']) {
    const missing = [];
    for (const binary of required) {
        if (!(await commandExists(binary)) || !(await getVersion(binary, ['-version']))) {
            missing.push(binary);
        }
    }
    return missing;
}

async function cmdInstallBrowser() {
    console.log(`${colors.lime}${colors.bright}\n  🍋 Browser Tool Setup${colors.reset}\n`);
    await ensureBrowserAndChromium();
}

async function ensureFeatureVenv() {
    const venvDir = venvDirPath();
    const venvPython = venvPythonPath();
    if (fs.existsSync(venvPython)) {
        await ensureSupportedPython(venvPython, 'Virtual environment Python', venvDir);
        return venvPython;
    }
    const systemPython = await getSystemPython();
    await ensureSupportedPython(systemPython, 'System Python', venvDir);
    await ensureVenvExecutable({
        venvDir,
        venvPython,
        systemPython,
        fs,
        create: (python, target) => runDependencyCommand(python, ['-m', 'venv', target], {
            env: { ...process.env },
            label: 'Python virtual environment creation',
            retryCommand: `${python} -m venv "${target}"`,
        }),
        validate: (python) => ensureSupportedPython(python, 'Virtual environment Python', venvDir),
    });
    return venvPython;
}

async function installOptionalFeature(feature) {
    const name = String(feature || '').toLowerCase();
    if (!FEATURE_DEFINITIONS[name]) {
        throw new Error(`Unknown feature '${feature}'. Choose: ${Object.keys(FEATURE_DEFINITIONS).join(', ')}.`);
    }
    const definition = FEATURE_DEFINITIONS[name];
    const venvPython = definition.kind === 'python' ? await ensureFeatureVenv() : venvPythonPath();
    const spec = getFeatureInstallSpec(name, { rootDir, venvPython });
    const runtime = definition.kind === 'python'
        ? (await ensureSupportedPython(venvPython, 'Virtual environment Python', venvDirPath())).versionText
        : process.version;
    const fingerprint = buildFeatureFingerprint({ manifestPath: spec.manifestPath, runtime });
    const state = loadDependencyState(DEPENDENCY_STATE_PATH) || createDependencyState();
    const nodeFeatureSentinels = {
        whatsapp: path.join(rootDir, 'node_modules', '@whiskeysockets', 'baileys', 'package.json'),
        extension: path.join(rootDir, 'node_modules', '@types', 'chrome', 'package.json'),
    };
    const sentinels = definition.kind === 'node'
        ? [path.join(rootDir, 'node_modules', '.package-lock.json'), nodeFeatureSentinels[name]]
        : [venvPython];
    if (isFeatureCurrent(state, name, fingerprint, sentinels)) {
        if (name === 'video') {
            const missing = await missingVideoBinaries(definition.requiredBinaries || []);
            if (missing.length) {
                throw new Error(
                    `Video Python dependencies are current, but ${missing.join(' and ')} are missing. ` +
                    `Install FFmpeg and retry:\n  ${getVideoBinaryInstallInstructions()}`
                );
            }
        }
        success(`${name} feature dependencies unchanged.`);
        return;
    }
    info(`Installing optional ${name} feature...`);
    const command = spec.command === 'npm' && process.platform === 'win32' ? 'npm.cmd' : spec.command;
    await runDependencyCommand(command, spec.args, {
        env: buildChildEnv(),
        label: `${name} feature installation`,
        retryCommand: `${spec.command} ${spec.args.join(' ')}`,
    });
    if (name === 'video') {
        const missing = await missingVideoBinaries(definition.requiredBinaries || []);
        if (missing.length) {
            throw new Error(
                `Video Python dependencies installed, but ${missing.join(' and ')} are missing. ` +
                `Install FFmpeg and retry:\n  ${getVideoBinaryInstallInstructions()}`
            );
        }
    }
    writeDependencyStateAtomic(
        DEPENDENCY_STATE_PATH,
        recordFeatureInstall(state, name, fingerprint),
    );
    success(`${name} feature installed.`);
}

async function cmdFeature(args) {
    const action = String(args[0] || '').toLowerCase();
    const name = String(args[1] || '').toLowerCase();
    if (action !== 'install' || !name) {
        throw new Error(`Usage: limebot feature install <${Object.keys(FEATURE_DEFINITIONS).join('|')}|all>`);
    }
    if (name === 'all') {
        console.log(`${colors.lime}${colors.bright}\n  🍋 Full Optional Feature Setup${colors.reset}\n`);
    }
    await installRequestedFeatureSet(name, {
        installFeature: installOptionalFeature,
        ensureBrowser: ensureBrowserAndChromium,
    });
    if (name === 'all') {
        success('All optional LimeBot features are installed and ready.');
    }
}

async function cmdSkill(args) {
    const subCommand = args[0]?.toLowerCase() || 'list';

    // FIX: allowlist subcommands to prevent shell injection via user-supplied args
    const VALID_SUBCMDS = new Set(['list', 'install', 'uninstall', 'update']);
    if (!VALID_SUBCMDS.has(subCommand)) {
        error(`Unknown skill subcommand '${subCommand}'. Valid: ${[...VALID_SUBCMDS].join(', ')}`);
        process.exit(1);
    }

    const venvPython = venvPythonPath();
    const systemPython = await getSystemPython();
    const pythonCmd = fs.existsSync(venvPython) ? venvPython : systemPython;

    // FIX: pass args as array (no shell:true) so user input can't be interpreted as shell syntax
    return new Promise((resolve) => {
        const proc = spawn(pythonCmd, ['-m', 'core.skill_installer', subCommand, ...args.slice(1)], {
            cwd: rootDir,
            stdio: 'inherit',
            // shell: false (default) — intentionally NOT using shell to avoid injection
        });
        proc.on('close', (code) => {
            if (code !== 0 && subCommand !== 'list') {
                console.log(`\n  ${colors.dim}Run ${colors.cyan}limebot skill${colors.dim} for usage.${colors.reset}\n`);
            }
            resolve();
        });
        proc.on('error', (err) => {
            error(`Failed to run skill installer: ${err.message}`);
            resolve();
        });
    });
}

async function cmdPlugin(args) {
    const subCommand = args[0]?.toLowerCase() || 'list';
    const VALID_SUBCMDS = new Set(['list', 'install', 'uninstall']);
    if (!VALID_SUBCMDS.has(subCommand)) {
        error(`Unknown plugin subcommand '${subCommand}'. Valid: ${[...VALID_SUBCMDS].join(', ')}`);
        process.exit(1);
    }

    const venvPython = venvPythonPath();
    const systemPython = await getSystemPython();
    const pythonCmd = fs.existsSync(venvPython) ? venvPython : systemPython;

    return new Promise((resolve) => {
        const proc = spawn(pythonCmd, ['-m', 'core.plugin_installer', subCommand, ...args.slice(1)], {
            cwd: rootDir,
            stdio: 'inherit',
        });
        proc.on('close', (code) => {
            if (code !== 0 && subCommand !== 'list') {
                console.log(`\n  ${colors.dim}Run ${colors.cyan}limebot plugin${colors.dim} for usage.${colors.reset}\n`);
            }
            resolve();
        });
        proc.on('error', (err) => {
            error(`Failed to run plugin installer: ${err.message}`);
            resolve();
        });
    });
}

async function cmdStatus() {
    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Status${colors.reset}\n`);

    const backendPort = getConfiguredPort('WEB_PORT', 8000);
    const frontendPort = getConfiguredPort('FRONTEND_PORT', 5173);
    const [backendUp, bridgeUp, frontendUp] = await Promise.all([
        isPortReachable(backendPort), isPortReachable(3000), isPortReachable(frontendPort),
    ]);

    backendUp ? success(`Backend is running (port ${backendPort})`) : info('Backend is not running');
    bridgeUp ? success('WhatsApp bridge is running (port 3000)') : info('WhatsApp bridge is not running');
    frontendUp ? success(`Frontend is running (port ${frontendPort})`) : info('Frontend is not running');

    console.log('');
    if (backendUp && frontendUp && bridgeUp) {
        log(colors.green, `  LimeBot is fully operational! Open ${colors.cyan}http://localhost:${frontendPort}${colors.reset}`);
    } else if (!backendUp && !frontendUp && !bridgeUp) {
        log(colors.gray, `  LimeBot is not running. Use ${colors.cyan}limebot start${colors.reset} to launch.`);
    } else {
        log(colors.yellow, '  LimeBot is partially running.');
    }
    console.log('');
}

async function cmdLogs(args) {
    const lines = parseInt(args[0]) || 50;
    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Logs (last ${lines} lines)${colors.reset}\n`);

    const logFile = path.join(rootDir, 'logs', 'limebot.log');
    if (!fs.existsSync(logFile)) {
        info('No log file found. Start LimeBot to generate logs.');
        console.log('');
        return;
    }

    // FIX: read from end of file instead of loading everything into memory
    try {
        const stat = fs.statSync(logFile);
        const chunkSize = Math.min(stat.size, 128 * 1024); // read up to 128 KB from end
        const fd = fs.openSync(logFile, 'r');
        const buf = Buffer.alloc(chunkSize);
        fs.readSync(fd, buf, 0, chunkSize, stat.size - chunkSize);
        fs.closeSync(fd);

        const recent = buf.toString('utf-8').split('\n').filter(l => l.trim()).slice(-lines);
        if (recent.length === 0) {
            info('Log file is empty.');
        } else {
            console.log(colors.gray + '  --- Recent Logs ---' + colors.reset);
            for (const line of recent) console.log(`  ${line}`);
            console.log(colors.gray + '  --- End of Logs ---' + colors.reset);
        }
    } catch (e) {
        error(`Failed to read log file: ${e.message}`);
    }
    console.log('');
}

async function cmdStop() {
    console.log(`${colors.lime}${colors.bright}\n  🍋 Stopping LimeBot${colors.reset}\n`);

    const backendPort = getConfiguredPort('WEB_PORT', 8000);
    const frontendPort = getConfiguredPort('FRONTEND_PORT', 5173);

    // 1. Stop primary services by port (standard behavior)
    const primaryPorts = [
        [backendPort, 'backend'],
        [frontendPort, 'frontend'],
        [3000, 'WhatsApp bridge']
    ];

    let anyKilled = false;
    for (const [port, label] of primaryPorts) {
        const killed = await killPort(port);
        killed ? success(`Stopped ${label} (port ${port})`) : info(`${label} was not running`);
        if (killed) anyKilled = true;
    }




    if (process.platform === 'win32') {
        try {
            // Aggressive pattern-based cleanup for Windows orphans
            // Using 'call terminate' is often more reliable than 'delete'
            execSync('wmic process where "commandline like \'%main.py%\' or commandline like \'%bridge/dist/index.js%\' or commandline like \'%vite%\'" call terminate', { stdio: 'ignore' });
        } catch (e) { /* ignore */ }
    } else {

        try {
            execSync('pkill -f "main.py"', { stdio: 'ignore' });
            execSync('pkill -f "bridge/dist/index.js"', { stdio: 'ignore' });
            execSync('pkill -f "vite"', { stdio: 'ignore' });
            anyKilled = true;
        } catch (e) { }
    }

    if (!anyKilled) {
        info('No running LimeBot processes found.');
    } else {
        success('Deep clean complete: All LimeBot instances terminated.');
    }

    try {
        const result = cleanupStoppedTaskState({
            tasksPath: path.join(rootDir, 'data', 'tasks.json'),
        });
        if (result.cleanedTasks || result.cleanedWorkspaces) {
            success(
                `Cleared ${result.cleanedTasks} active task(s) and ${result.cleanedWorkspaces} active workspace(s).`
            );
        }
    } catch (e) {
        error(`Failed to clear task state: ${e.message}`);
    }

    // Give OS time to release ports
    await sleep(500);
    console.log('');
}

async function cmdAutorun(args) {
    const action = args[0]?.toLowerCase();
    if (action !== 'enable' && action !== 'disable') {
        error("Usage: limebot autorun <enable|disable>");
        process.exit(1);
    }

    console.log(`${colors.lime}${colors.bright}\n  🍋 LimeBot Autorun Configuration${colors.reset}\n`);

    if (process.platform === 'win32') {
        const taskName = "LimeBotGateway";
        const gatewayPath = path.join(rootDir, 'bin', 'gateway.cmd');

        if (action === 'enable') {
            info(`Creating Windows Scheduled Task: ${taskName}`);
            try {
                execSync(
                    `schtasks /create /tn "${taskName}" /tr "${gatewayPath}" /sc onlogon /RL HIGHEST /f`,
                    { stdio: 'pipe' }
                );
                success("Autorun enabled! LimeBot will start whenever you log in.");
            } catch (e) {
                const errorLog = (e.stdout?.toString() || "") + (e.stderr?.toString() || "");
                if (errorLog.toLowerCase().includes('acceso denegado') || errorLog.toLowerCase().includes('access is denied') || e.status === 1) {
                    error("Access Denied: Creating a Scheduled Task requires Administrator privileges.");
                    log(colors.yellow, "  Please restart your terminal (PowerShell/CMD) as Administrator and run the command again.");
                } else {
                    error(`Failed to enable autorun: ${e.message}`);
                }
            }
        } else {
            info(`Removing Windows Scheduled Task: ${taskName}`);
            try {
                execSync(`schtasks /delete /tn "${taskName}" /f`, { stdio: 'inherit' });
                success("Autorun disabled (Scheduled Task removed).");
            } catch (e) {
                error(`Could not disable autorun: ${e.message}`);
                log(colors.yellow, "  Note: You may need to run this command as Administrator.");
            }
        }
    } else if (process.platform === 'darwin') {
        const label = "com.limebot.gateway";
        const plistPath = path.join(process.env.HOME, 'Library', 'LaunchAgents', `${label}.plist`);
        const gatewayPath = path.join(rootDir, 'bin', 'gateway.sh');

        if (action === 'enable') {
            info(`Creating macOS LaunchAgent: ${label}`);
            const plistContent = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${gatewayPath}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>${rootDir}</string>
    <key>StandardOutPath</key>
    <string>${path.join(rootDir, 'logs', 'gateway.log')}</string>
    <key>StandardErrorPath</key>
    <string>${path.join(rootDir, 'logs', 'gateway.log')}</string>
</dict>
</plist>`;
            try {
                const launchAgentsDir = path.join(process.env.HOME, 'Library', 'LaunchAgents');
                if (!fs.existsSync(launchAgentsDir)) fs.mkdirSync(launchAgentsDir, { recursive: true });
                fs.writeFileSync(plistPath, plistContent);
                execSync(`launchctl load "${plistPath}"`, { stdio: 'inherit' });
                success("Autorun enabled! LimeBot will start automatically on login.");
            } catch (e) {
                error(`Failed to enable autorun: ${e.message}`);
            }
        } else {
            info(`Removing macOS LaunchAgent: ${label}`);
            try {
                if (fs.existsSync(plistPath)) {
                    execSync(`launchctl unload "${plistPath}"`, { stdio: 'inherit' });
                    fs.unlinkSync(plistPath);
                }
                success("Autorun disabled.");
            } catch (e) {
                error(`Failed to disable autorun: ${e.message}`);
            }
        }
    } else {
        // Linux (systemd)
        const serviceName = "limebot.service";
        const homeDir = process.env.HOME;
        const systemdDir = path.join(homeDir, '.config', 'systemd', 'user');
        const servicePath = path.join(systemdDir, serviceName);
        const gatewayPath = path.join(rootDir, 'bin', 'gateway.sh');

        if (action === 'enable') {
            info(`Creating systemd user service: ${serviceName}`);
            const serviceContent = `[Unit]
Description=LimeBot 24/7 assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${rootDir}
ExecStart=${gatewayPath}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
`;
            try {
                if (!fs.existsSync(systemdDir)) fs.mkdirSync(systemdDir, { recursive: true });
                fs.writeFileSync(servicePath, serviceContent);
                execSync('systemctl --user daemon-reload', { stdio: 'inherit' });
                execSync('systemctl --user enable limebot', { stdio: 'inherit' });
                execSync('systemctl --user start limebot', { stdio: 'inherit' });
                success("Autorun enabled! LimeBot is now running as a systemd user service.");
            } catch (e) {
                error(`Failed to enable autorun: ${e.message}`);
            }
        } else {
            info(`Removing systemd user service: ${serviceName}`);
            try {
                execSync('systemctl --user stop limebot', { stdio: 'inherit' });
                execSync('systemctl --user disable limebot', { stdio: 'inherit' });
                if (fs.existsSync(servicePath)) fs.unlinkSync(servicePath);
                execSync('systemctl --user daemon-reload', { stdio: 'inherit' });
                success("Autorun disabled.");
            } catch (e) {
                error(`Failed to disable autorun: ${e.message}`);
            }
        }
    }
    console.log('');
}

async function cmdStart(args, updateStatus = null) {
    process.stdout.write(LOGO);
    const quickMode = args.includes('--quick') || args.includes('-q');
    const backendOnly = args.includes('--backend-only');
    const frontendOnly = args.includes('--frontend-only');

    if (backendOnly && frontendOnly) {
        error('--backend-only and --frontend-only cannot be used together.');
        process.exit(1);
    }

    console.log(`${colors.lime}${colors.bright}\n  🍋 Starting LimeBot${colors.reset}\n`);
    log(colors.gray, `  ${colors.bright}Tip:${colors.reset} Run ${colors.cyan}npm run lime-bot help${colors.reset} to see all available CLI commands.`);
    if (quickMode) {
        info('Quick mode: Skipping dependency and update checks...');
    } else if (updateStatus) {
        printUpdateStatus(updateStatus, { alwaysShowSummary: true });
    } else {
        info('Update status will be checked in the background.');
    }

    const envFile = path.join(rootDir, '.env');
    const isConfigured = fs.existsSync(envFile);
    if (!isConfigured) warning('Initial configuration not found. Setup wizard will open.');

    const venvLayout = resolveVenvLayout();
    const venvDir = venvLayout.venvDir;
    const venvPython = venvPythonPath();
    let systemPython = null;
    let backendPython = venvPython;
    const configuredBackendPort = getConfiguredPort('WEB_PORT', 8000);
    const configuredFrontendPort = getConfiguredPort('FRONTEND_PORT', 5173);
    const configuredBackendPortCheck = frontendOnly
        ? { available: true }
        : await checkPortAvailability(configuredBackendPort);
    if (!configuredBackendPortCheck.available) {
        warning(
            `Backend port ${configuredBackendPort} cannot be bound: ` +
            describePortBindFailure(configuredBackendPortCheck)
        );
    }
    const backendPort = frontendOnly
        ? configuredBackendPort
        : await findAvailablePort(configuredBackendPort, 100);
    const frontendReservedPorts = new Set([backendPort]);
    const configuredFrontendPortCheck = backendOnly || frontendReservedPorts.has(configuredFrontendPort)
        ? { available: true }
        : await checkPortAvailability(configuredFrontendPort);
    if (!configuredFrontendPortCheck.available) {
        warning(
            `Frontend port ${configuredFrontendPort} cannot be bound: ` +
            describePortBindFailure(configuredFrontendPortCheck)
        );
    }
    const frontendPort = backendOnly
        ? configuredFrontendPort
        : await findAvailablePort(configuredFrontendPort, 100, frontendReservedPorts);

    if (backendPort !== configuredBackendPort) {
        warning(`Backend port ${configuredBackendPort} is busy. Using ${backendPort}.`);
    }
    if (frontendPort !== configuredFrontendPort) {
        warning(`Frontend port ${configuredFrontendPort} is busy. Using ${frontendPort}.`);
    }
    if (venvLayout.usingFallback) {
        warning(`Windows path safety: using venv at ${venvDir} (projected max path ${venvLayout.projectedMaxPathLength}/${WIN_MAX_PATH_SAFE})`);
    }

    if (!frontendOnly) {
        const venvState = fs.existsSync(venvDir)
            ? (fs.existsSync(venvPython) ? 'valid' : 'incomplete')
            : 'missing';
        if (venvState === 'valid') {
            await ensureSupportedPython(venvPython, 'Virtual environment Python', venvDir);
        } else {
            systemPython = await getSystemPython();
            await ensureSupportedPython(systemPython, 'System Python', venvDir);
            if (venvState === 'incomplete') {
                warning(`Incomplete virtual environment found at ${venvDir}; preserving and repairing it.`);
            }
            await runWithSpinner('Creating Python virtual environment...', () => {
                return ensureVenvExecutable({
                    venvDir,
                    venvPython,
                    systemPython,
                    fs,
                    create: (python, target) => runDependencyCommand(
                        python,
                        ['-m', 'venv', target],
                        {
                            env: { ...process.env },
                            label: 'Python virtual environment creation',
                            retryCommand: `${python} -m venv "${target}"`,
                        },
                    ),
                    validate: (python) => ensureSupportedPython(
                        python, 'Virtual environment Python', venvDir
                    ),
                });
            });
        }
        backendPython = venvPython;
    }

    const childEnv = buildChildEnv();

    childEnv.WEB_PORT = String(backendPort);
    childEnv.PORT = String(backendPort);
    childEnv.VITE_DEV_SERVER_PORT = String(frontendPort);
    childEnv.FRONTEND_PORT = String(frontendPort);
    childEnv.VITE_BACKEND_URL = `http://127.0.0.1:${backendPort}`;
    childEnv.VITE_BACKEND_WS_URL = `ws://127.0.0.1:${backendPort}`;
    childEnv.VITE_API_BASE_URL = `http://127.0.0.1:${backendPort}`;
    childEnv.VITE_WS_BASE_URL = `ws://127.0.0.1:${backendPort}`;
    // Override CORS origins so the backend accepts requests from the actual frontend port
    childEnv.WEB_ALLOWED_ORIGINS = [
        `http://localhost:${frontendPort}`,
        `http://127.0.0.1:${frontendPort}`,
    ].join(',');

    // ── Dependency installation ──────────────────────────────────

    if (!quickMode) {
        const dependencyStartedAt = Date.now();
        let dependencyState = loadDependencyState(DEPENDENCY_STATE_PATH) || createDependencyState();
        const nodeModules = path.join(rootDir, 'node_modules');
        const npmSentinels = [
            nodeModules,
            path.join(nodeModules, '.package-lock.json'),
        ];
        const npmFingerprint = buildNpmFingerprint({
            lockfilePath: path.join(rootDir, 'package-lock.json'),
            nodeVersion: process.version,
        });
        const npmDecision = evaluateDependencyState(
            'npm', dependencyState.npm, npmFingerprint, npmSentinels
        );

        if (!npmDecision.installRequired) {
            success('NPM dependencies unchanged.');
        }

        // FIX: venv is for the backend — only needed when NOT frontend-only
        let pythonFingerprint = null;
        let pythonDecision = null;
        if (!frontendOnly) {
            const pythonInfo = await ensureSupportedPython(
                venvPython, 'Virtual environment Python', venvDir
            );
            pythonFingerprint = buildPythonFingerprint({
                requirementsPath: path.join(rootDir, 'requirements.txt'),
                venvPython,
                pythonVersion: pythonInfo.version,
            });
            pythonDecision = evaluateDependencyState(
                'python', dependencyState.python, pythonFingerprint, [venvPython]
            );

            if (!pythonDecision.installRequired) {
                success('Python dependencies unchanged.');
            }
        }
        const lanes = {};
        if (npmDecision.installRequired) {
            const npmSpec = getCoreNpmInstallSpec({ clean: !fs.existsSync(nodeModules) });
            info(`Refreshing core NPM dependencies: ${npmDecision.reason}.`);
            lanes.npm = async () => {
                await runDependencyCommand(
                    process.platform === 'win32' ? 'npm.cmd' : npmSpec.command,
                    npmSpec.args,
                    {
                        env: childEnv,
                        label: 'Core NPM dependency installation',
                        retryCommand: `${npmSpec.command} ${npmSpec.args.join(' ')}`,
                    },
                );
                return buildNpmFingerprint({
                    lockfilePath: path.join(rootDir, 'package-lock.json'),
                    nodeVersion: process.version,
                });
            };
        }
        if (pythonDecision?.installRequired) {
            info(`Refreshing core Python dependencies: ${pythonDecision.reason}.`);
            lanes.python = async () => {
                await runVenvPip(venvPython, ['install', '-r', 'requirements.txt', '--quiet'], {
                    run: (command, commandArgs) => runDependencyCommand(command, commandArgs, {
                        env: childEnv,
                        label: 'Core Python dependency installation',
                        retryCommand: `"${venvPython}" -m pip install -r requirements.txt`,
                    }),
                });
                return pythonFingerprint;
            };
        }
        const { successes: installed, failures } = await settleDependencyLanes(lanes);
        if (installed.npm) {
            dependencyState = recordSuccessfulInstall(dependencyState, 'npm', installed.npm);
            dependencyState = clearFeatures(dependencyState, ['whatsapp', 'extension']);
        }
        if (installed.python) {
            dependencyState = recordSuccessfulInstall(dependencyState, 'python', installed.python);
        }
        if (Object.keys(installed).length > 0) {
            writeDependencyStateAtomic(DEPENDENCY_STATE_PATH, dependencyState);
        }
        for (const [lane, failure] of Object.entries(failures)) {
            error(`${lane} dependency lane failed: ${failure?.message || failure}`);
        }
        if (Object.keys(failures).length > 0) {
            throw new Error(`Dependency installation failed in: ${Object.keys(failures).join(', ')}`);
        }
        success(`Dependency phase completed in ${Date.now() - dependencyStartedAt} ms.`);
    }

    // ── Process management ────────────────────────────────────────

    let backendProc = null;
    let frontendProc = null;
    let bridgeProc = null;

    const startBackend = async () => {
        // FIX: removed duplicate info('Starting backend...') line
        if (backendProc) {
            info('Stopping existing backend...');
            killProc(backendProc);
            backendProc = null;

            info(`Waiting for port ${backendPort} to be released...`);
            for (let i = 0; i < 10; i++) {
                if (!await isPortReachable(backendPort)) break;
                await sleep(500);
            }
            (await isPortReachable(backendPort))
                ? warning(`Port ${backendPort} still in use — startup might fail.`)
                : success(`Port ${backendPort} is free.`);
        }

        info('Starting backend...');
        backendProc = spawn(backendPython, ['main.py'], { cwd: rootDir, shell: true, stdio: 'inherit', env: childEnv });
        backendProc.on('error', (err) => error(`Backend failed to start: ${err.message}`));
        backendProc.on('exit', (code) => {
            if (code !== null && code !== 0) error(`Backend exited with code ${code}`);
            else info('Backend stopped.');
        });
    };

    const updateBridgeState = async () => {
        const enabled = isWhatsAppEnabled();

        if (enabled && !bridgeProc) {
            await installFeatureThen({
                install: () => installOptionalFeature('whatsapp'),
                next: async () => {
                    const bridgeDir = path.join(rootDir, 'bridge');
                    if (fs.existsSync(bridgeDir) && !fs.existsSync(path.join(bridgeDir, 'dist', 'index.js'))) {
                        await runWithSpinner('Building WhatsApp bridge...', () => {
                            return new Promise((resolve, reject) => {
                                const p = spawn('npm', ['run', 'build'], { cwd: bridgeDir, shell: true, stdio: 'pipe', env: childEnv });
                                p.on('close', (code) => code === 0 ? resolve() : reject(new Error(`bridge build failed (code ${code})`)));
                            });
                        });
                    }
                    info('WhatsApp enabled. Starting bridge...');

                    bridgeProc = spawn('node', ['dist/index.js'], {
                        cwd: path.join(rootDir, 'bridge'), shell: true, stdio: 'inherit', env: childEnv,
                    });
                    bridgeProc.on('error', (err) => { error(`WhatsApp bridge error: ${err.message}`); bridgeProc = null; });
                    bridgeProc.on('exit', () => { bridgeProc = null; });

                    if (backendProc) {
                        info('Restarting backend to connect to bridge...');
                        await startBackend();
                    }
                },
            });

        } else if (!enabled && bridgeProc) {
            info('WhatsApp disabled. Stopping bridge...');
            killProc(bridgeProc);
            bridgeProc = null;
        }
    };

    // ── Startup sequence ──────────────────────────────────────────

    let bridgeUpdate = Promise.resolve();
    const scheduleBridgeUpdate = () => {
        bridgeUpdate = bridgeUpdate.then(updateBridgeState);
        return bridgeUpdate;
    };
    await scheduleBridgeUpdate();
    if (!frontendOnly && !backendProc) await startBackend();
    try {
        watchConfigFile({
            directory: rootDir,
            filename: '.env',
            debounceMs: 1000,
            onChange: () => scheduleBridgeUpdate().catch((err) => {
                error(`Could not apply WhatsApp configuration: ${err.message}`);
            }),
        });
        info('Watching project configuration (including first .env creation)...');
    } catch {
        warning('Could not watch .env — restart to apply config changes.');
    }

    // ── Wait for backend before starting frontend ─────────────────
    // Combined startup only waits for process liveness. Capability readiness
    // continues in the backend while the UI becomes available.
    let backendStartupStatus = null;
    const waitTarget = startupWaitTarget({ backendOnly, frontendOnly });
    if (waitTarget !== 'none') {
        const spinner = new Spinner('Waiting for backend process...');
        spinner.start();
        backendStartupStatus = waitTarget === 'readiness'
            ? await waitForBackendReadiness(backendPort, {
                configured: isConfigured,
                apiKey: readEnvValue('APP_API_KEY'),
                maxAttempts: 30,
                onPhase: (phase) => spinner.update(`Preparing backend: ${phase}...`),
            })
            : await waitForBackendLiveness(backendPort, {
                maxAttempts: 60,
                intervalMs: 250,
                requestTimeoutMs: 250,
            });
        if (backendStartupStatus.ready) {
            const suffix = backendStartupStatus.status === 'degraded'
                ? ` (degraded: ${(backendStartupStatus.degraded_reasons || []).join(', ')})`
                : '';
            spinner.stop(`Backend capabilities are ready${suffix}.`);
        } else if (backendStartupStatus.status === 'setup') {
            spinner.stop('Backend is live; first-run setup is ready.');
        } else if (backendStartupStatus.live) {
            spinner.stop(waitTarget === 'liveness'
                ? 'Backend is live; capabilities may still be loading.'
                : `Backend is live but capabilities are ${backendStartupStatus.status} (${backendStartupStatus.phase}).`,
                waitTarget === 'liveness');
        } else {
            spinner.stop('Backend did not respond in 30 s - continuing in diagnostic mode.', false);
        }
    }

    let actualFrontendPort = frontendPort;

    if (!backendOnly) {
        info('Starting frontend dev server...');
        frontendProc = spawn('npm', ['run', 'dev'], {
            cwd: path.join(rootDir, 'web'), shell: true, stdio: 'pipe', env: childEnv,
        });
        frontendProc.on('error', (err) => error(`Frontend failed to start: ${err.message}`));


        const portPattern = /localhost:(\d+)/;
        for (const stream of [frontendProc.stdout, frontendProc.stderr]) {
            if (!stream) continue;
            stream.on('data', (data) => {
                const text = data.toString();
                process.stdout.write(text);
                const match = text.match(portPattern);
                if (match) actualFrontendPort = parseInt(match[1], 10);
            });
        }
    }

    if (!backendOnly) {
        info('Waiting for UI to be ready...');
        // Probe immediately; waitForServer already retries while Vite binds.
        if (await waitForServer(actualFrontendPort)) {
            const url = isConfigured
                ? `http://localhost:${actualFrontendPort}`
                : `http://localhost:${actualFrontendPort}/setup`;
            success(`LimeBot is ready at ${url}`);
            openBrowser(url);
        } else {
            error('Timeout waiting for UI. Check logs above.');
        }
    } else {
        if (backendStartupStatus?.ready) {
            success(`Backend ready on port ${backendPort}`);
        } else if (backendStartupStatus?.live) {
            warning(`Backend is live on port ${backendPort}, but agent capabilities are not ready.`);
        } else {
            error('Backend did not start in time. Check logs.');
        }
    }

    if (shouldDiscoverUpdates({ quickMode })) {
        void startBackgroundUpdateDiscovery({
            discover: () => getUpdateStatus({ forceRefresh: true }),
            onStatus: (status) => {
                if (status?.hasUpdate) printUpdateStatus(status);
            },
            onError: () => { },
        });
    }

    let cleaned = false;
    const cleanup = () => {
        if (cleaned) return;
        cleaned = true;

        log(colors.gray, '\n  Stopping LimeBot...');
        killProc(backendProc);
        killProc(frontendProc);
        killProc(bridgeProc);
        process.exitCode = 0;
    };
    process.once('SIGINT', cleanup);
    process.once('SIGTERM', cleanup);
    process.once('exit', cleanup);
}

// ── Entry point ───────────────────────────────────────────────────

async function main() {
    const previewArgs = process.argv.slice(2);
    const previewCommand = previewArgs[0]?.toLowerCase() || 'help';
    const nodeOptionalCommands = new Set([
        'help', '--help', '-h', 'setup', 'doctor', 'status', 'logs', 'stop',
    ]);
    if (!isSupportedNodeVersion(process.version)) {
        if (!nodeOptionalCommands.has(previewCommand)) {
            error(`LimeBot cannot start yet.`);
            console.log(`\n${explainUnsupportedNode(process.version)}\n`);
            process.exit(1);
        }
        warning(`This computer has Node.js ${process.version}. Starting LimeBot still needs 22.19+.`);
        console.log(`\n${explainUnsupportedNode(process.version)}\n`);
    }

    await refreshWindowsProcessPath();

    const args = previewArgs;
    const command = previewCommand;
    const updateStatusCommands = new Set(['start', 'status', 'doctor', 'skill', 'install-browser']);
    const quickStart = command === 'start' && (args.includes('--quick') || args.includes('-q'));
    const updateStatus = command === 'start'
        ? (quickStart ? null : readRecentUpdateCache(UPDATE_CHECK_CACHE_PATH, {
            fs,
            ttlMs: UPDATE_CHECK_TTL_MS,
        }))
        : (updateStatusCommands.has(command) ? await getUpdateStatus() : null);
    if (command !== 'start' && updateStatusCommands.has(command)) {
        printUpdateStatus(updateStatus);
    }

    switch (command) {
        case 'setup': await cmdSetup(args.slice(1)); break;
        case 'start': await cmdStart(args.slice(1), updateStatus); break;
        case 'stop': await cmdStop(); break;
        case 'status': await cmdStatus(); break;
        case 'update': await cmdUpdate(args.slice(1)); break;
        case 'update-check': await cmdUpdateCheck(args.slice(1)); break;
        case 'auth': await cmdAuth(args.slice(1)); break;
        case 'doctor': await cmdDoctor(args.slice(1)); break;
        case 'logs': await cmdLogs(args.slice(1)); break;
        case 'review-diff': await cmdReviewDiff(args.slice(1)); break;
        case 'install-browser': await cmdInstallBrowser(); break;
        case 'feature': await cmdFeature(args.slice(1)); break;
        case 'skill': await cmdSkill(args.slice(1)); break;
        case 'plugin': await cmdPlugin(args.slice(1)); break;
        case 'autorun': await cmdAutorun(args.slice(1)); break;
        case 'help': case '--help': case '-h':
            await cmdHelp(); break;
        default:
            error(`Unknown command '${command}'`);
            console.log(`  Run ${colors.cyan}limebot help${colors.reset} for available commands.\n`);
            process.exit(1);
    }
}

main().catch(err => {
    error(`Fatal: ${err.message}`);
    process.exit(1);
});
