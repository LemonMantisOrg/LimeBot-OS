export type TurnStatus = 'running' | 'completed' | 'retrying' | 'failed' | 'blocked' | 'cancelled';

export type ChatToolExecution = {
  tool: string;
  status:
    | 'planned'
    | 'running'
    | 'completed'
    | 'error'
    | 'pending_confirmation'
    | 'progress'
    | 'waiting_confirmation';
  args: any;
  result?: string;
  tool_call_id: string;
  conf_id?: string;
  turnStatus?: TurnStatus;
  logs?: string[];
  preview?: {
    kind: string;
    summary?: string;
    path?: string;
    mode?: string;
    content_preview?: string;
    diff?: string;
    diff_error?: string;
    command?: string;
    cwd?: string;
    risk_flags?: string[];
    affected_paths?: string[];
    target_type?: string;
    args_preview?: string;
  };
};

export type ChatConfirmation = {
  id: string;
  action: string;
  description: string;
  details?: string;
  status: 'pending' | 'approved' | 'denied';
};

export type ChatAttachment = {
  name: string;
  mimeType: string;
  kind: 'image' | 'document';
  url: string;
};

export type ChatChangeSet = {
  id?: string | null;
  artifact_type?: 'change_set' | 'coding_plan';
  status: 'planned' | 'awaiting_approval' | 'applied' | 'verified' | 'failed' | 'blocked';
  summary: string;
  added?: number;
  removed?: number;
  truncated?: boolean;
  changed_files?: Array<{
    file_id: string;
    added: number;
    removed: number;
    hunks?: Array<{ old_start: number; old_count: number; new_start: number; new_count: number; heading?: string }>;
  }>;
  redacted_diff?: string;
  verification?: Array<{
    id: string;
    label: string;
    status: 'pending' | 'running' | 'passed' | 'failed' | 'blocked';
    exit_code?: number | null;
    diagnostic?: string;
  }>;
};

export type ChatMessage = {
  sender: 'user' | 'bot';
  type?: 'text' | 'tool' | 'confirmation' | 'changeset';
  content: string;
  thinking?: string;
  isStreaming?: boolean;
  image?: string | null;
  attachments?: ChatAttachment[];
  toolExecution?: ChatToolExecution;
  changeSet?: ChatChangeSet;
  confirmation?: ChatConfirmation;
  variant?: 'default' | 'destructive' | 'warning';
  messageId?: string;
  turnId?: string;
  turnStatus?: TurnStatus;
  voiceUrl?: string;
};

type MessageTarget = {
  messageId?: string | null;
  turnId?: string | null;
};

type StreamDelta = MessageTarget & {
  contentDelta?: string;
  thinkingDelta?: string;
};

type FinalText = MessageTarget & {
  content: string;
  variant: 'default' | 'destructive' | 'warning';
  turnStatus?: TurnStatus;
  image?: string | null;
  attachments?: ChatAttachment[];
  voiceUrl?: string;
};

type ToolUpdate = MessageTarget & {
  content?: string;
  toolExecution: ChatToolExecution;
};

type ChangeSetUpdate = MessageTarget & {
  changeSet: ChatChangeSet;
};

const isBotTextMessage = (message: ChatMessage) =>
  message.sender === 'bot' && message.type !== 'tool' && !message.confirmation;

const isUserTextMessage = (message: ChatMessage) =>
  message.sender === 'user' && message.type !== 'tool' && !message.confirmation;

function dedupeRepeatedSections(content: string): string {
  if (!content) return content;

  const trimmed = content.trim();
  const length = trimmed.length;
  if (length > 80 && length % 2 === 0) {
    const half = length / 2;
    if (trimmed.slice(0, half) === trimmed.slice(half)) {
      return trimmed.slice(0, half);
    }
  }

  if (length <= 80) return trimmed;

  const paragraphs = trimmed
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
  const count = paragraphs.length;

  if (count >= 4 && count % 2 === 0) {
    const half = count / 2;
    const firstHalf = paragraphs.slice(0, half);
    const secondHalf = paragraphs.slice(half);
    if (firstHalf.join('\n\n') === secondHalf.join('\n\n')) {
      return firstHalf.join('\n\n');
    }
  }

  if (count >= 3) {
    const half = Math.floor(count / 2);
    if (half >= 2) {
      const firstHalf = paragraphs.slice(0, half);
      const trailingHalf = paragraphs.slice(count - half);
      if (firstHalf.join('\n\n') === trailingHalf.join('\n\n')) {
        return paragraphs.slice(0, count - half).join('\n\n');
      }
    }
  }

  return trimmed;
}

