import test from 'node:test';
import assert from 'node:assert/strict';
import {
    animationFramesForPet,
    JENNIE_PET,
    derivePetState,
    frameForPet,
    normalizePetPreferences,
    PET_CATALOG,
} from '../src/lib/pet.js';

test('pet catalog and preferences default to the built-in pet', () => {
    assert.equal(PET_CATALOG.length, 1);
    assert.deepEqual(normalizePetPreferences(null), {
        enabled: true,
        petId: 'jennie',
        preferAnimatedPet: false,
    });
    assert.deepEqual(
        normalizePetPreferences({ enabled: false, petId: 'missing-pet', preferAnimatedPet: true }),
        { enabled: false, petId: 'jennie', preferAnimatedPet: true },
    );
    assert.equal(normalizePetPreferences({ petId: 'crimson' }).petId, 'jennie');
});

test('Jennie exposes the v2 atlas geometry and standard state rows', () => {
    assert.equal(JENNIE_PET.displayName, 'Jennie');
    assert.equal(JENNIE_PET.spritesheetPath, '/pets/jennie/spritesheet.webp');
    assert.equal(JENNIE_PET.spriteVersionNumber, 2);
    assert.deepEqual(
        { width: JENNIE_PET.frameWidth, height: JENNIE_PET.frameHeight, columns: JENNIE_PET.columns, rows: JENNIE_PET.rows },
        { width: 192, height: 208, columns: 8, rows: 11 },
    );
    assert.equal(JENNIE_PET.stateRows.idle, 0);
    assert.equal(JENNIE_PET.stateRows.waiting, 6);
    assert.equal(JENNIE_PET.stateRows.running, 7);
});

test('look directions map to the two v2 look rows in clockwise order', () => {
    assert.deepEqual(frameForPet(JENNIE_PET, 'idle', 0, '000'), { row: 9, column: 0 });
    assert.deepEqual(frameForPet(JENNIE_PET, 'idle', 0, '090'), { row: 9, column: 4 });
    assert.deepEqual(frameForPet(JENNIE_PET, 'idle', 0, '180'), { row: 10, column: 0 });
    assert.deepEqual(frameForPet(JENNIE_PET, 'idle', 0, '270'), { row: 10, column: 4 });
});

test('semantic UI states resolve to their corresponding animation rows', () => {
    assert.equal(frameForPet(JENNIE_PET, 'idle', 0).row, 0);
    assert.equal(frameForPet(JENNIE_PET, 'thinking', 0).row, 8);
    assert.equal(frameForPet(JENNIE_PET, 'working', 0).row, 7);
    assert.equal(frameForPet(JENNIE_PET, 'approval', 0).row, 6);
    assert.equal(frameForPet(JENNIE_PET, 'warning', 0).row, 5);
    assert.equal(frameForPet(JENNIE_PET, 'celebrating', 0).row, 3);
    assert.equal(frameForPet(JENNIE_PET, 'offline', 0).row, 0);
});

test('short state rows do not advance into transparent unused columns', () => {
    assert.equal(frameForPet(JENNIE_PET, 'idle', 6).column, 0);
    assert.equal(frameForPet(JENNIE_PET, 'approval', 6).column, 0);
    assert.equal(frameForPet(JENNIE_PET, 'celebrating', 4).column, 0);
    assert.equal(frameForPet(JENNIE_PET, 'working', 6).column, 0);
});

test('Pet timing preserves the slower state-specific frame holds', () => {
    assert.deepEqual(JENNIE_PET.stateFrameDurations?.idle, [280, 110, 110, 140, 140, 320]);
    assert.deepEqual(JENNIE_PET.stateFrameDurations?.waving, [140, 140, 140, 280]);
    assert.deepEqual(JENNIE_PET.stateFrameDurations?.review, [150, 150, 150, 150, 150, 280]);
});

test('Pet playback slows idle and eases active states through an idle tail', () => {
    const idleFrames = animationFramesForPet(JENNIE_PET, 'idle');
    assert.deepEqual(idleFrames.map((frame) => frame.duration), [1680, 660, 660, 840, 840, 1920]);

    const workingFrames = animationFramesForPet(JENNIE_PET, 'working');
    assert.equal(workingFrames.length, 24);
    assert.deepEqual(
        workingFrames.slice(0, 6).map((frame) => frame.duration),
        [120, 120, 120, 120, 120, 220],
    );
    assert.equal(workingFrames[18].row, JENNIE_PET.stateRows.idle);
    assert.equal(workingFrames[18].duration, 1680);
});

test('pet state follows connection, approval, activity, and latest error signals', () => {
    const empty = { messages: [], pendingApprovals: 0 };
    assert.equal(derivePetState({ ...empty, isConnected: false, isTyping: false }), 'offline');
    assert.equal(derivePetState({ ...empty, isConnected: true, isTyping: false }), 'idle');
    assert.equal(derivePetState({ ...empty, isConnected: true, isTyping: true }), 'thinking');
    assert.equal(derivePetState({ ...empty, isConnected: true, isTyping: false, pendingApprovals: 1 }), 'approval');
    assert.equal(derivePetState({
        ...empty,
        isConnected: true,
        isTyping: false,
        messages: [{ toolExecution: { status: 'running' } }],
    }), 'working');
    assert.equal(derivePetState({
        ...empty,
        isConnected: true,
        isTyping: false,
        messages: [{ variant: 'destructive' }],
    }), 'warning');
});
