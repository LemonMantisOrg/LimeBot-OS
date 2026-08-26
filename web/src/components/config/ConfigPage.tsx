import { useState, useEffect } from 'react';
import axios from 'axios';
import { API_BASE_URL } from "@/lib/api";
import { Save, Settings, Key, Cpu, RefreshCw, Globe, Server, User, Trash, Plus, BrainCircuit, PlugZap, ShieldCheck, CheckCircle2, Circle } from 'lucide-react';
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
} from "@/components/ui/card";
import { DEFAULT_MODEL_BY_PROVIDER, getAdditionalModels, getInitialModelForProvider, getModelProvider, getRecommendedModels, PROVIDER_LABELS, type LlmModelOption } from "@/lib/llm-models";
import { type ConfigApiResponse, type ConfigSecretsMap, getSecretInfo, getSecretPlaceholder } from "@/lib/config-secrets";

type ConfigValue = string | string[] | undefined;

interface ConfigState {
    LLM_MODEL?: string;
    ALLOWED_PATHS?: string[];
    AUTONOMOUS_MODE?: string;
    APPROVAL_POLICY_PROFILE?: 'manual' | 'session' | 'autonomous' | 'review';
    LIMEBOT_ENABLE_TOOL_SHORTLIST?: string;
    MAX_ITERATIONS?: string;
    WEB_PORT?: string;
    LLM_PROXY_URL?: string;
    BROWSER_MODE?: string;
    BROWSER_CHANNEL?: string;
    BROWSER_CDP_URL?: string;
    BROWSER_USER_DATA_DIR?: string;
    BROWSER_PROFILE_DIRECTORY?: string;
    VIDEO_WHISPER_ENABLED?: string;
    [key: string]: ConfigValue;
}

type SecretDrafts = Record<string, string>;

const SECRET_KEYS = [
    "APP_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "NVIDIA_API_KEY",
    "DASHSCOPE_API_KEY",
    "ELEVENLABS_API_KEY",
] as const;

const DEFAULT_CUSTOM_MODEL = "ollama/llama3";

type SecretKey = typeof SECRET_KEYS[number];

const AI_PROVIDER_SECRETS: Array<{ key: SecretKey; label: string; placeholder: string; note: string }> = [
    { key: "OPENAI_API_KEY", label: "OpenAI", placeholder: "sk-...", note: "GPT models, image generation, and optional Whisper" },
    { key: "GEMINI_API_KEY", label: "Google Gemini", placeholder: "AIza...", note: "Gemini chat and embeddings" },
    { key: "ANTHROPIC_API_KEY", label: "Anthropic", placeholder: "sk-ant-...", note: "Claude models" },
    { key: "OPENROUTER_API_KEY", label: "OpenRouter", placeholder: "sk-or-...", note: "Multi-provider model routing" },
    { key: "XAI_API_KEY", label: "xAI", placeholder: "xai-...", note: "Grok models" },
    { key: "DEEPSEEK_API_KEY", label: "DeepSeek", placeholder: "sk-...", note: "DeepSeek models" },
    { key: "MOONSHOT_API_KEY", label: "Moonshot / Kimi", placeholder: "sk-...", note: "Kimi and Moonshot models" },
    { key: "NVIDIA_API_KEY", label: "NVIDIA NIM", placeholder: "nvapi-...", note: "NVIDIA-hosted models" },
    { key: "DASHSCOPE_API_KEY", label: "Qwen / DashScope", placeholder: "sk-...", note: "Alibaba Qwen models" },
];

const CAPABILITY_SECRETS: SecretKey[] = [
    "ELEVENLABS_API_KEY",
];

const PROVIDER_SECRET_KEYS: Record<string, SecretKey> = {
    openai: "OPENAI_API_KEY",
    gemini: "GEMINI_API_KEY",
    anthropic: "ANTHROPIC_API_KEY",
    openrouter: "OPENROUTER_API_KEY",
    xai: "XAI_API_KEY",
    deepseek: "DEEPSEEK_API_KEY",
    moonshot: "MOONSHOT_API_KEY",
    nvidia: "NVIDIA_API_KEY",
    qwen: "DASHSCOPE_API_KEY",
};