function mergeChatAttachments(
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

function findMessageIndex(
  messages: ChatMessage[],
  target: MessageTarget,
  options?: { streamingOnly?: boolean }
): number {
  const { messageId, turnId } = target;
  const streamingOnly = options?.streamingOnly ?? false;

  if (messageId) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const message = messages[i];
      if (
        isBotTextMessage(message) &&
        message.messageId === messageId &&
        (!streamingOnly || message.isStreaming)
      ) {
        return i;
      }
    }
  }

  if (turnId) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const message = messages[i];
      if (!isBotTextMessage(message)) continue;
      if (message.turnId !== turnId) continue;
      if (streamingOnly && !message.isStreaming) continue;
      return i;
    }
  }

  // A targeted event must never fall through to an unrelated streaming
  // bubble. This is especially important when an old stream finishes after a
  // newer turn has already started.
  if (messageId || turnId) return -1;

  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (!isBotTextMessage(message)) continue;
    if (!message.isStreaming) continue;
    return i;
  }

  return -1;
}

export function upsertStreamDelta(
  messages: ChatMessage[],
  delta: StreamDelta
): ChatMessage[] {
  const contentDelta = delta.contentDelta ?? '';
  const thinkingDelta = delta.thinkingDelta ?? '';
  if (!contentDelta && !thinkingDelta) return messages;

  // `stop_typing` is also used as a pause before tool execution. When the
  // post-tool model response starts, reopen the exact same assistant bubble
  // instead of creating a second one.
  const streamingIndex = findMessageIndex(messages, delta, { streamingOnly: true });
  const index = streamingIndex !== -1
    ? streamingIndex
    : findMessageIndex(messages, delta);
  if (index === -1) {
    return [
      ...messages,
      {
        sender: 'bot',
        type: 'text',
        content: contentDelta,
        thinking: thinkingDelta || undefined,
        isStreaming: true,
        messageId: delta.messageId || undefined,
        turnId: delta.turnId || undefined,
      },
    ];
  }

  const existing = messages[index];
  const nextContent = `${existing.content}${contentDelta}`;
  const nextThinking = thinkingDelta
    ? `${existing.thinking || ''}${thinkingDelta}`
    : existing.thinking;

  if (
    nextContent === existing.content &&
    nextThinking === existing.thinking &&
    existing.isStreaming
  ) {
    return messages;
  }

  const updated = [...messages];
  updated[index] = {
    ...existing,
    type: 'text',
    content: nextContent,
    thinking: nextThinking,
    isStreaming: true,
    messageId: delta.messageId || existing.messageId,
    turnId: delta.turnId || existing.turnId,
  };
  return updated;
}

/**
 * Replace the visible stream with an intermediate provider snapshot.
 *
 * This is intentionally different from a final assistant message: snapshots
 * can be emitted before tool execution and must keep the bubble streamable.
 */
export function applyStreamSnapshot(
  messages: ChatMessage[],
  payload: MessageTarget & { content: string }
): ChatMessage[] {
  const index = findMessageIndex(messages, payload);
  if (index === -1) {
    return [
      ...messages,
      {
        sender: 'bot',
        type: 'text',
        content: payload.content,
        isStreaming: true,
        messageId: payload.messageId || undefined,
        turnId: payload.turnId || undefined,
      },
    ];
  }

  const updated = [...messages];
  updated[index] = {
    ...updated[index],
    type: 'text',
    content: payload.content,
    isStreaming: true,
    messageId: payload.messageId || updated[index].messageId,
    turnId: payload.turnId || updated[index].turnId,
  };
  return updated;
}

export function applyFinalAssistantMessage(
  messages: ChatMessage[],
  payload: FinalText
): ChatMessage[] {
  const content = dedupeRepeatedSections(payload.content);
  const index = findMessageIndex(messages, payload);
  const incomingHasMedia = Boolean(payload.image || payload.attachments?.length);

  if (index === -1) {
    return [
      ...messages,
      {
        sender: 'bot',
        content,
        variant: payload.variant,
        type: 'text',
        isStreaming: false,
        image: payload.image ?? null,
        attachments: payload.attachments,
        voiceUrl: payload.voiceUrl,
        messageId: payload.messageId || undefined,
        turnId: payload.turnId || undefined,
        turnStatus: payload.turnStatus,
      },
    ];
  }

  const existing = messages[index];
  const existingHasContent = Boolean(existing.content?.trim());
  let nextContent = content;
  if (incomingHasMedia && existingHasContent) {
    // A send_media caption must not clobber the streamed/final assistant text.
    nextContent = existing.content;
  } else if (!content.trim() && existing.content) {
    nextContent = existing.content;
  }

  const updated = [...messages];
  updated[index] = {
    ...existing,
    content: nextContent,
    variant: payload.variant,
    type: 'text',
    isStreaming: false,
    image: payload.image || existing.image || null,
    attachments: mergeChatAttachments(existing.attachments, payload.attachments),
    voiceUrl: payload.voiceUrl ?? existing.voiceUrl,
    messageId: payload.messageId || existing.messageId,
    turnId: payload.turnId || existing.turnId,
    turnStatus: payload.turnStatus || updated[index].turnStatus,
  };
  return updated;
}

export function applyStopTyping(
  messages: ChatMessage[],
  target: MessageTarget
): ChatMessage[] {
  const index = findMessageIndex(messages, target, { streamingOnly: true });
  if (index === -1) return messages;

  const updated = [...messages];
  updated[index] = {
    ...updated[index],
    isStreaming: false,
    messageId: target.messageId || updated[index].messageId,
    turnId: target.turnId || updated[index].turnId,
  };
  return updated;
}

