import { useState, useEffect } from 'react';
import { Card } from "@/components/ui/card";
import {
    FileText,
    Save,
    Terminal,
    FolderOpen,
    Search,
    Cpu,
    CheckCircle2,
    XCircle,
    Loader2,
    ChevronDown,
    ChevronUp,
    ShieldAlert
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { TurnStatus } from "@/lib/chat-state";

export interface ConfirmationPreview {
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
}

export interface ToolExecution {
    tool: string;
    status: 'planned' | 'running' | 'completed' | 'error' | 'pending_confirmation' | 'progress' | 'waiting_confirmation';
    args: any;
    result?: string;
    tool_call_id: string;
    conf_id?: string;
    turnStatus?: TurnStatus;
    logs?: string[];
    preview?: ConfirmationPreview;
}

interface ToolCardProps {
    execution: ToolExecution;
    onConfirm?: (toolCallId: string, approved: boolean) => void;
    onConfirmSession?: (toolCallId: string) => void;
    onConfirmSideChannel?: (confId: string, approved: boolean, sessionWhitelist: boolean) => Promise<void>;
}



export function ToolCard({ execution, onConfirm, onConfirmSession, onConfirmSideChannel }: ToolCardProps) {
    const [isExpanded, setIsExpanded] = useState(false);
    const [confirmationStatus, setConfirmationStatus] = useState<'pending' | 'approved' | 'denied' | null>(null);
    const [confirmationSubmitting, setConfirmationSubmitting] = useState(false);

    // Auto-expand when logs arrive
    useEffect(() => {
        if (execution.logs && execution.logs.length > 0 && !isExpanded && execution.status === 'running') {
            setIsExpanded(true);
        }
        if (execution.status === 'error' && !isExpanded) {
            setIsExpanded(true);
        }
    }, [execution.logs, execution.status, isExpanded]);

    useEffect(() => {
        setConfirmationStatus(null);
        setConfirmationSubmitting(false);
    }, [execution.conf_id, execution.tool_call_id]);

    // Check if the action is blocked by backend
    const isBlocked = execution.status === 'waiting_confirmation' || execution.result?.includes("ACTION BLOCKED: Confirmation Required");

    const parseDepsNotice = (result?: string) => {
        if (!result) return { notice: '', cleaned: result || '' };
        const marker = "[SKILL_DEPS_MISSING]";
        if (!result.includes(marker)) return { notice: '', cleaned: result };
        const [before, after] = result.split(marker, 2);
        return {
            notice: (after || '').trim(),
            cleaned: (before || '').trim(),
        };
    };

    const depsNotice = parseDepsNotice(execution.result);
    const preview = execution.preview;

    // Needs confirmation if:
    // 1. Backend signaled waiting_confirmation status (New Way)
    // 2. Completed but blocked (Old Way - for compatibility)
    const needsConfirmation = isBlocked;

    const resolveConfirmation = async (approved: boolean, sessionWhitelist: boolean) => {
        if (confirmationSubmitting || confirmationStatus !== null) return;
        setConfirmationSubmitting(true);
        try {
            if (execution.conf_id && onConfirmSideChannel) {
                await onConfirmSideChannel(execution.conf_id, approved, sessionWhitelist);
            } else if (approved && sessionWhitelist) {
                onConfirmSession?.(execution.tool_call_id);
            } else {
                onConfirm?.(execution.tool_call_id, approved);
            }
            setConfirmationStatus(approved ? 'approved' : 'denied');
        } catch {
            setConfirmationStatus(null);
        } finally {
            setConfirmationSubmitting(false);
        }
    };



    const getIcon = () => {
        if (needsConfirmation && confirmationStatus === null) {
            return <ShieldAlert className="h-4 w-4 text-amber-400" />;
        }
        switch (execution.tool) {
            case 'read_file': return <FileText className="h-4 w-4" />;
            case 'write_file': return <Save className="h-4 w-4" />;
            case 'run_command': return <Terminal className="h-4 w-4" />;
            case 'list_dir': return <FolderOpen className="h-4 w-4" />;
            case 'search_web': return <Search className="h-4 w-4" />;
            default: return <Cpu className="h-4 w-4" />;
        }
    };

    const getSummary = () => {
        if (preview?.summary) return preview.summary;
        const args = execution.args;
        if (!args) return '';

        switch (execution.tool) {
            case 'read_file': return `Reading ${getFileName(args.path)}`;
            case 'write_file': return `Writing ${getFileName(args.path)}`;
            case 'run_command': return `> ${args.command}`;
            case 'list_dir': return `Listing ${args.path}`;
            case 'search_web': return `Searching "${args.query}"`;
            default: return JSON.stringify(args).slice(0, 50);
        }
    };

    const renderPreview = () => {
        if (!preview) return null;

        return (
            <div className="mb-3 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-[11px] text-amber-100/90">
                {preview.summary && (
                    <div className="font-semibold text-amber-200">{preview.summary}</div>
                )}

                {preview.risk_flags && preview.risk_flags.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                        {preview.risk_flags.map((flag) => (
                            <span
                                key={flag}
                                className="rounded border border-amber-400/20 bg-amber-400/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-300"
                            >
                                {flag.replace(/_/g, ' ')}
                            </span>
                        ))}
                    </div>
                )}

                {preview.affected_paths && preview.affected_paths.length > 0 && (
                    <div className="mt-2">
                        <div className="text-amber-200/70">Affected paths</div>
                        <div className="mt-1 font-mono text-amber-100/80 whitespace-pre-wrap break-words">
                            {preview.affected_paths.join('\n')}
                        </div>
                    </div>
                )}

                {preview.diff && (
                    <div className="mt-2">
                        <div className="text-amber-200/70">Diff preview</div>
                        <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded border border-amber-400/10 bg-black/30 p-2 text-[10px] text-amber-50">
                            {preview.diff}
                        </pre>
                    </div>
                )}

                {!preview.diff && preview.content_preview && (
                    <div className="mt-2">
                        <div className="text-amber-200/70">Content preview</div>
                        <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded border border-amber-400/10 bg-black/30 p-2 text-[10px] text-amber-50">
                            {preview.content_preview}
                        </pre>
                    </div>
                )}

                {preview.command && (
                    <div className="mt-2">
                        <div className="text-amber-200/70">Command</div>
                        <pre className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap break-words rounded border border-amber-400/10 bg-black/30 p-2 text-[10px] text-amber-50">
                            {preview.command}
                        </pre>
                    </div>
                )}

                {preview.diff_error && (
                    <div className="mt-2 text-amber-200/70">
                        Diff preview unavailable: {preview.diff_error}
                    </div>
                )}
            </div>
        );
    };

    const getFileName = (path: string) => {
        if (!path) return 'file';
        return path.split(/[/\\]/).pop();
    };

    const cardClasses = cn(
        "bg-card border-border text-foreground overflow-hidden",
        needsConfirmation && confirmationStatus === null && "border-primary/40 bg-primary/10",
        depsNotice.notice && "border-red-500/30"
    );

    return (
        <div className="my-2 max-w-[80%]">
            <Card className={cardClasses}>
                <div
                    className="flex items-center gap-3 p-3 cursor-pointer hover:bg-slate-800/50 transition-colors"
                    onClick={() => setIsExpanded(!isExpanded)}
                >
                    <div className={cn(
                        "p-2 rounded-md",
                        needsConfirmation && confirmationStatus === null
                            ? "bg-primary/20"
                            : "bg-muted",
                        (execution.status === 'running' || execution.status === 'planned') && !needsConfirmation && "animate-pulse"
                    )}>
                        {getIcon()}
                    </div>

                    <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                            <span className={cn(
                                "font-medium text-sm",
                                needsConfirmation && confirmationStatus === null
                                    ? "text-primary"
                                    : "text-foreground"
                            )}>
                                {needsConfirmation && confirmationStatus === null
                                    ? "Confirmation Required"
                                    : execution.tool}
                            </span>
                            {depsNotice.notice && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded border border-red-500/30 text-red-500 bg-red-500/10">
                                    Missing Deps
                                </span>
                            )}
                            <span className="text-xs text-muted-foreground">
                                {confirmationStatus === 'approved' ? 'Approved' :
                                    confirmationStatus === 'denied' ? 'Denied' :
                                        needsConfirmation && confirmationStatus === null ? 'Blocked' :
                                            execution.status === 'planned' ? 'Planned' :
                                            execution.status === 'running' ? 'Running...' :
                                                execution.status === 'completed' ? 'Completed' : 'Failed'}
                            </span>
                        </div>
                        <div className="text-xs text-muted-foreground truncate font-mono">
                            {getSummary()}
                        </div>
                    </div>

                    <div className="flex items-center gap-2 text-muted-foreground">
                        {(execution.status === 'running' || execution.status === 'planned') && !needsConfirmation && <Loader2 className="h-4 w-4 animate-spin" />}
                        {execution.status === 'completed' && !isBlocked && <CheckCircle2 className="h-4 w-4 text-primary" />}
                        {execution.status === 'error' && <XCircle className="h-4 w-4 text-red-500" />}
                        {confirmationStatus === 'approved' && <CheckCircle2 className="h-4 w-4 text-primary" />}
                        {confirmationStatus === 'denied' && <XCircle className="h-4 w-4 text-red-500" />}

                        {isExpanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                    </div>
                </div>

                {needsConfirmation && confirmationStatus === null && (
                    <div className="flex gap-2 p-2 pt-2 border-t border-primary/30 bg-primary/5">
                        <button
                            type="button"
                            disabled={confirmationSubmitting}
                            onClick={(e) => {
                                e.stopPropagation();
                                void resolveConfirmation(true, false);
                            }}
                            className="bg-primary/20 hover:bg-primary/30 text-primary px-3 py-1 rounded text-xs font-semibold disabled:cursor-wait disabled:opacity-60"
                        >
                            Allow Once
                        </button>
                        <button
                            type="button"
                            disabled={confirmationSubmitting}
                            onClick={(e) => {
                                e.stopPropagation();
                                void resolveConfirmation(true, true);
                            }}
                            className="bg-secondary hover:bg-secondary/80 text-secondary-foreground px-3 py-1 rounded text-xs font-semibold disabled:cursor-wait disabled:opacity-60"
                            title="Allow all executions of this tool for the rest of this session"
                        >
                            Allow Session
                        </button>
                        <div className="flex-1" />
                        <button
                            type="button"
                            disabled={confirmationSubmitting}
                            onClick={(e) => {
                                e.stopPropagation();
                                void resolveConfirmation(false, false);
                            }}
                            className="bg-destructive/20 hover:bg-destructive/30 text-destructive px-3 py-1 rounded text-xs font-semibold disabled:cursor-wait disabled:opacity-60"
                        >
                            Deny
                        </button>
                    </div>
                )}

                {isExpanded && (
                    <div className="p-3 pt-0 border-t border-border/50 bg-background/40">
                        {depsNotice.notice && (
                            <div className="mt-3 mb-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-500">
                                <div className="font-semibold mb-1">Skill dependencies missing</div>
                                <div className="font-mono">{depsNotice.notice}</div>
                            </div>
                        )}
                        {renderPreview()}
                        <div className="mt-2 text-xs font-mono whitespace-pre-wrap text-muted-foreground max-h-60 overflow-y-auto">
                            {execution.logs && execution.logs.length > 0 && (
                                <div className="mb-3 flex flex-col gap-1">
                                    {execution.logs.map((log, i) => (
                                        <div key={i} className="text-primary/80 border-l border-primary/30 pl-2">
                                            {log}
                                        </div>
                                    ))}
                                    {execution.status === 'running' && (
                                        <div className="flex items-center gap-2 text-primary/40 animate-pulse mt-1">
                                            <div className="w-1 h-1 bg-primary rounded-full" />
                                            Listening for updates...
                                        </div>
                                    )}
                                </div>
                            )}

                            <div className="mb-2">
                                <span className="text-muted-foreground select-none">$ args: </span>
                                {JSON.stringify(execution.args, null, 2)}
                            </div>
                            {depsNotice.cleaned && (
                                <div>
                                    <span className="text-muted-foreground select-none">$ result: </span>
                                    {depsNotice.cleaned}
                                </div>
                            )}
                        </div>
                    </div>
                )}
            </Card>
        </div>
    );
}
