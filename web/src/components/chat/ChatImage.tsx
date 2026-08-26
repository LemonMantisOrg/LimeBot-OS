import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Minus, Plus, RotateCcw, X } from "lucide-react";

const MIN_SCALE = 1;
const MAX_SCALE = 6;
const SCALE_STEP = 0.25;

export function ChatImage({
    src,
    alt,
    onError,
}: {
    src: string;
    alt: string;
    onError?: () => void;
}) {
    const [error, setError] = useState(false);
    const [open, setOpen] = useState(false);

    if (error) return null;

    return (
        <>
            <img
                src={src}
                alt={alt}
                className="chat-inline-image max-h-[28rem] w-full cursor-zoom-in rounded-xl bg-muted/30 object-contain"
                onError={() => {
                    setError(true);
                    onError?.();
                }}
                onClick={() => setOpen(true)}
            />
            {open && (
                <ImageLightbox src={src} alt={alt} onClose={() => setOpen(false)} />
            )}
        </>
    );
}

function ImageLightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
    const [scale, setScale] = useState(1);
    const [offset, setOffset] = useState({ x: 0, y: 0 });
    const dragRef = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
    const [dragging, setDragging] = useState(false);

    const reset = useCallback(() => {
        setScale(1);
        setOffset({ x: 0, y: 0 });
    }, []);

    const clampScale = (value: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, value));

    const zoomBy = useCallback((delta: number) => {
        setScale((prev) => {
            const next = clampScale(prev + delta);
            if (next === MIN_SCALE) setOffset({ x: 0, y: 0 });
            return next;
        });
    }, []);

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === "Escape") onClose();
            else if (e.key === "+" || e.key === "=") zoomBy(SCALE_STEP);
            else if (e.key === "-") zoomBy(-SCALE_STEP);
            else if (e.key === "0") reset();
        };
        window.addEventListener("keydown", onKey);
        const prevOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => {
            window.removeEventListener("keydown", onKey);
            document.body.style.overflow = prevOverflow;
        };
    }, [onClose, zoomBy, reset]);

    const onWheel = (e: React.WheelEvent) => {
        e.preventDefault();
        zoomBy(e.deltaY < 0 ? SCALE_STEP : -SCALE_STEP);
    };

    const onPointerDown = (e: React.PointerEvent) => {
        if (scale <= 1) return;
        (e.target as HTMLElement).setPointerCapture(e.pointerId);
        dragRef.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y };
        setDragging(true);
    };

    const onPointerMove = (e: React.PointerEvent) => {
        if (!dragRef.current) return;
        setOffset({
            x: dragRef.current.ox + (e.clientX - dragRef.current.x),
            y: dragRef.current.oy + (e.clientY - dragRef.current.y),
        });
    };

    const endDrag = () => {
        dragRef.current = null;
        setDragging(false);
    };

    const stop = (e: React.MouseEvent) => e.stopPropagation();

    return createPortal(
        <div
            className="fixed inset-0 z-[120] flex items-center justify-center bg-black/85 backdrop-blur-sm animate-in fade-in"
            onClick={onClose}
            role="dialog"
            aria-modal="true"
            aria-label={alt || "Image preview"}
        >
            <div className="absolute right-4 top-4 z-10 flex items-center gap-1" onClick={stop}>
                <LightboxButton title="Zoom out (-)" onClick={() => zoomBy(-SCALE_STEP)} disabled={scale <= MIN_SCALE}>
                    <Minus className="h-4 w-4" />
                </LightboxButton>
                <span className="min-w-[3rem] text-center text-xs font-semibold text-white/80">
                    {Math.round(scale * 100)}%
                </span>
                <LightboxButton title="Zoom in (+)" onClick={() => zoomBy(SCALE_STEP)} disabled={scale >= MAX_SCALE}>
                    <Plus className="h-4 w-4" />
                </LightboxButton>
                <LightboxButton title="Reset (0)" onClick={reset} disabled={scale === MIN_SCALE && offset.x === 0 && offset.y === 0}>
                    <RotateCcw className="h-4 w-4" />
                </LightboxButton>
                <LightboxButton title="Close (Esc)" onClick={onClose}>
                    <X className="h-4 w-4" />
                </LightboxButton>
            </div>

            <img
                src={src}
                alt={alt}
                draggable={false}
                onClick={stop}
                onWheel={onWheel}
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={endDrag}
                onPointerCancel={endDrag}
                onDoubleClick={() => (scale > 1 ? reset() : zoomBy(1))}
                className="max-h-[92vh] max-w-[94vw] select-none rounded-lg object-contain shadow-2xl"
                style={{
                    transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
                    cursor: scale > 1 ? (dragging ? "grabbing" : "grab") : "zoom-in",
                    transition: dragging ? "none" : "transform 0.12s ease-out",
                    touchAction: "none",
                }}
            />
        </div>,
        document.body,
    );
}

function LightboxButton({
    children,
    title,
    onClick,
    disabled = false,
}: {
    children: React.ReactNode;
    title: string;
    onClick: () => void;
    disabled?: boolean;
}) {
    return (
        <button
            type="button"
            title={title}
            onClick={onClick}
            disabled={disabled}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-white/10 text-white/90 transition-colors hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-40"
        >
            {children}
        </button>
    );
}
