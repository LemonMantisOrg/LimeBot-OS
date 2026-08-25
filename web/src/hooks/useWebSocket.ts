/**
 * web/src/hooks/useWebSocket.ts
 * ─────────────────────────────
 * WebSocket connection, stream buffering, message dispatch, send & new-chat
 * handlers, all extracted from App.tsx.
 *
 * Exports:
 *  - useWebSocket(options) — returns all WS-related state and callbacks
 */

import { useState, useRef, useEffect } from 'react';
import axios from 'axios';
import { API_BASE_URL, WS_BASE_URL } from '@/lib/api';
import {
    applyUserMessageEdit,
    applyFinalAssistantMessage,
    applyStreamSnapshot,
    applyStopTyping,
    type ChatAttachment,
    type ChatMessage,
    getUserTurnIndex,
    upsertChangeSet,
    upsertToolExecution,
    upsertStreamDelta,
} from '@/lib/chat-state';
import type { ToolExecution } from '@/components/chat/ToolCard';
import { classifyStreamDelta, type StreamRenderState } from '@/lib/stream-rendering';

type Message = ChatMessage & {
    toolExecution?: ToolExecution;
};

const normalizeIncomingAttachments = (value: unknown): ChatAttachment[] | undefined => {
    if (!Array.isArray(value)) return undefined;

    const attachments = value
        .map((item) => {
            if (!item || typeof item !== 'object') return null;
            const attachment = item as Record<string, unknown>;
            const name = typeof attachment.name === 'string' ? attachment.name : 'attachment';
            const mimeType =
                typeof attachment.mimeType === 'string'
                    ? attachment.mimeType
                    : typeof attachment.mime_type === 'string'
                        ? attachment.mime_type
                        : 'application/octet-stream';
            const kind =
                attachment.kind === 'image' || attachment.kind === 'document'
                    ? attachment.kind
                    : mimeType.startsWith('image/')
                        ? 'image'
                        : 'document';
            const url =
                typeof attachment.url === 'string'
                    ? attachment.url
                    : typeof attachment.data_url === 'string'
                        ? attachment.data_url
                        : '';

            if (!url) return null;
            return { name, mimeType, kind, url } satisfies ChatAttachment;
        })
        .filter((item): item is ChatAttachment => Boolean(item));

    return attachments.length > 0 ? attachments : undefined;
};

interface UseWebSocketOptions {
    /** Called when the server reports an identity update. */
    onIdentityUpdated: () => void;
    /** Called when a rate-limit event arrives. */
    onRateLimit: () => void;
    /** Called when a ghost-activity message arrives. */
    onActivity: (text: string) => void;
    /** Called when ghost activity should be cleared. */
    onActivityClear: () => void;
}

type SendMessageOptions = {
    echoUserMessage?: boolean;
    metadata?: Record<string, unknown>;
};

const STREAM_FLUSH_INTERVAL_MS = 32;
const EMPTY_STREAM_RENDER_STATE: StreamRenderState = {
    key: null,
    contentRendered: false,
    thinkingRendered: false,
};

