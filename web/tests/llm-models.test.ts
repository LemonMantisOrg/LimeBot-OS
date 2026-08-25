import test from "node:test";
import { strict as assert } from "node:assert";

import {
  DEFAULT_MODEL_BY_PROVIDER,
  getAdditionalModels,
  getRecommendedModels,
  type LlmModelOption,
} from "../src/lib/llm-models.js";

const models: LlmModelOption[] = [
  { id: "openai/gpt-5.6-sol", name: "GPT-5.6 Sol", provider: "openai" },
  { id: "openai/gpt-5.6-terra", name: "GPT-5.6 Terra", provider: "openai" },
  { id: "openai/gpt-5.6-luna", name: "GPT-5.6 Luna", provider: "openai" },
  { id: "openai/gpt-5.5", name: "GPT-5.5", provider: "openai" },
];

test("OpenAI recommends exact live API model IDs in capability order", () => {
  assert.equal(DEFAULT_MODEL_BY_PROVIDER.openai, "openai/gpt-5.6-sol");
  assert.deepEqual(
    getRecommendedModels(models, "openai").map((model) => model.id),
    [
      "openai/gpt-5.6-sol",
      "openai/gpt-5.6-terra",
      "openai/gpt-5.6-luna",
      "openai/gpt-5.5",
    ],
  );
  assert.deepEqual(getAdditionalModels(models, "openai").map((model) => model.id), []);
});

test("DeepSeek recommends the current V4 API models first", () => {
  const deepseekModels: LlmModelOption[] = [
    { id: "deepseek/deepseek-v4-flash", name: "DeepSeek V4 Flash", provider: "deepseek" },
    { id: "deepseek/deepseek-v4-pro", name: "DeepSeek V4 Pro", provider: "deepseek" },
    { id: "deepseek/deepseek-v3.2", name: "DeepSeek V3.2", provider: "deepseek" },
    { id: "deepseek/deepseek-chat", name: "DeepSeek Chat (legacy)", provider: "deepseek" },
  ];

  assert.equal(DEFAULT_MODEL_BY_PROVIDER.deepseek, "deepseek/deepseek-v4-flash");
  assert.deepEqual(
    getRecommendedModels(deepseekModels, "deepseek").map((model) => model.id),
    [
      "deepseek/deepseek-v4-flash",
      "deepseek/deepseek-v4-pro",
      "deepseek/deepseek-v3.2",
      "deepseek/deepseek-chat",
    ],
  );
});
