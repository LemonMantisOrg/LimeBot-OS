import { useRef, useEffect, useState, memo } from 'react';
import { api, API_BASE_URL } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { BotVisual } from "@/components/pet/BotVisual";
import { cn } from "@/lib/utils";
import { Send, Brain, Power, Paperclip, X, Plus, ArrowDown, ShieldAlert, Wifi, WifiOff, Play, Pause, Volume2, VolumeX, Download, Square, Pencil } from "lucide-react";
import {
    AlertDialog,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Loader2 } from "lucide-react";
import { ToolCard, ToolExecution } from './ToolCard';
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertTriangle, Info } from "lucide-react";
import { toast } from "sonner";
import type { ChatAttachment, ChatChangeSet } from "@/lib/chat-state";
import { renderableAttachmentsForMessage } from "@/lib/chat-media";
import { readinessLabel, type AgentReadiness } from "@/lib/agent-readiness";


import { ToolTimeline } from './ToolTimeline';
import { ChangeSetCard } from './ChangeSetCard';
import { AttachmentPreview } from './AttachmentPreview';
import { MarkdownMessage } from './MarkdownMessage';
import { parseSubagentReport, SubagentReportCard } from './SubagentReportCard';
import { SystemUpdateCard } from './SystemUpdateCard';

interface Message {
    sender: 'user' | 'bot';
    type?: 'text' | 'tool' | 'confirmation' | 'changeset';
    content: string;
    thinking?: string;
    isStreaming?: boolean;
    image?: string | null;
    attachments?: ChatAttachment[];
    toolExecution?: ToolExecution;
    changeSet?: ChatChangeSet;
    variant?: 'default' | 'destructive' | 'warning';
    messageId?: string;
    turnId?: string;
    voiceUrl?: string;
}

interface SkillOption {
    id: string;
    name: string;
    description: string;
    enabled: boolean;
    active: boolean;
    source?: string;
}

type ChatSendOptions = {
    echoUserMessage?: boolean;
    metadata?: Record<string, unknown>;
};

interface ChatInterfaceProps {
    messages: Message[];
    inputValue: string;
    isConnected: boolean;
    isTyping?: boolean;
    botIdentity?: { name: string; avatar: string | null };
    onInputChange: (value: string) => void;
    onSendMessage: (
        content?: string | null,
        attachment?: ChatAttachment | null,
        options?: ChatSendOptions,
    ) => void;
    onEditMessage: (
        targetMessageId: string,
        content: string,
        options?: ChatSendOptions,
    ) => Promise<unknown>;
    onReconnect: () => void;
    onNewChat?: () => void;
    activeChatId: string;
    activityText?: string | null;
    agentReadiness: AgentReadiness;
    onRetryReadiness: () => void;
}

const QUICK_ACTIONS = [
    { label: "Project status", prompt: "Review the current project state and tell me what needs attention." },
    { label: "Session recap", prompt: "Summarize what you remember about this session and what should happen next." },
    { label: "UI suggestion", prompt: "Inspect the codebase and propose the next UI improvement." },
];

const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
const ACCEPTED_ATTACHMENT_TYPES = [
    "image/*",
    ".pdf",
    ".doc",
    ".docx",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
].join(",");

const WORD_DOCUMENT_MIME_TYPES = new Set([
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]);

type PonytailMode = 'off' | 'full';

const PONYTAIL_MODE_STORAGE_KEY = 'limebot_ponytail_mode';

type SlashSuggestion = {
    id: string;
    title: string;
    description: string;
    insertText: string;
    badge: string;
    kind: 'command' | 'skill';
};

type SelectedSkillState = {
    id: string;
    name: string;
    description: string;
};

const BASE_SLASH_COMMANDS: SlashSuggestion[] = [
    {
        id: 'command-skills',
        title: '/skills',
        description: 'List the skills currently enabled for this session.',
        insertText: '/skills',
        badge: 'Command',
        kind: 'command',
    },
    {
        id: 'command-skill',
        title: '/skill',
        description: 'Choose a skill and then describe the task.',
        insertText: '/skill ',
        badge: 'Command',
        kind: 'command',
    },
];

type EditDraftState = {
    messageId: string;
    originalContent: string;
};

function getSlashSuggestions(inputValue: string, skills: SkillOption[]): SlashSuggestion[] {
    if (!inputValue.startsWith('/') || inputValue.includes('\n')) {
        return [];
    }

    const activeSkills = skills
        .filter((skill) => skill.enabled && skill.active)
        .sort((a, b) => a.name.localeCompare(b.name));
    const trimmed = inputValue.trimEnd();

    if (trimmed === '/') {
        const skillSuggestions: SlashSuggestion[] = activeSkills.map((skill) => ({
            id: `skill-${skill.id}`,
            title: `/${skill.name}`,
            description: skill.description || 'Enabled skill',
            insertText: `/${skill.name} `,
            badge: 'Skill',
            kind: 'skill',
        }));
        return [
            ...BASE_SLASH_COMMANDS,
            ...skillSuggestions,
        ];
    }

    if (trimmed.startsWith('/skill ')) {
        const nameQuery = trimmed.slice('/skill '.length).trimStart();
        if (nameQuery.includes(' ')) {
            return [];
        }
        const normalizedQuery = nameQuery.toLowerCase();
        return activeSkills
            .filter((skill) => {
                if (!normalizedQuery) return true;
                return (
                    skill.name.toLowerCase().includes(normalizedQuery) ||
                    skill.description.toLowerCase().includes(normalizedQuery)
                );
            })
            .map<SlashSuggestion>((skill) => ({
                id: `skill-verbose-${skill.id}`,
                title: skill.name,
                description: skill.description || 'Enabled skill',
                insertText: `/skill ${skill.name} `,
                badge: 'Skill',
                kind: 'skill',
            }));
    }

    const shorthandQuery = trimmed.slice(1);
    if (shorthandQuery.includes(' ')) {
        return [];
    }
    const normalizedQuery = shorthandQuery.toLowerCase();
    const commandSuggestions = BASE_SLASH_COMMANDS.filter((suggestion) =>
        suggestion.title.toLowerCase().includes(`/${normalizedQuery}`)
    );

    const skillSuggestions: SlashSuggestion[] = activeSkills
        .filter((skill) => {
            if (!normalizedQuery) return true;
            return (
                skill.name.toLowerCase().includes(normalizedQuery) ||
                skill.description.toLowerCase().includes(normalizedQuery)
            );
        })
        .map((skill) => ({
            id: `skill-${skill.id}`,
            title: `/${skill.name}`,
            description: skill.description || 'Enabled skill',
            insertText: `/${skill.name} `,
            badge: 'Skill',
            kind: 'skill',
        }));

    return [...commandSuggestions, ...skillSuggestions];
}

function StatusChip({
    label,
    value,
    tone = 'default',
}: {
    label: string;
    value: string;
    tone?: 'default' | 'good' | 'warn';
}) {
    return (
        <div
            className={cn(
                "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em]",
                tone === 'good' && "border-primary/20 bg-primary/10 text-primary",
                tone === 'warn' && "border-amber-500/20 bg-amber-500/10 text-amber-500",
                tone === 'default' && "border-border bg-background/70 text-muted-foreground"
            )}
        >
            <span>{label}</span>
            <span className="normal-case tracking-normal text-foreground">{value}</span>
        </div>
    );
}

function isSubagentReportMessage(msg?: Message | null): boolean {
    if (!msg || msg.sender !== 'bot') return false;
    return !!parseSubagentReport(msg.content || '');
}

