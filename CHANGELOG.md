# Changelog

## 1.0.13 - 2026-07-26 (Draft)
### Added
- Safe `edit_file`, `verify_files`, and optional provider diagnostics for
  bounded coding workflows with stale-edit protection.
- Copy-on-write subagent workspaces with bounded structured diffs and explicit
  parent-side change application.
- Animated pet companions for the web dashboard and browser extension,
  including pet selection, preferences, and activity-aware animation states.
- The `hatch-pet` skill for creating, validating, and packaging animated pets.

### Changed
- Agent prompts, tool routing, confirmation metadata, and workspace context
  now expose coding phases, read-only status, and subagent isolation details.
- Chat and task-progress surfaces now use the selected pet as the assistant
  visual and reflect connection, approval, activity, and error states.

### Fixed
- Coding and review subagents no longer implicitly mutate the live workspace
  when running with automatic isolation.

## 1.0.12 - 2026-07-09
### Added
- Core-only first installation with retryable optional profiles for browser,
  semantic memory, documents, MCP, WhatsApp, and the browser companion.
- `limebot feature install all` for every optional profile plus
  launch-verified Chromium.
- Durable coding attempts, change-set review artifacts, deterministic recovery
  steps, and improved task/workspace event reporting.
- Speech-to-text support and safer WhatsApp delivery acknowledgement handling.
- Provider-first-delta and first-useful-output latency metrics.
- Bounded per-channel delivery workers, atomic delivery snapshots, and a
  bounded background metrics writer.
- Secure Docker build contexts, health checks, optional WhatsApp profile, and
  first-run preparation scripts for PowerShell and POSIX shells.

### Changed
- Normal startup now opens the dashboard at backend liveness while capability
  readiness continues in the UI.
- Remote update discovery runs after launch, and npm/Python dependency lanes
  install concurrently when both need refreshing.
- Fast AI harness mode is now the default, using request-specific tool schemas,
  an 80ms Auto-RAG budget, and no tools for clearly casual turns.
- Daily and long-term memory prompt context is cached by file signature and
  privacy scope.
- Streaming assistant output stays lightweight plain text until completion,
  then switches once to rich Markdown.
- Docker now exposes only the loopback-bound Nginx gateway by default and uses
  same-origin API, media, and WebSocket proxying.

### Fixed
- Windows `npm.cmd` startup failures caused by `spawn EINVAL`.
- Interrupted virtual environments are preserved, repaired, and never fall
  back to installing packages into system Python.
- Cross-channel head-of-line blocking from slow sockets and synchronous
  persistence.
- Stale web sockets are removed after a bounded send timeout.
- WhatsApp interim typing, reconnect, queued-send, and delivery state handling.
- Docker images no longer receive local credentials, runtime state,
  dependency trees, or generated session data in their build contexts.

## 1.0.11 - 2026-06-27
### Added
- Browser companion extension (manifest V3) for page help, text selection sharing, live task status, and tool approvals.
- Durable task workspaces and embedding fallbacks for automatic provider resolution.
- Slash skill invocation support (`/skill <name>` and `/<name>`) to execute registered commands directly from chat.
- Redesigned and restored app-server API endpoints under `/api/app/*` (read-only state, events, message sending, and approval delegation).
- CLI utility (`review-diff`) and entrypoint script for automated pull request code review.
- Dynamic model capability checks and readiness gates before starting the session.

### Changed
- Default command execution and watchdog timeouts to `0` (disabled) to avoid installation timeouts.
- Completely removed the VS Code companion extension codebase, including all associated workspace configurations, build scripts, tests, and documentation.
- Updated extension payload protocol, client connection flow, and Discord integration.

### Fixed
- Deduplication of repeated sections in final assistant replies.
- Browser tab inspection logic and error reporting.
- Removed local-only hint pollution from the skills registry.

