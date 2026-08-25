import { useState, useEffect, useEffectEvent, useRef } from 'react';
import axios from 'axios';
import { API_BASE_URL } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Key, Bot, ShieldCheck, ArrowRight, CheckCircle2, RefreshCw, Trash, Plus } from 'lucide-react';
import { Badge } from '../ui/badge';
import { DEFAULT_MODEL_BY_PROVIDER, getAdditionalModels, getInitialModelForProvider, getRecommendedModels, PROVIDER_LABELS, type LlmModelOption } from '@/lib/llm-models';
import {
    clearSetupProgress,
    loadSetupProgress,
    normalizeSetupError,
    saveSetupProgress,
    setupRetryDelay,
    type SetupPhase,
    type SetupProgress,
} from '@/lib/setup-state';

const FALLBACK_MODELS: LlmModelOption[] = [
    { id: 'gemini/gemini-3.6-flash', name: 'Gemini 3.6 Flash', provider: 'gemini' },
    { id: 'gemini/gemini-3.5-flash', name: 'Gemini 3.5 Flash', provider: 'gemini' },
    { id: 'gemini/gemini-3.5-flash-lite', name: 'Gemini 3.5 Flash-Lite', provider: 'gemini' },
    { id: 'gemini/gemini-3.1-pro-preview', name: 'Gemini 3.1 Pro (Preview)', provider: 'gemini' },
    { id: 'openai/gpt-5.6-sol', name: 'GPT-5.6 Sol', provider: 'openai' },
    { id: 'openai/gpt-5.6-terra', name: 'GPT-5.6 Terra', provider: 'openai' },
    { id: 'openai/gpt-5.6-luna', name: 'GPT-5.6 Luna', provider: 'openai' },
    { id: 'openai/gpt-5.5', name: 'GPT-5.5', provider: 'openai' },
    { id: 'openai-codex/gpt-5.6-sol', name: 'GPT-5.6 Sol', provider: 'openai-codex' },
    { id: 'openai-codex/gpt-5.6-luna', name: 'GPT-5.6 Luna', provider: 'openai-codex' },
    { id: 'openai-codex/gpt-5.6-terra', name: 'GPT-5.6 Terra', provider: 'openai-codex' },
    { id: 'openai-codex/gpt-5.5', name: 'GPT-5.5', provider: 'openai-codex' },
    { id: 'openai-codex/gpt-5.4', name: 'GPT-5.4', provider: 'openai-codex' },
    { id: 'openai-codex/gpt-5.4-mini', name: 'GPT-5.4 Mini', provider: 'openai-codex' },
    { id: 'openrouter/openai/gpt-5.6-sol', name: 'OpenRouter: OpenAI GPT-5.6 Sol', provider: 'openrouter' },
    { id: 'openrouter/anthropic/claude-opus-5', name: 'OpenRouter: Claude Opus 5', provider: 'openrouter' },
    { id: 'openrouter/google/gemini-3.7-flash', name: 'OpenRouter: Gemini 3.7 Flash', provider: 'openrouter' },
    { id: 'anthropic/claude-opus-5', name: 'Claude Opus 5', provider: 'anthropic' },
    { id: 'anthropic/claude-sonnet-5', name: 'Claude Sonnet 5', provider: 'anthropic' },
    { id: 'deepseek/deepseek-v4-flash', name: 'DeepSeek V4 Flash', provider: 'deepseek' },
    { id: 'deepseek/deepseek-v4-pro', name: 'DeepSeek V4 Pro', provider: 'deepseek' },
    { id: 'deepseek/deepseek-v3.2', name: 'DeepSeek V3.2', provider: 'deepseek' },
    { id: 'xai/grok-4.6', name: 'Grok 4.6', provider: 'xai' },
    { id: 'moonshot/kimi-k3', name: 'Kimi K3', provider: 'moonshot' },
    { id: 'moonshot/kimi-k2.6', name: 'Kimi K2.6', provider: 'moonshot' },
    { id: 'moonshot/kimi-k2.5', name: 'Kimi K2.5', provider: 'moonshot' },
    { id: 'qwen/qwen3.5-plus', name: 'Qwen 3.5 Plus', provider: 'qwen' },
    { id: 'qwen/qwen3.5-flash', name: 'Qwen 3.5 Flash', provider: 'qwen' },
    { id: 'qwen/qwen3-max', name: 'Qwen 3 Max', provider: 'qwen' },
    { id: 'nvidia/deepseek-ai/deepseek-v4-flash-0731', name: 'NVIDIA: DeepSeek V4 Flash', provider: 'nvidia' },
    { id: 'nvidia/deepseek-ai/deepseek-v4-pro', name: 'NVIDIA: DeepSeek V4 Pro', provider: 'nvidia' },
    { id: 'nvidia/qwen/qwen3-coder-next', name: 'NVIDIA: Qwen 3 Coder Next', provider: 'nvidia' },
    { id: 'nvidia/openai/gpt-oss-120b', name: 'GPT-OSS 120B', provider: 'nvidia' },
    { id: 'nvidia/openai/gpt-oss-20b', name: 'GPT-OSS 20B', provider: 'nvidia' },
    { id: 'nvidia/meta/llama-4-maverick-17b-128e-instruct', name: 'Llama 4 Maverick', provider: 'nvidia' },
    { id: 'nvidia/deepseek-ai/deepseek-v3.2', name: 'DeepSeek V3.2', provider: 'nvidia' },
];