function isSpawnAgentToolMessage(msg?: Message | null): boolean {
    return msg?.type === 'tool' && msg.toolExecution?.tool === 'spawn_agent';
}

function isBotTextLikeMessage(msg?: Message | null): boolean {
    return !!msg && msg.sender === 'bot' && msg.type !== 'tool' && !msg.isStreaming;
}

type ToolExecutionWithTurn = ToolExecution & {
    turnId?: string;
};

function shouldHideSpawnAgentGroup(toolGroup: ToolExecutionWithTurn[], nextMessage?: Message | null): boolean {
    if (!toolGroup.length || !toolGroup.every((execution) => execution.tool === 'spawn_agent')) {
        return false;
    }
    if (isSubagentReportMessage(nextMessage)) {
        return true;
    }
    if (!isBotTextLikeMessage(nextMessage)) {
        return false;
    }

    const nextTurnId = String(nextMessage?.turnId || '').trim();
    if (!nextTurnId) {
        return false;
    }

    return toolGroup.some((execution) => {
        const executionTurnId = String(execution.turnId || '').trim();
        return executionTurnId && executionTurnId === nextTurnId;
    });
}

function isSubagentOrchestrationThought(
    msg?: Message | null,
    prevMsg?: Message | null,
    nextMsg?: Message | null
): boolean {
    if (!msg || msg.sender !== 'bot' || !msg.thinking) return false;
    if (String(msg.content || '').trim()) return false;
    if (msg.attachments?.length || msg.image) return false;

    return (
        isSpawnAgentToolMessage(prevMsg) ||
        isSpawnAgentToolMessage(nextMsg) ||
        isSubagentReportMessage(prevMsg) ||
        isSubagentReportMessage(nextMsg)
    );
}

const UnreadSeparator = ({ count }: { count: number }) => (
    <div className="flex items-center gap-3 py-1">
        <div className="h-px flex-1 bg-primary/20" />
        <div className="rounded-full border border-primary/20 bg-primary/10 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-primary shadow-sm">
            {count <= 1 ? "New Message" : `${count} New Messages`}
        </div>
        <div className="h-px flex-1 bg-primary/20" />
    </div>
);

function TypingIndicator({
    botIdentity,
}: {
    botIdentity?: { name: string; avatar: string | null };
}) {
    return (
        <div className="flex w-full max-w-[48rem] gap-3 animate-in fade-in slide-in-from-bottom-2 duration-200">
            <BotVisual
                avatar={botIdentity?.avatar}
                size="chat"
            />
            <div className="flex items-center">
                <div className="flex items-center gap-1 rounded-2xl bg-muted/50 px-4 py-3 text-muted-foreground">
                    <span
                        className="h-2 w-2 rounded-full bg-muted-foreground/60"
                        style={{ animation: 'typing-dot 1.2s infinite ease-in-out', animationDelay: '0ms' }}
                    />
                    <span
                        className="h-2 w-2 rounded-full bg-muted-foreground/60"
                        style={{ animation: 'typing-dot 1.2s infinite ease-in-out', animationDelay: '200ms' }}
                    />
                    <span
                        className="h-2 w-2 rounded-full bg-muted-foreground/60"
                        style={{ animation: 'typing-dot 1.2s infinite ease-in-out', animationDelay: '400ms' }}
                    />
                </div>
            </div>
        </div>
    );
}

