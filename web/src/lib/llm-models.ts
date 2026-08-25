export type LlmModelOption = {
  id: string;
  name: string;
  provider: string;
};

export const DEFAULT_MODEL_BY_PROVIDER: Record<string, string> = {
  gemini: "gemini/gemini-3.6-flash",
  openai: "openai/gpt-5.6-sol",
  "openai-codex": "openai-codex/gpt-5.6-sol",
  anthropic: "anthropic/claude-opus-5",
  xai: "xai/grok-4.6",
  deepseek: "deepseek/deepseek-v4-flash",
  moonshot: "moonshot/kimi-k3",
  qwen: "qwen/qwen3.5-plus",
  nvidia: "nvidia/deepseek-ai/deepseek-v4-flash-0731",
  openrouter: "openrouter/openai/gpt-5.6-sol",
};

export const OPENROUTER_CURATED_MODEL_IDS = [
  "openrouter/anthropic/claude-haiku-4.5",
  "openrouter/anthropic/claude-opus-5",
  "openrouter/anthropic/claude-sonnet-5",
  "openrouter/deepseek/deepseek-v4-flash",
  "openrouter/deepseek/deepseek-v4-pro",
  "openrouter/google/gemini-3.7-flash",
  "openrouter/google/gemini-3.6-flash",
  "openrouter/google/gemini-3.1-pro-preview",
  "openrouter/inception/mercury-2",
  "openrouter/meta-llama/llama-3.3-70b-instruct",
  "openrouter/minimax/minimax-m2.5",
  "openrouter/mistralai/codestral-2508",
  "openrouter/mistralai/mistral-7b-instruct-v0.1",
  "openrouter/mistralai/mistral-large",
  "openrouter/mistralai/mistral-medium-3.1",
  "openrouter/mistralai/mistral-small-3.2-24b-instruct-2506",
  "openrouter/moonshotai/kimi-k3",
  "openrouter/openai/gpt-5.6-sol",
  "openrouter/openai/gpt-5.6-sol-pro",
  "openrouter/openai/gpt-5.6-terra",
  "openrouter/openai/gpt-5.6-luna",
  "openrouter/openai/gpt-oss-120b",
  "openrouter/perplexity/sonar",
  "openrouter/perplexity/sonar-pro",
  "openrouter/qwen/qwen3.8-27b",
  "openrouter/x-ai/grok-4.6",
  "openrouter/z-ai/glm-5.3",
];

export const PROVIDER_LABELS: Record<string, string> = {
  gemini: "Google Gemini",
  openai: "OpenAI",
  "openai-codex": "OpenAI / Codex",
  anthropic: "Anthropic Claude",
  xai: "xAI (Grok)",
  deepseek: "DeepSeek",
  moonshot: "Moonshot AI (Kimi)",
  qwen: "Qwen (DashScope)",
  nvidia: "NVIDIA",
  openrouter: "OpenRouter",
  custom: "Custom / Local",
};

const FEATURED_MODEL_IDS_BY_PROVIDER: Record<string, string[]> = {
  gemini: [
    "gemini/gemini-3.6-flash",
    "gemini/gemini-3.5-flash",
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.1-flash-lite",
    "gemini/gemini-3.1-pro-preview",
  ],
  openai: [
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
    "openai/gpt-5.5",
  ],
  "openai-codex": [
    "openai-codex/gpt-5.6-sol",
    "openai-codex/gpt-5.6-luna",
    "openai-codex/gpt-5.6-terra",
    "openai-codex/gpt-5.5",
    "openai-codex/gpt-5.4",
    "openai-codex/gpt-5.4-mini",
  ],
  anthropic: [
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-fable-5",
    "anthropic/claude-haiku-4-5-20251001",
  ],
  xai: ["xai/grok-4.6"],
  deepseek: [
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v3.2",
    "deepseek/deepseek-chat",
  ],
  moonshot: [
    "moonshot/kimi-k3",
    "moonshot/kimi-k2.6",
    "moonshot/kimi-k2.5",
  ],
  qwen: [
    "qwen/qwen3.5-plus",
    "qwen/qwen3.5-flash",
    "qwen/qwen3-max",
    "qwen/qwen3.5-397b-a17b",
  ],
  nvidia: [
    "nvidia/deepseek-ai/deepseek-v4-flash-0731",
    "nvidia/deepseek-ai/deepseek-v4-pro",
    "nvidia/qwen/qwen3-coder-next",
    "nvidia/zai-org/glm-5",
    "nvidia/openai/gpt-oss-120b",
  ],
  openrouter: [
    "openrouter/openai/gpt-5.6-sol",
    "openrouter/anthropic/claude-opus-5",
    "openrouter/google/gemini-3.7-flash",
    "openrouter/deepseek/deepseek-v4-pro",
    "openrouter/moonshotai/kimi-k3",
    "openrouter/x-ai/grok-4.6",
  ],
};

export function getModelProvider(modelId?: string): string {
  const normalized = String(modelId || "").trim();
  if (!normalized.includes("/")) {
    return "custom";
  }
  return normalized.split("/")[0] || "custom";
}

export function getProviderModels(
  models: LlmModelOption[],
  provider: string,
): LlmModelOption[] {
  const seen = new Set<string>();
  return models
    .filter((model) => model.provider === provider)
    .sort((a, b) => a.name.localeCompare(b.name))
    .filter((model) => {
      if (seen.has(model.id)) {
        return false;
      }
      seen.add(model.id);
      return true;
    });
}

export function getRecommendedModels(
  models: LlmModelOption[],
  provider: string,
): LlmModelOption[] {
  const providerModels = getProviderModels(models, provider);
  const byId = new Map(providerModels.map((model) => [model.id, model]));
  const orderedIds = FEATURED_MODEL_IDS_BY_PROVIDER[provider] || [];

  const picks = orderedIds
    .map((id) => byId.get(id))
    .filter((model): model is LlmModelOption => Boolean(model));

  if (picks.length > 0) {
    return picks;
  }

  return providerModels.slice(0, 6);
}

export function getAdditionalModels(
  models: LlmModelOption[],
  provider: string,
): LlmModelOption[] {
  const recommendedIds = new Set(
    getRecommendedModels(models, provider).map((model) => model.id),
  );
  return getProviderModels(models, provider).filter(
    (model) => !recommendedIds.has(model.id),
  );
}

export function getInitialModelForProvider(
  models: LlmModelOption[],
  provider: string,
): string {
  const recommended = getRecommendedModels(models, provider);
  if (recommended.length > 0) {
    return recommended[0].id;
  }

  const providerModels = getProviderModels(models, provider);
  if (providerModels.length > 0) {
    return providerModels[0].id;
  }

  return DEFAULT_MODEL_BY_PROVIDER[provider] || "";
}

export function getVisibleModels(
  models: LlmModelOption[],
  provider: string,
  selectedModelId: string,
  showAll: boolean,
): LlmModelOption[] {
  const recommended = getRecommendedModels(models, provider);
  const additional = getAdditionalModels(models, provider);
  const visible = showAll ? [...recommended, ...additional] : [...recommended];

  if (
    selectedModelId &&
    !visible.some((model) => model.id === selectedModelId)
  ) {
    const selected = getProviderModels(models, provider).find(
      (model) => model.id === selectedModelId,
    );
    if (selected) {
      visible.unshift(selected);
    }
  }

  const seen = new Set<string>();
  return visible.filter((model) => {
    if (seen.has(model.id)) {
      return false;
    }
    seen.add(model.id);
    return true;
  });
}
