import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';

type BotVisualProps = {
    avatar?: string | null;
    size?: 'tiny' | 'chat' | 'hero' | 'sidebar';
};

export function BotVisual({
    avatar,
    size = 'chat',
}: BotVisualProps) {
    const className = size === 'hero'
        ? 'h-14 w-14 shadow-lg shadow-primary/20'
        : size === 'sidebar'
            ? 'h-10 w-10 shadow-md shadow-primary/10'
            : size === 'tiny'
                ? 'h-5 w-5'
                : 'h-8 w-8 border border-border/70 shadow-sm';

    return (
        <Avatar className={className}>
            <AvatarImage
                src={avatar?.trim() || '/limesimple.png'}
                className={avatar?.trim() ? 'object-cover' : 'object-contain p-0.5'}
            />
            <AvatarFallback className="bg-primary/10 text-primary text-xs font-semibold">Bot</AvatarFallback>
        </Avatar>
    );
}
