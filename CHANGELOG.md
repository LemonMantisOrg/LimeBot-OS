# Changelog

## 1.0.16 - 2026-08-30
### Fixed
- After a successful `web_search(kind="images")` the host always downloads the
  best image URL and delivers it (web envelope or Discord/WhatsApp file). Image
  search results are the signal; delivery-verb lists are not the classifier.
- Exclusive photo-send shortlist uses a photo noun (foto/photo/pic/picture/
  image/imagen) and not generate/draw, instead of stacking more slang verbs.

## 1.0.15 - 2026-08-30
### Fixed
- Discord/WhatsApp photo-send: after `web_search(kind="images")` the host
  downloads the best image and sends it as a file. Spanish tráeme / pásame /
  mándame (and English bring/send/show me a photo) count as chat media delivery.
- Photo-send turns no longer expose `analyze_video`, and an empty model wrap-up
  no longer replaces a delivered photo with a tool-trace apology.

## 1.0.14 - 2026-08-26
### Changed
- Host-owned `web_search(kind=web|news|images)`: the runtime fetches and parses
  results, drops ads/tracking URLs, retries another engine on an empty, ads-only,
  or thin SERP, and never asks the model to open a search page.
- Web photo-send attaches the best image URL itself (`metadata.image` +
  attachments). `send_media` stays for Discord/WhatsApp file share.
- Collapsed overlapping search and browser tools into `web_search` plus
  `browser_navigate` / `browser_act` / `browser_extract`.
- Photo-send and generate-image turns use an exclusive tool shortlist.

### Removed
- Model-facing `google_search`, `image_search`, `deep_research`, and
  `capability_search`.

### Fixed
- `generate_image` no longer sends `response_format` to OpenAI `gpt-image-*`
  models (LiteLLM `UnsupportedParamsError` on `openai/gpt-image-2`).
- Host search drops Bing/Google `aclk`, DoubleClick, Google Ads, and leftover
  click-wrappers, unwraps Bing organic `ck/a` destinations, and prefers official
  hosts such as python.org for software version queries.

## 1.0.13 - 2026-08-23
### Added
- Durable SQLite job queue with persist-before-run, leases, heartbeats, and
  crash resume. Interrupted jobs are re-queued on boot instead of fail-closed.
- Companion/web/Discord chats persist as durable jobs before the agent runs and
  resume after `kill -9` instead of 404ing.
- `limebot setup` first-run helper and `--recommended` browser + Chromium install.
- `vm-lab` skill: allowlisted ISO download, QEMU/KVM create/start, wait-ssh, ssh.
  `wait-ssh` requires an `SSH-` banner (QEMU slirp can accept TCP first).
  Cloud images boot the disk with a nocloud seed; KVM with an empty serial
  log falls back to TCG.
- Honest cron completion: `last_status=ok` only after the agent turn finishes.
- Unattended allowlists for scheduled/queued jobs. Live chat stays gated.
- Cursor plugin package format and `limebot plugin install` (official schemas
  under `schemas/cursor-plugin/`).
- systemd unit files under `deploy/systemd/` for 24/7 supervision.

### Changed
- Telegram is documented as a working long-poller, not a scaffold.
- Fast-harness casual-turn tool suppression does not apply to scheduled jobs.
- `edit_file` treats an already-correct file as `already_applied` after resume.
- Isolated explorer/reviewer sub-agents inherit parent allowlists for reads.
- Missing Playwright returns one setup command instead of a silent search-only path.
- Unsupported Node/Python prints a plain-language next step, not a stack trace.
- `browser_download` can follow a direct URL and wait up to 30 minutes for large files.

### Removed
- Showcase Jennie / hatch-pet authoring and the Pets dashboard.
- Built-in `github` skill (replaced by the official Cursor GitHub plugin).
- Unused `browser-harness` skill (not LimeBot's Playwright browser runtime).

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
