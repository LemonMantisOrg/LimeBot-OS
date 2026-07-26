export type PetState =
    | 'idle'
    | 'thinking'
    | 'working'
    | 'approval'
    | 'warning'
    | 'celebrating'
    | 'offline';

export type PetDirection =
    | '000'
    | '022.5'
    | '045'
    | '067.5'
    | '090'
    | '112.5'
    | '135'
    | '157.5'
    | '180'
    | '202.5'
    | '225'
    | '247.5'
    | '270'
    | '292.5'
    | '315'
    | '337.5';

export type PetManifest = {
    id: string;
    displayName: string;
    description: string;
    spriteVersionNumber: 2;
    spritesheetPath: string;
    frameWidth: number;
    frameHeight: number;
    columns: number;
    rows: number;
    fps: number;
    stateRows: Record<string, number>;
    stateFrameCounts?: Record<string, number>;
    stateFrameDurations?: Record<string, number[]>;
    lookRows: {
        upper: number;
        lower: number;
    };
};

export type PetAnimationFrame = {
    row: number;
    column: number;
    duration: number;
};

const PET_IDLE_DURATION_SCALE = 6;
const PET_ACTIVE_STATE_REPEATS = 3;

export const JENNIE_PET: PetManifest = {
    id: 'jennie',
    displayName: 'Jennie',
    description: "Jennie is LimeBot's default showcase companion: a red-haired gothic schoolgirl with a black outfit, expressive eyes, and lively gestures.",
    spriteVersionNumber: 2,
    spritesheetPath: '/pets/jennie/spritesheet.webp',
    frameWidth: 192,
    frameHeight: 208,
    columns: 8,
    rows: 11,
    fps: 8,
    stateRows: {
        idle: 0,
        'running-right': 1,
        'running-left': 2,
        waving: 3,
        jumping: 4,
        failed: 5,
        waiting: 6,
        running: 7,
        review: 8,
    },
    stateFrameCounts: {
        idle: 6,
        'running-right': 8,
        'running-left': 8,
        waving: 4,
        jumping: 5,
        failed: 8,
        waiting: 6,
        running: 6,
        review: 6,
    },
    stateFrameDurations: {
        idle: [280, 110, 110, 140, 140, 320],
        'running-right': [120, 120, 120, 120, 120, 120, 120, 220],
        'running-left': [120, 120, 120, 120, 120, 120, 120, 220],
        waving: [140, 140, 140, 280],
        jumping: [140, 140, 140, 140, 280],
        failed: [140, 140, 140, 140, 140, 140, 140, 240],
        waiting: [150, 150, 150, 150, 150, 260],
        running: [120, 120, 120, 120, 120, 220],
        review: [150, 150, 150, 150, 150, 280],
    },
    lookRows: { upper: 9, lower: 10 },
};

export const PET_CATALOG: readonly PetManifest[] = [JENNIE_PET];
export const DEFAULT_PET_ID = JENNIE_PET.id;
export const PET_PREFERENCES_STORAGE_KEY = 'limebot-pet-preferences';

export type PetPreferences = {
    enabled: boolean;
    petId: string;
    preferAnimatedPet: boolean;
};

export function findPetManifest(
    petId?: string | null,
    catalog: readonly PetManifest[] = PET_CATALOG,
): PetManifest {
    return catalog.find((pet) => pet.id === petId) || catalog[0] || JENNIE_PET;
}

export function normalizePetPreferences(
    value: unknown,
    catalog: readonly PetManifest[] = PET_CATALOG,
): PetPreferences {
    const raw = value && typeof value === 'object'
        ? value as Partial<PetPreferences>
        : {};
    const petId = typeof raw.petId === 'string' && catalog.some((pet) => pet.id === raw.petId)
        ? raw.petId
        : findPetManifest(DEFAULT_PET_ID, catalog).id;

    return {
        enabled: raw.enabled !== false,
        petId,
        preferAnimatedPet: raw.preferAnimatedPet === true,
    };
}

export function isPetManifest(value: unknown): value is PetManifest {
    if (!value || typeof value !== 'object') return false;
    const manifest = value as Partial<PetManifest>;
    return (
        typeof manifest.id === 'string' &&
        typeof manifest.displayName === 'string' &&
        typeof manifest.description === 'string' &&
        manifest.spriteVersionNumber === 2 &&
        typeof manifest.spritesheetPath === 'string' &&
        manifest.frameWidth === 192 &&
        manifest.frameHeight === 208 &&
        manifest.columns === 8 &&
        manifest.rows === 11 &&
        typeof manifest.fps === 'number' &&
        typeof manifest.stateRows === 'object' &&
        typeof manifest.lookRows === 'object'
    );
}