## 1.0.10 - 2026-06-13
### Added
- ElevenLabs voice and text-to-speech integration, plus image generation support.
- Discord DM support and a configurable tool shortlist for tighter agent workflows.
- Curated embedding model auto-detection and wider provider support for vector memory.

### Changed
- `save_*` tag handling now works through compatibility tool interception with parameter fallback.
- Scheduler state tracking and cron catch-up behavior are more resilient across restarts.
- Default voice configuration has been reset to a safer baseline in `limebot.json`.

### Fixed
- Duplicate assistant history appends and repeated tool-continuation replies in the agent loop.
- Stream parsing and Windows compatibility regressions in the core runtime.
- Cross-user dedup behavior in shared Discord channels.

## 1.0.9 - 2026-05-02
### Added
- OpenAI Codex integration with OAuth flow and configuration UI.
- Automatic LLM fallback mechanism (downgrades from Pro models to Free models if necessary).
- Operator dashboards for observability with Task and Delivery queue trackers.

### Fixed
- Fixed API controllers throwing attribute errors related to task queues.

## 1.0.8 - 2026-03-31
### Added
- Lightweight subagent system with built-in specialist profiles such as reviewer, verifier, and explorer.
- New Subagents dashboard page to create, edit, delete, and manage specialist profiles visually.
- Sidebar assistant mode selector for choosing the default subagent behavior.
- Structured subagent report cards in chat so delegated results are easier to read than raw orchestration text.
- Support for project and user subagent directories in both `.limebot/agents` and `.claude/agents`.
- Unit coverage for subagent registry loading, shadowing, selection, and tool schema behavior.

### Changed
- `spawn_agent` can now target named specialist profiles and optionally run in the background.
- Prompt guidance now includes subagent recommendations so delegation is more intentional and task-matched.
- Tool definitions now advertise available named subagents to the model.
- Chat UI suppresses noisy empty orchestration traces around delegated subagent work.
- RAG trace handling is more defensive when trace buckets are missing or malformed.

### Fixed
- Subagent API responses now use the same loaded registry instance for definitions, default selection, and selector options.
- Explicit `tools: []` for subagents now correctly means “no tools” instead of falling back to inherited tools.
- Tool alias normalization now handles `read_filejson`-style names more safely.
- Voice Preview Studio channel cards no longer overflow when labels and badges get tight on smaller widths.

## 1.0.7 - 2026-03-29
### Added
- Telegram channel scaffold with Bot API long polling, config loading, startup wiring, and tests.
- Telegram dashboard controls in Channels and Credentials so bot token, API base, allow lists, and polling timeout can be managed from the UI.
- Cron pause/resume controls in the dashboard and scheduler API, including persisted active state for jobs.

### Changed
- Persona files are now local-first runtime state. Fresh installs bootstrap `SOUL.md`, `IDENTITY.md`, and `MEMORY.md` from shipped `.example` templates instead of relying on tracked live persona files.
- Cron tooling now reports whether a job is active or paused.

### Fixed
- NVIDIA embedding provider resolution now maps to LiteLLM's supported `nvidia_nim/NV-Embed-v2` format and keeps legacy NVIDIA embedding config values working.
- Telegram integration is now usable end-to-end from the dashboard once the bot token is saved and the channel is enabled.

## 1.0.3 - 2026-02-26
### Added
- Discord personalization UI with per-guild/per-channel tone, verbosity, emoji usage, signatures, and embed theming.
- GitHub skill defaults and notifications: default repo/base/PR template, auto-labels/reviewers, and Discord/Web notifications.
- Skill dependency visibility in the UI, including required deps and missing-deps alerts.
- Unit tests for Discord personalization and WhatsApp safety checks.

### Changed
- Discord avatar override is now global-only (bots do not support per-guild avatars).
- Tool embeds are branded and themed for Discord.
- Skill metadata includes dependencies and per-skill requirements files.

### Fixed
- Reflection skips LLM calls when no journal exists (prevents setup-time errors).
