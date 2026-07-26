import { Sidebar } from "./Sidebar";
import { useState } from "react";
import { Menu, Wifi, WifiOff, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { BotVisual } from "@/components/pet/BotVisual";
import type { PetManifest, PetState } from "@/lib/pet";

interface AppLayoutProps {
    children: React.ReactNode;
    botIdentity?: { name: string; avatar: string | null };
    activeView?: string;
    onNavigate?: (view: string) => void;
    pageTitle?: string;
    pageDescription?: string;
    petState?: PetState;
    petEnabled?: boolean;
    preferAnimatedPet?: boolean;
    petManifest?: PetManifest;
    runtimeStatus?: {
        isConnected: boolean;
        autonomousMode: boolean;
        pendingApprovals: number;
    };
}

export function AppLayout({
    children,
    botIdentity,
    activeView,
    onNavigate,
    pageTitle,
    pageDescription,
    petState,
    petEnabled,
    preferAnimatedPet,
    petManifest,
    runtimeStatus,
}: AppLayoutProps) {
    const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

    const handleNavigate = (view: string) => {
        onNavigate?.(view);
        setMobileMenuOpen(false); // Close menu on navigation
    };

    return (
        <div className="flex h-screen bg-transparent overflow-hidden relative">
            {/* Desktop Sidebar */}
            <Sidebar
                className="hidden md:flex"
                botIdentity={botIdentity}
                petState={petState}
                petEnabled={petEnabled}
                preferAnimatedPet={preferAnimatedPet}
                petManifest={petManifest}
                activeView={activeView}
                onNavigate={onNavigate}
                runtimeStatus={runtimeStatus}
            />

            {/* Mobile Header */}
            <div className="md:hidden absolute top-0 left-0 right-0 z-50 border-b border-border bg-background/88 backdrop-blur-md">
                <div className="flex items-start gap-3 px-4 py-3">
                    <Button variant="ghost" size="icon" onClick={() => setMobileMenuOpen(true)} className="mt-0.5 shrink-0">
                        <Menu className="h-6 w-6" />
                    </Button>
                    <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                            <BotVisual
                                avatar={botIdentity?.avatar}
                                petState={petState}
                                petEnabled={petEnabled}
                                preferAnimatedPet={preferAnimatedPet}
                                manifest={petManifest}
                                size="tiny"
                            />
                            <span className="truncate">{botIdentity?.name || "LimeBot"}</span>
                            <span
                                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium normal-case tracking-normal ${
                                    runtimeStatus?.isConnected
                                        ? "border-primary/20 bg-primary/10 text-primary"
                                        : "border-amber-500/20 bg-amber-500/10 text-amber-500"
                                }`}
                            >
                                {runtimeStatus?.isConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
                                {runtimeStatus?.isConnected ? "Connected" : "Reconnecting"}
                            </span>
                        </div>
                        <div className="min-w-0">
                            <div className="truncate text-base font-semibold text-foreground">
                                {pageTitle || botIdentity?.name || "LimeBot"}
                            </div>
                            <div className="truncate text-xs text-muted-foreground">
                                {pageDescription || "LimeBot control surface"}
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            {/* Mobile Sidebar Drawer */}
            {mobileMenuOpen && (
                <div className="fixed inset-0 z-50 md:hidden">
                    {/* Backdrop */}
                    <div
                        className="absolute inset-0 bg-background/80 backdrop-blur-sm"
                        onClick={() => setMobileMenuOpen(false)}
                    />

                    {/* Drawer Content */}
                    <div className="absolute left-0 top-0 bottom-0 w-[80%] max-w-sm bg-card border-r border-border p-4 shadow-2xl animate-in slide-in-from-left duration-200 flex flex-col">
                        <div className="flex justify-end mb-2 shrink-0">
                            <Button variant="ghost" size="icon" onClick={() => setMobileMenuOpen(false)}>
                                <X className="h-5 w-5" />
                            </Button>
                        </div>
                        <Sidebar
                            className="flex flex-1 min-h-0 h-full w-full m-0 border-none shadow-none"
                            botIdentity={botIdentity}
                            petState={petState}
                            petEnabled={petEnabled}
                            preferAnimatedPet={preferAnimatedPet}
                            petManifest={petManifest}
                            activeView={activeView}
                            onNavigate={handleNavigate}
                            runtimeStatus={runtimeStatus}
                        />
                    </div>
                </div>
            )}

            <main className="flex-1 h-full min-w-0 pt-20 md:pt-4 md:py-4 md:pr-4 relative z-0">
                <div className="h-full rounded-none md:rounded-2xl overflow-hidden shadow-none md:shadow-2xl border-t md:border border-border bg-card">
                    {children}
                </div>
            </main>
        </div>
    );
}
