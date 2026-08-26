import { useState } from "react";
import { ExternalLink, FileText, Image as ImageIcon } from "lucide-react";
import { API_BASE_URL } from "@/lib/api";
import type { ChatAttachment } from "@/lib/chat-state";
import { resolveAttachmentUrl } from "@/lib/chat-media";
import { ChatImage } from "./ChatImage";

const WORD_DOCUMENT_MIME_TYPES = new Set([
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]);

const isPdfAttachment = (attachment: ChatAttachment) =>
    attachment.mimeType === "application/pdf" || attachment.name.toLowerCase().endsWith(".pdf");

const attachmentLabel = (attachment: ChatAttachment) => {
    if (attachment.kind === "image") return "Image";
    if (isPdfAttachment(attachment)) return "PDF";
    if (WORD_DOCUMENT_MIME_TYPES.has(attachment.mimeType) || /\.(doc|docx)$/i.test(attachment.name)) {
        return "Word";
    }
    return "Document";
};

function FileCard({
    href,
    name,
    label,
    compact = false,
}: {
    href: string;
    name: string;
    label: string;
    compact?: boolean;
}) {
    return (
        <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-3 rounded-xl border border-border/80 bg-background/90 px-3 py-2 text-left shadow-sm transition-colors hover:border-primary/30 hover:bg-background"
        >
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                {label === "Image" ? <ImageIcon className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
            </div>
            <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold text-foreground">{name}</div>
                <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">{label}</div>
            </div>
            {!compact && <ExternalLink className="h-4 w-4 shrink-0 text-muted-foreground" />}
        </a>
    );
}

export function AttachmentPreview({
    attachment,
    compact = false,
}: {
    attachment: ChatAttachment;
    compact?: boolean;
}) {
    const resolvedUrl = resolveAttachmentUrl(attachment.url, API_BASE_URL);
    const label = attachmentLabel(attachment);
    const [imageFailed, setImageFailed] = useState(false);

    if (attachment.kind === "image") {
        if (compact) {
            return (
                <div className="max-h-24 overflow-hidden rounded-lg">
                    {!imageFailed && (
                        <ChatImage
                            src={resolvedUrl}
                            alt={attachment.name || "Attached image"}
                            onError={() => setImageFailed(true)}
                        />
                    )}
                    {imageFailed && <FileCard href={resolvedUrl} name={attachment.name} label={label} compact />}
                </div>
            );
        }
        return (
            <div className="chat-media-block mb-2 overflow-hidden rounded-xl border border-border/70 bg-background/70 shadow-sm">
                {!imageFailed && (
                    <ChatImage
                        src={resolvedUrl}
                        alt={attachment.name || "Chat image"}
                        onError={() => setImageFailed(true)}
                    />
                )}
                <div className="border-t border-border/60">
                    <FileCard href={resolvedUrl} name={attachment.name || "image"} label={label} />
                </div>
            </div>
        );
    }

    if (isPdfAttachment(attachment) && !compact) {
        return (
            <div className="mb-2 overflow-hidden rounded-xl border border-border bg-background/80 shadow-sm">
                <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
                    <div className="min-w-0">
                        <div className="truncate text-xs font-semibold text-foreground">{attachment.name}</div>
                        <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">{label}</div>
                    </div>
                    <a
                        href={resolvedUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-2 py-1 text-[10px] font-semibold text-muted-foreground transition-colors hover:border-primary/30 hover:text-foreground"
                    >
                        <ExternalLink className="h-3 w-3" />
                        Open
                    </a>
                </div>
                <iframe
                    src={resolvedUrl}
                    title={attachment.name}
                    className="h-64 w-full bg-white"
                />
            </div>
        );
    }

    return <FileCard href={resolvedUrl} name={attachment.name} label={label} compact={compact} />;
}
