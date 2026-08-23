import { useEffect, useState } from 'react';
import axios from 'axios';
import { API_BASE_URL } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Volume2, Play, Pause, Download, Loader2, Sparkles, AlertTriangle, Check, RotateCcw } from "lucide-react";
import { toast } from "sonner";

interface VoiceSettings {
    enabled: boolean;
    voice_id: string;
    stability: number;
    similarity_boost: number;
    style: number;
    use_speaker_boost: boolean;
    speed: number;
    model_id: string;
    output_format: string;
    channels: string[];
    send_text_with_audio: boolean;
}

interface ElevenLabsVoice {
    voice_id: string;
    name: string;
    category: string;
    description: string;
    preview_url: string;
}

export function VoicePage() {
    const [hasKey, setHasKey] = useState(false);
    const [settings, setSettings] = useState<VoiceSettings>({
        enabled: false,
        voice_id: '',
        stability: 0.5,
        similarity_boost: 0.75,
        style: 0.0,
        use_speaker_boost: true,
        speed: 1.0,
        model_id: 'eleven_multilingual_v2',
        output_format: 'mp3_44100_128',
        channels: ['web'],
        send_text_with_audio: false
    });
    const [originalSettings, setOriginalSettings] = useState<VoiceSettings | null>(null);
    const [voices, setVoices] = useState<ElevenLabsVoice[]>([]);
    const [loadingSettings, setLoadingSettings] = useState(true);
    const [loadingVoices, setLoadingVoices] = useState(false);
    const [saving, setSaving] = useState(false);

    // Preview state
    const [previewText, setPreviewText] = useState("Hi, I'm LimeBot, your AI assistant! How is your day going?");
    const [previewUrl, setPreviewUrl] = useState<string | null>(null);
    const [generatingPreview, setGeneratingPreview] = useState(false);

    useEffect(() => {
        fetchSettings();
    }, []);

    const fetchSettings = async () => {
        setLoadingSettings(true);
        try {
            const res = await axios.get(`${API_BASE_URL}/api/voice/settings`);
            setHasKey(res.data.has_key);
            
            // Backfill defaults for new properties if missing from database
            const loaded = res.data.settings || {};
            const finalSettings: VoiceSettings = {
                enabled: loaded.enabled ?? false,
                voice_id: loaded.voice_id ?? '',
                stability: loaded.stability ?? 0.5,
                similarity_boost: loaded.similarity_boost ?? 0.75,
                style: loaded.style ?? 0.0,
                use_speaker_boost: loaded.use_speaker_boost ?? true,
                speed: loaded.speed ?? 1.0,
                model_id: loaded.model_id ?? 'eleven_multilingual_v2',
                output_format: loaded.output_format ?? 'mp3_44100_128',
                channels: loaded.channels ?? ['web'],
                send_text_with_audio: loaded.send_text_with_audio ?? false
            };
            
            setSettings(finalSettings);
            setOriginalSettings(finalSettings);
            
            if (res.data.has_key) {
                fetchVoices();
            }
        } catch (err) {
            console.error("Failed to fetch ElevenLabs settings:", err);
            toast.error("Failed to load settings.");
        } finally {
            setLoadingSettings(false);
        }
    };

    const fetchVoices = async () => {
        setLoadingVoices(true);
        try {
            const res = await axios.get(`${API_BASE_URL}/api/voice/voices`);
            const loadedVoices = res.data.voices || [];
            setVoices(loadedVoices);
            
            // Set initial voice if none selected
            setSettings(prev => {
                if (!prev.voice_id && loadedVoices.length > 0) {
                    return { ...prev, voice_id: loadedVoices[0].voice_id };
                }
                return prev;
            });
        } catch (err) {
            console.error("Failed to fetch voices list:", err);
        } finally {
            setLoadingVoices(false);
        }
    };

    const handleSave = async () => {
        setSaving(true);
        try {
            await axios.post(`${API_BASE_URL}/api/voice/settings`, settings);
            setOriginalSettings(settings);
            toast.success("Voice settings saved successfully.");
        } catch (err) {
            console.error("Failed to save settings:", err);
            toast.error("Failed to save settings.");
        } finally {
            setSaving(false);
        }
    };

    const handleGeneratePreview = async () => {
        if (!previewText.trim()) return;
        setGeneratingPreview(true);
        setPreviewUrl(null);
        try {
            const res = await axios.post(`${API_BASE_URL}/api/voice/synthesize`, {
                text: previewText,
                voice_id: settings.voice_id,
                stability: settings.stability,
                similarity_boost: settings.similarity_boost,
                style: settings.style,
                use_speaker_boost: settings.use_speaker_boost,
                speed: settings.speed,
                model_id: settings.model_id,
                output_format: settings.output_format
            });
            if (res.data.status === "success" && res.data.url) {
                setPreviewUrl(res.data.url);
                toast.success("Preview generated successfully!");
            }
        } catch (err) {
            console.error("Failed to generate voice preview:", err);
            toast.error("Generation failed. Check ElevenLabs key and quota.");
        } finally {
            setGeneratingPreview(false);
        }
    };

    const toggleChannel = (name: string, on: boolean) => {
        setSettings(prev => ({
            ...prev,
            channels: on
                ? Array.from(new Set([...(prev.channels ?? []), name]))
                : (prev.channels ?? []).filter(c => c !== name),
        }));
    };

    const handleResetValues = () => {
        setSettings(prev => ({
            ...prev,
            stability: 0.5,
            similarity_boost: 0.75,
            style: 0.0,
            use_speaker_boost: true,
            speed: 1.0,
            model_id: 'eleven_multilingual_v2',
            output_format: 'mp3_44100_128'
        }));
        toast.info("Voice settings reset to defaults.");
    };

    const isDirty = JSON.stringify(settings) !== JSON.stringify(originalSettings);
    const activeVoice = voices.find(v => v.voice_id === settings.voice_id);

    if (loadingSettings) {
        return (
            <div className="flex h-[50vh] items-center justify-center">
                <Loader2 className="h-8 w-8 animate-spin text-primary" />
            </div>
        );
    }

    return (
        <div className="h-full overflow-y-auto p-4 md:p-6 bg-background/50 font-sans">
            <div className="mx-auto max-w-4xl space-y-6">
                {/* Warning if API Key is missing */}
                {!hasKey && (
                    <Card className="border-amber-500/20 bg-amber-500/5 animate-pulse">
                        <CardHeader className="flex flex-row items-center gap-3 space-y-0 pb-3">
                            <AlertTriangle className="h-6 w-6 text-amber-500 shrink-0" />
                            <div>
                                <CardTitle className="text-amber-500 text-base font-bold">ElevenLabs Key Missing</CardTitle>
                                <CardDescription className="text-xs">
                                    Add your ElevenLabs API key under <strong>Settings → Credentials</strong> to enable text-to-speech, then reload this page.
                                </CardDescription>
                            </div>
                        </CardHeader>
                    </Card>
                )}

                <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                    {/* Voice Control panel */}
                    <Card className="md:col-span-2 border-border/60 shadow-lg bg-card/50">
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2 text-lg font-bold">
                                <Volume2 className="h-5 w-5 text-primary" />
                                Voice Configuration
                            </CardTitle>
                            <CardDescription>
                                Configure ElevenLabs Text-to-Speech settings for LimeBot.
                            </CardDescription>
                        </CardHeader>

                        <CardContent className="space-y-6">
                            {/* Enabled Switch */}
                            <div className="flex items-center justify-between p-4 rounded-xl border border-border bg-background/50">
                                <div className="space-y-0.5">
                                    <Label className="font-semibold text-sm">Enable Voice Responses</Label>
                                    <p className="text-xs text-muted-foreground">
                                        LimeBot will automatically synthesize and play voice audio on every chat response.
                                    </p>
                                </div>
                                <Switch
                                    checked={settings.enabled}
                                    onCheckedChange={(val) => setSettings(prev => ({ ...prev, enabled: val }))}
                                    disabled={!hasKey}
                                />
                            </div>

                            {/* Channel delivery */}
                            <div className="space-y-3 rounded-xl border border-border bg-background/50 p-4">
                                <div className="space-y-0.5">
                                    <Label className="font-semibold text-sm">Voice on channels</Label>
                                    <p className="text-xs text-muted-foreground">
                                        Where LimeBot auto-sends voice. Web plays inline; Discord & WhatsApp receive the audio file itself.
                                    </p>
                                </div>
                                {(['web', 'discord', 'whatsapp'] as const).map((ch) => (
                                    <div key={ch} className="flex items-center justify-between">
                                        <Label className="text-xs capitalize">{ch}</Label>
                                        <Switch
                                            checked={(settings.channels ?? []).includes(ch)}
                                            onCheckedChange={(val) => toggleChannel(ch, val)}
                                            disabled={!hasKey}
                                        />
                                    </div>
                                ))}
                                <div className="flex items-center justify-between border-t border-border pt-3">
                                    <div className="space-y-0.5">
                                        <Label className="text-xs">Also send the text</Label>
                                        <p className="text-[11px] text-muted-foreground">
                                            On Discord/WhatsApp, send the written reply alongside the audio.
                                        </p>
                                    </div>
                                    <Switch
                                        checked={settings.send_text_with_audio}
                                        onCheckedChange={(val) => setSettings(prev => ({ ...prev, send_text_with_audio: val }))}
                                        disabled={!hasKey}
                                    />
                                </div>
                            </div>

                            {/* Voice dropdown */}
                            <div className="space-y-2">
                                <Label className="text-sm font-semibold">Select Voice</Label>
                                {loadingVoices ? (
                                    <div className="flex items-center gap-2 text-xs text-muted-foreground font-mono">
                                        <Loader2 className="h-3 w-3 animate-spin" /> Fetching your ElevenLabs voices...
                                    </div>
                                ) : (
                                    <Select
                                        value={settings.voice_id}
                                        onValueChange={(val) => setSettings(prev => ({ ...prev, voice_id: val }))}
                                        disabled={!hasKey}
                                    >
                                        <SelectTrigger className="w-full bg-background border-border/80">
                                            <SelectValue placeholder="Choose a voice" />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {voices.map((voice) => (
                                                <SelectItem key={voice.voice_id} value={voice.voice_id}>
                                                    {voice.name} <span className="text-[10px] text-muted-foreground opacity-80 uppercase ml-1">({voice.category})</span>
                                                </SelectItem>
                                            ))}
                                        </SelectContent>
                                    </Select>
                                )}
                                {activeVoice && (
                                    <p className="text-xs text-muted-foreground leading-relaxed mt-1">
                                        {activeVoice.description || "Custom voice cloned or generated from ElevenLabs."}
                                    </p>
                                )}
                            </div>

                            {/* Model select */}
                            <div className="space-y-2">
                                <Label className="text-sm font-semibold">Model</Label>
                                <Select
                                    value={settings.model_id}
                                    onValueChange={(val) => setSettings(prev => ({ ...prev, model_id: val }))}
                                    disabled={!hasKey}
                                >
                                    <SelectTrigger className="w-full bg-background border-border/80">
                                        <SelectValue placeholder="Select TTS model" />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="eleven_multilingual_v2">Eleven Multilingual v2</SelectItem>
                                        <SelectItem value="eleven_monolingual_v1">Eleven Monolingual v1</SelectItem>
                                        <SelectItem value="eleven_turbo_v2_5">Eleven Turbo v2.5</SelectItem>
                                        <SelectItem value="eleven_flash_v2_5">Eleven Flash v2.5</SelectItem>
                                    </SelectContent>
                                </Select>
                            </div>

                            {/* Slider controls */}
                            <div className="space-y-5 border-t border-border pt-4">
                                <h3 className="text-xs font-bold uppercase tracking-wider text-muted-foreground">Voice Settings</h3>

                                {/* Speed */}
                                <div className="space-y-2">
                                    <div className="flex justify-between text-sm items-center">
                                        <Label className="font-semibold text-xs">Speed</Label>
                                        <span className="bg-primary/10 text-primary px-2 py-0.5 rounded font-mono text-xs font-bold">{settings.speed}</span>
                                    </div>
                                    <input
                                        type="range"
                                        min="0.7"
                                        max="1.2"
                                        step="0.05"
                                        value={settings.speed}
                                        onChange={(e) => setSettings(prev => ({ ...prev, speed: parseFloat(e.target.value) }))}
                                        className="h-1.5 w-full bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                                        disabled={!hasKey}
                                    />
                                    <div className="flex justify-between text-[10px] text-muted-foreground">
                                        <span>Slower</span>
                                        <span>Faster</span>
                                    </div>
                                </div>

                                {/* Stability */}
                                <div className="space-y-2">
                                    <div className="flex justify-between text-sm items-center">
                                        <Label className="font-semibold text-xs">Stability</Label>
                                        <span className="bg-primary/10 text-primary px-2 py-0.5 rounded font-mono text-xs font-bold">{settings.stability}</span>
                                    </div>
                                    <input
                                        type="range"
                                        min="0"
                                        max="1"
                                        step="0.05"
                                        value={settings.stability}
                                        onChange={(e) => setSettings(prev => ({ ...prev, stability: parseFloat(e.target.value) }))}
                                        className="h-1.5 w-full bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                                        disabled={!hasKey}
                                    />
                                    <div className="flex justify-between text-[10px] text-muted-foreground">
                                        <span>More variable</span>
                                        <span>More stable</span>
                                    </div>
                                </div>

                                {/* Similarity boost */}
                                <div className="space-y-2">
                                    <div className="flex justify-between text-sm items-center">
                                        <Label className="font-semibold text-xs">Similarity</Label>
                                        <span className="bg-primary/10 text-primary px-2 py-0.5 rounded font-mono text-xs font-bold">{settings.similarity_boost}</span>
                                    </div>
                                    <input
                                        type="range"
                                        min="0"
                                        max="1"
                                        step="0.05"
                                        value={settings.similarity_boost}
                                        onChange={(e) => setSettings(prev => ({ ...prev, similarity_boost: parseFloat(e.target.value) }))}
                                        className="h-1.5 w-full bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                                        disabled={!hasKey}
                                    />
                                    <div className="flex justify-between text-[10px] text-muted-foreground">
                                        <span>Low</span>
                                        <span>High</span>
                                    </div>
                                </div>

                                {/* Style */}
                                <div className="space-y-2">
                                    <div className="flex justify-between text-sm items-center">
                                        <Label className="font-semibold text-xs">Style Exaggeration</Label>
                                        <span className="bg-primary/10 text-primary px-2 py-0.5 rounded font-mono text-xs font-bold">{settings.style}</span>
                                    </div>
                                    <input
                                        type="range"
                                        min="0"
                                        max="1"
                                        step="0.05"
                                        value={settings.style}
                                        onChange={(e) => setSettings(prev => ({ ...prev, style: parseFloat(e.target.value) }))}
                                        className="h-1.5 w-full bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                                        disabled={!hasKey}
                                    />
                                    <div className="flex justify-between text-[10px] text-muted-foreground">
                                        <span>None</span>
                                        <span>Exaggerated</span>
                                    </div>
                                </div>

                                {/* Output Format */}
                                <div className="space-y-2 border-t border-border pt-4">
                                    <Label className="text-xs font-semibold text-muted-foreground">Output Format</Label>
                                    <Select
                                        value={settings.output_format}
                                        onValueChange={(val) => setSettings(prev => ({ ...prev, output_format: val }))}
                                        disabled={!hasKey}
                                    >
                                        <SelectTrigger className="w-full bg-background border-border/80">
                                            <SelectValue placeholder="Choose output quality" />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="mp3_44100_128">MP3 44.1 kHz (128kbps)</SelectItem>
                                            <SelectItem value="mp3_44100_192">MP3 44.1 kHz (192kbps) - Creator Tier</SelectItem>
                                            <SelectItem value="pcm_44100">PCM 44.1 kHz - Pro Tier</SelectItem>
                                            <SelectItem value="ulaw_8000">u-law 8kHz - Twilio</SelectItem>
                                        </SelectContent>
                                    </Select>
                                </div>

                                {/* Speaker Boost & Reset Block */}
                                <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 p-3 rounded-lg border border-border bg-background/30">
                                    <div className="flex items-center justify-between flex-1">
                                        <div className="space-y-0.5">
                                            <Label className="font-semibold text-xs">Speaker boost</Label>
                                            <p className="text-[11px] text-muted-foreground">
                                                Recommended for custom/cloned voices.
                                            </p>
                                        </div>
                                        <Switch
                                            checked={settings.use_speaker_boost}
                                            onCheckedChange={(val) => setSettings(prev => ({ ...prev, use_speaker_boost: val }))}
                                            disabled={!hasKey}
                                            className="ml-4 shrink-0"
                                        />
                                    </div>

                                    <div className="flex justify-end border-t sm:border-t-0 border-border pt-2 sm:pt-0 shrink-0">
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            onClick={handleResetValues}
                                            disabled={!hasKey}
                                            className="h-8 text-xs text-muted-foreground hover:text-foreground gap-1.5 px-2"
                                        >
                                            <RotateCcw className="h-3.5 w-3.5" />
                                            Reset values
                                        </Button>
                                    </div>
                                </div>
                            </div>
                        </CardContent>

                        <CardFooter className="flex justify-end gap-2 border-t border-border pt-4">
                            <Button
                                variant="outline"
                                onClick={fetchSettings}
                                disabled={!isDirty || saving}
                                className="h-9 text-xs"
                            >
                                Reset Changes
                            </Button>
                            <Button
                                onClick={handleSave}
                                disabled={!isDirty || saving || !hasKey}
                                className="h-9 text-xs gap-1.5 font-bold"
                            >
                                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                                Save Configuration
                            </Button>
                        </CardFooter>
                    </Card>

                    {/* Voice Preview Studio */}
                    <Card className="border-border/60 shadow-lg bg-card/50 flex flex-col">
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2 text-base font-bold">
                                <Sparkles className="h-4.5 w-4.5 text-primary animate-pulse" />
                                Preview Studio
                            </CardTitle>
                            <CardDescription className="text-xs">
                                Test your selected voice and settings instantly.
                            </CardDescription>
                        </CardHeader>

                        <CardContent className="space-y-4 flex-1 flex flex-col justify-between">
                            <div className="space-y-2">
                                <Label className="text-xs font-semibold text-muted-foreground">Test Script</Label>
                                <Textarea
                                    value={previewText}
                                    onChange={(e) => setPreviewText(e.target.value)}
                                    placeholder="Type something for the bot to speak..."
                                    rows={4}
                                    className="bg-background border-border/85 text-xs resize-none"
                                    disabled={!hasKey}
                                />
                            </div>

                            <div className="mt-4">
                                <Button
                                    className="w-full h-9 text-xs font-bold gap-2"
                                    onClick={handleGeneratePreview}
                                    disabled={generatingPreview || !previewText.trim() || !hasKey}
                                >
                                    {generatingPreview ? (
                                        <>
                                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                            Synthesizing speech...
                                        </>
                                    ) : (
                                        <>
                                            <Volume2 className="h-4 w-4" />
                                            Generate Voice Preview
                                        </>
                                    )}
                                </Button>
                            </div>
                        </CardContent>

                        {previewUrl && (
                            <CardFooter className="border-t border-border pt-4 bg-primary/5 flex flex-col items-center">
                                <span className="text-[10px] font-bold text-primary uppercase tracking-wide mb-2">Generation Result</span>
                                <PreviewPlayer url={previewUrl} />
                            </CardFooter>
                        )}
                    </Card>
                </div>
            </div>
        </div>
    );
}

