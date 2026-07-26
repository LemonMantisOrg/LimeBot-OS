import {
    MessageSquare,
    LayoutDashboard,
    Settings,
    Users,
    Activity,
    Zap,
    FileText,
    Palette,
    User2,
    Brain,
    Cpu,
    ActivitySquare,
    Bot,
    ShieldCheck,
    Wifi,
    WifiOff,
    List,
    Globe,
    Volume2,
    PawPrint
} from "lucide-react";
import { cn } from "@/lib/utils";
import { BotVisual } from "@/components/pet/BotVisual";
import type { PetState } from "@/lib/pet";
import { useEffect, useState } from "react";
import axios from "axios";
import { API_BASE_URL } from "@/lib/api";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";

interface SidebarProps {
    className?: string;
    botIdentity?: { name: string; avatar: string | null };
    petState?: PetState;
    petEnabled?: boolean;
    preferAnimatedPet?: boolean;
    petManifest?: import("@/lib/pet").PetManifest;
    activeView?: string;
    onNavigate?: (view: string) => void;
    runtimeStatus?: {
        isConnected: boolean;
        autonomousMode: boolean;
        pendingApprovals: number;
    };
}

export function Sidebar({
    className,
    botIdentity,
    petState,
    petEnabled,
    preferAnimatedPet,
    petManifest,
    activeView = 'chat',
    onNavigate,
    runtimeStatus,
}: SidebarProps) {
    const [activeModel, setActiveModel] = useState("Loading...");
    const [memoryLabel, setMemoryLabel] = useState("Checking...");
    const [memoryTone, setMemoryTone] = useState("text-muted-foreground");
    const [subagentSelection, setSubagentSelection] = useState("auto");
    const [subagentOptions, setSubagentOptions] = useState<Array<{ value: string; label: string }>>([
        { value: "auto", label: "Auto" },
    ]);
    const [savingSubagentSelection, setSavingSubagentSelection] = useState(false);

    const handleNav = (view: string) => {
        onNavigate?.(view);
    };

    const handleSubagentSelectionChange = async (value: string) => {
        const previous = subagentSelection;
        setSubagentSelection(value);
        setSavingSubagentSelection(true);
        try {
            const res = await axios.put(`${API_BASE_URL}/api/subagents/settings`, {
                default_selection: value,
            });
            setSubagentSelection(res.data?.default_selection || value);
            const options = Array.isArray(res.data?.selection_options)
                ? res.data.selection_options.map((option: any) => ({
                    value: String(option.value),
                    label: String(option.label || option.value),
                }))
                : null;
            if (options && options.length > 0) {
                setSubagentOptions(options);
            }
        } catch (error) {
            console.error("Failed to update subagent mode:", error);
            setSubagentSelection(previous);
        } finally {
            setSavingSubagentSelection(false);
        }
    };

    const navItems = [
        { id: 'chat', icon: MessageSquare, label: "Chat" },
        { id: 'overview', icon: LayoutDashboard, label: "Overview" },
        { id: 'queue', icon: List, label: "Task Queue" },
        { id: 'memory', icon: Brain, label: "Memory" },
        { id: 'channels', icon: Activity, label: "Channels" },
        { id: 'logs', icon: FileText, label: "System Logs" },
        { id: 'instances', icon: Users, label: "Instances" },
        { id: 'cron', icon: Zap, label: "Cron Jobs" },
    ];

    const agentItems = [
        { id: 'skills', icon: Zap, label: "Skills" },
        { id: 'subagents', icon: Bot, label: "Subagents" },
        { id: 'browsers', icon: Globe, label: "Browser Sessions" },
        { id: 'mcp', icon: Cpu, label: "MCP" },
        { id: 'voice', icon: Volume2, label: "ElevenLabs Voice" },
    ];

    const configItems = [
        { id: 'persona', icon: User2, label: "Persona" },
        { id: 'pets', icon: PawPrint, label: "Pets" },
        { id: 'appearance', icon: Palette, label: "Appearance" },
        { id: 'config', icon: Settings, label: "Configuration" }
    ];

    useEffect(() => {
        let isMounted = true;

        const refreshRuntimeDetails = async () => {
            try {
                const [configRes, memoryRes, subagentRes] = await Promise.all([
                    axios.get(`${API_BASE_URL}/api/config`),
                    axios.get(`${API_BASE_URL}/api/memory`),
                    axios.get(`${API_BASE_URL}/api/subagents`),
                ]);

                if (!isMounted) return;

                setActiveModel(configRes.data?.env?.LLM_MODEL || "Unknown");
                setSubagentSelection(subagentRes.data?.default_selection || "auto");
                const options = Array.isArray(subagentRes.data?.selection_options)
                    ? subagentRes.data.selection_options.map((option: any) => ({
                        value: String(option.value),
                        label: String(option.label || option.value),
                    }))
                    : [{ value: "auto", label: "Auto" }];
                setSubagentOptions(options);

                const enabled = memoryRes.data?.enabled;
                const mode = memoryRes.data?.mode || (enabled ? "vector" : "grep_fallback");
                if (mode === "grep_fallback") {
                    setMemoryLabel("Markdown Fallback");
                    setMemoryTone("text-amber-500");
                } else if (enabled === false) {
                    setMemoryLabel("Offline");
                    setMemoryTone("text-amber-500");
                } else {
                    setMemoryLabel("Vector Online");
                    setMemoryTone("text-primary");
                }
            } catch (error: any) {
                if (error?.response?.status !== 401 && error?.response?.status !== 403) {
                    console.error("Failed to load runtime details:", error);
                }
                if (!isMounted) return;
                setActiveModel("Unavailable");
                setMemoryLabel("Unavailable");
                setMemoryTone("text-muted-foreground");
                setSubagentSelection("auto");
            }
        };

        refreshRuntimeDetails();
        const interval = window.setInterval(refreshRuntimeDetails, 15000);
        return () => {
            isMounted = false;
            window.clearInterval(interval);
        };
    }, []);

    return (
        <div className={cn("hidden h-[calc(100vh-2rem)] m-4 w-72 flex-col rounded-2xl bg-card border border-border md:flex shadow-xl", className)}>
            <div className="flex h-20 items-center px-6">
                <a
                    className="flex items-center gap-3 font-bold text-xl cursor-pointer group"
                    onClick={() => onNavigate?.('persona')}
                >
                    <BotVisual
                        avatar={botIdentity?.avatar}
                        petState={petState}
                        petEnabled={petEnabled}
                        preferAnimatedPet={preferAnimatedPet}
                        manifest={petManifest}
                        size="sidebar"
                    />
                    <span className="text-foreground group-hover:text-primary transition-colors">{botIdentity?.name || "LimeBot"}</span>
                </a>
            </div>
            <div className="flex-1 overflow-auto py-4 px-3">
                <div className="mb-5 rounded-xl border border-border/70 bg-background/60 p-3">
                    <div className="mb-2 text-[10px] font-bold uppercase tracking-[0.18em] text-muted-foreground">
                        Assistant Mode
                    </div>
                    <Select
                        value={subagentSelection}
                        onValueChange={handleSubagentSelectionChange}
                        disabled={savingSubagentSelection}
                    >
                        <SelectTrigger className="h-9 rounded-lg border-border/70 bg-card/70 text-sm">
                            <SelectValue placeholder="Choose a mode" />
                        </SelectTrigger>
                        <SelectContent>
                            {subagentOptions.map((option) => (
                                <SelectItem key={option.value} value={option.value}>
                                    {option.label}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                    <div className="mt-2 text-[11px] text-muted-foreground">
                        Applies to web, Discord, Telegram, and WhatsApp.
                    </div>
                </div>

                <nav className="grid items-start gap-1">
                    <div className="px-3 py-2 text-[10px] font-bold text-muted-foreground uppercase tracking-widest">
                        Chat
                    </div>
                    {navItems.map((item) => (
                        <NavItem
                            key={item.id}
                            icon={item.icon}
                            label={item.label}
                            active={activeView === item.id}
                            onClick={() => handleNav(item.id)}
                        />
                    ))}

                    <div className="mt-6 px-3 py-2 text-[10px] font-bold text-muted-foreground uppercase tracking-widest">
                        Agent
                    </div>
                    {agentItems.map((item) => (
                        <NavItem
                            key={item.id}
                            icon={item.icon}
                            label={item.label}
                            active={activeView === item.id}
                            onClick={() => handleNav(item.id)}
                        />
                    ))}

                    <div className="mt-6 px-3 py-2 text-[10px] font-bold text-muted-foreground uppercase tracking-widest">
                        Settings
                    </div>
                    {configItems.map((item) => (
                        <NavItem
                            key={item.id}
                            icon={item.icon}
                            label={item.label}
                            active={activeView === item.id}
                            onClick={() => handleNav(item.id)}
                        />
                    ))}
                </nav>
            </div>
            <div className="mt-auto m-2 rounded-xl border border-border bg-muted/30 p-4">
                <div className="mb-3 flex items-center justify-between">
                    <div className="text-[10px] font-bold uppercase tracking-[0.22em] text-muted-foreground">
                        Runtime Status
                    </div>
                    <span
                        className={cn(
                            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold",
                            runtimeStatus?.isConnected
                                ? "border-primary/20 bg-primary/10 text-primary"
                                : "border-amber-500/20 bg-amber-500/10 text-amber-500"
                        )}
                    >
                        {runtimeStatus?.isConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
                        {runtimeStatus?.isConnected ? "Gateway Live" : "Reconnecting"}
                    </span>
                </div>

                <div className="grid grid-cols-2 gap-2">
                    <StatusTile icon={Bot} label="Model" value={activeModel} />
                    <StatusTile icon={ShieldCheck} label="Mode" value={runtimeStatus?.autonomousMode ? "Autonomous" : "Guarded"} />
                    <StatusTile icon={Brain} label="Memory" value={memoryLabel} valueClassName={memoryTone} />
                    <StatusTile
                        icon={ActivitySquare}
                        label="Approvals"
                        value={runtimeStatus?.pendingApprovals ? String(runtimeStatus.pendingApprovals) : "Clear"}
                        valueClassName={runtimeStatus?.pendingApprovals ? "text-amber-500" : "text-primary"}
                    />
                </div>
            </div>
        </div>
    );
}

function StatusTile({
    icon: Icon,
    label,
    value,
    valueClassName,
}: {
    icon: any;
    label: string;
    value: string;
    valueClassName?: string;
}) {
    return (
        <div className="rounded-lg border border-border/70 bg-background/60 p-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                <Icon className="h-3 w-3" />
                {label}
            </div>
            <div className={cn("truncate text-xs font-medium text-foreground", valueClassName)}>
                {value}
            </div>
        </div>
    );
}

function NavItem({ icon: Icon, label, active, onClick }: { icon: any, label: string, active?: boolean, onClick?: () => void }) {
    return (
        <button
            onClick={onClick}
            className={cn(
                "group w-full flex items-center gap-3 rounded-xl px-3 py-2.5 transition-all duration-200",
                active
                    ? "bg-primary/10 text-primary shadow-sm"
                    : "text-muted-foreground hover:bg-white/5 hover:text-foreground"
            )}
        >
            <Icon className={cn("h-4 w-4 transition-transform group-hover:scale-110", active && "fill-current")} />
            <span className="font-medium text-sm">{label}</span>
            {active && <div className="ml-auto w-1 h-1 rounded-full bg-primary" />}
        </button>
    );
}
