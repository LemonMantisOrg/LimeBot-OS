import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { usePetPreferences } from '@/hooks/usePetPreferences';
import { type PetState } from '@/lib/pet';
import { cn } from '@/lib/utils';
import { CheckCircle2, Eye, PackageCheck, PawPrint, Sparkles, Wand2 } from 'lucide-react';
import { PetSprite } from '@/components/pet/PetSprite';

interface PetsPageProps {
    onNavigate?: (view: string) => void;
}

const PREVIEW_STATES: Array<{ state: PetState; label: string; description: string }> = [
    { state: 'idle', label: 'Idle', description: 'Ready' },
    { state: 'thinking', label: 'Thinking', description: 'Processing' },
    { state: 'working', label: 'Working', description: 'Using tools' },
    { state: 'approval', label: 'Approval', description: 'Needs you' },
    { state: 'warning', label: 'Warning', description: 'Attention' },
    { state: 'celebrating', label: 'Celebrating', description: 'Completed' },
];

export function PetsPage({ onNavigate }: PetsPageProps) {
    const {
        enabled,
        catalog,
        preferAnimatedPet,
        selectedPet,
        setPetEnabled,
        setPetId,
        setPreferAnimatedPet,
    } = usePetPreferences();

    return (
        <div className="h-full overflow-y-auto bg-background/50 p-6 md:p-8">
            <div className="mx-auto max-w-7xl space-y-6">
                <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
                    <div>
                        <div className="flex flex-wrap items-center gap-2">
                            <PawPrint className="h-7 w-7 text-primary" />
                            <h1 className="text-2xl font-bold">Pets</h1>
                            <Badge variant={enabled ? 'secondary' : 'outline'}>
                                {enabled ? 'Animated pet on' : 'Pet hidden'}
                            </Badge>
                        </div>
                        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                            Choose the animated companion used throughout the LimeBot dashboard. Preferences are saved in this browser.
                        </p>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2 md:min-w-[560px]">
                        <div className="flex items-center gap-3 rounded-xl border border-border/70 bg-card/70 px-4 py-3">
                            <div className="min-w-0">
                                <div className="text-sm font-semibold">Show animated pet</div>
                                <div className="text-xs text-muted-foreground">
                                    {enabled ? 'The pet reacts to LimeBot activity.' : 'Use the static LimeBot avatar instead.'}
                                </div>
                            </div>
                            <Switch
                                checked={enabled}
                                onCheckedChange={setPetEnabled}
                                aria-label="Show animated pet"
                            />
                        </div>
                        <div className="flex items-center gap-3 rounded-xl border border-border/70 bg-card/70 px-4 py-3">
                            <div className="min-w-0">
                                <div className="text-sm font-semibold">Prefer animated pet</div>
                                <div className="text-xs text-muted-foreground">
                                    Use it even when Persona has an avatar.
                                </div>
                            </div>
                            <Switch
                                checked={preferAnimatedPet}
                                onCheckedChange={setPreferAnimatedPet}
                                aria-label="Prefer animated pet over Persona avatar"
                            />
                        </div>
                    </div>
                </header>

                <Card className="overflow-hidden border-primary/20 bg-gradient-to-br from-card via-card to-primary/5">
                    <CardContent className="grid gap-6 p-5 md:grid-cols-[minmax(0,1fr)_auto] md:items-center md:p-7">
                        <div className="flex min-w-0 items-start gap-5">
                            <div className="flex h-44 w-36 shrink-0 items-end justify-center overflow-hidden rounded-2xl border border-primary/20 bg-background/60 shadow-inner">
                                <PetSprite
                                    manifest={selectedPet}
                                    state="celebrating"
                                    size={132}
                                    label={`${selectedPet.displayName} preview`}
                                />
                            </div>
                            <div className="min-w-0 pt-1">
                                <div className="flex flex-wrap items-center gap-2">
                                    <h2 className="text-2xl font-bold">{selectedPet.displayName}</h2>
                                    <Badge variant="outline">v2 atlas</Badge>
                                    {enabled && <Badge variant="secondary" className="gap-1"><CheckCircle2 className="h-3 w-3" />Active</Badge>}
                                    {preferAnimatedPet && <Badge variant="outline">Avatar override</Badge>}
                                </div>
                                <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">
                                    {selectedPet.description}
                                </p>
                                <div className="mt-4 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                                    <span className="rounded-full border border-border/70 bg-background/60 px-2.5 py-1">8 × 11 frames</span>
                                    <span className="rounded-full border border-border/70 bg-background/60 px-2.5 py-1">16 look directions</span>
                                    <span className="rounded-full border border-border/70 bg-background/60 px-2.5 py-1">
                                        {selectedPet.stateFrameDurations ? 'Codex timing' : `${selectedPet.fps} fps`}
                                    </span>
                                </div>
                            </div>
                        </div>
                        <div className="rounded-xl border border-border/70 bg-background/50 p-4 text-sm">
                            <div className="flex items-center gap-2 font-semibold">
                                <Eye className="h-4 w-4 text-primary" />
                                Where it appears
                            </div>
                            <ul className="mt-3 space-y-2 text-xs text-muted-foreground">
                                <li>• Chat messages and typing indicator</li>
                                <li>• Tool timeline and approval states</li>
                                <li>• Sidebar and mobile header</li>
                            </ul>
                        </div>
                    </CardContent>
                </Card>

                <section className="space-y-3">
                    <div className="flex items-center gap-2">
                        <Sparkles className="h-5 w-5 text-primary" />
                        <h2 className="text-lg font-semibold">Pet catalog</h2>
                        <Badge variant="outline">{catalog.length}</Badge>
                    </div>
                    <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
                        {catalog.map((pet) => {
                            const isSelected = selectedPet.id === pet.id;
                            return (
                                <Card
                                    key={pet.id}
                                    className={cn(
                                        'overflow-hidden transition-colors',
                                        isSelected ? 'border-primary/50 shadow-md shadow-primary/10' : 'border-border hover:border-primary/30',
                                    )}
                                >
                                    <CardHeader className="pb-3">
                                        <div className="flex items-start justify-between gap-3">
                                            <div>
                                                <CardTitle className="text-base">{pet.displayName}</CardTitle>
                                                <CardDescription className="mt-1">{pet.description}</CardDescription>
                                            </div>
                                            {isSelected && <Badge variant="secondary">Selected</Badge>}
                                        </div>
                                    </CardHeader>
                                    <CardContent>
                                        <div className="grid grid-cols-3 gap-2 rounded-xl border border-border/70 bg-background/50 p-3 sm:grid-cols-6 md:grid-cols-3 xl:grid-cols-6">
                                            {PREVIEW_STATES.map(({ state, label, description }) => (
                                                <div key={state} className="flex min-w-0 flex-col items-center gap-1.5 text-center">
                                                    <div className="flex h-20 w-14 items-end justify-center overflow-hidden rounded-lg bg-muted/40">
                                                        <PetSprite manifest={pet} state={state} size={56} label={`${pet.displayName} ${label}`} />
                                                    </div>
                                                    <span className="truncate text-[10px] font-semibold">{label}</span>
                                                    <span className="truncate text-[9px] text-muted-foreground">{description}</span>
                                                </div>
                                            ))}
                                        </div>
                                        <Button
                                            className="mt-4 w-full gap-2"
                                            variant={isSelected ? 'secondary' : 'outline'}
                                            disabled={isSelected}
                                            onClick={() => setPetId(pet.id)}
                                        >
                                            {isSelected ? <CheckCircle2 className="h-4 w-4" /> : <PawPrint className="h-4 w-4" />}
                                            {isSelected ? 'Using this pet' : 'Use this pet'}
                                        </Button>
                                    </CardContent>
                                </Card>
                            );
                        })}
                    </div>
                </section>

                <Card className="border-border/70 bg-card/70">
                    <CardHeader className="pb-3">
                        <CardTitle className="flex items-center gap-2 text-lg">
                            <PackageCheck className="h-5 w-5 text-primary" />
                            Hatch a new pet
                        </CardTitle>
                        <CardDescription>
                            Use the enabled <code className="rounded bg-muted px-1 py-0.5">hatch-pet</code> skill to create, validate, and package another v2 sprite atlas.
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                        <div className="grid gap-2 text-sm text-muted-foreground md:grid-cols-3 md:gap-4">
                            <div className="flex items-start gap-2"><Wand2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" /><span>Generate or repair the sprite atlas.</span></div>
                            <div className="flex items-start gap-2"><PackageCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" /><span>Run v2 validation and QA.</span></div>
                            <div className="flex items-start gap-2"><PawPrint className="mt-0.5 h-4 w-4 shrink-0 text-primary" /><span>Add the approved package to this catalog.</span></div>
                        </div>
                        {onNavigate && (
                            <Button variant="outline" className="shrink-0 gap-2" onClick={() => onNavigate('chat')}>
                                Open chat with hatch-pet
                                <Wand2 className="h-4 w-4" />
                            </Button>
                        )}
                    </CardContent>
                </Card>
            </div>
        </div>
    );
}