export function VoiceAudioPlayer({ url }: { url: string }) {
    const audioRef = useRef<HTMLAudioElement | null>(null);
    const [isPlaying, setIsPlaying] = useState(false);
    const [duration, setDuration] = useState(0);
    const [currentTime, setCurrentTime] = useState(0);
    const [isMuted, setIsMuted] = useState(false);

    useEffect(() => {
        const audio = new Audio(`${API_BASE_URL}${url}`);
        audioRef.current = audio;

        const onTimeUpdate = () => setCurrentTime(audio.currentTime);
        const onLoadedMetadata = () => setDuration(audio.duration);
        const onEnded = () => setIsPlaying(false);

        audio.addEventListener('timeupdate', onTimeUpdate);
        audio.addEventListener('loadedmetadata', onLoadedMetadata);
        audio.addEventListener('ended', onEnded);

        return () => {
            audio.pause();
            audio.removeEventListener('timeupdate', onTimeUpdate);
            audio.removeEventListener('loadedmetadata', onLoadedMetadata);
            audio.removeEventListener('ended', onEnded);
            audioRef.current = null;
        };
    }, [url]);

    const togglePlay = () => {
        if (!audioRef.current) return;
        if (isPlaying) {
            audioRef.current.pause();
            setIsPlaying(false);
        } else {
            audioRef.current.play().catch(err => console.error("Audio playback failed:", err));
            setIsPlaying(true);
        }
    };

    const toggleMute = () => {
        if (!audioRef.current) return;
        audioRef.current.muted = !isMuted;
        setIsMuted(!isMuted);
    };

    const formatTime = (time: number) => {
        if (isNaN(time)) return '0:00';
        const mins = Math.floor(time / 60);
        const secs = Math.floor(time % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    const handleProgressChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (!audioRef.current) return;
        const newTime = parseFloat(e.target.value);
        audioRef.current.currentTime = newTime;
        setCurrentTime(newTime);
    };

    return (
        <div className="mt-3 flex flex-col gap-2 rounded-xl border border-primary/20 bg-primary/5 p-3 max-w-sm animate-in fade-in slide-in-from-bottom-2 duration-300">
            <div className="flex items-center gap-2">
                <Button
                    variant="outline"
                    size="icon"
                    onClick={togglePlay}
                    className="h-8 w-8 shrink-0 rounded-full border-primary/20 bg-background hover:bg-primary/10 hover:text-primary transition-all duration-200"
                >
                    {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
                </Button>

                <div className="flex-1 min-w-0 flex flex-col gap-1">
                    <span className="text-[11px] font-bold text-primary tracking-wide uppercase">Voice Audio Response</span>
                    <input
                        type="range"
                        min={0}
                        max={duration || 1}
                        value={currentTime}
                        onChange={handleProgressChange}
                        className="h-1 w-full bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                    />
                    <div className="flex justify-between text-[10px] text-muted-foreground font-mono">
                        <span>{formatTime(currentTime)}</span>
                        <span>{formatTime(duration)}</span>
                    </div>
                </div>

                <div className="flex items-center gap-1">
                    <Button
                        variant="ghost"
                        size="icon"
                        onClick={toggleMute}
                        className="h-7 w-7 text-muted-foreground hover:text-foreground"
                    >
                        {isMuted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
                    </Button>
                    <a
                        href={`${API_BASE_URL}${url}`}
                        download
                        className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                    >
                        <Download className="h-3.5 w-3.5" />
                    </a>
                </div>
            </div>
        </div>
    );
}

const MemoizedMessageItem = memo(({
    msg,
    botIdentity,
    handleToolConfirmSideChannel,
    onSendMessage,
    onStartEdit,
    canEdit,
    showAvatar,
    showHeader
}: {
    msg: Message;
    botIdentity: ChatInterfaceProps['botIdentity'];
    handleToolConfirmSideChannel: (confId: string, approved: boolean, sessionWhitelist: boolean) => Promise<void>;
    onSendMessage: ChatInterfaceProps['onSendMessage'];
    onStartEdit: (message: Message) => void;
    canEdit: boolean;
    showAvatar: boolean;
    showHeader: boolean;
}) => {
    const isUser = msg.sender === 'user';
    const isBot = msg.sender === 'bot';
    const renderableAttachments = renderableAttachmentsForMessage(msg);
    const subagentReport = !isUser ? parseSubagentReport(msg.content) : null;

    if (msg.content?.includes('[CONFIRM_SESSION]') || msg.content?.includes('[CONFIRM_EXECUTION]') || msg.type === 'confirmation') {
        return null; // Hidden confirmation trace messages
    }

    return (
        <div className={cn(
            "flex w-full gap-3",
            isUser ? "justify-end" : "max-w-[48rem]"
        )}>
            {isBot && showAvatar ? (
                <BotVisual
                    avatar={botIdentity?.avatar}
                    size="chat"
                />
            ) : isBot ? (
                <div className="h-8 w-8 shrink-0" />
            ) : (
                <div className="hidden" />
            )}

            <div className={cn(
                "flex min-w-0 flex-1 flex-col gap-0.5",
                isUser ? "items-end" : "items-start"
            )}>
                {!isUser && showHeader && (
                    <div className="ml-0.5 text-[11px] font-medium text-muted-foreground/70">
                        {botIdentity?.name || "LimeBot"}
                    </div>
                )}

                {msg.type === 'changeset' && msg.changeSet ? (
                    <div className="w-full max-w-2xl">
                        <ChangeSetCard changeSet={msg.changeSet} />
                    </div>
                ) : msg.type === 'tool' && msg.toolExecution ? (
                    <div className="w-full max-w-2xl">
                        <ToolCard
                            execution={msg.toolExecution}
                            onConfirmSideChannel={handleToolConfirmSideChannel}
                            onConfirm={(toolCallId, approved) => {
                                if (toolCallId !== msg.toolExecution?.tool_call_id) return;
                                if (approved) {
                                    const argsStr = JSON.stringify(msg.toolExecution?.args).slice(0, 500);
                                    onSendMessage(`[CONFIRM_EXECUTION] Proceed with ${msg.toolExecution?.tool} using args: ${argsStr}`);
                                } else {
                                    onSendMessage(`Cancel execution of ${msg.toolExecution?.tool}`);
                                }
                            }}
                            onConfirmSession={() => {
                                const argsStr = JSON.stringify(msg.toolExecution?.args).slice(0, 500);
                                onSendMessage(`[CONFIRM_SESSION] Proceed with ${msg.toolExecution?.tool} using args: ${argsStr}`);
                            }}
                        />
                    </div>
                ) : msg.variant && msg.variant !== 'default' ? (
                    <Alert variant={msg.variant} className="max-w-xl shadow-sm border-l-4">
                        {msg.variant === 'destructive' ? <AlertTriangle className="h-4 w-4" /> : <Info className="h-4 w-4" />}
                        <AlertTitle className="ml-2 font-bold">{msg.variant === 'destructive' ? 'Error' : 'Warning'}</AlertTitle>
                        <AlertDescription className="ml-2 mt-1 text-xs opacity-90">{msg.content}</AlertDescription>
                    </Alert>
                ) : (
                    <>
                        {(msg.content || renderableAttachments.length > 0) && (
                            <div className={cn(
                                "relative max-w-full overflow-hidden transition-all duration-200",
                                isUser
                                    ? "group max-w-[min(82%,30rem)] rounded-2xl rounded-tr-md bg-user-bubble px-3.5 py-2 text-[14px] leading-tight text-user-bubble-foreground shadow-sm transition-all duration-300 hover:shadow-md"
                                    : renderableAttachments.length > 0
                                        ? "chat-media-bubble w-full max-w-[min(100%,28rem)] rounded-2xl rounded-tl-md border border-border/70 bg-muted/35 p-2 text-foreground shadow-sm"
                                        : "w-full bg-transparent px-0 py-0 text-foreground"
                            )}>
                                <div className="max-w-full overflow-x-auto whitespace-normal break-words">
                                    {renderableAttachments.map((attachment) => (
                                        <AttachmentPreview
                                            key={`${attachment.kind}:${attachment.name}:${attachment.url.slice(0, 24)}`}
                                            attachment={attachment}
                                        />
                                    ))}
                                    {(() => {
                                        const trimmedContent = msg.content?.trim() || "";
                                        const isSystemUpdate = !isUser && (trimmedContent === "(Persona configuration updated.)" || trimmedContent === "(System updated configuration/memory files.)");

                                        if (isSystemUpdate) {
                                            return <SystemUpdateCard content={trimmedContent} />;
                                        } else if (subagentReport) {
                                            return <SubagentReportCard report={subagentReport} />;
                                        } else {
                                            return (
                                                <MarkdownMessage
                                                    content={msg.content}
                                                    isUser={isUser}
                                                    isStreaming={msg.isStreaming}
                                                />
                                            );
                                        }
                                    })()}
                                    {msg.voiceUrl && (
                                        <VoiceAudioPlayer url={msg.voiceUrl} />
                                    )}
                                </div>
                                {isUser && canEdit && (
                                    <button
                                        type="button"
                                        onClick={() => onStartEdit(msg)}
                                        className="absolute -left-10 top-2 hidden rounded-full border border-border/70 bg-background/95 p-1.5 text-muted-foreground shadow-sm transition-colors hover:text-foreground group-hover:inline-flex"
                                        aria-label="Edit message"
                                        title="Edit and rerun from here"
                                    >
                                        <Pencil className="h-3.5 w-3.5" />
                                    </button>
                                )}
                            </div>
                        )}
                    </>
                )
                }
            </div >
        </div >
    );
});

export function ChatInterface({
    messages,
    inputValue,
    isConnected,
    isTyping,
    botIdentity,
    onInputChange,
    onSendMessage,
    onEditMessage,
    onNewChat,
    activeChatId,
    activityText,
    agentReadiness,
    onRetryReadiness,
}: ChatInterfaceProps) {
    const scrollAreaRef = useRef<HTMLDivElement>(null);
    const scrollRef = useRef<HTMLDivElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const [selectedAttachment, setSelectedAttachment] = useState<ChatAttachment | null>(null);
    const [isDraggingFile, setIsDraggingFile] = useState(false);
    const dragDepthRef = useRef(0);
    const [isAtBottom, setIsAtBottom] = useState(true);
    const [unreadAnchorIndex, setUnreadAnchorIndex] = useState<number | null>(null);
    const [unreadCount, setUnreadCount] = useState(0);
    const [skills, setSkills] = useState<SkillOption[]>([]);
    const [skillsLoading, setSkillsLoading] = useState(false);
    const [dismissedSlashValue, setDismissedSlashValue] = useState<string | null>(null);
    const [highlightedSlashIndex, setHighlightedSlashIndex] = useState(0);
    const [selectedSkill, setSelectedSkill] = useState<SelectedSkillState | null>(null);
    const [editDraft, setEditDraft] = useState<EditDraftState | null>(null);
    const [editSubmitting, setEditSubmitting] = useState(false);
    const [ponytailMode, setPonytailMode] = useState<PonytailMode>(() =>
        localStorage.getItem(PONYTAIL_MODE_STORAGE_KEY) === 'full' ? 'full' : 'off'
    );
    const isAtBottomRef = useRef(true);
    const prevMessageCountRef = useRef(messages.length);
    const ponytailActive = ponytailMode === 'full';
    const sendMetadata: Record<string, unknown> = {};
    if (ponytailActive) {
        sendMetadata.ponytail_mode = ponytailMode;
    }
    if (selectedSkill?.name) {
        sendMetadata.skill_name = selectedSkill.name;
    }
    const ponytailSendOptions: ChatSendOptions | undefined =
        Object.keys(sendMetadata).length > 0
            ? { metadata: sendMetadata }
            : undefined;
    const slashSuggestions = getSlashSuggestions(inputValue, skills);
    const showSlashMenu =
        slashSuggestions.length > 0 &&
        dismissedSlashValue !== inputValue &&
        !selectedAttachment;
    const isEditing = !!editDraft;

    const getScrollViewport = () =>
        scrollAreaRef.current?.querySelector('[data-radix-scroll-area-viewport]') as HTMLDivElement | null;

    const clearUnread = () => {
        setUnreadAnchorIndex(null);
        setUnreadCount(0);
    };

    const syncBottomState = (forceClearUnread = false) => {
        const viewport = getScrollViewport();
        if (!viewport) return;

        const atBottom = viewport.scrollHeight - (viewport.scrollTop + viewport.clientHeight) <= 96;
        isAtBottomRef.current = atBottom;
        setIsAtBottom(atBottom);

        if (atBottom || forceClearUnread) {
            clearUnread();
        }
    };

    const scrollToLatest = (behavior: ScrollBehavior = 'smooth') => {
        const viewport = getScrollViewport();
        if (viewport) {
            viewport.scrollTo({ top: viewport.scrollHeight, behavior });
        } else if (scrollRef.current) {
            scrollRef.current.scrollIntoView({ behavior });
        }
        isAtBottomRef.current = true;
        setIsAtBottom(true);
        clearUnread();

        requestAnimationFrame(() => {
            requestAnimationFrame(() => syncBottomState(true));
        });
    };


    useEffect(() => {
        if (!scrollRef.current) return;
        const lastMsg = messages[messages.length - 1];
        const shouldStick = isAtBottomRef.current || lastMsg?.sender === 'user';
        if (shouldStick) {
            scrollToLatest(lastMsg?.sender === 'user' ? 'smooth' : 'auto');
            return;
        }
        requestAnimationFrame(() => syncBottomState(false));
    }, [messages, selectedAttachment]);

    useEffect(() => {
        const previousCount = prevMessageCountRef.current;
        const nextCount = messages.length;

        if (nextCount < previousCount) {
            clearUnread();
        } else if (nextCount > previousCount && !isAtBottomRef.current) {
            setUnreadAnchorIndex((current) => current ?? previousCount);
        }

        prevMessageCountRef.current = nextCount;
    }, [messages.length]);

    useEffect(() => {
        localStorage.setItem(PONYTAIL_MODE_STORAGE_KEY, ponytailMode);
    }, [ponytailMode]);

    useEffect(() => {
        let cancelled = false;

        const loadSkills = async () => {
            setSkillsLoading(true);
            try {
                const res = await api.get('/api/skills');
                if (!cancelled) {
                    setSkills(Array.isArray(res.data?.skills) ? res.data.skills : []);
                }
            } catch (err) {
                console.error("Failed to load slash skills:", err);
            } finally {
                if (!cancelled) {
                    setSkillsLoading(false);
                }
            }
        };

        loadSkills();
        return () => {
            cancelled = true;
        };
    }, []);

    useEffect(() => {
        setHighlightedSlashIndex(0);
    }, [inputValue]);

    useEffect(() => {
        if (!showSlashMenu) {
            setHighlightedSlashIndex(0);
            return;
        }
        setHighlightedSlashIndex((current) =>
            Math.min(current, Math.max(slashSuggestions.length - 1, 0))
        );
    }, [showSlashMenu, slashSuggestions.length]);

    useEffect(() => {
        if (unreadAnchorIndex === null) {
            if (unreadCount !== 0) {
                setUnreadCount(0);
            }
            return;
        }

        const nextUnreadCount = Math.max(messages.length - unreadAnchorIndex, 0);
        if (nextUnreadCount !== unreadCount) {
            setUnreadCount(nextUnreadCount);
        }
    }, [messages.length, unreadAnchorIndex, unreadCount]);

    useEffect(() => {
        setIsAtBottom(true);
        isAtBottomRef.current = true;
        clearUnread();
        prevMessageCountRef.current = messages.length;
        setEditDraft(null);
        setEditSubmitting(false);
    }, [activeChatId]);

    const runningTool = [...messages].reverse().find(
        (m) => m.type === 'tool' && (m.toolExecution?.status === 'running' || m.toolExecution?.status === 'planned')
    );
    const waitingTool = [...messages].reverse().find(
        (m) => m.type === 'tool' && (m.toolExecution?.status === 'waiting_confirmation' || m.toolExecution?.status === 'pending_confirmation')
    );
    const runningToolCount = messages.filter(
        (m) => m.type === 'tool' && (m.toolExecution?.status === 'running' || m.toolExecution?.status === 'planned')
    ).length;
    const waitingToolCount = messages.filter(
        (m) => m.type === 'tool' && (m.toolExecution?.status === 'waiting_confirmation' || m.toolExecution?.status === 'pending_confirmation')
    ).length;
    const sessionLabel = activeChatId.slice(0, 8).toUpperCase();
    const waitingExecution = waitingTool?.toolExecution;
    const runningExecution = runningTool?.toolExecution;

    let railTitle = "Ready";
    let railTone: 'default' | 'good' | 'warn' = isConnected ? 'good' : 'default';

    if (!isConnected) {
        railTitle = "Gateway reconnecting";
        railTone = 'default';
    } else if (waitingExecution) {
        railTitle = waitingToolCount > 1 ? `${waitingToolCount} approvals needed` : "Approval required";
        railTone = 'warn';
    } else if (runningExecution) {
        railTitle = runningToolCount > 1 ? `Executing ${runningToolCount} tools` : "Executing tool";
        railTone = 'good';
    } else if (activityText) {
        railTitle = "Working";
        railTone = 'good';
    } else if (isTyping) {
        railTitle = "Drafting response";
        railTone = 'good';
    }

    const showComposerPrompts =
        !inputValue.trim() &&
        !selectedAttachment &&
        !isTyping &&
        messages.length < 4;

    const clearSelectedAttachment = () => {
        setSelectedAttachment(null);
        if (fileInputRef.current) {
            fileInputRef.current.value = '';
        }
    };

    const fileToAttachment = (file: File) =>
        new Promise<ChatAttachment>((resolve, reject) => {
            const displayName = file.name || (file.type.startsWith('image/') ? 'pasted-image.png' : 'attachment');
            if (file.size > MAX_ATTACHMENT_BYTES) {
                reject(new Error("Attachments are limited to 8 MB."));
                return;
            }

            const lowerName = displayName.toLowerCase();
            const mimeType = file.type || (
                lowerName.endsWith('.pdf')
                    ? 'application/pdf'
                    : lowerName.endsWith('.docx')
                        ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                        : lowerName.endsWith('.doc')
                            ? 'application/msword'
                            : 'application/octet-stream'
            );
            const isSupportedImage = mimeType.startsWith('image/');
            const isSupportedDocument =
                mimeType === 'application/pdf' ||
                WORD_DOCUMENT_MIME_TYPES.has(mimeType) ||
                /\.(pdf|doc|docx)$/i.test(file.name);

            if (!isSupportedImage && !isSupportedDocument) {
                reject(new Error("Only images, PDF, DOC, and DOCX files are supported."));
                return;
            }

            const reader = new FileReader();
            reader.onerror = () => reject(new Error("Failed to read the selected file."));
            reader.onloadend = () => {
                if (typeof reader.result !== 'string') {
                    reject(new Error("Failed to load the selected file."));
                    return;
                }
                resolve({
                    name: displayName,
                    mimeType,
                    kind: isSupportedImage ? 'image' : 'document',
                    url: reader.result,
                });
            };
            reader.readAsDataURL(file);
        });

    const handleKeyPress = (e: React.KeyboardEvent) => {
        if (showSlashMenu && slashSuggestions.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setHighlightedSlashIndex((current) => (current + 1) % slashSuggestions.length);
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                setHighlightedSlashIndex((current) =>
                    current === 0 ? slashSuggestions.length - 1 : current - 1
                );
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                const selectedSuggestion = slashSuggestions[highlightedSlashIndex];
                if (selectedSuggestion) {
                    if (selectedSuggestion.kind === 'skill') {
                        const matchedSkill = skills.find(
                            (skill) => `/${skill.name}` === selectedSuggestion.title
                        );
                        if (matchedSkill) {
                            setSelectedSkill({
                                id: matchedSkill.id,
                                name: matchedSkill.name,
                                description: matchedSkill.description || 'Enabled skill',
                            });
                            setDismissedSlashValue(null);
                            onInputChange('');
                            requestAnimationFrame(() => textareaRef.current?.focus());
                        }
                    } else {
                        setDismissedSlashValue(null);
                        onInputChange(selectedSuggestion.insertText);
                        requestAnimationFrame(() => {
                            textareaRef.current?.focus();
                            const length = selectedSuggestion.insertText.length;
                            textareaRef.current?.setSelectionRange(length, length);
                        });
                    }
                }
                return;
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                setDismissedSlashValue(inputValue);
                return;
            }
        }

        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    const handleSend = () => {
        if (!isConnected || !agentReadiness.ready) return;
        if (isEditing) {
            if (!editDraft || !inputValue.trim() || editSubmitting) return;
            setEditSubmitting(true);
            void onEditMessage(editDraft.messageId, inputValue, ponytailSendOptions)
                .then(() => {
                    clearSelectedAttachment();
                    setSelectedSkill(null);
                    setEditDraft(null);
                    if (textareaRef.current) {
                        textareaRef.current.style.height = 'auto';
                    }
                })
                .catch((error) => {
                    const description =
                        error instanceof Error ? error.message : "Failed to edit the selected message.";
                    toast.error("Edit failed", { description });
                })
                .finally(() => {
                    setEditSubmitting(false);
                });
            return;
        }
        if (inputValue.trim() || selectedAttachment) {
            onSendMessage(null, selectedAttachment, ponytailSendOptions);
            clearSelectedAttachment();
            setSelectedSkill(null);
            if (textareaRef.current) {
                textareaRef.current.style.height = 'auto';
            }
        }
    };

    const startEditingMessage = (message: Message) => {
        if (!message.messageId || message.sender !== 'user') return;
        setEditDraft({
            messageId: message.messageId,
            originalContent: message.content,
        });
        clearSelectedAttachment();
        onInputChange(message.content);
        setDismissedSlashValue(null);
        requestAnimationFrame(() => {
            textareaRef.current?.focus();
            const length = message.content.length;
            textareaRef.current?.setSelectionRange(length, length);
        });
    };

    const attachFile = async (file: File, failureTitle = "Attachment rejected") => {
        try {
            setSelectedAttachment(await fileToAttachment(file));
        } catch (error) {
            const description = error instanceof Error ? error.message : "Failed to attach file.";
            toast.error(failureTitle, { description });
            clearSelectedAttachment();
        }
    };

    const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        await attachFile(file);
    };

    const handlePaste = async (e: React.ClipboardEvent) => {
        const items = e.clipboardData.items;
        for (let i = 0; i < items.length; i++) {
            if (items[i].type.indexOf('image') !== -1) {
                const blob = items[i].getAsFile();
                if (blob) {
                    await attachFile(blob, "Attachment rejected");
                    e.preventDefault();
                    break;
                }
            }
        }
    };

    const handleComposerDragEnter = (e: React.DragEvent) => {
        e.preventDefault();
        dragDepthRef.current += 1;
        if (e.dataTransfer.types.includes("Files")) {
            setIsDraggingFile(true);
        }
    };

    const handleComposerDragOver = (e: React.DragEvent) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
    };

    const handleComposerDragLeave = (e: React.DragEvent) => {
        e.preventDefault();
        dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
        if (dragDepthRef.current === 0) {
            setIsDraggingFile(false);
        }
    };

    const handleComposerDrop = async (e: React.DragEvent) => {
        e.preventDefault();
        dragDepthRef.current = 0;
        setIsDraggingFile(false);
        if (isEditing) return;
        const file = e.dataTransfer.files?.[0];
        if (file) {
            await attachFile(file);
        }
    };

    const [powerModalOpen, setPowerModalOpen] = useState(false);
    const [powerLoading, setPowerLoading] = useState(false);

    const handlePowerAction = async (action: 'restart' | 'shutdown') => {
        setPowerLoading(true);
        try {
            const res = await api.post(`/api/control/${action}`);
            console.log(res.data);
            // Allow some time for the server to react before closing/resetting
            setTimeout(() => {
                setPowerLoading(false);
                setPowerModalOpen(false);
                if (action === 'restart') {
                    // Trigger a reconnect attempt or reload page
                    window.location.reload();
                }
            }, 2000);
        } catch (err) {
            console.error(`Failed to ${action}:`, err);
            setPowerLoading(false);
        }
    };

    const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
        const viewport = e.currentTarget;
        const atBottom = viewport.scrollHeight - (viewport.scrollTop + viewport.clientHeight) < 80;
        isAtBottomRef.current = atBottom;
        if (atBottom) {
            setIsAtBottom(true);
            clearUnread();
        } else if (isAtBottom) {
            setIsAtBottom(false);
        }
    };

    const handleToolConfirmSideChannel = async (confId: string, approved: boolean, sessionWhitelist: boolean) => {
        try {
            const response = await api.post('/api/confirm-tool', {
                conf_id: confId,
                approved,
                session_whitelist: sessionWhitelist
            });
            if (response.data?.status !== 'success') {
                throw new Error(response.data?.message || 'The approval request was not accepted.');
            }
            toast.success(approved ? 'Action approved' : 'Action denied');
        } catch (err) {
            console.error("Failed to confirm tool via side-channel:", err);
            toast.error('Could not update the approval', {
                description: err instanceof Error ? err.message : 'The server did not accept the decision.',
            });
            throw err;
        }
    };


    return (
        <div className="flex flex-col h-full bg-background relative">
            {/* Chat Header - Hidden on mobile, shown on md+ */}
            <header className="hidden md:flex h-14 items-center justify-between gap-3 px-6 border-b border-border bg-card z-10 transition-all duration-200">
                <div className="flex items-center gap-3 min-w-0">
                    <h1 className="text-base font-bold text-foreground leading-tight">Chat</h1>

                    {/* Compact rail status pill */}
                    <div
                        className={cn(
                            "flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] transition-all",
                            railTone === 'good' && "border-primary/20 bg-primary/5 text-primary",
                            railTone === 'warn' && "border-amber-500/20 bg-amber-500/5 text-amber-500",
                            railTone === 'default' && "border-border bg-card/70 text-muted-foreground"
                        )}
                    >
                        <span className="flex h-5 w-5 shrink-0 items-center justify-center">
                            {!isConnected ? (
                                <WifiOff className="h-3.5 w-3.5" />
                            ) : waitingExecution ? (
                                <ShieldAlert className="h-3.5 w-3.5" />
                            ) : isTyping || runningExecution || activityText ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                                <Wifi className="h-3.5 w-3.5" />
                            )}
                        </span>
                        <span className="font-semibold">{railTitle}</span>
                        <span className="uppercase tracking-[0.14em] opacity-60">
                            {waitingExecution ? "review" : runningExecution || isTyping || activityText ? "live" : "idle"}
                        </span>
                    </div>
                </div>

                <div className="flex items-center gap-2">
                    {onNewChat && (
                        <Button
                            variant="outline"
                            size="sm"
                            onClick={onNewChat}
                            className="h-8 gap-2 text-primary border-primary/20 hover:bg-primary/10 hover:text-primary"
                        >
                            <Plus className="h-4 w-4" />
                            <span className="font-semibold text-xs">New Session</span>
                        </Button>
                    )}

                    <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => setPowerModalOpen(true)}
                        aria-label="Power controls"
                        title="Power Controls"
                        className={cn("h-8 w-8 text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring", isConnected ? "hover:text-red-500" : "")}
                    >
                        <Power className="h-4 w-4" />
                    </Button>

                    <AlertDialog open={powerModalOpen} onOpenChange={setPowerModalOpen}>
                        <AlertDialogContent className="border-primary/20">
                            <AlertDialogHeader>
                                <AlertDialogTitle className="flex items-center gap-2">
                                    <Power className="h-5 w-5 text-primary" />
                                    System Control
                                </AlertDialogTitle>
                                <AlertDialogDescription>
                                    Manage the LimeBot backend process.
                                </AlertDialogDescription>
                            </AlertDialogHeader>

                            <div className="grid grid-cols-2 gap-4 py-4">
                                <Button
                                    variant="outline"
                                    className="h-24 flex flex-col gap-2 hover:bg-primary/10 hover:border-primary/50 hover:text-primary"
                                    onClick={() => handlePowerAction('restart')}
                                    disabled={powerLoading}
                                >
                                    {powerLoading ? <Loader2 className="h-6 w-6 animate-spin" /> : <Power className="h-6 w-6" />}
                                    <span className="font-bold">Restart</span>
                                    <span className="text-xs text-muted-foreground font-normal">Reload backend & config</span>
                                </Button>

                                <Button
                                    variant="outline"
                                    className="h-24 flex flex-col gap-2 hover:bg-red-500/10 hover:border-red-500/50 hover:text-red-500"
                                    onClick={() => handlePowerAction('shutdown')}
                                    disabled={powerLoading}
                                >
                                    {powerLoading ? <Loader2 className="h-6 w-6 animate-spin" /> : <Power className="h-6 w-6" />}
                                    <span className="font-bold">Shutdown</span>
                                    <span className="text-xs text-muted-foreground font-normal">Stop process completely</span>
                                </Button>
                            </div>

                            <AlertDialogFooter>
                                <AlertDialogCancel disabled={powerLoading}>Cancel</AlertDialogCancel>
                            </AlertDialogFooter>
                        </AlertDialogContent>
                    </AlertDialog>
                </div>
            </header>


            {/* Chat Messages */}
            <div className="flex-1 overflow-hidden relative">
                <ScrollArea ref={scrollAreaRef} className="h-full p-3 md:p-5" onScroll={handleScroll}>
                    <div className="mx-auto flex max-w-[48rem] flex-col gap-[var(--message-gap)] pb-8 font-sans">
                        {messages.length === 0 && (
                            <div className="flex flex-col items-center justify-center py-12 text-center">
                                <BotVisual
                                    avatar={botIdentity?.avatar}
                                    size="hero"
                                />
                                <div className="mt-4 space-y-1">
                                    <div className="text-[10px] font-bold uppercase tracking-[0.24em] text-primary/70">
                                        Session {sessionLabel}
                                    </div>
                                    <h3 className="text-lg font-semibold text-foreground">
                                        {botIdentity?.name ? `${botIdentity.name} is ready` : "LimeBot is ready"}
                                    </h3>
                                    <p className="text-xs text-muted-foreground">
                                        Ask anything, or pick a suggestion below.
                                    </p>
                                </div>
                            </div>
                        )}
                        {(() => {
                            const items: Array<
                                | { kind: 'message'; msg: Message; showAvatar: boolean; showHeader: boolean; key: string; absoluteIndex: number }
                                | { kind: 'tool_timeline'; executions: ToolExecution[]; key: string; absoluteIndex: number }
                            > = [];

                            for (let i = 0; i < messages.length; i++) {
                                const msg = messages[i];
                                const prevRaw = i > 0 ? messages[i - 1] : null;
                                const nextRaw = i + 1 < messages.length ? messages[i + 1] : null;

                                if (isSubagentOrchestrationThought(msg, prevRaw, nextRaw)) {
                                    continue;
                                }

                                if (msg.type === 'tool' && msg.toolExecution) {
                                    const start = i;
                                    const toolGroup: ToolExecutionWithTurn[] = [];
                                    while (i < messages.length && messages[i].type === 'tool' && messages[i].toolExecution) {
                                        toolGroup.push({
                                            ...(messages[i].toolExecution as ToolExecution),
                                            turnId: messages[i].turnId,
                                        });
                                        i++;
                                    }
                                    const nextMessage = i < messages.length ? messages[i] : null;
                                    if (shouldHideSpawnAgentGroup(toolGroup, nextMessage)) {
                                        i -= 1;
                                        continue;
                                    }
                                    const count = toolGroup.length;
                                    if (count >= 3) {
                                        items.push({
                                            kind: 'tool_timeline',
                                            executions: toolGroup,
                                            key: `${activeChatId}-tools-${start}`,
                                            absoluteIndex: start,
                                        });
                                    } else {
                                        for (let j = 0; j < toolGroup.length; j++) {
                                            const m = messages[start + j];
                                            items.push({
                                                kind: 'message',
                                                msg: m,
                                                showAvatar: j === 0,
                                                showHeader: j === 0,
                                                key: `${activeChatId}-${start + j}`,
                                                absoluteIndex: start + j,
                                            });
                                        }
                                    }
                                    i -= 1;
                                    continue;
                                }

                                const prev = items.length > 0 && items[items.length - 1].kind === 'message'
                                    ? (items[items.length - 1] as { kind: 'message'; msg: Message }).msg
                                    : null;
                                const showAvatar = !prev || prev.sender !== msg.sender || prev.type !== msg.type;
                                const showHeader = showAvatar && msg.sender === 'bot';

                                items.push({
                                    kind: 'message',
                                    msg,
                                    showAvatar,
                                    showHeader,
                                    key: `${activeChatId}-${i}`,
                                    absoluteIndex: i,
                                });
                            }

                            return (
                                <>
                                    {items.map((item) => (
                                        <div key={item.key} className="contents">
                                            {item.absoluteIndex === unreadAnchorIndex && unreadCount > 0 && (
                                                <UnreadSeparator count={unreadCount} />
                                            )}
                                            {item.kind === 'tool_timeline' ? (
                                                <ToolTimeline
                                                    executions={item.executions}
                                                    botIdentity={botIdentity}
                                                    onConfirmSideChannel={handleToolConfirmSideChannel}
                                                />
                                            ) : (
                                                <MemoizedMessageItem
                                                    msg={item.msg}
                                                    botIdentity={botIdentity}
                                                    handleToolConfirmSideChannel={handleToolConfirmSideChannel}
                                                    onSendMessage={onSendMessage}
                                                    onStartEdit={startEditingMessage}
                                                    canEdit={
                                                        item.msg.sender === 'user' &&
                                                        !!item.msg.messageId &&
                                                        !item.msg.attachments?.length &&
                                                        !item.msg.image
                                                    }
                                                    showAvatar={item.showAvatar}
                                                    showHeader={item.showHeader}
                                                />
                                            )}
                                        </div>
                                    ))}
                                </>
                            );
                        })()}
                    </div>

                    {/* ChatGPT-style typing indicator */}
                    {isTyping && (() => {
                        const last = messages[messages.length - 1];
                        const botAlreadyStreaming = last?.sender === 'bot' && last?.isStreaming;
                        return !botAlreadyStreaming ? (
                            <TypingIndicator
                                botIdentity={botIdentity}
                            />
                        ) : null;
                    })()}

                    <div ref={scrollRef} />
                </ScrollArea>

                {!isAtBottom && messages.length > 0 && (
                    <div className="pointer-events-none absolute bottom-5 right-5 z-20 flex flex-col items-end gap-2">
                        {unreadCount > 0 && (
                            <div className="pointer-events-auto rounded-full border border-primary/20 bg-background/95 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-primary shadow-lg backdrop-blur">
                                {unreadCount <= 1 ? "1 New Message" : `${unreadCount} New Messages`}
                            </div>
                        )}
                        <Button
                            variant="outline"
                            size="sm"
                            onClick={() => scrollToLatest('smooth')}
                            aria-label="Jump to latest message"
                            className="pointer-events-auto h-9 rounded-full border-primary/20 bg-background/95 pl-3 pr-3 shadow-lg backdrop-blur hover:bg-background"
                        >
                            <ArrowDown className="mr-1.5 h-4 w-4" />
                            Jump to latest
                        </Button>
                    </div>
                )}
            </div>

            {/* Input Area */}
            <div className="chat-composer-shell border-t border-border bg-background p-4 relative z-20">
                <div className="mx-auto max-w-[48rem] relative group font-sans">
                    <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
                        <StatusChip
                            label="Compose"
                            value="Enter sends"
                        />
                        <StatusChip
                            label="Line break"
                            value="Shift+Enter"
                        />
                        {waitingToolCount > 0 && (
                            <StatusChip label="Pending approval" value={String(waitingToolCount)} tone="warn" />
                        )}
                        {ponytailActive && (
                            <StatusChip label="Ponytail" value="Full" tone="good" />
                        )}
                        {selectedAttachment && (
                            <StatusChip label="Attachment" value={selectedAttachment.name} tone="good" />
                        )}
                    </div>

                    <div
                        className={cn(
                            "chat-composer relative rounded-2xl border bg-muted/40 shadow-sm transition-colors",
                            isDraggingFile
                                ? "border-primary bg-primary/5 ring-2 ring-primary/20"
                                : "border-border/80 focus-within:border-primary/40 focus-within:ring-2 focus-within:ring-primary/15"
                        )}
                        onDragEnter={handleComposerDragEnter}
                        onDragOver={handleComposerDragOver}
                        onDragLeave={handleComposerDragLeave}
                        onDrop={handleComposerDrop}
                    >
                        {isDraggingFile && (
                            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl border-2 border-dashed border-primary/50 bg-background/80 text-sm font-medium text-primary">
                                Drop an image to attach
                            </div>
                        )}

                        {selectedAttachment && (
                            <div className="flex items-start gap-3 border-b border-border/70 px-3 pt-3">
                                <div className="relative max-w-[12rem]">
                                    <AttachmentPreview attachment={selectedAttachment} compact />
                                    <button
                                        onClick={clearSelectedAttachment}
                                        aria-label="Remove attachment"
                                        className="absolute -right-2 -top-2 rounded-full bg-destructive p-1 text-white shadow-sm transition-colors hover:bg-destructive/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                    >
                                        <X className="h-3 w-3" />
                                    </button>
                                </div>
                            </div>
                        )}

                    <div className="relative flex items-end">
                        <Button
                            variant="ghost"
                            size="icon"
                            className="h-10 w-10 mb-1 ml-1 text-muted-foreground hover:text-foreground"
                            onClick={() => {
                                if (!isEditing) {
                                    fileInputRef.current?.click();
                                }
                            }}
                            aria-label="Attach file"
                            title="Attach file"
                            disabled={isEditing}
                        >
                            <Paperclip className="h-5 w-5" />
                        </Button>
                        <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className={cn(
                                "h-10 w-10 mb-1 text-muted-foreground hover:text-foreground",
                                ponytailActive && "bg-primary/10 text-primary hover:bg-primary/15 hover:text-primary"
                            )}
                            onClick={() => setPonytailMode((mode) => mode === 'full' ? 'off' : 'full')}
                            title={ponytailActive ? "Ponytail mode: Full" : "Activate Ponytail mode"}
                            aria-label={ponytailActive ? "Deactivate Ponytail mode" : "Activate Ponytail mode"}
                            aria-pressed={ponytailActive}
                        >
                            <Brain className="h-5 w-5" />
                        </Button>
                        <input
                            type="file"
                            ref={fileInputRef}
                            className="hidden"
                            accept={ACCEPTED_ATTACHMENT_TYPES}
                            onChange={handleFileSelect}
                        />

                        {/* Stop Button (visible when typing or executing tools) */}
                        {(isTyping || messages.some(m => m.type === 'tool' && (m.toolExecution?.status === 'running' || m.toolExecution?.status === 'planned'))) && (
                            <Button
                                variant="ghost"
                                size="icon"
                                className="h-10 w-10 mb-1 text-destructive hover:text-destructive hover:bg-destructive/10 animate-in fade-in zoom-in duration-200"
                                onClick={async () => {
                                    if (!activeChatId) return;
                                    try {
                                        const response = await api.post(
                                            `/api/chat/${encodeURIComponent(activeChatId)}/stop`
                                        );
                                        if (response.data?.status === 'ignored') {
                                            toast.info('No active task was found to stop.');
                                        } else {
                                            toast.success('Generation stopped');
                                        }
                                    } catch (e) {
                                        console.error("Failed to stop generation:", e);
                                        toast.error('Could not stop generation', {
                                            description: e instanceof Error ? e.message : 'The server did not accept the stop request.',
                                        });
                                    }
                                }}
                                title="Stop generating"
                                aria-label="Stop generating"
                            >
                                <Square className="h-5 w-5" />
                            </Button>
                        )}

                        <div className="flex-1">
                            {!agentReadiness.ready && (
                                <div className="mb-1 mt-2 flex items-center justify-between gap-3 rounded-xl border border-amber-500/25 bg-amber-500/8 px-3 py-2 text-xs text-amber-100">
                                    <div className="flex min-w-0 items-center gap-2">
                                        {agentReadiness.status === 'failed' || agentReadiness.status === 'timeout' ? (
                                            <AlertTriangle className="h-4 w-4 shrink-0 text-amber-400" />
                                        ) : (
                                            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-amber-400" />
                                        )}
                                        <span className="truncate">{readinessLabel(agentReadiness)}</span>
                                    </div>
                                    {(agentReadiness.status === 'failed' || agentReadiness.status === 'timeout') && (
                                        <button
                                            type="button"
                                            onClick={onRetryReadiness}
                                            className="shrink-0 rounded-md border border-amber-400/30 px-2 py-1 font-semibold text-amber-200 hover:bg-amber-400/10"
                                        >
                                            Retry
                                        </button>
                                    )}
                                </div>
                            )}
                            {selectedSkill && (
                                <div className="mb-1 mt-2 flex max-w-max items-center gap-2 rounded-full border border-primary/30 bg-primary/10 px-3 py-1.5 text-xs text-primary shadow-sm">
                                    <span className="font-semibold uppercase tracking-[0.18em] text-primary">
                                        Skill
                                    </span>
                                    <span className="truncate font-medium text-foreground">
                                        {selectedSkill.name}
                                    </span>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setSelectedSkill(null);
                                            requestAnimationFrame(() => textareaRef.current?.focus());
                                        }}
                                        className="rounded-full p-0.5 text-primary/80 transition-colors hover:bg-primary/20 hover:text-primary"
                                        aria-label={`Remove ${selectedSkill.name} skill`}
                                        title={`Remove ${selectedSkill.name} skill`}
                                    >
                                        <X className="h-3 w-3" />
                                    </button>
                                </div>
                            )}
                            {editDraft && (
                                <div className="mb-1 mt-2 flex items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
                                    <div className="min-w-0">
                                        <div className="font-semibold uppercase tracking-[0.18em] text-amber-300">
                                            Editing Message
                                        </div>
                                        <div className="mt-0.5 truncate text-amber-50/90">
                                            Later replies will be replaced when you resend this prompt.
                                        </div>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setEditDraft(null);
                                            setEditSubmitting(false);
                                            onInputChange('');
                                            requestAnimationFrame(() => textareaRef.current?.focus());
                                        }}
                                        className="shrink-0 rounded-md border border-amber-400/30 px-2 py-1 font-semibold text-amber-100 hover:bg-amber-400/10"
                                    >
                                        Cancel
                                    </button>
                                </div>
                            )}
                            <Textarea
                                ref={textareaRef}
                                placeholder={
                                    !isConnected
                                        ? "Waiting for the gateway to reconnect..."
                                        : !agentReadiness.ready
                                            ? readinessLabel(agentReadiness)
                                        : waitingToolCount > 0
                                            ? "Approve or deny the waiting action below, or send a clarification..."
                                            : editDraft
                                                ? "Update the selected message and resend it from this point..."
                                            : selectedSkill
                                                ? `Message with ${selectedSkill.name} skill context...`
                                                : "Message LimeBot, or paste / drop an image..."
                                }
                                value={inputValue}
                                onChange={(e) => {
                                    setDismissedSlashValue(null);
                                    onInputChange(e.target.value);
                                }}
                                onKeyDown={handleKeyPress}
                                onPaste={handlePaste}
                                disabled={!isConnected || !agentReadiness.ready}
                                className="min-h-[50px] max-h-[200px] border-0 bg-transparent py-4 px-4 focus-visible:ring-0 focus-visible:ring-offset-0 placeholder:text-muted-foreground text-sm resize-none"
                                rows={1}
                                onInput={(e: React.FormEvent<HTMLTextAreaElement>) => {
                                    const target = e.target as HTMLTextAreaElement;
                                    target.style.height = 'auto';
                                    target.style.height = `${target.scrollHeight}px`;
                                }}
                            />
                        </div>
                        <div className="pb-2 pr-2">
                            <Button
                                onClick={handleSend}
                                disabled={
                                    !isConnected ||
                                    !agentReadiness.ready ||
                                    editSubmitting ||
                                    (!isEditing && isTyping) ||
                                    (isEditing
                                        ? !inputValue.trim()
                                        : (!inputValue.trim() && !selectedAttachment))
                                }
                                size="icon"
                                aria-label={isEditing ? "Apply edit and rerun" : "Send message"}
                                className={cn(
                                    "h-9 w-9 rounded-lg transition-all duration-200",
                                    (inputValue.trim() || selectedAttachment)
                                        ? "bg-primary hover:bg-primary/90 text-primary-foreground"
                                        : "bg-transparent text-muted-foreground hover:bg-muted-foreground/10"
                                )}
                            >
                                <Send className="h-4 w-4" />
                                <span className="sr-only">Send</span>
                            </Button>
                        </div>
                    </div>
                    </div>

                    {(showSlashMenu || (skillsLoading && inputValue === '/')) && (
                        <div className="absolute bottom-full left-0 right-0 z-30 mb-3 overflow-hidden rounded-2xl border border-border/80 bg-background/98 shadow-2xl backdrop-blur">
                            <div className="border-b border-border/70 px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
                                Slash Skills
                            </div>
                            <div className="max-h-80 overflow-y-auto p-2">
                                {showSlashMenu && slashSuggestions.map((suggestion, index) => (
                                    <button
                                        key={suggestion.id}
                                        type="button"
                                        onClick={() => {
                                            if (suggestion.kind === 'skill') {
                                                const matchedSkill = skills.find(
                                                    (skill) => `/${skill.name}` === suggestion.title
                                                );
                                                if (matchedSkill) {
                                                    setSelectedSkill({
                                                        id: matchedSkill.id,
                                                        name: matchedSkill.name,
                                                        description: matchedSkill.description || 'Enabled skill',
                                                    });
                                                    setDismissedSlashValue(null);
                                                    onInputChange('');
                                                    requestAnimationFrame(() => textareaRef.current?.focus());
                                                }
                                                return;
                                            }
                                            setDismissedSlashValue(null);
                                            onInputChange(suggestion.insertText);
                                            requestAnimationFrame(() => {
                                                textareaRef.current?.focus();
                                                const length = suggestion.insertText.length;
                                                textareaRef.current?.setSelectionRange(length, length);
                                            });
                                        }}
                                        className={cn(
                                            "flex w-full items-start justify-between gap-3 rounded-xl px-3 py-3 text-left transition-colors",
                                            index === highlightedSlashIndex
                                                ? "bg-primary/10 text-foreground"
                                                : "hover:bg-muted/70 text-foreground"
                                        )}
                                    >
                                        <div className="min-w-0">
                                            <div className="text-sm font-medium">{suggestion.title}</div>
                                            <div className="mt-1 text-xs text-muted-foreground">
                                                {suggestion.description}
                                            </div>
                                        </div>
                                        <span className="shrink-0 rounded-full border border-border bg-muted/80 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                                            {suggestion.badge}
                                        </span>
                                    </button>
                                ))}
                                {!showSlashMenu && skillsLoading && (
                                    <div className="flex items-center gap-2 px-3 py-4 text-sm text-muted-foreground">
                                        <Loader2 className="h-4 w-4 animate-spin" />
                                        Loading skills...
                                    </div>
                                )}
                            </div>
                            <div className="border-t border-border/70 px-4 py-2 text-[11px] text-muted-foreground">
                                Enter or Tab inserts. Use <span className="font-medium text-foreground">/skills</span> to inspect what is enabled.
                            </div>
                        </div>
                    )}

                    {showComposerPrompts && (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                            {QUICK_ACTIONS.map(({ label, prompt }) => (
                                <button
                                    key={label}
                                    type="button"
                                    onClick={() => onSendMessage(prompt, null, ponytailSendOptions)}
                                    disabled={!isConnected || !agentReadiness.ready}
                                    className="rounded-full border border-border bg-background px-2.5 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/30 hover:bg-primary/5 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                >
                                    {label}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
                <div className="mx-auto mt-2 flex max-w-[48rem] flex-wrap justify-between gap-2 px-1 text-[10px] font-medium text-muted-foreground">
                    <span>
                        {waitingToolCount > 0
                            ? "Pending approvals are handled directly in the tool timeline."
                            : "Secure channel with guarded tool execution."}
                    </span>
                    <span>LimeBot v1.0.12</span>
                </div>
            </div>
        </div>
    );
}