export function getUserTurnIndex(
  messages: ChatMessage[],
  targetMessageId: string
): number {
  let userTurnIndex = 0;
  for (const message of messages) {
    if (!isUserTextMessage(message)) continue;
    if (message.messageId === targetMessageId) {
      return userTurnIndex;
    }
    userTurnIndex += 1;
  }
  return -1;
}

export function applyUserMessageEdit(
  messages: ChatMessage[],
  targetMessageId: string,
  nextContent: string,
  nextMessageId: string
): ChatMessage[] {
  const targetIndex = messages.findIndex(
    (message) => isUserTextMessage(message) && message.messageId === targetMessageId
  );
  if (targetIndex === -1) return messages;

  const updated = messages.slice(0, targetIndex + 1);
  const existing = updated[targetIndex];
  updated[targetIndex] = {
    ...existing,
    content: nextContent,
    messageId: nextMessageId,
  };
  return updated;
}

function findFinalMessageIndexForTurn(
  messages: ChatMessage[],
  turnId?: string | null
): number {
  if (!turnId) return -1;
  return messages.findIndex(
    (message) =>
      isBotTextMessage(message) &&
      message.turnId === turnId &&
      !message.isStreaming
  );
}

function moveToolBeforeFinalMessage(
  messages: ChatMessage[],
  toolIndex: number,
  turnId?: string | null
): ChatMessage[] {
  const finalIndex = findFinalMessageIndexForTurn(messages, turnId);
  if (finalIndex === -1 || toolIndex < finalIndex) return messages;

  const updated = [...messages];
  const [toolMessage] = updated.splice(toolIndex, 1);
  const insertionIndex = toolIndex < finalIndex ? finalIndex - 1 : finalIndex;
  updated.splice(insertionIndex, 0, toolMessage);
  return updated;
}

export function upsertToolExecution(
  messages: ChatMessage[],
  update: ToolUpdate
): ChatMessage[] {
  const execution = update.toolExecution;
  const existingIndex = messages.findIndex(
    (message) =>
      message.type === 'tool' &&
      message.toolExecution?.tool_call_id === execution.tool_call_id
  );

  if (existingIndex !== -1) {
    const existingMessage = messages[existingIndex];
    const existingExec = existingMessage.toolExecution!;
    const logs =
      execution.status === 'progress'
        ? [...(existingExec.logs || []), update.content || '']
        : existingExec.logs || [];
    const updated = [...messages];
    updated[existingIndex] = {
      ...existingMessage,
      messageId: update.messageId || existingMessage.messageId,
      turnId: update.turnId || existingMessage.turnId,
      toolExecution: {
        ...existingExec,
        ...execution,
        status:
          execution.status === 'progress'
            ? existingExec.status
            : execution.status,
        result: execution.result,
        conf_id: execution.conf_id || existingExec.conf_id,
        logs,
        preview: execution.preview || existingExec.preview,
      },
    };
    return moveToolBeforeFinalMessage(
      updated,
      existingIndex,
      update.turnId || existingMessage.turnId
    );
  }

  const newMessage: ChatMessage = {
    sender: 'bot',
    type: 'tool',
    content: '',
    messageId: update.messageId || undefined,
    turnId: update.turnId || undefined,
    toolExecution: {
      ...execution,
      logs: execution.logs || [],
    },
  };

  const finalIndex = findFinalMessageIndexForTurn(messages, update.turnId);
  if (finalIndex === -1) return [...messages, newMessage];

  const updated = [...messages];
  updated.splice(finalIndex, 0, newMessage);
  return updated;
}

const changeSetKey = (changeSet: ChatChangeSet, target: MessageTarget) =>
  changeSet.id || target.turnId || `${changeSet.status}:${changeSet.summary}`;

export function upsertChangeSet(
  messages: ChatMessage[],
  update: ChangeSetUpdate
): ChatMessage[] {
  const key = changeSetKey(update.changeSet, update);
  const index = messages.findIndex(
    (message) =>
      message.type === 'changeset' &&
      message.changeSet &&
      changeSetKey(message.changeSet, message) === key
  );
  if (index === -1) {
    return [
      ...messages,
      {
        sender: 'bot',
        type: 'changeset',
        content: '',
        messageId: update.messageId || undefined,
        turnId: update.turnId || undefined,
        changeSet: update.changeSet,
      },
    ];
  }
  const updated = [...messages];
  const prior = updated[index].changeSet!;
  const terminalStatuses = new Set<ChatChangeSet['status']>(['verified', 'failed', 'blocked']);
  const preserveTerminal =
    terminalStatuses.has(prior.status) && !terminalStatuses.has(update.changeSet.status);
  updated[index] = {
    ...updated[index],
    messageId: update.messageId || updated[index].messageId,
    turnId: update.turnId || updated[index].turnId,
    changeSet: {
      ...prior,
      ...update.changeSet,
      status: preserveTerminal ? prior.status : update.changeSet.status,
      verification: update.changeSet.verification || updated[index].changeSet?.verification,
    },
  };
  return updated;
}
