export const MIN_NODE_VERSION = Object.freeze({ major: 22, minor: 19, patch: 0 });

export function parseNodeVersion(value) {
    const match = String(value || '').trim().match(/^v?(\d+)\.(\d+)\.(\d+)/);
    if (!match) return null;
    return {
        major: Number(match[1]),
        minor: Number(match[2]),
        patch: Number(match[3]),
    };
}

export function isSupportedNodeVersion(value) {
    const parsed = parseNodeVersion(value);
    if (!parsed) return false;
    const current = [parsed.major, parsed.minor, parsed.patch];
    const minimum = [MIN_NODE_VERSION.major, MIN_NODE_VERSION.minor, MIN_NODE_VERSION.patch];
    for (let index = 0; index < current.length; index += 1) {
        if (current[index] !== minimum[index]) return current[index] > minimum[index];
    }
    return true;
}

export function describeSupportedNode() {
    const { major, minor, patch } = MIN_NODE_VERSION;
    return `Node.js ${major}.${minor}.${patch} or newer`;
}

export function explainUnsupportedNode(foundVersion) {
    const found = String(foundVersion || '').trim() || 'an unknown version';
    return [
        `LimeBot needs the app called Node.js, version ${MIN_NODE_VERSION.major}.${MIN_NODE_VERSION.minor} or newer.`,
        `This computer currently has ${found}.`,
        '',
        'Do this next (no command-line experience needed):',
        '1. Open https://nodejs.org in your web browser.',
        `2. Download the ${MIN_NODE_VERSION.major} LTS installer (${MIN_NODE_VERSION.major}.${MIN_NODE_VERSION.minor} or newer) and run it.`,
        '3. Close every terminal window, open a new one, and type: node --version',
        '4. Come back to the LimeBot folder and run: npm start',
        '',
        'LimeBot did not change any files yet.',
    ].join('\n');
}
