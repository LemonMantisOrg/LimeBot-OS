import { useEffect, useState } from 'react';
import axios from 'axios';
import { API_BASE_URL } from "@/lib/api";
import { Search, Zap, CheckCircle2, AlertCircle, Box } from 'lucide-react';
import { Input } from "@/components/ui/input";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

interface Skill {
    id: string;
    name: string;
    description: string;
    path?: string;
    enabled: boolean;
    active: boolean;
    source_kind?: 'bundled' | 'git-managed' | 'local' | 'legacy-local' | string;
    editable?: boolean;
    deps_ok?: boolean;
    missing_deps?: {
        python?: string[];
        node?: string[];
        binaries?: string[];
    };
    required_deps?: {
        python?: string[];
        node?: string[];
        binaries?: string[];
    };
}

export function SkillsPage() {
    const [skills, setSkills] = useState<Skill[]>([]);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState("");
    const [installingSkillId, setInstallingSkillId] = useState<string | null>(null);

    const loadSkills = async () => {
        try {
            const res = await axios.get(`${API_BASE_URL}/api/skills`);
            setSkills(res.data.skills || []);
            setLoading(false);
        } catch (err) {
            console.error("Failed to load skills:", err);
            setLoading(false);
        }
    };

    useEffect(() => {
        loadSkills();
    }, []);

    const toggleSkill = async (skill: Skill, enable: boolean) => {
        if (skill.deps_ok === false && enable) {
            toast.error(`Install dependencies for ${skill.name} before enabling it.`);
            return;
        }
        // Optimistic update
        setSkills(skills.map(s => s.id === skill.id ? {
            ...s,
            enabled: enable,
            active: enable && s.deps_ok !== false,
        } : s));

        try {
            const res = await axios.post(`${API_BASE_URL}/api/skills/${skill.id}/toggle`, { enable });
            if (res.data?.status !== "success") {
                throw new Error(res.data?.message || "Failed to toggle skill");
            }
            toast.success(enable ? `${skill.name} enabled` : `${skill.name} disabled`);
            // The backend restarts, so we might lose connection briefly
        } catch (err) {
            console.error("Failed to toggle skill:", err);
            // Revert on error
            setSkills(skills.map(s => s.id === skill.id ? {
                ...s,
                enabled: skill.enabled,
                active: skill.active,
            } : s));
            toast.error(`Failed to ${enable ? "enable" : "disable"} ${skill.name}`);
        }
    };

    const installDeps = async (skill: Skill) => {
        setInstallingSkillId(skill.id);
        try {
            const res = await axios.post(`${API_BASE_URL}/api/skills/${skill.id}/deps`);
            const status = res.data?.status;
            if (status === "success") {
                toast.success(`Dependencies installed for ${skill.name}`);
            } else if (status === "partial") {
                toast.warning(`Some dependencies for ${skill.name} are still missing`);
            } else if (status === "error") {
                toast.error(res.data?.message || `Failed to install dependencies for ${skill.name}`);
            } else {
                throw new Error(res.data?.message || "Failed to install dependencies");
            }
            await loadSkills();
        } catch (err: any) {
            console.error("Failed to install skill deps:", err);
            const msg = err?.response?.data?.message || err?.message || `Failed to install dependencies for ${skill.name}`;
            toast.error(msg);
        } finally {
            setInstallingSkillId(null);
        }
    };

    const filteredSkills = skills.filter(skill =>
        skill.name.toLowerCase().includes(search.toLowerCase()) ||
        skill.description.toLowerCase().includes(search.toLowerCase())
    );

    const formatMissingDeps = (skill: Skill) => {
        const missing = skill.missing_deps || {};
        const python = missing.python || [];
        const node = missing.node || [];
        const binaries = missing.binaries || [];
        const parts: string[] = [];
        if (python.length) parts.push(`python: ${python.join(", ")}`);
        if (node.length) parts.push(`node: ${node.join(", ")}`);
        if (binaries.length) parts.push(`binaries: ${binaries.join(", ")}`);
        return parts.join(" | ");
    };

    const formatRequiredDeps = (skill: Skill) => {
        const required = skill.required_deps || {};
        const python = required.python || [];
        const node = required.node || [];
        const binaries = required.binaries || [];
        const parts: string[] = [];
        if (python.length) parts.push(`python: ${python.join(", ")}`);
        if (node.length) parts.push(`node: ${node.join(", ")}`);
        if (binaries.length) parts.push(`binaries: ${binaries.join(", ")}`);
        return parts.join(" | ");
    };

    const SkillCard = ({ skill }: { skill: Skill }) => {
        const missingDeps = skill.deps_ok === false;
        const missingSummary = missingDeps ? formatMissingDeps(skill) : "";
        const disableEnable = missingDeps && !skill.enabled;
        const requiredSummary = formatRequiredDeps(skill);
        const hasRequired = !!requiredSummary;
        const isConfigured = skill.enabled;
        const isRunnable = skill.active;

        return (
        <Card className="border-border hover:border-primary/30 transition-colors group">
            <CardHeader className="pb-3">
                <div className="flex items-start justify-between">
                    <div className="flex items-center gap-3">
                        <div className={`p-2 rounded-lg transition-colors ${
                            skill.active 
                                ? 'bg-primary/10 text-primary'
                                : 'bg-muted text-muted-foreground'
                        }`}>
                            <Zap className="h-5 w-5" />
                        </div>
                        <div>
                            <CardTitle className="text-base flex items-center gap-2">
                                {skill.name}
                                {missingDeps && (
                                    <Badge
                                        variant="destructive"
                                        className="text-[10px] h-5"
                                        title={missingSummary}
                                    >
                                        MISSING DEPS
                                    </Badge>
                                )}
                            </CardTitle>
                        </div>
                    </div>
                    <Switch
                        checked={skill.enabled}
                        disabled={disableEnable}
                        onCheckedChange={(checked) => toggleSkill(skill, checked)}
                    />
                </div>
            </CardHeader>
            <CardContent>
                <CardDescription className="line-clamp-3 text-sm min-h-[60px]">
                    {skill.description}
                </CardDescription>
                {hasRequired && (
                    <div className="mt-3 text-[11px] text-muted-foreground font-mono bg-muted/40 rounded-lg px-2.5 py-2 border border-border/60">
                        <span className="uppercase tracking-wide text-[9px] text-muted-foreground/70 block mb-1">Dependencies</span>
                        <div className="whitespace-pre-wrap break-words">{requiredSummary}</div>
                    </div>
                )}
                {missingDeps && (
                    <div className="mt-3 flex items-center justify-between gap-3 rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2">
                        <div className="min-w-0 text-[11px] text-red-200/90">
                            <div className="font-medium text-red-300">Dependency install required</div>
                            <div className="break-words text-red-200/70">{missingSummary}</div>
                        </div>
                        <Button
                            size="sm"
                            variant="outline"
                            className="shrink-0"
                            disabled={installingSkillId === skill.id}
                            onClick={() => installDeps(skill)}
                        >
                            {installingSkillId === skill.id ? "Installing..." : "Install Deps"}
                        </Button>
                    </div>
                )}
                <div className="mt-4 pt-4 border-t border-border flex items-center justify-between text-xs text-muted-foreground font-mono">
                    <span className="truncate max-w-[150px] opacity-70">
                        {skill.source_kind || 'local'}{skill.editable ? ' · editable' : ' · read-only'}
                    </span>
                    {isRunnable ? (
                        <span className="flex items-center gap-1 text-primary">
                            <CheckCircle2 className="h-3 w-3" /> Enabled
                        </span>
                    ) : isConfigured ? (
                        <span className="flex items-center gap-1 text-amber-400">
                            <AlertCircle className="h-3 w-3" /> Needs Deps
                        </span>
                    ) : (
                        <span className="flex items-center gap-1">
                            <AlertCircle className="h-3 w-3" /> Disabled
                        </span>
                    )}
                </div>
            </CardContent>
        </Card>
    );
    };

    return (
        <div className="h-full overflow-y-auto p-6 md:p-8 bg-background/50">
            <div className="max-w-6xl mx-auto space-y-10">
                <header className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
                    <div>
                        <h1 className="text-2xl font-bold flex items-center gap-2">
                            <Box className="h-7 w-7 text-primary" />
                            Skills Library
                        </h1>
                        <p className="text-muted-foreground mt-1">
                            Manage LimeBot's core capabilities and ClawHub extensions.
                        </p>
                    </div>
                    <div className="relative w-full md:w-72">
                        <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                        <Input
                            placeholder="Search skills..."
                            className="pl-9 bg-background/50"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                        />
                    </div>
                </header>

                {loading ? (
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                        {[1, 2, 3, 4, 5, 6].map(i => (
                            <div key={i} className="h-48 rounded-xl bg-muted/50 animate-pulse" />
                        ))}
                    </div>
                ) : (
                    <div className="space-y-10">
                        {filteredSkills.length > 0 && (
                            <div className="space-y-4">
                                <div className="flex items-center gap-2 pb-2 border-b border-border/50">
                                    <Zap className="h-5 w-5 text-primary" />
                                    <h2 className="text-lg font-semibold">Skills</h2>
                                    <Badge variant="outline" className="ml-2">{filteredSkills.length}</Badge>
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                                    {filteredSkills.map(skill => (
                                        <SkillCard key={skill.id} skill={skill} />
                                    ))}
                                </div>
                            </div>
                        )}

                        {filteredSkills.length === 0 && (
                            <div className="col-span-full py-12 text-center text-muted-foreground">
                                <div className="inline-flex items-center justify-center p-4 rounded-full bg-muted mb-4">
                                    <Search className="h-6 w-6 opacity-50" />
                                </div>
                                <p>No skills found matching "{search}"</p>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
