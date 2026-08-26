import type { ChatAttachment } from "./chat-state.js";

export function normalizeIncomingAttachments(value: unknown): ChatAttachment[] | undefined {
  if (!Array.isArray(value)) return undefined;

  const attachments = value
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const attachment = item as Record<string, unknown>;
      const name = typeof attachment.name === "string" ? attachment.name : "attachment";
      const mimeType =
        typeof attachment.mimeType === "string"
          ? attachment.mimeType
          : typeof attachment.mime_type === "string"
            ? attachment.mime_type
            : "application/octet-stream";
      const kind =
        attachment.kind === "image" || attachment.kind === "document"
          ? attachment.kind
          : mimeType.startsWith("image/")
            ? "image"
            : "document";
      const url =
        typeof attachment.url === "string"
          ? attachment.url
          : typeof attachment.data_url === "string"
            ? attachment.data_url
            : "";
      if (!url) return null;
      return { name, mimeType, kind, url } satisfies ChatAttachment;
    })
    .filter((item): item is ChatAttachment => item !== null);

  return attachments.length > 0 ? attachments : undefined;
}

export function resolveAttachmentUrl(url: string, apiBase = ""): string {
  if (!url) return "";
  if (url.startsWith("data:") || /^https?:\/\//i.test(url)) return url;
  if (!apiBase) return url;
  if (url.startsWith("/")) return `${apiBase}${url}`;
  return `${apiBase}/${url}`;
}

export function renderableAttachmentsForMessage(message: {
  image?: string | null;
  attachments?: ChatAttachment[];
}): ChatAttachment[] {
  if (message.attachments?.length) {
    return message.attachments;
  }
  if (message.image) {
    return [
      {
        name: "Image",
        mimeType: "image/*",
        kind: "image",
        url: message.image,
      },
    ];
  }
  return [];
}

export function mergeChatAttachments(
  existing?: ChatAttachment[],
  incoming?: ChatAttachment[],
): ChatAttachment[] | undefined {
  if (!incoming?.length) return existing;
  if (!existing?.length) return incoming;
  const seen = new Set(existing.map((item) => item.url));
  const merged = [...existing];
  for (const item of incoming) {
    if (!item.url || seen.has(item.url)) continue;
    seen.add(item.url);
    merged.push(item);
  }
  return merged;
}

export function messageHasInlineImage(message: {
  image?: string | null;
  attachments?: ChatAttachment[];
}): boolean {
  if (typeof message.image === "string" && message.image.length > 0) return true;
  return Boolean(message.attachments?.some((item) => item.kind === "image" && item.url));
}