function PreviewPlayer({ url }: { url: string }) {
    const [isPlaying, setIsPlaying] = useState(false);
    const audioRef = useState<HTMLAudioElement | null>(() => new Audio(`${API_BASE_URL}${url}`))[0];

    useEffect(() => {
        if (!audioRef) return;
        const onEnded = () => setIsPlaying(false);
        audioRef.addEventListener('ended', onEnded);
        return () => {
            audioRef.pause();
            audioRef.removeEventListener('ended', onEnded);
        };
    }, [audioRef]);

    const togglePlay = () => {
        if (!audioRef) return;
        if (isPlaying) {
            audioRef.pause();
            setIsPlaying(false);
        } else {
            audioRef.play().catch(err => console.error("Playback failed", err));
            setIsPlaying(true);
        }
    };

    return (
        <div className="flex items-center gap-3 w-full bg-background border border-primary/20 p-2.5 rounded-lg">
            <Button
                variant="outline"
                size="icon"
                onClick={togglePlay}
                className="h-8 w-8 rounded-full bg-primary/5 text-primary border-primary/20 hover:bg-primary/10 shrink-0"
            >
                {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
            </Button>
            <div className="flex-1 min-w-0">
                <span className="text-[11px] font-bold block text-foreground truncate font-sans">preview_audio.mp3</span>
                <span className="text-[10px] text-muted-foreground block font-sans">Ready to play</span>
            </div>
            <a
                href={`${API_BASE_URL}${url}`}
                download="preview_audio.mp3"
                target="_blank"
                rel="noopener noreferrer"
                className="h-8 w-8 flex items-center justify-center rounded-md border border-border bg-card text-muted-foreground hover:text-foreground shrink-0"
            >
                <Download className="h-4 w-4" />
            </a>
        </div>
    );
}
