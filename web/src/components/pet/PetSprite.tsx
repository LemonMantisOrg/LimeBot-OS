import { useEffect, useMemo, useState } from 'react';
import {
    animationFramesForPet,
    JENNIE_PET,
    frameForPet,
    type PetDirection,
    type PetManifest,
    type PetState,
} from '@/lib/pet';

type PetSpriteProps = {
    state?: PetState;
    direction?: PetDirection;
    manifest?: PetManifest;
    size?: number;
    className?: string;
    label?: string;
};

export function PetSprite({
    state = 'idle',
    direction,
    manifest = JENNIE_PET,
    size = 48,
    className = '',
    label,
}: PetSpriteProps) {
    const [frame, setFrame] = useState(0);
    const cellHeight = manifest.frameHeight * (size / manifest.frameWidth);
    const animationFrames = useMemo(
        () => animationFramesForPet(manifest, state),
        [manifest, state],
    );
    const animationFrame = animationFrames[frame] || animationFrames[0];
    const directionalFrame = frameForPet(manifest, state, frame, direction);
    const row = direction ? directionalFrame.row : animationFrame.row;
    const column = direction ? directionalFrame.column : animationFrame.column;

    useEffect(() => {
        if (typeof window === 'undefined') return;
        let resetTimer: number | null = null;
        let timer: number | null = null;

        if (!direction) {
            resetTimer = window.setTimeout(() => setFrame(0), 0);
        }
        if (direction || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
            return () => {
                if (resetTimer !== null) window.clearTimeout(resetTimer);
            };
        }

        let currentFrame = 0;
        const scheduleNextFrame = () => {
            const delay = animationFrames[currentFrame]?.duration ?? 1000 / Math.max(1, manifest.fps);
            timer = window.setTimeout(() => {
                currentFrame = (currentFrame + 1) % animationFrames.length;
                setFrame(currentFrame);
                scheduleNextFrame();
            }, delay);
        };

        scheduleNextFrame();

        return () => {
            if (resetTimer !== null) window.clearTimeout(resetTimer);
            if (timer !== null) window.clearTimeout(timer);
        };
    }, [animationFrames, direction, manifest.fps]);

    return (
        <span
            aria-label={label || `${manifest.displayName} ${state}`}
            className={`pet-sprite inline-block shrink-0 ${className}`.trim()}
            data-pet-id={manifest.id}
            data-pet-state={state}
            role="img"
            style={{
                width: `${size}px`,
                height: `${cellHeight}px`,
                backgroundImage: `url("${manifest.spritesheetPath}")`,
                backgroundPosition: `-${column * size}px -${row * cellHeight}px`,
                backgroundRepeat: 'no-repeat',
                backgroundSize: `${manifest.columns * size}px ${manifest.rows * cellHeight}px`,
                imageRendering: 'pixelated',
            }}
        />
    );
}
