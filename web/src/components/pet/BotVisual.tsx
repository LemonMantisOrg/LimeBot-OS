import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { PetSprite } from './PetSprite';
import { JENNIE_PET, type PetManifest, type PetState } from '@/lib/pet';

type BotVisualProps = {
    avatar?: string | null;
    petState?: PetState;
    petEnabled?: boolean;
    preferAnimatedPet?: boolean;
    manifest?: PetManifest;
    size?: 'tiny' | 'chat' | 'hero' | 'sidebar';
};

const PET_SIZES = {
    tiny: 24,
    chat: 38,
    hero: 72,
    sidebar: 48,
} as const;

export function BotVisual({
    avatar,
    petState = 'idle',
    petEnabled = true,
    preferAnimatedPet = false,
    manifest = JENNIE_PET,
    size = 'chat',
}: BotVisualProps) {
    const hasAvatar = !!avatar?.trim();
    if (hasAvatar && (!preferAnimatedPet || !petEnabled)) {
        const className = size === 'hero'
            ? 'h-14 w-14 shadow-lg shadow-primary/20'
            : size === 'sidebar'
                ? 'h-10 w-10 shadow-md shadow-primary/10'
                : size === 'tiny'
                    ? 'h-5 w-5'
                    : 'h-8 w-8 border border-border/70 shadow-sm';

        return (
            <Avatar className={className}>
                <AvatarImage src={avatar ?? undefined} className="object-cover" />
                <AvatarFallback className="bg-primary/10 text-primary text-xs font-semibold">Bot</AvatarFallback>
            </Avatar>
        );
    }

    if (!petEnabled) {
        const className = size === 'hero'
            ? 'h-14 w-14 shadow-lg shadow-primary/20'
            : size === 'sidebar'
                ? 'h-10 w-10 shadow-md shadow-primary/10'
                : size === 'tiny'
                    ? 'h-5 w-5'
                    : 'h-8 w-8 border border-border/70 shadow-sm';

        return (
            <Avatar className={className}>
                <AvatarImage src="/limesimple.png" className="object-contain p-0.5" />
                <AvatarFallback className="bg-primary/10 text-primary text-xs font-semibold">Bot</AvatarFallback>
            </Avatar>
        );
    }

    return (
        <PetSprite
            manifest={manifest}
            size={PET_SIZES[size]}
            state={petState}
            label={`${manifest.displayName} ${petState}`}
        />
    );
}
