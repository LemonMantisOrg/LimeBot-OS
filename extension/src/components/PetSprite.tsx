import { useEffect, useMemo, useState } from 'react';
import type { CompanionStatus } from '@/lib/protocol';

const STATUS_ROW: Record<CompanionStatus, number> = {
  offline: 0,
  idle: 0,
  thinking: 8,
  working: 7,
  approval: 6,
  warning: 5,
  celebrating: 3,
};

const STATUS_FRAME_COUNT: Record<CompanionStatus, number> = {
  offline: 6,
  idle: 6,
  thinking: 6,
  working: 6,
  approval: 6,
  warning: 8,
  celebrating: 4,
};

const STATUS_FRAME_DURATIONS: Record<CompanionStatus, number[]> = {
  offline: [280, 110, 110, 140, 140, 320],
  idle: [280, 110, 110, 140, 140, 320],
  thinking: [150, 150, 150, 150, 150, 280],
  working: [120, 120, 120, 120, 120, 220],
  approval: [150, 150, 150, 150, 150, 260],
  warning: [140, 140, 140, 140, 140, 140, 140, 240],
  celebrating: [140, 140, 140, 280],
};

const FRAME_WIDTH = 192;
const FRAME_HEIGHT = 208;
const COLUMNS = 8;
const ROWS = 11;
const FPS = 8;
const PET_IDLE_DURATION_SCALE = 6;

type AnimationFrame = {
  row: number;
  column: number;
  duration: number;
};

function framesForStatus(status: CompanionStatus): AnimationFrame[] {
  const idleFrames = Array.from({ length: STATUS_FRAME_COUNT.idle }, (_, column) => ({
    row: STATUS_ROW.idle,
    column,
    duration: STATUS_FRAME_DURATIONS.idle[column] * PET_IDLE_DURATION_SCALE,
  }));

  if (status === 'idle' || status === 'offline') return idleFrames;

  const activeFrames = Array.from({ length: STATUS_FRAME_COUNT[status] }, (_, column) => ({
    row: STATUS_ROW[status],
    column,
    duration: STATUS_FRAME_DURATIONS[status][column] ?? 1000 / FPS,
  }));

  return [
    ...activeFrames,
    ...activeFrames,
    ...activeFrames,
    ...idleFrames,
  ];
}

type PetSpriteProps = {
  status: CompanionStatus;
  size?: number;
  className?: string;
  label?: string;
};

export function PetSprite({ status, size = 46, className = '', label }: PetSpriteProps) {
  const [frame, setFrame] = useState(0);
  const cellHeight = FRAME_HEIGHT * (size / FRAME_WIDTH);
  const animationFrames = useMemo(() => framesForStatus(status), [status]);
  const animationFrame = animationFrames[frame] ?? animationFrames[0];

  useEffect(() => {
    setFrame(0);
    if (typeof window === 'undefined') return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;

    let timer: number | null = null;
    let currentFrame = 0;
    const scheduleNextFrame = () => {
      const delay = animationFrames[currentFrame]?.duration ?? 1000 / FPS;
      timer = window.setTimeout(() => {
        currentFrame = (currentFrame + 1) % animationFrames.length;
        setFrame(currentFrame);
        scheduleNextFrame();
      }, delay);
    };

    scheduleNextFrame();

    return () => {
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [animationFrames]);

  return (
    <span
      aria-label={label || `Jennie ${status}`}
      className={`mascot-pet ${className}`.trim()}
      data-pet-id="jennie"
      data-pet-state={status}
      role="img"
      style={{
        width: `${size}px`,
        height: `${cellHeight}px`,
        backgroundImage: 'url("/pets/jennie/spritesheet.webp")',
        backgroundPosition: `-${animationFrame.column * size}px -${animationFrame.row * cellHeight}px`,
        backgroundRepeat: 'no-repeat',
        backgroundSize: `${COLUMNS * size}px ${ROWS * cellHeight}px`,
        imageRendering: 'pixelated',
      }}
    />
  );
}