const LOOK_DIRECTIONS: PetDirection[] = [
    '000', '022.5', '045', '067.5', '090', '112.5', '135', '157.5',
    '180', '202.5', '225', '247.5', '270', '292.5', '315', '337.5',
];

export const PET_STATE_ROW_KEYS: Record<PetState, string> = {
    idle: 'idle',
    thinking: 'review',
    working: 'running',
    approval: 'waiting',
    warning: 'failed',
    celebrating: 'waving',
    offline: 'idle',
};

function frameCountForRow(manifest: PetManifest, rowKey: string) {
    return Math.max(
        1,
        Math.min(manifest.columns, manifest.stateFrameCounts?.[rowKey] ?? manifest.columns),
    );
}

function framesForRow(manifest: PetManifest, rowKey: string, durationScale = 1): PetAnimationFrame[] {
    const frameCount = frameCountForRow(manifest, rowKey);
    const row = manifest.stateRows[rowKey] ?? manifest.stateRows.idle ?? 0;
    const durations = manifest.stateFrameDurations?.[rowKey] ?? [];
    const fallbackDuration = 1000 / Math.max(1, manifest.fps);

    return Array.from({ length: frameCount }, (_, column) => ({
        row,
        column,
        duration: (durations[column] ?? fallbackDuration) * durationScale,
    }));
}

/**
 * Reproduce the LimeBot v2 avatar player: idle is intentionally slow, while
 * active states get three passes followed by a slow idle tail before looping.
 */
export function animationFramesForPet(manifest: PetManifest, state: PetState): PetAnimationFrame[] {
    const stateRowKey = PET_STATE_ROW_KEYS[state] || 'idle';
    const idleFrames = framesForRow(manifest, 'idle', PET_IDLE_DURATION_SCALE);

    if (stateRowKey === 'idle') return idleFrames;

    const activeFrames = framesForRow(manifest, stateRowKey);
    return [
        ...Array.from({ length: PET_ACTIVE_STATE_REPEATS }, () => activeFrames).flat(),
        ...idleFrames,
    ];
}

const ACTIVE_TOOL_STATES = new Set(['planned', 'running', 'progress']);
const APPROVAL_STATES = new Set(['pending_confirmation', 'waiting_confirmation']);

export function frameForPet(
    manifest: PetManifest,
    state: PetState,
    frame: number,
    direction?: PetDirection,
) {
    if (direction) {
        const directionIndex = LOOK_DIRECTIONS.indexOf(direction);
        if (directionIndex >= 0) {
            return {
                row: directionIndex < 8 ? manifest.lookRows.upper : manifest.lookRows.lower,
                column: directionIndex % 8,
            };
        }
    }

    const stateRowKey = PET_STATE_ROW_KEYS[state] || 'idle';
    const frameCount = Math.max(
        1,
        Math.min(manifest.columns, manifest.stateFrameCounts?.[stateRowKey] ?? manifest.columns),
    );
    const normalizedFrame = Math.abs(Math.floor(frame)) % frameCount;
    return {
        row: manifest.stateRows[stateRowKey] ?? manifest.stateRows.idle ?? 0,
        column: normalizedFrame,
    };
}

type PetMessageLike = {
    variant?: string;
    toolExecution?: {
        status?: string;
    };
};

export function derivePetState({
    isConnected,
    isTyping,
    pendingApprovals,
    messages,
}: {
    isConnected: boolean;
    isTyping: boolean;
    pendingApprovals: number;
    messages: ReadonlyArray<PetMessageLike>;
}): PetState {
    if (!isConnected) return 'offline';
    if (pendingApprovals > 0 || messages.some((message) => APPROVAL_STATES.has(message.toolExecution?.status || ''))) {
        return 'approval';
    }

    const latestMessage = messages[messages.length - 1];
    if (latestMessage?.variant === 'destructive' || latestMessage?.toolExecution?.status === 'error') {
        return 'warning';
    }
    if (isTyping) return 'thinking';
    if (messages.some((message) => ACTIVE_TOOL_STATES.has(message.toolExecution?.status || ''))) {
        return 'working';
    }
    return 'idle';
}
