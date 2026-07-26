import { useCallback, useEffect, useState } from 'react';
import {
    findPetManifest,
    isPetManifest,
    normalizePetPreferences,
    DEFAULT_PET_ID,
    PET_CATALOG,
    PET_PREFERENCES_STORAGE_KEY,
    type PetManifest,
    type PetPreferences,
} from '@/lib/pet';

const PET_PREFERENCES_EVENT = 'limebot-pet-preferences-change';

function readPetPreferences(): PetPreferences {
    if (typeof window === 'undefined') {
        return normalizePetPreferences(null);
    }

    try {
        const raw: unknown = JSON.parse(window.localStorage.getItem(PET_PREFERENCES_STORAGE_KEY) || 'null');
        if (raw && typeof raw === 'object') {
            const stored = raw as Partial<PetPreferences>;
            return {
                enabled: stored.enabled !== false,
                petId: typeof stored.petId === 'string' ? stored.petId : DEFAULT_PET_ID,
                preferAnimatedPet: stored.preferAnimatedPet === true,
            };
        }
        return normalizePetPreferences(null);
    } catch {
        return normalizePetPreferences(null);
    }
}

function writePetPreferences(preferences: PetPreferences) {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(PET_PREFERENCES_STORAGE_KEY, JSON.stringify(preferences));
    window.dispatchEvent(new Event(PET_PREFERENCES_EVENT));
}

export function usePetPreferences() {
    const [preferences, setPreferences] = useState<PetPreferences>(readPetPreferences);
    const [catalog, setCatalog] = useState<readonly PetManifest[]>(PET_CATALOG);

    useEffect(() => {
        const sync = () => setPreferences(readPetPreferences());
        window.addEventListener('storage', sync);
        window.addEventListener(PET_PREFERENCES_EVENT, sync);
        return () => {
            window.removeEventListener('storage', sync);
            window.removeEventListener(PET_PREFERENCES_EVENT, sync);
        };
    }, []);

    useEffect(() => {
        let cancelled = false;

        const loadCatalog = async () => {
            try {
                const indexResponse = await fetch('/pets/index.json', { cache: 'no-store' });
                if (!indexResponse.ok) return;
                const index = await indexResponse.json() as {
                    pets?: Array<{ id?: string; manifestPath?: string }>;
                };
                const entries = Array.isArray(index.pets) ? index.pets : [];
                const manifests = await Promise.all(entries.map(async (entry) => {
                    if (!entry.manifestPath) return null;
                    try {
                        const response = await fetch(entry.manifestPath, { cache: 'no-store' });
                        if (!response.ok) return null;
                        const manifest: unknown = await response.json();
                        return isPetManifest(manifest) ? manifest : null;
                    } catch {
                        return null;
                    }
                }));
                const validCatalog = manifests.filter((manifest): manifest is PetManifest => !!manifest);
                if (cancelled || validCatalog.length === 0) return;
                setCatalog(validCatalog);
                setPreferences((current) => normalizePetPreferences(current, validCatalog));
            } catch {
                // The built-in catalog remains available if a static index is unavailable.
            }
        };

        void loadCatalog();
        return () => {
            cancelled = true;
        };
    }, []);

    const updatePreferences = useCallback((patch: Partial<PetPreferences>) => {
        setPreferences((current) => {
            const next = normalizePetPreferences({ ...current, ...patch }, catalog);
            writePetPreferences(next);
            return next;
        });
    }, [catalog]);

    const selectedPet: PetManifest = findPetManifest(preferences.petId, catalog);

    return {
        ...preferences,
        catalog,
        selectedPet,
        setPetId: (petId: string) => updatePreferences({ petId }),
        setPetEnabled: (enabled: boolean) => updatePreferences({ enabled }),
        setPreferAnimatedPet: (preferAnimatedPet: boolean) => updatePreferences({ preferAnimatedPet }),
    };
}