export function ConfigPage() {
    const [config, setConfig] = useState<ConfigState>({});
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [status, setStatus] = useState<{ type: 'success' | 'error', message: string } | null>(null);
    const [secretMeta, setSecretMeta] = useState<ConfigSecretsMap>({});
    const [secretDrafts, setSecretDrafts] = useState<SecretDrafts>({});
    const [clearedSecrets, setClearedSecrets] = useState<string[]>([]);

    const [availableModels, setAvailableModels] = useState<LlmModelOption[]>([]);
    const [modelsLoading, setModelsLoading] = useState(false);
    const [showAllModels, setShowAllModels] = useState(false);
    const [showAllProviderKeys, setShowAllProviderKeys] = useState(false);

    useEffect(() => {
        fetchConfig();
        fetchModels();
    }, []);

    const fetchModels = async () => {
        try {
            setModelsLoading(true);
            const res = await axios.get(`${API_BASE_URL}/api/llm/models`);
            if (res.data.models) setAvailableModels(res.data.models);
        } catch (err) {
            console.error("Failed to load models:", err);
        } finally {
            setModelsLoading(false);
        }
    };

    const getFilteredModels = () => {
        if (!config) return [];
        return availableModels.filter(model => {
            if (model.provider === 'gemini') return !!secretDrafts.GEMINI_API_KEY || getSecretInfo(secretMeta, 'GEMINI_API_KEY').configured;
            if (model.provider === 'openai') return !!secretDrafts.OPENAI_API_KEY || getSecretInfo(secretMeta, 'OPENAI_API_KEY').configured;
            if (model.provider === 'openrouter') return !!secretDrafts.OPENROUTER_API_KEY || getSecretInfo(secretMeta, 'OPENROUTER_API_KEY').configured;
            if (model.provider === 'anthropic') return !!secretDrafts.ANTHROPIC_API_KEY || getSecretInfo(secretMeta, 'ANTHROPIC_API_KEY').configured;
            if (model.provider === 'xai') return !!secretDrafts.XAI_API_KEY || getSecretInfo(secretMeta, 'XAI_API_KEY').configured;
            if (model.provider === 'deepseek') return !!secretDrafts.DEEPSEEK_API_KEY || getSecretInfo(secretMeta, 'DEEPSEEK_API_KEY').configured;
            if (model.provider === 'moonshot') return !!secretDrafts.MOONSHOT_API_KEY || getSecretInfo(secretMeta, 'MOONSHOT_API_KEY').configured;
            if (model.provider === 'nvidia') return !!secretDrafts.NVIDIA_API_KEY || getSecretInfo(secretMeta, 'NVIDIA_API_KEY').configured;
            if (model.provider === 'qwen') return !!secretDrafts.DASHSCOPE_API_KEY || getSecretInfo(secretMeta, 'DASHSCOPE_API_KEY').configured;
            return true;
        });
    };

    const fetchConfig = async () => {
        setLoading(true);
        // ... existing fetchConfig logic ...
        try {
            const res = await axios.get<ConfigApiResponse<ConfigState>>(`${API_BASE_URL}/api/config`);
            const nextEnv = res.data.env || {};
            setConfig(nextEnv);
            setSecretMeta(res.data.secrets || {});
            setSecretDrafts({});
            setClearedSecrets([]);
        } catch (err) {
            console.error("Failed to load settings:", err);
            setStatus({ type: 'error', message: "Failed to load configuration." });
        } finally {
            setLoading(false);
        }
    };


    // ... existing handleSave ...

    const handleSave = async () => {
        setSaving(true);
        setStatus(null);

        const normalizedModel = String(config.LLM_MODEL || "").trim() || DEFAULT_CUSTOM_MODEL;

        try {
            const secretUpdates = Object.fromEntries(
                Object.entries(secretDrafts).filter(([, value]) => value.trim() !== "")
            );
            const res = await axios.post(`${API_BASE_URL}/api/config`, {
                env: {
                    ...config,
                    LLM_MODEL: normalizedModel,
                    ...secretUpdates,
                },
                clear_secrets: clearedSecrets,
            });
            if (res.data.error) throw new Error(res.data.error);
            if (secretDrafts.APP_API_KEY?.trim()) {
                localStorage.setItem("limebot_api_key", secretDrafts.APP_API_KEY.trim());
                axios.defaults.headers.common["X-API-Key"] = secretDrafts.APP_API_KEY.trim();
            }
            setSecretMeta((prev) => {
                const next = { ...prev };
                for (const key of SECRET_KEYS) {
                    if (clearedSecrets.includes(key)) {
                        next[key] = { configured: false, masked: "", last4: "" };
                    } else if (secretDrafts[key]?.trim()) {
                        const val = secretDrafts[key].trim();
                        next[key] = {
                            configured: true,
                            masked: `••••${val.slice(-4)}`,
                            last4: val.slice(-4),
                        };
                    }
                }
                return next;
            });
            setSecretDrafts({});
            setClearedSecrets([]);
            setStatus({ type: 'success', message: "Configuration saved!" });
        } catch (err) {
            console.error("Failed to save config:", err);
            setStatus({ type: 'error', message: "Failed to save configuration." });
        } finally {
            setSaving(false);
        }
    };

    const handleChange = (key: string, value: ConfigValue) => {
        setConfig(prev => ({ ...prev, [key]: value }));
    };

    const handleSecretChange = (key: string, value: string) => {
        setSecretDrafts(prev => ({ ...prev, [key]: value }));
        setClearedSecrets(prev => prev.filter((item) => item !== key));
    };

    const markSecretForClear = (key: string) => {
        setSecretDrafts(prev => ({ ...prev, [key]: "" }));
        setClearedSecrets(prev => prev.includes(key) ? prev : [...prev, key]);
    };

    const isSecretConfigured = (key: SecretKey) => (
        !clearedSecrets.includes(key)
        && (!!secretDrafts[key]?.trim() || getSecretInfo(secretMeta, key).configured)
    );

    const renderSecretInput = (key: SecretKey, label: string, placeholder: string, note?: string) => {
        const info = getSecretInfo(secretMeta, key);
        const isCleared = clearedSecrets.includes(key);
        const configured = isSecretConfigured(key);
        return (
            <div className="grid gap-3 rounded-xl border border-border/60 bg-background/40 p-4">
                <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0">
                        <Label htmlFor={key.toLowerCase()} className="font-semibold">{label}</Label>
                        {note && <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{note}</p>}
                    </div>
                    <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-1 text-[10px] font-medium ${configured ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}>
                        {configured ? <CheckCircle2 className="h-3 w-3" /> : <Circle className="h-3 w-3" />}
                        {isCleared ? 'Clear on save' : configured ? 'Connected' : 'Optional'}
                    </span>
                </div>
                <div className="flex gap-2">
                    <Input
                        id={key.toLowerCase()}
                        type="password"
                        value={secretDrafts[key] || ""}
                        onChange={(e) => handleSecretChange(key, e.target.value)}
                        placeholder={getSecretPlaceholder(secretMeta, key, placeholder)}
                    />
                    <Button
                        type="button"
                        variant="outline"
                        size="icon"
                        onClick={() => markSecretForClear(key)}
                        title={`Clear ${label}`}
                        disabled={!configured && !secretDrafts[key]}
                    >
                        <Trash className="h-4 w-4" />
                    </Button>
                </div>
                {info.configured && !isCleared && (
                    <p className="text-[10px] text-muted-foreground">Stored securely as {info.masked}</p>
                )}
            </div>
        );
    };

    const renderAiProviderKeys = () => {
        const connected = AI_PROVIDER_SECRETS.filter(({ key }) => isSecretConfigured(key)).length;
        const selectedKey = PROVIDER_SECRET_KEYS[selectedProvider];
        const visibleProviders = showAllProviderKeys
            ? AI_PROVIDER_SECRETS
            : AI_PROVIDER_SECRETS.filter(({ key }) => key === selectedKey || isSecretConfigured(key));
        const hiddenCount = AI_PROVIDER_SECRETS.length - visibleProviders.length;
        return (
            <Card>
                <CardHeader className="gap-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                            <CardTitle className="flex items-center gap-2">
                                <Key className="h-5 w-5" />
                                AI Provider Connections
                            </CardTitle>
                            <CardDescription className="mt-1.5">
                                Add only the providers you use. Keys stay redacted after saving.
                            </CardDescription>
                        </div>
                        <span className="rounded-full border border-border/60 bg-muted/40 px-3 py-1 text-xs text-muted-foreground">
                            {connected} of {AI_PROVIDER_SECRETS.length} connected
                        </span>
                    </div>
                </CardHeader>
                <CardContent className="space-y-4">
                    {visibleProviders.length > 0 ? (
                        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                            {visibleProviders.map(({ key, label, placeholder, note }) => (
                                <div key={key}>{renderSecretInput(key, label, placeholder, note)}</div>
                            ))}
                        </div>
                    ) : (
                        <div className="rounded-xl border border-dashed border-border/70 p-5 text-sm text-muted-foreground">
                            The selected local/custom provider does not need a cloud API key.
                        </div>
                    )}
                    {(hiddenCount > 0 || showAllProviderKeys) && (
                        <Button type="button" variant="ghost" className="w-full border border-dashed" onClick={() => setShowAllProviderKeys((value) => !value)}>
                            {showAllProviderKeys ? 'Hide unused providers' : `Show ${hiddenCount} other providers`}
                        </Button>
                    )}
                </CardContent>
            </Card>
        );
    };

    const renderAppAccessKey = () => {
        const configured = isSecretConfigured("APP_API_KEY");
        return (
            <Card className="border-amber-500/25">
                <CardHeader>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                            <CardTitle className="flex items-center gap-2">
                                <ShieldCheck className="h-5 w-5 text-amber-500" />
                                Dashboard Access Key
                            </CardTitle>
                            <CardDescription className="mt-1.5">
                                Protects the LimeBot dashboard and API. This is not an AI provider credential.
                            </CardDescription>
                        </div>
                        <span className={`rounded-full px-3 py-1 text-xs font-medium ${configured ? 'bg-primary/10 text-primary' : 'bg-amber-500/10 text-amber-500'}`}>
                            {configured ? 'Protected' : 'Not configured'}
                        </span>
                    </div>
                </CardHeader>
                <CardContent>
                    <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4">
                        <div className="flex gap-2">
                            <Input
                                id="app_api_key"
                                type="password"
                                value={secretDrafts.APP_API_KEY || ""}
                                onChange={(e) => handleSecretChange("APP_API_KEY", e.target.value)}
                                placeholder={getSecretPlaceholder(secretMeta, 'APP_API_KEY', 'Generate or enter an access key')}
                                className="bg-background/70"
                            />
                            <Button type="button" variant="outline" size="icon" onClick={() => handleSecretChange("APP_API_KEY", crypto.randomUUID())} title="Generate new key">
                                <RefreshCw className="h-4 w-4" />
                            </Button>
                            <Button type="button" variant="outline" size="icon" onClick={() => markSecretForClear("APP_API_KEY")} title="Clear access key" disabled={!configured}>
                                <Trash className="h-4 w-4" />
                            </Button>
                        </div>
                        <p className="mt-3 text-xs text-muted-foreground">
                            Changing this key signs clients out. Save it somewhere secure before restarting LimeBot.
                        </p>
                    </div>
                </CardContent>
            </Card>
        );
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center h-full">
                <RefreshCw className="w-8 h-8 animate-spin text-primary" />
            </div>
        );
    }

    const filteredModels = getFilteredModels();
    const knownModel = filteredModels.find((model) => model.id === config.LLM_MODEL);
    const selectedProvider = knownModel?.provider || getModelProvider(config.LLM_MODEL);
    const providerOptions = Object.keys(PROVIDER_LABELS).filter((provider) => provider !== "custom");
    const recommendedModels = selectedProvider === 'custom'
        ? []
        : getRecommendedModels(filteredModels, selectedProvider);
    const additionalModels = selectedProvider === 'custom'
        ? []
        : getAdditionalModels(filteredModels, selectedProvider);
    const selectedHiddenModel = additionalModels.find((model) => model.id === config.LLM_MODEL);
    const recommendedDisplayModels = selectedHiddenModel
        ? [selectedHiddenModel, ...recommendedModels]
        : recommendedModels;
    const additionalDisplayModels = selectedHiddenModel
        ? additionalModels.filter((model) => model.id !== selectedHiddenModel.id)
        : additionalModels;
    const connectedAiProviders = AI_PROVIDER_SECRETS.filter(({ key }) => isSecretConfigured(key)).length;
    const connectedCapabilities = CAPABILITY_SECRETS.filter((key) => isSecretConfigured(key)).length;
    const dashboardProtected = isSecretConfigured("APP_API_KEY");

    return (
        <div className="h-full overflow-y-auto p-6 md:p-8 bg-background/50">
            <div className="max-w-5xl mx-auto space-y-7">
                <header className="sticky top-0 z-20 -mx-2 flex items-center justify-between gap-4 rounded-2xl border border-border/50 bg-background/90 px-4 py-3 shadow-sm backdrop-blur-xl">
                    <div>
                        <h1 className="text-2xl font-bold flex items-center gap-2">
                            <Settings className="h-6 w-6 text-primary" />
                            Settings
                        </h1>
                        <p className="text-muted-foreground mt-1">
                            Connect providers, enable capabilities, and define LimeBot's safety boundaries.
                        </p>
                    </div>
                    <Button onClick={handleSave} disabled={saving} className="bg-primary hover:bg-primary/90 text-primary-foreground font-bold">
                        {saving ? (
                            <>Saving...</>
                        ) : (
                            <>
                                <Save className="mr-2 h-4 w-4" />
                                <span className="hidden sm:inline">Save Changes</span>
                                <span className="sm:hidden">Save</span>
                            </>
                        )}
                    </Button>
                </header>

                {status && (
                    <Alert className={status.type === 'success' ? "border-primary/50 bg-primary/10 text-primary" : "border-destructive/50 bg-destructive/10 text-destructive"}>
                        {status.type === 'success' ? <RefreshCw className="h-4 w-4" /> : <Settings className="h-4 w-4" />}
                        <AlertTitle>{status.type === 'success' ? "Saved" : "Error"}</AlertTitle>
                        <AlertDescription>{status.message}</AlertDescription>
                    </Alert>
                )}

                <div className="grid gap-3 md:grid-cols-3">
                    <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
                        <div className="flex items-center justify-between">
                            <BrainCircuit className="h-5 w-5 text-primary" />
                            <span className="text-xs text-muted-foreground">{connectedAiProviders} connected</span>
                        </div>
                        <p className="mt-3 font-semibold">AI & Models</p>
                        <p className="mt-1 text-xs text-muted-foreground">Choose the brain and connect only its provider.</p>
                    </div>
                    <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
                        <div className="flex items-center justify-between">
                            <PlugZap className="h-5 w-5 text-primary" />
                            <span className="text-xs text-muted-foreground">{connectedCapabilities} services</span>
                        </div>
                        <p className="mt-3 font-semibold">Capabilities</p>
                        <p className="mt-1 text-xs text-muted-foreground">Search, video transcription, voice, and browser behavior.</p>
                    </div>
                    <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
                        <div className="flex items-center justify-between">
                            <ShieldCheck className="h-5 w-5 text-amber-500" />
                            <span className={`text-xs ${dashboardProtected ? 'text-primary' : 'text-amber-500'}`}>
                                {dashboardProtected ? 'Protected' : 'Needs attention'}
                            </span>
                        </div>
                        <p className="mt-3 font-semibold">Security & Runtime</p>
                        <p className="mt-1 text-xs text-muted-foreground">Approvals, sandbox paths, access, and advanced execution.</p>
                    </div>
                </div>

                <Tabs defaultValue="ai" className="w-full">
                    <TabsList className="grid h-auto w-full grid-cols-3 rounded-xl bg-muted/60 p-1">
                        <TabsTrigger value="ai" className="gap-2 py-2.5"><BrainCircuit className="h-4 w-4" /><span className="hidden sm:inline">AI & Models</span><span className="sm:hidden">AI</span></TabsTrigger>
                        <TabsTrigger value="capabilities" className="gap-2 py-2.5"><PlugZap className="h-4 w-4" /><span className="hidden sm:inline">Capabilities</span><span className="sm:hidden">Tools</span></TabsTrigger>
                        <TabsTrigger value="security" className="gap-2 py-2.5"><ShieldCheck className="h-4 w-4" /><span className="hidden sm:inline">Security & Runtime</span><span className="sm:hidden">Safety</span></TabsTrigger>
                    </TabsList>

                    {/* AI & MODEL SETTINGS */}
                    <TabsContent value="ai" className="space-y-4 mt-6">
                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Cpu className="h-5 w-5" />
                                    Model Settings
                                </CardTitle>
                                <CardDescription>Select and configure the AI model.</CardDescription>
                            </CardHeader>
                            <CardContent className="space-y-4">
                                <div className="grid gap-2">
                                    <Label htmlFor="provider">Provider</Label>
                                    <Select
                                        value={selectedProvider}
                                        onValueChange={(value) => {
                                            setShowAllModels(false);
                                            if (value === "custom") {
                                                handleChange("LLM_MODEL", config.LLM_MODEL?.trim() || DEFAULT_CUSTOM_MODEL);
                                                return;
                                            }
                                            handleChange(
                                                "LLM_MODEL",
                                                getInitialModelForProvider(filteredModels, value) || DEFAULT_MODEL_BY_PROVIDER[value],
                                            );
                                        }}
                                    >
                                        <SelectTrigger id="provider">
                                            <SelectValue placeholder="Select Provider" />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {providerOptions.map((provider) => (
                                                <SelectItem key={provider} value={provider}>
                                                    {PROVIDER_LABELS[provider] || provider}
                                                </SelectItem>
                                            ))}
                                            <SelectSeparator />
                                            <SelectItem value="custom">{PROVIDER_LABELS.custom}</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-[10px] text-muted-foreground">
                                        Start with a short recommended list per provider. Expand only when you need niche or experimental models.
                                    </p>
                                </div>

                                <div className="grid gap-2">
                                    <Label htmlFor="model">LLM Model</Label>
                                    <div className="flex gap-2">
                                        <Select
                                            disabled={modelsLoading}
                                            value={
                                                modelsLoading
                                                    ? "loading"
                                                    : selectedProvider === "custom"
                                                        ? "custom"
                                                        : filteredModels.find(m => m.id === config.LLM_MODEL)
                                                        ? config.LLM_MODEL
                                                        : "custom"
                                            }
                                            onValueChange={(val) => {
                                                if (val === "loading") return;
                                                if (val === "custom") {
                                                    const isCurrentlyKnown = filteredModels.find(m => m.id === config.LLM_MODEL);
                                                    if (isCurrentlyKnown) {
                                                        handleChange("LLM_MODEL", "");
                                                    }
                                                } else {
                                                    handleChange("LLM_MODEL", val);
                                                }
                                            }}
                                        >
                                            <SelectTrigger id="model" className="flex-1">
                                                <SelectValue placeholder={modelsLoading ? "Scanning Models..." : "Select Model"} />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {modelsLoading ? (
                                                    <SelectItem value="loading" disabled className="text-muted-foreground flex items-center gap-2">
                                                        <RefreshCw className="h-3 w-3 animate-spin" /> Scanning Models...
                                                    </SelectItem>
                                                ) : (
                                                    <>
                                                        <SelectItem value="custom" className="font-semibold text-primary">
                                                            Custom / Local Model
                                                        </SelectItem>
                                                        {recommendedDisplayModels.length > 0 && (
                                                            <>
                                                                <SelectSeparator />
                                                                <SelectGroup>
                                                                    <SelectLabel>Recommended</SelectLabel>
                                                                    {recommendedDisplayModels.map((model) => (
                                                                        <SelectItem key={model.id} value={model.id}>
                                                                            {model.name}
                                                                        </SelectItem>
                                                                    ))}
                                                                </SelectGroup>
                                                            </>
                                                        )}
                                                        {showAllModels && additionalDisplayModels.length > 0 && (
                                                            <>
                                                                <SelectSeparator />
                                                                <SelectGroup>
                                                                    <SelectLabel>All {PROVIDER_LABELS[selectedProvider] || selectedProvider} Models</SelectLabel>
                                                                    {additionalDisplayModels.map((model) => (
                                                                        <SelectItem key={model.id} value={model.id}>
                                                                            {model.name}
                                                                        </SelectItem>
                                                                    ))}
                                                                </SelectGroup>
                                                            </>
                                                        )}
                                                    </>
                                                )}
                                            </SelectContent>
                                        </Select>
                                    </div>

                                    {selectedProvider !== 'custom' && additionalDisplayModels.length > 0 && (
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

                                    {(selectedProvider === "custom" || !filteredModels.find(m => m.id === config.LLM_MODEL) || config.LLM_MODEL?.startsWith("ollama/")) && (
                                        <div className="pt-2 animate-in fade-in slide-in-from-top-2">
                                            <Label htmlFor="custom_model" className="text-xs text-muted-foreground">Custom Model Name (e.g. ollama/llama3)</Label>
                                            <Input
                                                id="custom_model"
                                                value={config.LLM_MODEL || ""}
                                                onChange={(e) => handleChange("LLM_MODEL", e.target.value)}
                                                placeholder="provider/model-name"
                                                className="mt-1"
                                            />
                                        </div>
                                    )}
                                </div>

                                <div className="grid gap-2">
                                    <Label htmlFor="base_url">LLM Base URL (Optional)</Label>
                                    <Input
                                        id="base_url"
                                        value={config.LLM_BASE_URL || ""}
                                        onChange={(e) => handleChange("LLM_BASE_URL", e.target.value)}
                                        placeholder="e.g. http://localhost:11434"
                                        className="font-mono text-sm"
                                    />
                                    <p className="text-[10px] text-muted-foreground">
                                        Required for local models (Ollama, LocalAI). Defaults to empty for cloud providers.
                                    </p>
                                </div>

                                <div className="grid gap-2">
                                    <Label htmlFor="llm_proxy_url">LLM Proxy / Gateway URL (Middleware)</Label>
                                    <Input
                                        id="llm_proxy_url"
                                        placeholder="http://localhost:8080/v1 (e.g., Open Guardian, AI Gateway)"
                                        value={config.LLM_PROXY_URL || ""}
                                        onChange={(e) => handleChange("LLM_PROXY_URL", e.target.value)}
                                        className="font-mono text-sm"
                                    />
                                    <p className="text-[10px] text-muted-foreground">
                                        Routes all LLM traffic through external security or caching middleware. Overrides default provider endpoints.
                                    </p>
                                </div>
                            </CardContent>
                        </Card>

                        {renderAiProviderKeys()}

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Settings className="h-5 w-5" />
                                    Model Tool Awareness
                                </CardTitle>
                                <CardDescription>
                                    Tune how much tool schema is sent to the model on each turn.
                                </CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="flex items-center justify-between p-4 bg-card/50 rounded-lg border border-border/50">
                                    <div className="space-y-0.5">
                                        <Label htmlFor="tool_shortlist" className="font-semibold">Enable Tool Shortlist</Label>
                                        <p className="text-xs text-muted-foreground max-w-xl">
                                            Shows the model a smaller, request-specific tool set instead of the full registry. This can reduce prompt size and make tool selection less noisy.
                                        </p>
                                    </div>
                                    <Switch
                                        id="tool_shortlist"
                                        checked={config.LIMEBOT_ENABLE_TOOL_SHORTLIST === 'true'}
                                        onCheckedChange={(checked) => handleChange('LIMEBOT_ENABLE_TOOL_SHORTLIST', checked ? 'true' : 'false')}
                                    />
                                </div>
                            </CardContent>
                        </Card>

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <User className="h-5 w-5" />
                                    Personalization
                                </CardTitle>
                                <CardDescription>Configure how the bot interacts with users.</CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="flex items-center justify-between rounded-lg border border-primary/20 bg-primary/10 p-4">
                                    <div className="space-y-0.5">
                                        <div className="flex items-center gap-2">
                                            <Label htmlFor="dynamic_personality">Adaptive Persona</Label>
                                            <span className="rounded bg-primary px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-primary-foreground">Experimental</span>
                                        </div>
                                        <p className="text-xs text-muted-foreground max-w-md">
                                            Allows the bot to learn from your conversations, adjusting its tone and relationship level dynamically based on interactions.
                                        </p>
                                    </div>
                                    <Switch
                                        id="dynamic_personality"
                                        checked={config.ENABLE_DYNAMIC_PERSONALITY === 'true'}
                                        onCheckedChange={(checked) => handleChange('ENABLE_DYNAMIC_PERSONALITY', checked ? 'true' : 'false')}
                                    />
                                </div>
                            </CardContent>
                        </Card>
                    </TabsContent>


                    {/* OPTIONAL CAPABILITIES */}
                    <TabsContent value="capabilities" className="space-y-4 mt-6">
                        <Alert className="border-primary/20 bg-primary/5">
                            <PlugZap className="h-4 w-4 text-primary" />
                            <AlertTitle>Connect only what LimeBot should use</AlertTitle>
                            <AlertDescription>
                                Every service below is optional. Channel tokens remain under Channels & Presence so connection settings stay with their channel.
                            </AlertDescription>
                        </Alert>

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Globe className="h-5 w-5" />
                                    Web Intelligence
                                </CardTitle>
                                <CardDescription>
                                    Search, news, images, and deep research run through the real Playwright browser — the same stack as the browser skill.
                                </CardDescription>
                            </CardHeader>
                            <CardContent className="space-y-3">
                                <p className="text-sm text-muted-foreground">
                                    Install the browser once from the LimeBot folder. After that, web_search and image_search work without a separate search API key.
                                </p>
                                <p className="text-[11px] font-mono text-muted-foreground break-all">
                                    npm run lime-bot setup -- --recommended
                                </p>
                                <p className="text-[11px] text-muted-foreground">
                                    Or: <span className="font-mono">npm run lime-bot feature install browser && npm run install-browser</span>
                                </p>
                            </CardContent>
                        </Card>

                        <Card>
                            <CardHeader>
                                <div className="flex flex-wrap items-center justify-between gap-3">
                                    <CardTitle>Video Understanding</CardTitle>
                                    <span className={`rounded-full px-2.5 py-1 text-[10px] font-medium ${isSecretConfigured("OPENAI_API_KEY") ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}>
                                        {isSecretConfigured("OPENAI_API_KEY") ? 'OpenAI connected' : 'Uses captions without a key'}
                                    </span>
                                </div>
                                <CardDescription>
                                    Optional transcription fallback for videos without captions.
                                </CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="flex items-center justify-between gap-6 rounded-lg border border-border/60 bg-background/30 p-4">
                                    <div className="space-y-1">
                                        <Label htmlFor="video_whisper_enabled">Use OpenAI Whisper</Label>
                                        <p className="text-xs text-muted-foreground">
                                            When enabled, caption-less video audio is uploaded to OpenAI for transcription. Uses the existing OpenAI API key and is disabled by default.
                                        </p>
                                    </div>
                                    <Switch
                                        id="video_whisper_enabled"
                                        checked={config.VIDEO_WHISPER_ENABLED === 'true'}
                                        onCheckedChange={(checked) => handleChange('VIDEO_WHISPER_ENABLED', checked ? 'true' : 'false')}
                                    />
                                </div>
                            </CardContent>
                        </Card>

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Key className="h-5 w-5" />
                                    Voice & Audio
                                </CardTitle>
                                <CardDescription>
                                    Text-to-speech key. Manage voices and playback in the Voice tab.
                                </CardDescription>
                            </CardHeader>
                            <CardContent className="space-y-6">
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    {renderSecretInput("ELEVENLABS_API_KEY", "ElevenLabs", "sk_...", "Text-to-speech voices and playable audio replies")}
                                </div>
                            </CardContent>
                        </Card>
                    </TabsContent>

                    {/* SECURITY & RUNTIME */}
                    <TabsContent value="security" className="space-y-4 mt-6">
                        {renderAppAccessKey()}

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Cpu className="h-5 w-5 text-yellow-500" />
                                    Safety & Execution
                                </CardTitle>
                                <CardDescription className="text-yellow-500/80">
                                    Choose how sensitive tools are reviewed before LimeBot runs them.
                                </CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="grid gap-3 p-4 bg-yellow-500/10 rounded-lg border border-yellow-500/20">
                                    <Label htmlFor="approval_policy" className="text-yellow-500 font-bold">Sensitive tool policy</Label>
                                    <Select
                                        value={config.APPROVAL_POLICY_PROFILE || (config.AUTONOMOUS_MODE === 'true' ? 'autonomous' : 'manual')}
                                        onValueChange={(value) => handleChange('APPROVAL_POLICY_PROFILE', value)}
                                    >
                                        <SelectTrigger id="approval_policy">
                                            <SelectValue placeholder="Select an approval policy" />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="manual">Manual - confirm sensitive actions</SelectItem>
                                            <SelectItem value="session">Session - remember explicit approvals</SelectItem>
                                            <SelectItem value="review">Review - confirm every sensitive action</SelectItem>
                                            <SelectItem value="autonomous">Autonomous - bypass confirmations</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">
                                        Review ignores session approvals. Autonomous still obeys hard path and command safety checks.
                                    </p>
                                </div>
                                <div className="mt-4 grid gap-3 p-4 bg-card/50 rounded-lg border border-border/50">
                                    <div className="space-y-1">
                                        <Label htmlFor="max_iterations" className="font-semibold flex items-center gap-2">
                                            Max Tool Interactions
                                        </Label>
                                        <p className="text-xs text-muted-foreground">
                                            The maximum number of tools the bot can run autonomously in a single response before forcefully stopping. Prevents infinite loops.
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-4">
                                        <Input
                                            id="max_iterations"
                                            type="number"
                                            min="1"
                                            max="200"
                                            value={config.MAX_ITERATIONS || "30"}
                                            onChange={(e) => handleChange("MAX_ITERATIONS", e.target.value)}
                                            className="w-24 font-mono text-center"
                                        />
                                    </div>
                                </div>
                                <div className="mt-4 grid gap-3 p-4 bg-card/50 rounded-lg border border-border/50">
                                    <div className="space-y-1">
                                        <Label htmlFor="command_timeout" className="font-semibold flex items-center gap-2">
                                            Command Timeout (Seconds)
                                        </Label>
                                        <p className="text-xs text-muted-foreground">
                                            The configured duration for a command. Set to 0 to use the hard safety cap below; commands never wait forever.
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-4">
                                        <Input
                                            id="command_timeout"
                                            type="number"
                                            min="0"
                                            value={config.COMMAND_TIMEOUT !== undefined ? config.COMMAND_TIMEOUT : "300.0"}
                                            onChange={(e) => handleChange("COMMAND_TIMEOUT", e.target.value)}
                                            className="w-24 font-mono text-center"
                                        />
                                    </div>
                                </div>
                                <div className="mt-4 grid gap-3 p-4 bg-card/50 rounded-lg border border-border/50">
                                    <div className="space-y-1">
                                        <Label htmlFor="run_command_max_seconds" className="font-semibold flex items-center gap-2">
                                            One-Shot Safety Cap (Seconds)
                                        </Label>
                                        <p className="text-xs text-muted-foreground">
                                            Absolute maximum for run_command, including when Command Timeout is 0. This prevents a server or prompt from leaving the chat stuck.
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-4">
                                        <Input
                                            id="run_command_max_seconds"
                                            type="number"
                                            min="10"
                                            max="3600"
                                            value={config.RUN_COMMAND_MAX_SECONDS !== undefined ? config.RUN_COMMAND_MAX_SECONDS : "180"}
                                            onChange={(e) => handleChange("RUN_COMMAND_MAX_SECONDS", e.target.value)}
                                            className="w-24 font-mono text-center"
                                        />
                                    </div>
                                </div>
                                <div className="mt-4 grid gap-3 p-4 bg-card/50 rounded-lg border border-border/50">
                                    <div className="space-y-1">
                                        <Label htmlFor="stall_timeout" className="font-semibold flex items-center gap-2">
                                            Stall Detection Timeout (Seconds)
                                        </Label>
                                        <p className="text-xs text-muted-foreground">
                                            If a command produces no output for this many seconds, it's assumed to be waiting for interactive input and gets killed. Set to 0 to disable stall detection.
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-4">
                                        <Input
                                            id="stall_timeout"
                                            type="number"
                                            min="0"
                                            value={config.STALL_TIMEOUT !== undefined ? config.STALL_TIMEOUT : "30"}
                                            onChange={(e) => handleChange("STALL_TIMEOUT", e.target.value)}
                                            className="w-24 font-mono text-center"
                                        />
                                    </div>
                                </div>
                                <div className="mt-4 grid gap-3 p-4 bg-card/50 rounded-lg border border-border/50">
                                    <div className="space-y-1">
                                        <Label htmlFor="web_port" className="font-semibold flex items-center gap-2">
                                            Web Server Port
                                        </Label>
                                        <p className="text-xs text-muted-foreground">
                                            The port used by the web server and API. Changing this will require a restart.
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-4">
                                        <Input
                                            id="web_port"
                                            type="number"
                                            min="1"
                                            max="65535"
                                            value={config.WEB_PORT || "8000"}
                                            onChange={(e) => handleChange("WEB_PORT", e.target.value)}
                                            className="w-24 font-mono text-center"
                                        />
                                    </div>
                                </div>
                                <div className="mt-4 grid gap-3 p-4 bg-card/50 rounded-lg border border-yellow-500/30">
                                    <div className="flex items-center justify-between">
                                        <div className="space-y-1">
                                            <Label htmlFor="allow_unsafe_commands" className="font-semibold flex items-center gap-2 text-yellow-500">
                                                Allow Unsafe Commands
                                            </Label>
                                            <p className="text-xs text-muted-foreground">
                                                Allows shell operators such as redirects ({'>'}), semicolons, and pipes, and also permits privileged commands like <code className="font-mono">sudo</code>, <code className="font-mono">chmod</code>, and <code className="font-mono">chown</code>. Useful for advanced workflows, but it reduces sandboxing.
                                            </p>
                                        </div>
                                        <Switch
                                            id="allow_unsafe_commands"
                                            checked={config.ALLOW_UNSAFE_COMMANDS === 'true'}
                                            onCheckedChange={(checked) => handleChange('ALLOW_UNSAFE_COMMANDS', checked ? 'true' : 'false')}
                                        />
                                    </div>
                                </div>
                            </CardContent>
                        </Card>

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Globe className="h-5 w-5" />
                                    Browser Runtime
                                </CardTitle>
                                <CardDescription>
                                    Control whether the browser tool uses isolated LimeBot profiles, a shared LimeBot profile, your system browser profile, or a live browser session over CDP.
                                </CardDescription>
                            </CardHeader>
                            <CardContent className="space-y-4">
                                <div className="grid gap-2">
                                    <Label htmlFor="browser_mode">Browser Mode</Label>
                                    <Select
                                        value={config.BROWSER_MODE || "isolated"}
                                        onValueChange={(value) => handleChange("BROWSER_MODE", value)}
                                    >
                                        <SelectTrigger id="browser_mode">
                                            <SelectValue placeholder="Select browser mode" />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="isolated">isolated</SelectItem>
                                            <SelectItem value="shared">shared</SelectItem>
                                            <SelectItem value="system">system</SelectItem>
                                            <SelectItem value="attach">attach</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-[10px] text-muted-foreground">
                                        `isolated` keeps one browser profile per chat. `shared` reuses one LimeBot profile. `system` launches against your real Chrome/Edge profile. `attach` connects to an already-running browser with remote debugging enabled.
                                    </p>
                                </div>

                                <div className="grid gap-2">
                                    <Label htmlFor="browser_channel">Browser Channel</Label>
                                    <Input
                                        id="browser_channel"
                                        value={config.BROWSER_CHANNEL || ""}
                                        onChange={(e) => handleChange("BROWSER_CHANNEL", e.target.value)}
                                        placeholder="chrome, msedge, or chromium"
                                        className="font-mono text-sm"
                                    />
                                    <p className="text-[10px] text-muted-foreground">
                                        Optional. Recommended for `system` mode so LimeBot opens the same browser family as your real profile.
                                    </p>
                                </div>

                                <div className="grid gap-2">
                                    <Label htmlFor="browser_cdp_url">CDP URL</Label>
                                    <Input
                                        id="browser_cdp_url"
                                        value={config.BROWSER_CDP_URL || ""}
                                        onChange={(e) => handleChange("BROWSER_CDP_URL", e.target.value)}
                                        placeholder="http://127.0.0.1:9222"
                                        className="font-mono text-sm"
                                    />
                                    <p className="text-[10px] text-muted-foreground">
                                        Used by `attach` mode. Start Chrome or Edge with `--remote-debugging-port=9222` to reuse the live logged-in session.
                                    </p>
                                </div>

                                <div className="grid gap-2">
                                    <Label htmlFor="browser_user_data_dir">Browser User Data Dir</Label>
                                    <Input
                                        id="browser_user_data_dir"
                                        value={config.BROWSER_USER_DATA_DIR || ""}
                                        onChange={(e) => handleChange("BROWSER_USER_DATA_DIR", e.target.value)}
                                        placeholder="C:\\Users\\you\\AppData\\Local\\Google\\Chrome\\User Data"
                                        className="font-mono text-sm"
                                    />
                                    <p className="text-[10px] text-muted-foreground">
                                        Optional override for `system` mode. Leave empty to auto-detect a local Chrome/Edge/Chromium profile.
                                    </p>
                                </div>

                                <div className="grid gap-2">
                                    <Label htmlFor="browser_profile_directory">Profile Directory</Label>
                                    <Input
                                        id="browser_profile_directory"
                                        value={config.BROWSER_PROFILE_DIRECTORY || ""}
                                        onChange={(e) => handleChange("BROWSER_PROFILE_DIRECTORY", e.target.value)}
                                        placeholder="Default or Profile 1"
                                        className="font-mono text-sm"
                                    />
                                    <p className="text-[10px] text-muted-foreground">
                                        Optional sub-profile inside the browser user data directory. Useful when your login lives in a non-default Chrome/Edge profile.
                                    </p>
                                </div>
                            </CardContent>
                        </Card>

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Settings className="h-5 w-5" />
                                    Workspace Access
                                </CardTitle>
                                <CardDescription>Control file access and permissions.</CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="grid gap-2">
                                    <Label htmlFor="allowed_paths">Allowed File Paths</Label>
                                    <div className="space-y-2 max-h-[300px] overflow-y-auto pr-2 custom-scrollbar">
                                        {(() => {
                                            let pathsArr: string[] = [];
                                            if (Array.isArray(config.ALLOWED_PATHS)) {
                                                pathsArr = config.ALLOWED_PATHS;
                                            } else if (typeof config.ALLOWED_PATHS === "string") {
                                                pathsArr = (config.ALLOWED_PATHS as string).split(",").filter(Boolean);
                                            }

                                            return pathsArr.map((path, idx) => (
                                                <div key={idx} className="flex gap-2 isolate">
                                                    <Input
                                                        value={path}
                                                        onChange={(e) => {
                                                            const paths = [...pathsArr];
                                                            paths[idx] = e.target.value;
                                                            handleChange("ALLOWED_PATHS", paths);
                                                        }}
                                                        placeholder="Absolute path..."
                                                    />
                                                    <Button
                                                        variant="ghost"
                                                        size="icon"
                                                        className="text-destructive hover:bg-destructive/10 shrink-0"
                                                        onClick={() => {
                                                            const paths = [...pathsArr];
                                                            paths.splice(idx, 1);
                                                            handleChange("ALLOWED_PATHS", paths);
                                                        }}
                                                    >
                                                        <Trash className="h-4 w-4" />
                                                    </Button>
                                                </div>
                                            ));
                                        })()}
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            className="w-full flex items-center justify-center gap-2 border-dashed"
                                            onClick={() => {
                                                let pathsArr: string[] = [];
                                                if (Array.isArray(config.ALLOWED_PATHS)) {
                                                    pathsArr = [...config.ALLOWED_PATHS];
                                                } else if (typeof config.ALLOWED_PATHS === "string") {
                                                    pathsArr = (config.ALLOWED_PATHS as string).split(",").filter(Boolean);
                                                }
                                                pathsArr.push("");
                                                handleChange("ALLOWED_PATHS", pathsArr);
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
                            </CardContent>
                        </Card>

                        <Card>
                            <CardHeader>
                                <CardTitle className="flex items-center gap-2">
                                    <Globe className="h-5 w-5" />
                                    Remote Access
                                </CardTitle>
                                <CardDescription>Network details for connecting to LimeBot.</CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="bg-muted/30 p-4 rounded-lg border border-border/50">
                                    <div className="flex items-start gap-3">
                                        <Server className="h-5 w-5 text-primary mt-1" />
                                        <div className="space-y-2 flex-1">
                                            <h3 className="text-sm font-medium">Network Interfaces</h3>
                                            <div className="space-y-1">
                                                <RemoteStatus port={config.WEB_PORT || "8000"} />
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </CardContent>
                        </Card>
                    </TabsContent>
                </Tabs>
            </div>
        </div>
    );
}

type NetworkInterface = {
    ip: string;
    is_tailscale: boolean;
};

type RemoteNetworkStatus = {
    error?: string;
    interfaces?: NetworkInterface[];
};

function RemoteStatus({ port }: { port: string }) {
    const [status, setStatus] = useState<RemoteNetworkStatus | null>(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        axios.get(`${API_BASE_URL}/api/setup/tailscale`)
            .then(res => setStatus(res.data))
            .catch(err => console.error("Failed to check network:", err))
            .finally(() => setLoading(false));
    }, []);

    if (loading) return <div className="text-xs text-muted-foreground animate-pulse">Scanning network...</div>;

    if (!status || status.error) {
        return <div className="text-xs text-destructive">Network check failed.</div>;
    }

    const tailscale = status.interfaces?.find((item) => item.is_tailscale);
    const lan = status.interfaces?.find((item) => !item.is_tailscale && item.ip !== '127.0.0.1');

    return (
        <div className="space-y-3">
            {tailscale ? (
                <div className="rounded border border-primary/20 bg-primary/10 p-2 text-sm">
                    <div className="mb-1 flex items-center gap-2 font-medium text-primary">
                        <span className="relative flex h-2 w-2">
                            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-75"></span>
                            <span className="relative inline-flex h-2 w-2 rounded-full bg-primary"></span>
                        </span>
                        Tailscale Active
                    </div>
                    <div className="font-mono text-xs select-all cursor-text bg-background/50 p-1 rounded px-2">
                        http://{tailscale.ip}:{port}
                    </div>
                </div>
            ) : (
                <div className="p-2 bg-orange-500/10 border border-orange-500/20 rounded text-sm">
                    <div className="text-orange-500 font-medium text-xs mb-1">Tailscale Not Detected</div>
                    <p className="text-[10px] text-muted-foreground">
                        Install Tailscale to access this bot securely from anywhere.
                    </p>
                </div>
            )}

            {lan && (
                <div className="space-y-1">
                    <div className="text-xs text-muted-foreground">LAN Access:</div>
                    <div className="font-mono text-xs select-all cursor-text bg-background/50 p-1 rounded px-2 inline-block">
                        http://{lan.ip}:{port}
                    </div>
                </div>
            )}
        </div>
    );
}