type SetupConfig = {
    LLM_MODEL: string;
    GEMINI_API_KEY: string;
    OPENROUTER_API_KEY: string;
    OPENAI_API_KEY: string;
    ANTHROPIC_API_KEY: string;
    XAI_API_KEY: string;
    DEEPSEEK_API_KEY: string;
    MOONSHOT_API_KEY: string;
    DASHSCOPE_API_KEY: string;
    NVIDIA_API_KEY: string;
    DISCORD_TOKEN: string;
    ENABLE_WHATSAPP: string;
    WHATSAPP_BRIDGE_URL: string;
    ALLOWED_PATHS: string[];
    ENABLE_DYNAMIC_PERSONALITY: string;
    APP_API_KEY: string;
};

export function SetupPage() {
    const [step, setStep] = useState(1);
    const [config, setConfig] = useState<SetupConfig>({
        LLM_MODEL: 'gemini/gemini-3.6-flash',
        GEMINI_API_KEY: '',
        OPENROUTER_API_KEY: '',
        OPENAI_API_KEY: '',
        ANTHROPIC_API_KEY: '',
        XAI_API_KEY: '',
        DEEPSEEK_API_KEY: '',
        MOONSHOT_API_KEY: '',
        DASHSCOPE_API_KEY: '',
        NVIDIA_API_KEY: '',
        DISCORD_TOKEN: '',
        ENABLE_WHATSAPP: 'false',
        WHATSAPP_BRIDGE_URL: 'ws://localhost:3000',
        ALLOWED_PATHS: ['./persona', './logs'] as string[],
        ENABLE_DYNAMIC_PERSONALITY: 'false',
        APP_API_KEY: localStorage.getItem('limebot_api_key') || crypto.randomUUID()
    });
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [setupPhase, setSetupPhase] = useState<SetupPhase>('editing');
    const [elapsedSeconds, setElapsedSeconds] = useState(0);
    const reconnectRunRef = useRef(0);

    const [availableModels, setAvailableModels] = useState<LlmModelOption[]>(FALLBACK_MODELS);
    const [isLoadingModels, setIsLoadingModels] = useState(false);
    const [showAllModels, setShowAllModels] = useState(false);
    const [codexAuth, setCodexAuth] = useState<{ configured: boolean; email?: string } | null>(null);

    const fetchModels = async (retries = 3) => {
        setIsLoadingModels(true);
        try {
            const res = await axios.get(`${API_BASE_URL}/api/llm/models`);
            if (res.data.models && res.data.models.length > 0) {
                setAvailableModels(res.data.models);
            }
            if (res.data.codexAuth) {
                setCodexAuth(res.data.codexAuth);
            }
        } catch (err) {
            console.error("Failed to load models:", err);
            if (retries > 0) {
                setTimeout(() => fetchModels(retries - 1), 2000);
            }
        } finally {
            setIsLoadingModels(false);
        }
    };

    const fetchModelsEvent = useEffectEvent(fetchModels);

    useEffect(() => {
        fetchModelsEvent();
    }, []);

    // Polling for Codex OAuth configuration
    useEffect(() => {
        let timer: ReturnType<typeof setInterval> | undefined;
        const currentProvider = config.LLM_MODEL.split('/')[0] || 'gemini';
        
        if (currentProvider === 'openai-codex' && !codexAuth?.configured) {
            timer = setInterval(() => {
                fetchModelsEvent(0); // poll without retries
            }, 3000);
        }
        
        return () => {
            if (timer) clearInterval(timer);
        };
    }, [config.LLM_MODEL, codexAuth?.configured]);

    const currentProvider = config.LLM_MODEL.split('/')[0] || 'gemini';
    const recommendedModels = getRecommendedModels(availableModels, currentProvider);
    const additionalModels = getAdditionalModels(availableModels, currentProvider);
    const selectedHiddenModel = additionalModels.find((model) => model.id === config.LLM_MODEL);
    const recommendedDisplayModels = selectedHiddenModel
        ? [selectedHiddenModel, ...recommendedModels]
        : recommendedModels;
    const additionalDisplayModels = selectedHiddenModel
        ? additionalModels.filter((model) => model.id !== selectedHiddenModel.id)
        : additionalModels;

    const getRequiredKeyError = () => {
        if (config.LLM_MODEL.startsWith('openai-codex')) {
            if (codexAuth?.configured === false) return 'Codex OAuth authentication is required.';
            return null;
        }
        if (config.LLM_MODEL.startsWith('openrouter') && !config.OPENROUTER_API_KEY) return 'OpenRouter API Key is required.';
        if (config.LLM_MODEL.startsWith('gemini') && !config.GEMINI_API_KEY) return 'Gemini API Key is required.';
        if (config.LLM_MODEL.startsWith('openai') && !config.OPENAI_API_KEY) return 'OpenAI API Key is required.';
        if (config.LLM_MODEL.startsWith('anthropic') && !config.ANTHROPIC_API_KEY) return 'Anthropic API Key is required.';
        if (config.LLM_MODEL.startsWith('xai') && !config.XAI_API_KEY) return 'xAI API Key is required.';
        if (config.LLM_MODEL.startsWith('deepseek') && !config.DEEPSEEK_API_KEY) return 'DeepSeek API Key is required.';
        if (config.LLM_MODEL.startsWith('moonshot') && !config.MOONSHOT_API_KEY) return 'Moonshot API Key is required.';
        if (config.LLM_MODEL.startsWith('qwen') && !config.DASHSCOPE_API_KEY) return 'Qwen API Key is required.';
        if (config.LLM_MODEL.startsWith('nvidia') && !config.NVIDIA_API_KEY) return 'NVIDIA API Key is required.';
        return null;
    };

    const handleChange = <K extends keyof SetupConfig>(key: K, value: SetupConfig[K]) => {
        setConfig(prev => ({ ...prev, [key]: value }));
    };

    const pollForRestart = async (progress: SetupProgress) => {
        const runId = ++reconnectRunRef.current;
        setStep(5);
        setSetupPhase('reconnecting');
        setError(null);
        saveSetupProgress(sessionStorage, { ...progress, phase: 'reconnecting' });

        for (let attempt = 0; attempt < 22; attempt += 1) {
            await new Promise(resolve => setTimeout(resolve, setupRetryDelay(attempt)));
            if (runId !== reconnectRunRef.current) return;

            setElapsedSeconds(Math.max(0, Math.round((Date.now() - progress.startedAt) / 1000)));
            try {
                const response = await axios.get(`${API_BASE_URL}/api/setup/status`, {
                    params: { restart_token: progress.restartToken },
                    timeout: 4_000,
                });
                const status = response.data;
                const restarted = status.boot_id && status.boot_id !== progress.previousBootId;
                if (status.configured && status.restart_recognized && restarted) {
                    clearSetupProgress(sessionStorage);
                    setSetupPhase('ready');
                    window.location.assign('/');
                    return;
                }
            } catch {
                // A brief connection failure is expected while the backend replaces itself.
            }
        }

        setSetupPhase('failed');
        setError(normalizeSetupError({ code: 'restart_timeout' }).message);
    };

    useEffect(() => {
        const progress = loadSetupProgress(sessionStorage);
        if (!progress) return;
        setSaving(true);
        void pollForRestart(progress).finally(() => setSaving(false));
        return () => {
            reconnectRunRef.current += 1;
        };
    }, []); // Resume is intentionally evaluated once from session storage.

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        setSetupPhase('validating');
        try {
            if (!config.LLM_MODEL.trim()) {
                throw new Error('Please select an LLM model.');
            }
            const keyError = getRequiredKeyError();
            if (keyError) {
                throw new Error(keyError);
            }

            const previousApiKey = localStorage.getItem('limebot_api_key');
            const response = await axios.post(
                `${API_BASE_URL}/api/setup/complete`,
                { env: config },
                {
                    timeout: 30_000,
                    ...(previousApiKey ? { headers: { 'X-API-Key': previousApiKey } } : {}),
                },
            );
            const data = response.data;
            if (data.status !== 'restarting' || !data.restart_token || !data.boot_id) {
                throw new Error('LimeBot returned an incomplete setup response.');
            }

            localStorage.setItem('limebot_api_key', config.APP_API_KEY);
            axios.defaults.headers.common['X-API-Key'] = config.APP_API_KEY;

            const progress: SetupProgress = {
                phase: 'restarting',
                restartToken: data.restart_token,
                previousBootId: data.boot_id,
                model: config.LLM_MODEL,
                startedAt: Date.now(),
            };
            saveSetupProgress(sessionStorage, progress);
            setSetupPhase('restarting');
            await pollForRestart(progress);
        } catch (err: unknown) {
            setSetupPhase('failed');
            const payload = axios.isAxiosError(err)
                ? (err.response?.data?.detail
                    ?? err.response?.data
                    ?? (err.code === 'ECONNABORTED'
                        ? { code: 'provider_timeout' }
                        : { message: err.message }))
                : { message: err instanceof Error ? err.message : undefined };
            if (
                payload &&
                typeof payload === 'object' &&
                'config_saved' in payload &&
                payload.config_saved === true
            ) {
                localStorage.setItem('limebot_api_key', config.APP_API_KEY);
                axios.defaults.headers.common['X-API-Key'] = config.APP_API_KEY;
            }
            setError(normalizeSetupError(payload).message);
        } finally {
            setSaving(false);
        }
    };

    const renderStep = () => {
        switch (step) {
            case 1: // Welcome
                return (
                    <div className="space-y-6 animate-in fade-in duration-500">
                        <div className="text-center space-y-2">
                            <div className="inline-flex items-center justify-center mb-4">
                                <img src="/limeBrain.png" alt="LimeBot logo" className="h-32 w-auto animate-in zoom-in duration-700" />
                            </div>
                            <h2 className="text-3xl font-bold tracking-tight">Welcome to LimeBot</h2>
                            <p className="text-muted-foreground text-lg max-w-md mx-auto">
                                Let's get your elegant agentic bot up and running in just a few steps.
                            </p>
                        </div>
                        <div className="pt-4 border-t border-border/50">
                            <Button onClick={() => setStep(2)} className="w-full h-12 text-lg font-bold group">
                                Start Configuration
                                <ArrowRight className="ml-2 h-5 w-5 group-hover:translate-x-1 transition-transform" />
                            </Button>
                        </div>
                    </div>
                );

            case 2: // LLM Settings
                return (
                    <div className="space-y-6 animate-in slide-in-from-right-4 duration-300">
                        <div className="flex items-center gap-3">
                            <div className="p-2 bg-primary/20 rounded-lg">
                                <Key className="h-6 w-6 text-primary" />
                            </div>
                            <div>
                                <h3 className="text-xl font-bold">LLM Brain</h3>
                                <p className="text-sm text-muted-foreground">Select your AI provider and model.</p>
                            </div>
                        </div>

                        <div className="space-y-4">
                            <div className="space-y-2">
                                <Label htmlFor="provider">Provider</Label>
                                <Select
                                    value={config.LLM_MODEL.split('/')[0] || 'gemini'}
                                    onValueChange={(val) => {
                                        setShowAllModels(false);
                                        const nextModel = getInitialModelForProvider(availableModels, val);
                                        if (nextModel) {
                                            handleChange('LLM_MODEL', nextModel);
                                        } else {
                                            handleChange('LLM_MODEL', DEFAULT_MODEL_BY_PROVIDER[val] || config.LLM_MODEL);
                                        }
                                    }}
                                >
                                    <SelectTrigger id="provider">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="gemini">{PROVIDER_LABELS.gemini}</SelectItem>
                                        <SelectItem value="openai">{PROVIDER_LABELS.openai}</SelectItem>
                                        <SelectItem value="openai-codex">{PROVIDER_LABELS["openai-codex"]}</SelectItem>
                                        <SelectItem value="openrouter">{PROVIDER_LABELS.openrouter}</SelectItem>
                                        <SelectItem value="anthropic">{PROVIDER_LABELS.anthropic}</SelectItem>
                                        <SelectItem value="xai">{PROVIDER_LABELS.xai}</SelectItem>
                                        <SelectItem value="deepseek">{PROVIDER_LABELS.deepseek}</SelectItem>
                                        <SelectItem value="moonshot">{PROVIDER_LABELS.moonshot}</SelectItem>
                                        <SelectItem value="qwen">{PROVIDER_LABELS.qwen}</SelectItem>
                                        <SelectItem value="nvidia">{PROVIDER_LABELS.nvidia}</SelectItem>
                                    </SelectContent>
                                </Select>
                                {isLoadingModels && (
                                    <p className="text-[10px] text-primary animate-pulse flex items-center gap-1 mt-1">
                                        <RefreshCw className="h-2 w-2 animate-spin" />
                                        Updating latest models...
                                    </p>
                                )}
                            </div>

                            <div className="space-y-2">
                                <Label htmlFor="model">Model</Label>
                                <Select value={config.LLM_MODEL} onValueChange={(val) => handleChange('LLM_MODEL', val)}>
                                    <SelectTrigger id="model">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        {recommendedDisplayModels.length > 0 && (
                                            <SelectGroup>
                                                <SelectLabel>Recommended</SelectLabel>
                                                {recommendedDisplayModels.map((model) => (
                                                    <SelectItem key={model.id} value={model.id}>
                                                        {model.name}
                                                    </SelectItem>
                                                ))}
                                            </SelectGroup>
                                        )}
                                        {showAllModels && additionalDisplayModels.length > 0 && (
                                            <>
                                                <SelectSeparator />
                                                <SelectGroup>
                                                    <SelectLabel>All {PROVIDER_LABELS[currentProvider]} Models</SelectLabel>
                                                    {additionalDisplayModels.map((model) => (
                                                        <SelectItem key={model.id} value={model.id}>
                                                            {model.name}
                                                        </SelectItem>
                                                    ))}
                                                </SelectGroup>
                                            </>
                                        )}
                                        {recommendedDisplayModels.length === 0 && (!showAllModels || additionalDisplayModels.length === 0) && (
                                            <div className="p-2 text-xs text-muted-foreground">Loading models...</div>
                                        )}
                                    </SelectContent>
                                </Select>
                                {additionalDisplayModels.length > 0 && (
                                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                                        <span>
                                            Showing {recommendedDisplayModels.length} recommended model{recommendedDisplayModels.length === 1 ? '' : 's'} first.
                                        </span>
                                        <Button
                                            type="button"
                                            variant="ghost"
                                            size="sm"
                                            className="h-7 px-2 text-xs"
                                            onClick={() => setShowAllModels((prev) => !prev)}
                                        >
                                            {showAllModels
                                                ? 'Show recommended only'
                                                : `Show all ${recommendedDisplayModels.length + additionalDisplayModels.length} models`}
                                        </Button>
                                    </div>
                                )}
                            </div>

                            {/* Dynamic API Key Input */}
                            <div className="space-y-4 animate-in fade-in duration-300">
                                {config.LLM_MODEL.startsWith('gemini') && (

                                    <div className="space-y-2">
                                        <Label htmlFor="gemini_key">Gemini API Key</Label>
                                        <Input
                                            id="gemini_key"
                                            type="password"
                                            placeholder="sk-..."
                                            value={config.GEMINI_API_KEY}
                                            onChange={(e) => handleChange('GEMINI_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('openai-codex') && (
                                    <div className="space-y-3 p-4 bg-primary/5 rounded-lg border border-primary/20 animate-in fade-in duration-300">
                                        <div className="flex items-center justify-between">
                                            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                                                Codex Auth Status
                                            </span>
                                            {codexAuth?.configured ? (
                                                <Badge className="border-primary/20 bg-primary/10 px-2 py-0.5 text-[10px] font-bold uppercase text-primary">
                                                    ✓ Configured
                                                </Badge>
                                            ) : (
                                                <Badge className="bg-amber-500/10 text-amber-500 border-amber-500/20 text-[10px] uppercase font-bold px-2 py-0.5 animate-pulse">
                                                    ● Awaiting Login
                                                </Badge>
                                            )}
                                        </div>

                                        {codexAuth?.configured ? (
                                            <div className="space-y-1">
                                                <p className="text-xs text-muted-foreground">
                                                    Your Codex OAuth session is active and ready to go.
                                                </p>
                                                {codexAuth.email && (
                                                    <p className="text-xs text-primary font-medium">
                                                        Connected as: {codexAuth.email}
                                                    </p>
                                                )}
                                            </div>
                                        ) : (
                                            <div className="space-y-3">
                                                <p className="text-xs text-muted-foreground leading-relaxed">
                                                    Codex uses ChatGPT OAuth. Run the login command in your terminal, complete the browser authorization, and this page will automatically activate.
                                                </p>
                                                <div className="bg-background/80 p-2.5 rounded border border-border/50 font-mono text-[11px] select-all flex items-center justify-between text-primary">
                                                    <code>limebot auth codex login</code>
                                                </div>
                                                <Button 
                                                    type="button" 
                                                    variant="outline" 
                                                    size="sm" 
                                                    className="w-full text-xs flex items-center justify-center gap-1.5 h-8"
                                                    onClick={() => fetchModels(0)}
                                                    disabled={isLoadingModels}
                                                >
                                                    <RefreshCw className={`h-3 w-3 ${isLoadingModels ? 'animate-spin' : ''}`} />
                                                    Check Status Now
                                                </Button>
                                            </div>
                                        )}
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('openai/') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="openai_key">OpenAI API Key</Label>
                                        <Input
                                            id="openai_key"
                                            type="password"
                                            placeholder="sk-..."
                                            value={config.OPENAI_API_KEY}
                                            onChange={(e) => handleChange('OPENAI_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('openrouter') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="openrouter_key">OpenRouter API Key</Label>
                                        <Input
                                            id="openrouter_key"
                                            type="password"
                                            placeholder="sk-or-..."
                                            value={config.OPENROUTER_API_KEY}
                                            onChange={(e) => handleChange('OPENROUTER_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('anthropic') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="anthropic_key">Anthropic API Key</Label>
                                        <Input
                                            id="anthropic_key"
                                            type="password"
                                            placeholder="sk-ant-..."
                                            value={config.ANTHROPIC_API_KEY}
                                            onChange={(e) => handleChange('ANTHROPIC_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('xai') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="xai_key">xAI API Key</Label>
                                        <Input
                                            id="xai_key"
                                            type="password"
                                            placeholder="xai-..."
                                            value={config.XAI_API_KEY}
                                            onChange={(e) => handleChange('XAI_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('deepseek') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="deepseek_key">DeepSeek API Key</Label>
                                        <Input
                                            id="deepseek_key"
                                            type="password"
                                            placeholder="sk-..."
                                            value={config.DEEPSEEK_API_KEY}
                                            onChange={(e) => handleChange('DEEPSEEK_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('moonshot') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="moonshot_key">Moonshot / Kimi API Key</Label>
                                        <Input
                                            id="moonshot_key"
                                            type="password"
                                            placeholder="sk-..."
                                            value={config.MOONSHOT_API_KEY}
                                            onChange={(e) => handleChange('MOONSHOT_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('qwen') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="dashscope_key">Qwen (DashScope) API Key</Label>
                                        <Input
                                            id="dashscope_key"
                                            type="password"
                                            placeholder="sk-..."
                                            value={config.DASHSCOPE_API_KEY}
                                            onChange={(e) => handleChange('DASHSCOPE_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                                {config.LLM_MODEL.startsWith('nvidia') && (
                                    <div className="space-y-2">
                                        <Label htmlFor="nvidia_key">NVIDIA API Key</Label>
                                        <Input
                                            id="nvidia_key"
                                            type="password"
                                            placeholder="nvapi-..."
                                            value={config.NVIDIA_API_KEY}
                                            onChange={(e) => handleChange('NVIDIA_API_KEY', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                            </div>
                        </div>

                        <div className="flex gap-3 pt-4 border-t border-border/50">
                            <Button variant="outline" onClick={() => setStep(1)} className="flex-1">Back</Button>
                            <Button
                                onClick={() => setStep(3)}
                                className="flex-1"
                                disabled={
                                    !!getRequiredKeyError()
                                }
                            >
                                Continue
                            </Button>
                        </div>
                    </div>
                );

            case 3: // Channels
                return (
                    <div className="space-y-6 animate-in slide-in-from-right-4 duration-300">
                        <div className="flex items-center gap-3">
                            <div className="p-2 bg-primary/20 rounded-lg">
                                <Bot className="h-6 w-6 text-primary" />
                            </div>
                            <div>
                                <h3 className="text-xl font-bold">Channels</h3>
                                <p className="text-sm text-muted-foreground">Where should LimeBot live?</p>
                            </div>
                        </div>

                        <div className="space-y-4">
                            <div className="space-y-2">
                                <Label htmlFor="discord_token">Discord Bot Token (Optional)</Label>
                                <Input
                                    id="discord_token"
                                    type="password"
                                    placeholder="your-bot-token"
                                    value={config.DISCORD_TOKEN}
                                    onChange={(e) => handleChange('DISCORD_TOKEN', e.target.value)}
                                    className="bg-background/50"
                                />
                                <p className="text-[10px] text-muted-foreground italic">You can add this later in settings.</p>
                            </div>

                            <div className="space-y-4 pt-4 border-t border-border/50">
                                <div className="flex items-center justify-between">
                                    <div className="space-y-0.5">
                                        <Label htmlFor="enable_whatsapp">Enable WhatsApp Integration</Label>
                                        <p className="text-xs text-muted-foreground">Connects via a local bridge to your phone.</p>
                                    </div>
                                    <Switch
                                        id="enable_whatsapp"
                                        checked={config.ENABLE_WHATSAPP === 'true'}
                                        onCheckedChange={(checked) => handleChange('ENABLE_WHATSAPP', checked ? 'true' : 'false')}
                                    />
                                </div>

                                {config.ENABLE_WHATSAPP === 'true' && (
                                    <div className="space-y-2 animate-in slide-in-from-top-2 duration-200">
                                        <Label htmlFor="wa_bridge">WhatsApp Bridge URL</Label>
                                        <Input
                                            id="wa_bridge"
                                            value={config.WHATSAPP_BRIDGE_URL}
                                            onChange={(e) => handleChange('WHATSAPP_BRIDGE_URL', e.target.value)}
                                            className="bg-background/50"
                                        />
                                    </div>
                                )}
                            </div>
                        </div>

                        <div className="flex gap-3 pt-4 border-t border-border/50">
                            <Button variant="outline" onClick={() => setStep(2)} className="flex-1">Back</Button>
                            <Button onClick={() => setStep(4)} className="flex-1">Continue</Button>
                        </div>
                    </div>
                );

            case 4: // Security & Finish
                return (
                    <div className="space-y-6 animate-in slide-in-from-right-4 duration-300">
                        <div className="flex items-center gap-3">
                            <div className="p-2 bg-primary/20 rounded-lg">
                                <ShieldCheck className="h-6 w-6 text-primary" />
                            </div>
                            <div>
                                <h3 className="text-xl font-bold">Security & Sandbox</h3>
                                <p className="text-sm text-muted-foreground">Control what the bot can access.</p>
                            </div>
                        </div>

                        <div className="space-y-4">
                            <div className="space-y-4">
                                <div className="space-y-2">
                                    <Label htmlFor="allowed_paths">Allowed File Paths</Label>
                                    <div className="space-y-2 max-h-[250px] overflow-y-auto pr-2 custom-scrollbar">
                                        {(config.ALLOWED_PATHS || []).map((path, idx) => (
                                            <div key={idx} className="flex gap-2 isolate">
                                                <Input
                                                    value={path}
                                                    onChange={(e) => {
                                                        const paths = [...(config.ALLOWED_PATHS || [])];
                                                        paths[idx] = e.target.value;
                                                        handleChange("ALLOWED_PATHS", paths);
                                                    }}
                                                    placeholder="Absolute path..."
                                                    className="bg-background/50"
                                                />
                                                <Button
                                                    variant="ghost"
                                                    size="icon"
                                                    className="text-destructive hover:bg-destructive/10 shrink-0"
                                                    onClick={() => {
                                                        const paths = [...(config.ALLOWED_PATHS || [])];
                                                        paths.splice(idx, 1);
                                                        handleChange("ALLOWED_PATHS", paths);
                                                    }}
                                                >
                                                    <Trash className="h-4 w-4" />
                                                </Button>
                                            </div>
                                        ))}
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            className="w-full flex items-center justify-center gap-2 border-dashed bg-background/50"
                                            onClick={() => {
                                                const paths = [...(config.ALLOWED_PATHS || [])];
                                                paths.push("");
                                                handleChange("ALLOWED_PATHS", paths);
                                            }}
                                        >
                                            <Plus className="h-4 w-4" />
                                            Add Path
                                        </Button>
                                    </div>
                                    <p className="text-xs text-muted-foreground mt-2">
                                        Absolute paths the bot is allowed to access and edit.
                                    </p>
                                </div>

                                <div className="space-y-2">
                                    <Label htmlFor="app_key">Application Security Key</Label>
                                    <div className="flex gap-2">
                                        <Input
                                            id="app_key"
                                            value={config.APP_API_KEY || ''}
                                            readOnly
                                            className="bg-background/50 font-mono text-xs"
                                        />
                                        <Button
                                            variant="outline"
                                            size="icon"
                                            onClick={() => handleChange('APP_API_KEY', crypto.randomUUID())}
                                            title="Regenerate"
                                        >
                                            <RefreshCw className="h-4 w-4" />
                                        </Button>
                                    </div>
                                    <p className="text-xs text-muted-foreground">
                                        This key secures your connection. It will be saved automatically.
                                    </p>
                                </div>

                                <div className="h-px bg-border my-2" />

                                <div className="flex items-center justify-between">
                                    <div className="space-y-0.5">
                                        <div className="flex items-center gap-2">
                                            <Label htmlFor="dynamic_personality">Adaptive Persona</Label>
                                            <Badge variant="outline" size="sm" className="text-[10px] uppercase tracking-wider opacity-70">Experimental</Badge>
                                        </div>
                                        <p className="text-[10px] text-muted-foreground">Bot adjusts trust and style based on interaction.</p>
                                    </div>
                                    <Switch
                                        id="dynamic_personality"
                                        checked={config.ENABLE_DYNAMIC_PERSONALITY === 'true'}
                                        onCheckedChange={(checked) => handleChange('ENABLE_DYNAMIC_PERSONALITY', checked ? 'true' : 'false')}
                                    />
                                </div>
                            </div>

                            <div className="bg-primary/5 p-4 rounded-lg border border-primary/10">
                                <p className="text-sm text-center">
                                    By finishing, we will write these settings to your <code>.env</code> file.
                                </p>
                            </div>
                        </div>

                        {error && (
                            <div className="p-3 bg-destructive/10 text-destructive text-sm rounded border border-destructive/20">
                                {error}
                            </div>
                        )}

                        <div className="flex gap-3 pt-4 border-t border-border/50">
                            <Button variant="outline" onClick={() => setStep(3)} className="flex-1" disabled={saving}>Back</Button>
                            <Button onClick={handleSave} className="flex-1 bg-primary hover:bg-primary/90 text-primary-foreground font-bold" disabled={saving}>
                                {saving ? "Saving..." : "Finish Setup"}
                            </Button>
                        </div>
                    </div>
                );

            case 5: // Success
                return (
                    <div className="text-center space-y-6 py-8 animate-in zoom-in duration-500">
                        <div className="inline-flex items-center justify-center p-6 bg-primary/10 rounded-full">
                            {setupPhase === 'ready' ? (
                                <CheckCircle2 className="h-16 w-16 text-primary animate-bounce-subtle" />
                            ) : (
                                <RefreshCw className="h-16 w-16 text-primary animate-spin" />
                            )}
                        </div>
                        <div className="space-y-2">
                            <h2 className="text-3xl font-bold text-primary">
                                {setupPhase === 'failed'
                                    ? 'Restart needs attention'
                                    : setupPhase === 'ready'
                                        ? 'Setup verified'
                                        : 'Preparing LimeBot'}
                            </h2>
                            <p className="text-muted-foreground text-lg">
                                {setupPhase === 'restarting'
                                    ? 'Configuration verified. Starting the secured backend...'
                                    : setupPhase === 'failed'
                                        ? error
                                        : 'Waiting for the new backend process to become ready...'}
                            </p>
                        </div>
                        {setupPhase !== 'failed' && (
                            <p className="text-sm text-muted-foreground pt-4 animate-pulse">
                                Reconnecting securely{elapsedSeconds > 0 ? ` - ${elapsedSeconds}s` : '...'}
                            </p>
                        )}
                        {setupPhase === 'failed' && (
                            <div className="flex gap-3 pt-2">
                                <Button
                                    variant="outline"
                                    className="flex-1"
                                    onClick={() => {
                                        clearSetupProgress(sessionStorage);
                                        setSetupPhase('editing');
                                        setStep(4);
                                    }}
                                >
                                    Edit setup
                                </Button>
                                <Button
                                    className="flex-1"
                                    onClick={() => {
                                        const progress = loadSetupProgress(sessionStorage);
                                        if (progress) {
                                            setSaving(true);
                                            void pollForRestart(progress).finally(() => setSaving(false));
                                        } else {
                                            setStep(4);
                                        }
                                    }}
                                >
                                    Retry reconnect
                                </Button>
                            </div>
                        )}
                    </div>
                );

            default:
                return null;
        }
    };

    return (
        <div className="min-h-screen bg-background flex flex-col items-center justify-center p-4 md:p-8">
            <div className="fixed inset-0 bg-[radial-gradient(circle_at_50%_50%,rgba(50,205,50,0.05),transparent_70%)] pointer-events-none" />

            <Card className="w-full max-w-lg border-2 border-primary/10 shadow-2xl relative overflow-hidden">
                <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-transparent via-primary/50 to-transparent" />

                <CardHeader className="pb-4">
                    <div className="flex items-center justify-between mb-2">
                        <span className="text-[10px] font-mono text-muted-foreground tracking-widest uppercase">LimeBot Setup v1.0</span>
                        {step < 5 && (
                            <div className="flex gap-1">
                                {[1, 2, 3, 4].map(i => (
                                    <div key={i} className={`h-1 w-4 rounded-full transition-colors duration-500 ${i <= step ? 'bg-primary' : 'bg-muted'}`} />
                                ))}
                            </div>
                        )}
                    </div>
                </CardHeader>

                <CardContent className="pt-2 min-h-[400px] flex flex-col justify-center">
                    {renderStep()}
                </CardContent>

                <CardFooter className="flex justify-center pb-8 pt-0">
                    <p className="text-[10px] text-muted-foreground opacity-50">
                        &copy; 2026 LimeBot - AI Assistant
                    </p>
                </CardFooter>
            </Card>
        </div>
    );
}