export function useWebSocket({
    onIdentityUpdated,
    onRateLimit,
    onActivity,
    onActivityClear,
}: UseWebSocketOptions) {
    const [messages, setMessages] = useState<Message[]>([]);
    const [inputValue, setInputValue] = useState('');
    const [isConnected, setIsConnected] = useState(false);
    const [isTyping, setIsTyping] = useState(false);
    const [sessionId, setSessionId] = useState(() => crypto.randomUUID());

    const ws = useRef<WebSocket | null>(null);
    const reconnectTimerRef = useRef<number | null>(null);
    const connectTimerRef = useRef<number | null>(null);
    const mountedRef = useRef(true);
    const sessionIdRef = useRef(sessionId);
    const streamFlushTimerRef = useRef<number | null>(null);
    const streamRenderStateRef = useRef<StreamRenderState>(EMPTY_STREAM_RENDER_STATE);
    // A terminal assistant event closes a stream. WebSocket delivery can
    // still contain already-queued ephemeral deltas after that event, so keep
    // a small client-side fence to prevent late chunks from resurrecting the
    // bubble or toggling the UI back into a live state.
    const settledStreamKeysRef = useRef<Set<string>>(new Set());
    const streamBufferRef = useRef<{
        chatId: string | null;
        messageId: string | null;
        turnId: string | null;
        content: string;
        thinking: string;
    }>({
        chatId: null,
        messageId: null,
        turnId: null,
        content: '',
        thinking: '',
    });

    // ── Stream buffer helpers ─────────────────────────────────────────────

    const clearStreamFlushTimer = () => {
        if (streamFlushTimerRef.current !== null) {
            window.clearTimeout(streamFlushTimerRef.current);
            streamFlushTimerRef.current = null;
        }
    };

    const clearReconnectTimer = () => {
        if (reconnectTimerRef.current !== null) {
            window.clearTimeout(reconnectTimerRef.current);
            reconnectTimerRef.current = null;
        }
    };

    const clearConnectTimer = () => {
        if (connectTimerRef.current !== null) {
            window.clearTimeout(connectTimerRef.current);
            connectTimerRef.current = null;
        }
    };

    const streamKey = (
        chatId: string | undefined,
        messageId: string | undefined,
        turnId: string | undefined,
    ) => {
        if (!chatId || (!messageId && !turnId)) return null;
        // Prefer the stable assistant message id, falling back to the turn id
        // for older/partial server events that do not carry both fields.
        return `${chatId}:${messageId || turnId}`;
    };

    const rememberSettledStream = (key: string | null) => {
        if (!key) return;
        const settled = settledStreamKeysRef.current;
        settled.add(key);
        // Turn IDs are unique, but keep the guard bounded for long-lived tabs.
        if (settled.size > 256) {
            const oldest = settled.values().next().value;
            if (oldest) settled.delete(oldest);
        }
    };

    const flushStreamBuffer = () => {
        clearStreamFlushTimer();
        const pending = streamBufferRef.current;
        if (!pending.chatId) return;

        const { chatId, messageId, turnId, content, thinking } = pending;
        streamBufferRef.current = { chatId: null, messageId: null, turnId: null, content: '', thinking: '' };

        if (chatId !== sessionIdRef.current) return;
        if (!content && !thinking) return;

        setMessages(prev =>
            upsertStreamDelta(prev, {
                messageId,
                turnId,
                contentDelta: content,
                thinkingDelta: thinking,
            })
        );
    };

    const queueStreamDelta = (
        chatId: string | undefined,
        messageId: string | undefined,
        turnId: string | undefined,
        contentDelta = '',
        thinkingDelta = ''
    ) => {
        if (!chatId || chatId !== sessionIdRef.current) return;
        const key = streamKey(chatId, messageId, turnId);
        if (key && settledStreamKeysRef.current.has(key)) return;

        if (
            streamBufferRef.current.chatId &&
            (
                streamBufferRef.current.chatId !== chatId ||
                streamBufferRef.current.messageId !== (messageId || null) ||
                streamBufferRef.current.turnId !== (turnId || null)
            )
        ) {
            flushStreamBuffer();
        }

        if (!streamBufferRef.current.chatId) {
            streamBufferRef.current.chatId = chatId;
            streamBufferRef.current.messageId = messageId || null;
            streamBufferRef.current.turnId = turnId || null;
        }
        if (contentDelta) streamBufferRef.current.content += contentDelta;
        if (thinkingDelta) streamBufferRef.current.thinking += thinkingDelta;

        const renderKey = `${chatId}:${messageId || turnId || 'assistant'}`;
        const classification = classifyStreamDelta(
            streamRenderStateRef.current,
            renderKey,
            contentDelta,
            thinkingDelta,
        );
        streamRenderStateRef.current = classification.next;
        if (classification.immediate) {
            flushStreamBuffer();
            return;
        }

        if (streamFlushTimerRef.current === null) {
            streamFlushTimerRef.current = window.setTimeout(() => {
                streamFlushTimerRef.current = null;
                flushStreamBuffer();
            }, STREAM_FLUSH_INTERVAL_MS);
        }
    };

    // Keep session-scoped buffers aligned without delaying first output.
    useEffect(() => {
        sessionIdRef.current = sessionId;
        if (streamFlushTimerRef.current !== null) {
            window.clearTimeout(streamFlushTimerRef.current);
            streamFlushTimerRef.current = null;
        }
        streamBufferRef.current = { chatId: null, messageId: null, turnId: null, content: '', thinking: '' };
        streamRenderStateRef.current = EMPTY_STREAM_RENDER_STATE;
        settledStreamKeysRef.current.clear();
    }, [sessionId]);

    // ── WebSocket connect ─────────────────────────────────────────────────

    const connectWebSocket = () => {
        if (!mountedRef.current) return;
        if (
            ws.current?.readyState === WebSocket.OPEN ||
            ws.current?.readyState === WebSocket.CONNECTING
        ) {
            return;
        }

        clearReconnectTimer();
        clearConnectTimer();
        connectTimerRef.current = window.setTimeout(() => {
            connectTimerRef.current = null;
            if (!mountedRef.current) return;
            if (
                ws.current?.readyState === WebSocket.OPEN ||
                ws.current?.readyState === WebSocket.CONNECTING
            ) {
                return;
            }

            const apiKey = localStorage.getItem('limebot_api_key');
            // Authenticate over the first WS frame instead of the query string so
            // the API key never lands in server logs, proxies, or browser history.
            const wsUrl = `${WS_BASE_URL}/ws`;

            const socket = new WebSocket(wsUrl);
            ws.current = socket;

            socket.onopen = () => {
                if (socket !== ws.current || !mountedRef.current) {
                    socket.close();
                    return;
                }
                console.log('Connected to LimeBot');
                // Send the auth frame first (WS preserves message order, so this
                // always reaches the server before any chat payload). Re-sent on
                // every reconnect because onopen runs again for each new socket.
                if (apiKey) {
                    try {
                        socket.send(JSON.stringify({ type: 'auth', api_key: apiKey }));
                    } catch (error) {
                        console.error('Failed to send WS auth frame:', error);
                    }
                }
                setIsConnected(true);
            };

            socket.onerror = (event) => {
                if (socket !== ws.current || !mountedRef.current) {
                    return;
                }
                console.error('WebSocket error:', event);
            };

            socket.onclose = () => {
                if (socket !== ws.current) {
                    return;
                }
                ws.current = null;
                console.log('Disconnected from LimeBot');
                setIsConnected(false);
                // Backend restarts invalidate its active-task registry. Do not
                // leave the composer locked to a request the new process cannot stop.
                setIsTyping(false);
                clearStreamFlushTimer();
                streamBufferRef.current = { chatId: null, messageId: null, turnId: null, content: '', thinking: '' };
                streamRenderStateRef.current = EMPTY_STREAM_RENDER_STATE;
                settledStreamKeysRef.current.clear();
                if (!mountedRef.current) {
                    return;
                }
                reconnectTimerRef.current = window.setTimeout(() => {
                    reconnectTimerRef.current = null;
                    console.log('Attempting to reconnect...');
                    connectWebSocket();
                }, 3000);
            };

            socket.onmessage = (event) => {
                if (socket !== ws.current) {
                    return;
                }
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'auth_ok') {
                        setIsConnected(true);
                        return;
                    }
                    const isMaintenanceEvent = data.type === 'maintenance' || data.metadata?.is_maintenance;
                    const eventChatId: string | undefined = typeof data.chat_id === 'string' ? data.chat_id : undefined;
                    const eventTurnId: string | undefined =
                        typeof data.turn_id === 'string'
                            ? data.turn_id
                            : typeof data.metadata?.turn_id === 'string'
                                ? data.metadata.turn_id
                                : undefined;
                    const eventMessageId: string | undefined =
                        typeof data.message_id === 'string'
                            ? data.message_id
                            : typeof data.metadata?.message_id === 'string'
                                ? data.metadata.message_id
                                : undefined;
                    const activeSessionId = sessionIdRef.current;
                    const streamType = data.metadata?.type;

                    if (isMaintenanceEvent) {
                        onActivity(data.content || '✨ Background task complete');
                        setTimeout(onActivityClear, 4000);
                        return;
                    }

                    if (eventChatId !== activeSessionId) return;

                    if (streamType === 'chunk') {
                        queueStreamDelta(eventChatId, eventMessageId, eventTurnId, data.content || '', '');
                        return;
                    }
                    if (streamType === 'thinking') {
                        queueStreamDelta(eventChatId, eventMessageId, eventTurnId, '', data.content || '');
                        return;
                    }

                    flushStreamBuffer();

                    if (data.type === 'message') {
                        rememberSettledStream(streamKey(eventChatId, eventMessageId, eventTurnId));
                        setIsTyping(false);
                        let variant: 'default' | 'destructive' | 'warning' = 'default';
                        if (data.metadata?.is_error) variant = 'destructive';
                        if (data.metadata?.is_warning) variant = 'warning';

                        setMessages(prev =>
                            applyFinalAssistantMessage(prev, {
                                messageId: eventMessageId,
                                turnId: eventTurnId,
                                content: data.content,
                                variant,
                                image: typeof data.metadata?.image === 'string' ? data.metadata.image : null,
                                attachments: normalizeIncomingAttachments(data.metadata?.attachments),
                                voiceUrl: typeof data.metadata?.voice_url === 'string' ? data.metadata.voice_url : undefined,
                            })
                        );

                        if (data.metadata?.identity_updated) {
                            onIdentityUpdated();
                        }
                    } else if (data.type === 'full_content') {
                        // This is an intermediate replacement snapshot used
                        // for tool-call sanitization/JSON buffering. The
                        // actual terminal reply is the later `message` event.
                        const key = streamKey(eventChatId, eventMessageId, eventTurnId);
                        if (!key || !settledStreamKeysRef.current.has(key)) {
                            setIsTyping(true);
                            setMessages(prev =>
                                applyStreamSnapshot(prev, {
                                    messageId: eventMessageId,
                                    turnId: eventTurnId,
                                    content: data.content || '',
                                })
                            );
                        }
                    } else if (data.type === 'cancellation' || data.metadata?.is_cancellation) {
                        rememberSettledStream(streamKey(eventChatId, eventMessageId, eventTurnId));
                        setIsTyping(false);
                        setMessages(prev => prev.map(m => {
                            if (m.type === 'tool' && m.toolExecution?.status === 'running') {
                                return {
                                    ...m,
                                    toolExecution: { ...m.toolExecution, status: 'error', result: 'Cancelled by user.' },
                                };
                            }
                            return m;
                        }));
                    } else if (data.type === 'stop_typing' || data.metadata?.type === 'stop_typing') {
                        setIsTyping(false);
                        flushStreamBuffer();
                        setMessages(prev => applyStopTyping(prev, { messageId: eventMessageId, turnId: eventTurnId }));
                    } else if (data.type === 'typing' || data.metadata?.type === 'typing') {
                        setIsTyping(true);
                    } else if (data.type === 'rate_limit_error') {
                        console.error('Rate Limit Error:', data.metadata?.details);
                        onRateLimit();
                    } else if (data.type === 'tool_execution') {
                        const toolData = data.metadata;
                        setMessages(prev =>
                            upsertToolExecution(prev, {
                                messageId: eventMessageId,
                                turnId: eventTurnId,
                                content: data.content,
                                toolExecution: {
                                    tool: toolData.tool,
                                    status: toolData.status,
                                    args: toolData.args,
                                    tool_call_id: toolData.tool_call_id,
                                    result: toolData.result,
                                    conf_id: toolData.conf_id,
                                    logs: [],
                                    preview: toolData.preview,
                                },
                            })
                        );
                    } else if (data.metadata?.type === 'changeset') {
                        const changeSet = data.metadata?.changeset;
                        if (changeSet && typeof changeSet === 'object') {
                            setMessages(prev =>
                                upsertChangeSet(prev, {
                                    messageId: eventMessageId,
                                    turnId: eventTurnId,
                                    changeSet,
                                })
                            );
                        }
                    } else if (data.metadata?.type === 'coding_plan') {
                        const plan = data.metadata?.plan;
                        if (plan && typeof plan === 'object') {
                            setMessages(prev =>
                                upsertChangeSet(prev, {
                                    messageId: eventMessageId,
                                    turnId: eventTurnId,
                                    changeSet: {
                                        ...plan,
                                        artifact_type: 'coding_plan',
                                        status: 'planned',
                                    },
                                })
                            );
                        }
                    } else if (data.metadata?.type === 'activity') {
                        console.log('👻 Activity:', data.metadata.text);
                        onActivity(data.metadata.text);
                        setTimeout(onActivityClear, 4000);
                    }
                } catch (error) {
                    console.error('Error parsing message:', error);
                }
            };
        }, 120);
    };

    // ── Send / new chat ───────────────────────────────────────────────────

    const handleSendMessage = (
        contentOverride?: string | null,
        attachment?: ChatAttachment | null,
        options: SendMessageOptions = {}
    ) => {
        const finalContent = contentOverride || inputValue.trim();
        if ((!finalContent && !attachment) || !ws.current || ws.current.readyState !== WebSocket.OPEN || isTyping) return;
        const { echoUserMessage = true, metadata } = options;
        const clientMessageId = `usr_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`;

        setIsTyping(true);
        const attachments = attachment
            ? [
                {
                    name: attachment.name,
                    mimeType: attachment.mimeType,
                    kind: attachment.kind,
                    data_url: attachment.url,
                },
            ]
            : [];
        const image = attachment?.kind === 'image' ? attachment.url : null;
        const payload: Record<string, unknown> = {
            content: finalContent,
            image,
            attachments,
            chat_id: sessionId,
            client_message_id: clientMessageId,
        };
        if (metadata && Object.keys(metadata).length > 0) {
            payload.metadata = metadata;
        }

        ws.current.send(JSON.stringify(payload));
        if (echoUserMessage) {
            setMessages(prev => [
                ...prev,
                {
                    sender: 'user',
                    content: finalContent,
                    image,
                    attachments: attachment ? [attachment] : undefined,
                    messageId: clientMessageId,
                },
            ]);
        }
        setInputValue('');
    };

    const editMessage = async (
        targetMessageId: string,
        nextContent: string,
        options: SendMessageOptions = {}
    ) => {
        const trimmedContent = nextContent.trim();
        if (!targetMessageId || !trimmedContent) {
            throw new Error('A target message and new content are required.');
        }

        const userTurnIndex = getUserTurnIndex(messages, targetMessageId);
        if (userTurnIndex < 0) {
            throw new Error('The selected message is no longer editable.');
        }

        const apiKey = localStorage.getItem('limebot_api_key');
        const response = await axios.post(
            `${API_BASE_URL}/api/chat/${encodeURIComponent(sessionIdRef.current)}/messages/${encodeURIComponent(targetMessageId)}/edit`,
            {
                content: trimmedContent,
                user_turn_index: userTurnIndex,
                metadata: options.metadata || {},
            },
            {
                headers: {
                    'Content-Type': 'application/json',
                    ...(apiKey ? { 'X-API-Key': apiKey } : {}),
                },
            }
        );

        const replayMessageId =
            typeof response.data?.message_id === 'string' && response.data.message_id
                ? response.data.message_id
                : targetMessageId;

        clearStreamFlushTimer();
        streamBufferRef.current = { chatId: null, messageId: null, turnId: null, content: '', thinking: '' };
        streamRenderStateRef.current = EMPTY_STREAM_RENDER_STATE;
        setMessages(prev =>
            applyUserMessageEdit(prev, targetMessageId, trimmedContent, replayMessageId)
        );
        setInputValue('');
        setIsTyping(true);
        return response.data;
    };

    const handleNewChat = () => {
        flushStreamBuffer();
        clearStreamFlushTimer();
        streamBufferRef.current = { chatId: null, messageId: null, turnId: null, content: '', thinking: '' };
        streamRenderStateRef.current = EMPTY_STREAM_RENDER_STATE;
        const newId = crypto.randomUUID();
        setSessionId(newId);
        setMessages([]);
        setIsTyping(false);
        console.log('Started new session:', newId);
    };

    // ── Cleanup on unmount ────────────────────────────────────────────────

    useEffect(() => {
        mountedRef.current = true;

        return () => {
            mountedRef.current = false;
            clearReconnectTimer();
            clearConnectTimer();
            clearStreamFlushTimer();
            streamBufferRef.current = { chatId: null, messageId: null, turnId: null, content: '', thinking: '' };
            streamRenderStateRef.current = EMPTY_STREAM_RENDER_STATE;
            ws.current?.close();
            ws.current = null;
        };
    }, []);

    return {
        messages,
        setMessages,
        inputValue,
        setInputValue,
        isConnected,
        isTyping,
        sessionId,
        connectWebSocket,
        handleSendMessage,
        editMessage,
        handleNewChat,
        ws,
    };
}
