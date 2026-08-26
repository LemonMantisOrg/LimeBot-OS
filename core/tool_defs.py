"""
Declarative tool definitions — compact, data-driven tool schema registry.

Each tool is defined as a simple dict with name, description, and params.
build_tool_definitions() inflates them into the full OpenAI-compatible JSON
schema format that LiteLLM expects.

To add a new tool:
  1. Add a dict to BASE_TOOLS, BROWSER_TOOLS, or a new list.
  2. Add the handler to _tool_registry in loop.py.
  Done — no 20-line JSON blob needed.
"""

import copy
import os
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

from core.media_intent import (
    CHAT_MEDIA_BLOCKED_TOOLS,
    CHAT_MEDIA_SUPPORTING_TOOLS,
    CHAT_MEDIA_TOOLS,
    is_chat_media_delivery,
    is_image_generation_request,
)


BASE_TOOLS = [
    {
        "name": "capability_search",
        "description": (
            "Inspect LimeBot's installed capabilities before claiming an integration, "
            "skill, tool, MCP connection, or specialist is unavailable. Returns a compact "
            "inventory with matching names, required tools, and operational state. "
            "Do not use this for sending a photo into the current chat — that is "
            "image_search then send_media."
        ),
        "params": {
            "query": {
                "type": "string",
                "description": (
                    "Capability or task to resolve, for example 'Jira ticket GDHD-1199' "
                    "or 'web research'. Leave empty to list the compact inventory."
                ),
            },
            "include_disabled": {
                "type": "boolean",
                "description": "Include discovered but disabled or dependency-missing capabilities (defaults to true).",
            },
        },
        "required": ["query"],
    },
    {
        "name": "read_file",
        "description": "Read a known file path. Use this after you already know the exact file to inspect. Prefer search_files first if you do not know where the file is. Supports line ranges and bounded reads, including .docx and .pdf text extraction. Example: read_file(path='core/loop.py', start_line=1, end_line=80).",
        "params": {
            "path": {
                "type": "string",
                "description": "Relative or absolute path to the file.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return (default 20000, max 200000).",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-based starting line number.",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-based ending line number (inclusive).",
            },
            "include_hash": {
                "type": "boolean",
                "description": "Include the current raw-file SHA-256 so a later edit_file call can reject stale edits.",
            },
        },
        "required": ["path"],
    },
    {
        "name": "edit_file",
        "description": (
            "Apply one or more exact, reviewable text edits to an existing UTF-8 source file. "
            "Use read_file(..., include_hash=true) first, then pass that SHA-256 as "
            "expected_sha256. Each edit must contain old_text and new_text; old_text must "
            "match exactly. The operation is preflighted and atomic: stale hashes, missing "
            "anchors, overlapping edits, and no-op edits are rejected without changing the file. "
            "Use write_file for intentional full replacement or creating a new file."
        ),
        "params": {
            "path": {
                "type": "string",
                "description": "Relative or absolute path to an existing UTF-8 text file.",
            },
            "edits": {
                "type": "array",
                "description": "Exact non-overlapping edits, applied against one file snapshot.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {
                            "type": "string",
                            "description": "Exact text to replace, including whitespace when relevant.",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Replacement text.",
                        },
                        "occurrence": {
                            "type": "integer",
                            "description": "1-based match occurrence when old_text appears more than once; defaults to 1.",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every exact match instead of one occurrence; defaults to false.",
                        },
                    },
                    "required": ["old_text", "new_text"],
                },
            },
            "expected_sha256": {
                "type": "string",
                "description": "SHA-256 from read_file(include_hash=true). Required for stale-edit protection when editing an existing file.",
            },
        },
        "required": ["path", "edits", "expected_sha256"],
    },
    {
        "name": "write_file",
        "description": "Write or overwrite a file when the user clearly asked to create or edit one. Do not use this just to explore or inspect. Creates directories if needed. To start a new skill scaffold, prefer create_skill instead.",
        "params": {
            "path": {
                "type": "string",
                "description": "Relative or absolute path to the file.",
            },
            "content": {
                "type": "string",
                "description": "The full text content to write.",
            },
        },
        "required": ["path", "content"],
    },
    {
        "name": "create_spreadsheet",
        "description": (
            "Create a real, styled Microsoft Excel .xlsx workbook without writing a script. "
            "Use this whenever the user asks for Excel or XLSX. Put column headings in the "
            "first row of each sheet; strings beginning with '=' become Excel formulas. "
            "After success, call send_media with the same path to deliver the workbook."
        ),
        "params": {
            "path": {
                "type": "string",
                "description": "Destination path ending in .xlsx.",
            },
            "title": {
                "type": "string",
                "description": "Optional workbook title metadata.",
            },
            "sheets": {
                "type": "array",
                "description": "One to twenty worksheets, each with a name and rectangular row arrays.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": ["string", "number", "boolean", "null"]
                                },
                            },
                        },
                    },
                    "required": ["name", "rows"],
                },
            },
        },
        "required": ["path", "sheets"],
    },
    {
        "name": "calculate",
        "description": (
            "Evaluate arithmetic exactly and safely. Use this for prices, totals, percentages, "
            "unit conversions, and comparisons instead of mental math or run_command. "
            "Supports +, -, *, /, //, %, parentheses, and bounded powers."
        ),
        "params": {
            "expression": {
                "type": "string",
                "description": "Arithmetic expression, for example 0.0104*730*12.",
            }
        },
        "required": ["expression"],
    },
    {
        "name": "delete_file",
        "description": "Delete a file or directory only when the user explicitly wants removal. Never use for cleanup by default. Requires confirmation.",
        "params": {
            "path": {
                "type": "string",
                "description": "Path to the file or directory to delete.",
            }
        },
        "required": ["path"],
    },
    {
        "name": "list_dir",
        "description": "List a directory when you need to inspect folder structure or browse candidates. Prefer this for broad exploration; prefer read_file for a known file and search_files for text/name lookup.",
        "params": {
            "path": {
                "type": "string",
                "description": "Path to the directory. Defaults to '.' (current directory).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum entries to return (default 200, max 1000).",
            },
            "offset": {
                "type": "integer",
                "description": "Pagination offset (default 0).",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include dotfiles and hidden entries (default false).",
            },
            "sort_by": {
                "type": "string",
                "enum": ["name", "type", "mtime", "size", "none"],
                "description": "Sort strategy (default name).",
            },
            "descending": {
                "type": "boolean",
                "description": "Sort descending when true (default false).",
            },
            "folders_first": {
                "type": "boolean",
                "description": "Show folders before files when applicable (default true).",
            }
        },
        "required": ["path"],
    },
    {
        "name": "search_files",
        "description": "Fast repo search. Use this first when you need to locate code, config, symbols, or filenames but do not know the exact path yet. Use mode='content' for text inside files and mode='name' for filenames. After finding a path, switch to read_file or list_dir. Example: search_files(query='verify_auth', mode='content').",
        "params": {
            "query": {
                "type": "string",
                "description": "Search text to look for (required).",
            },
            "path": {
                "type": "string",
                "description": "Root path to search. Defaults to current project directory.",
            },
            "file_glob": {
                "type": "string",
                "description": "Optional filename glob filter (e.g. '*.py', '*.md').",
            },
            "mode": {
                "type": "string",
                "enum": ["content", "name"],
                "description": "Search content lines or file names.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Set true for case-sensitive search. Default false.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (default 40, max 200).",
            },
        },
        "required": ["query"],
    },
    {
        "name": "verify_files",
        "description": (
            "Run safe, read-only checks on one or more changed files. This checks UTF-8 "
            "decoding, conflict markers, Python/JSON/TOML syntax when applicable, and "
            "git diff whitespace errors. Use it after editing and before claiming a coding "
            "task is complete; it does not run arbitrary commands or tests."
        ),
        "params": {
            "paths": {
                "type": "array",
                "description": "One or more existing files to verify.",
                "items": {"type": "string"},
            },
            "include_diagnostics": {
                "type": "boolean",
                "description": "Also run an installed optional linter/type checker when true.",
            },
            "provider": {
                "type": "string",
                "enum": ["auto", "ruff", "pyright", "eslint", "tsc", "none"],
                "description": "Optional diagnostics provider used when include_diagnostics is true.",
            },
        },
        "required": ["paths"],
    },
    {
        "name": "diagnose_files",
        "description": (
            "Run optional project diagnostics for changed source files using an installed "
            "Ruff, Pyright, ESLint, or TypeScript compiler. This uses direct subprocess "
            "arguments, never requires an LSP server, and returns skipped when no matching "
            "provider is installed. Use provider='auto' unless the user names a tool."
        ),
        "params": {
            "paths": {
                "type": "array",
                "description": "One or more existing source files to diagnose.",
                "items": {"type": "string"},
            },
            "provider": {
                "type": "string",
                "enum": ["auto", "ruff", "pyright", "eslint", "tsc", "none"],
                "description": "Optional diagnostics provider; defaults to auto.",
            },
            "timeout": {
                "type": "number",
                "description": "Maximum provider runtime in seconds (default 45, max 120).",
            },
        },
        "required": ["paths"],
    },
    {
        "name": "run_command",
        "description": (
            "Execute one shell command only when native tools are insufficient or the user explicitly "
            "wants command execution. Commands run from the project root on "
            f"{'Windows' if os.name == 'nt' else 'a POSIX system'}. Never use heredocs, newlines, "
            "&&, ||, semicolon chaining, redirects, backticks, or $(). Prefer read_file, list_dir, "
            "search_files, calculate, create_spreadsheet, or browser tools first."
        ),
        "params": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            }
        },
        "required": ["command"],
    },
    {
        "name": "memory_search",
        "description": "Search stored memory when the user asks what was remembered before or you need recalled facts from past logs. Do not use it for normal file/code search.",
        "params": {
            "query": {
                "type": "string",
                "description": "Keywords or phrase to search for in memory.",
            }
        },
        "required": ["query"],
    },
    {
        "name": "memory_save",
        "description": (
            "Persist a fact or event the user explicitly asks LimeBot to remember. "
            "This writes the Markdown source of truth and works without embeddings. "
            "Use scope='journal' for an append-only dated event (default), or "
            "scope='long_term' for a durable preference, identity fact, project, or relationship."
        ),
        "params": {
            "content": {
                "type": "string",
                "description": "The concise fact or event to remember.",
            },
            "scope": {
                "type": "string",
                "enum": ["journal", "long_term"],
                "description": "Where to persist it. Defaults to 'journal'.",
            },
        },
        "required": ["content"],
    },
    {
        "name": "spawn_agent",
        "description": (
            "Delegate a long, parallelizable, or specialized task to a sub-agent. "
            "Prefer direct tools for tiny tasks. Do not use this to send a photo into "
            "the current chat — that is image_search then send_media. "
            "Use spawn_agent when the work clearly matches a specialist such as codebase "
            "exploration, review, or verification."
        ),
        "params": {
            "task": {
                "type": "string",
                "description": "Full description of the task for the sub-agent.",
            },
            "background": {
                "type": "boolean",
                "description": "If true, start the sub-agent in the background and let it report back later instead of waiting for its result now.",
            },
            "isolation": {
                "type": "string",
                "enum": ["auto", "copy", "none"],
                "description": (
                    "Workspace mode for the sub-agent. 'auto' uses a temporary copy for coding, "
                    "repository, review, and verification work; 'copy' always isolates file edits; "
                    "'none' keeps the existing workspace and should be reserved for read-only or "
                    "explicitly shared tasks."
                ),
            },
        },
        "required": ["task"],
    },
    {
        "name": "get_task_output",
        "description": "Get status and output for a background sub-agent by task ID. Pass timeout_ms > 0 to wait; omit it or use 0 for a non-blocking poll.",
        "params": {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One or more stable background task IDs.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Optional maximum wait in milliseconds; 0 means poll.",
            },
        },
        "required": ["task_ids"],
    },
    {
        "name": "wait_tasks",
        "description": "Wait for one or more background sub-agent tasks to finish. This is a compatibility alias for get_task_output with a positive timeout.",
        "params": {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Stable background task IDs.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Maximum wait in milliseconds.",
            },
        },
        "required": ["task_ids"],
    },
    {
        "name": "kill_task",
        "description": "Cancel a running background sub-agent by its stable task ID. Completed tasks are reported as already exited.",
        "params": {
            "task_id": {
                "type": "string",
                "description": "Stable background task ID returned by spawn_agent.",
            }
        },
        "required": ["task_id"],
    },
    {
        "name": "cron_add",
        "description": (
            "Schedule a reminder or recurring task when the user asks to be reminded later or to automate something on a schedule. "
            "Use time_expr for a one-time delay (e.g. '1h', '30m') or cron_expr for repeating schedules (e.g. '0 9 * * 1-5')."
        ),
        "params": {
            "time_expr": {
                "type": "string",
                "description": "Relative delay: '10s', '5m', '2h', '1d'.",
            },
            "cron_expr": {
                "type": "string",
                "description": "Cron expression for repeating jobs, e.g. '0 9 * * 1-5' for weekdays at 9 AM.",
            },
            "tz": {
                "type": "string",
                "description": "Optional IANA timezone for cron_expr schedules, e.g. 'America/El_Salvador'.",
            },
            "message": {
                "type": "string",
                "description": "The reminder message or task description.",
            },
            "name": {
                "type": "string",
                "description": "Optional short human-readable job name, e.g. 'Daily finance brief'.",
            },
            "context": {
                "type": "object",
                "description": "Delivery context. YOU MUST pass your current channel, chat_id, and sender_id.",
                "properties": {
                    "channel": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "sender_id": {"type": "string"},
                },
                "required": ["channel", "chat_id"],
            },
        },
        "required": ["message", "context"],
    },
    {
        "name": "cron_list",
        "description": "List pending scheduled jobs when the user asks what reminders or automations already exist.",
        "params": {},
        "required": [],
    },
    {
        "name": "cron_remove",
        "description": "Remove a scheduled job by ID after the user asks to cancel or delete a reminder.",
        "params": {
            "job_id": {
                "type": "string",
                "description": "The job ID to remove (from cron_list).",
            }
        },
        "required": ["job_id"],
    },
    {
        "name": "create_skill",
        "description": "Create a new LimeBot skill scaffold inside skills/. Prefer this over write_file when the user wants a new skill, because it creates the correct structure and avoids polluting the codebase. Example: create_skill(name='weather_check', description='Fetch and summarize weather').",
        "params": {
            "name": {
                "type": "string",
                "description": "The name of the skill (e.g. 'weather_check'). Use snake_case.",
            },
            "description": {
                "type": "string",
                "description": "Brief summary of what the skill does.",
            },
        },
        "required": ["name", "description"],
    },
    {
        "name": "send_media",
        "description": (
            "Deliver a requested file or photo into the current chat (web, Discord, or WhatsApp). "
            "Accepts a local file path OR a remote http(s) URL (downloaded first, SSRF-guarded). "
            "This is the tool that actually attaches bytes to the outgoing chat message. "
            "For 'send/download me a picture of X', call image_search first, then pass one Image URL as 'path'. "
            "Use it once per artifact; never resend the same path to narrate progress. "
            "Live chat media fetch is not a filesystem write and does not need confirmation."
        ),
        "params": {
            "path": {
                "type": "string",
                "description": "Local file path OR a remote http(s) URL to fetch and send.",
            },
            "caption": {
                "type": "string",
                "description": "Optional caption to send alongside the file.",
            },
        },
        "required": ["path"],
    },
    {
        "name": "send_voice",
        "description": "Speak text aloud and send it as a voice message in the current chat. Delivers an audio file on Discord/WhatsApp (no text needed) or an inline playable clip on web. Use this when the user asks for a voice message/voice note. Requires an ElevenLabs API key. Example: send_voice(text='Hey! Here is your reminder.').",
        "params": {
            "text": {
                "type": "string",
                "description": "The text to speak aloud.",
            },
            "channel": {
                "type": "string",
                "description": "Optional target channel (web, discord, whatsapp). Defaults to the current chat's channel.",
            },
        },
        "required": ["text"],
    },
    {
        "name": "generate_image",
        "description": (
            "Generate or edit a NEW image using the configured image-capable model. "
            "Use this only when the user explicitly asks to create, draw, render, generate, or transform a picture. "
            "Do NOT use this to download, find, or send an existing photo of a person or subject — "
            "that is image_search then send_media. "
            "A mention of images already embedded in a document is not an image-generation request. "
            "Never call this tool with a prompt that says image generation is unnecessary. "
            "Images attached to the current message can be used automatically as visual references, "
            "including for named people and follow-up requests such as 'make it' or 'generate it'. "
            "The tool saves generated files locally and sends the image back to the active chat when supported."
        ),
        "params": {
            "prompt": {
                "type": "string",
                "description": "Detailed image prompt describing the desired visual result.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional image backend. Examples: openai-codex/gpt-5.4-mini, openai/gpt-image-2, "
                    "gemini/gemini-3.1-flash-image, gemini/gemini-3-pro-image, "
                    "gemini/gemini-2.5-flash-image."
                ),
            },
            "size": {
                "type": "string",
                "description": "Optional output size or aspect ratio, e.g. 1024x1024, 1024x1536, 1536x1024, 16:9.",
            },
            "quality": {
                "type": "string",
                "description": "Optional quality hint such as auto, low, medium, or high.",
            },
            "count": {
                "type": "integer",
                "description": "Number of images to generate. Defaults to 1; currently capped at 4.",
            },
            "reference_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional allowed local image paths to use as visual references. "
                    "Usually omit this for chat uploads; LimeBot carries the current or recently attached image automatically."
                ),
            },
            "use_attached_images": {
                "type": "boolean",
                "description": (
                    "Use images attached to the current or immediately preceding chat turn as references. "
                    "Set true when the user says this image/photo, same image, make it, or generate it. "
                    "Set false only when the user explicitly wants a fresh text-only image."
                ),
            },
        },
        "required": ["prompt"],
    },
    {
        "name": "send_discord_message",
        "description": (
            "Send a plain Discord message to a server channel or directly to a user DM. "
            "Use channel_id for public/server channels, user_id for DMs, or omit both to reply in the current Discord chat."
        ),
        "params": {
            "message": {
                "type": "string",
                "description": "Message text to send.",
            },
            "channel_id": {
                "type": "string",
                "description": "Optional numeric Discord channel ID for a public/server channel target.",
            },
            "user_id": {
                "type": "string",
                "description": "Optional numeric Discord user ID for a direct message target.",
            },
        },
        "required": ["message"],
    },
    {
        "name": "send_discord_embed",
        "description": "Send a native Discord embed. Use this for structured Discord output instead of faking an embed with plain text. Defaults to the current Discord chat when used from Discord; otherwise pass channel_id or user_id explicitly.",
        "params": {
            "title": {
                "type": "string",
                "description": "Optional embed title.",
            },
            "description": {
                "type": "string",
                "description": "Optional embed description.",
            },
            "color": {
                "type": "string",
                "description": "Optional hex color like #5865F2.",
            },
            "footer": {
                "type": "string",
                "description": "Optional footer text.",
            },
            "image": {
                "type": "string",
                "description": "Optional image URL.",
            },
            "thumbnail": {
                "type": "string",
                "description": "Optional thumbnail URL.",
            },
            "fields": {
                "type": "array",
                "description": "Optional embed fields.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "value": {"type": "string"},
                        "inline": {"type": "boolean"},
                    },
                    "required": ["name", "value"],
                },
            },
            "channel_id": {
                "type": "string",
                "description": "Optional numeric Discord channel ID. Required outside Discord chats.",
            },
            "user_id": {
                "type": "string",
                "description": "Optional numeric Discord user ID for a direct message target.",
            },
        },
        "required": [],
    },
    {
        "name": "list_discord_channels",
        "description": "List the Discord guilds and text channels LimeBot can currently access. Use this to discover channel IDs before sending to a specific Discord channel.",
        "params": {},
        "required": [],
    },
    {
        "name": "analyze_video",
        "description": "Analyze one allowed local video or public HTTP(S) video. Returns a bounded transcript and up to three timestamped contact sheets. Use transcript for audio-only questions, efficient for quick visual scans, and balanced for summaries, UI recordings, ads, and visual questions.",
        "params": {
            "source": {"type": "string", "description": "Allowed local video path or public HTTP(S) video URL."},
            "question": {"type": "string", "description": "What to inspect or answer about the video."},
            "detail": {"type": "string", "enum": ["transcript", "efficient", "balanced"], "description": "Analysis detail; defaults to balanced."},
            "start": {"type": "string", "description": "Optional range start as SS, MM:SS, or HH:MM:SS."},
            "end": {"type": "string", "description": "Optional range end as SS, MM:SS, or HH:MM:SS."},
            "max_frames": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Candidate-frame cap from 1 to 100."},
            "resolution": {"type": "integer", "enum": [512, 1024], "description": "Frame tile resolution; use 1024 only for small on-screen text."},
        },
        "required": ["source"],
    },
]


BROWSER_TOOLS = [
    {
        "name": "browser_navigate",
        "description": "Open a webpage when you have a URL or need to start browser automation on a site. Usually the first browser step. Follow with browser_snapshot, browser_click, browser_type, or browser_extract. Example: browser_navigate(url='https://example.com').",
        "params": {
            "url": {
                "type": "string",
                "description": "The full URL to navigate to, including https://.",
            }
        },
        "required": ["url"],
    },
    {
        "name": "browser_click",
        "description": "Click an interactive element from the latest browser snapshot. Use only after browser_snapshot or browser_navigate returned element IDs.",
        "params": {
            "element_id": {
                "type": "string",
                "description": "Element ID from the last snapshot (e.g. 'e5').",
            }
        },
        "required": ["element_id"],
    },
    {
        "name": "browser_download",
        "description": (
            "Download a real file through the browser into an allowlisted path. "
            "Use element_id after a snapshot for Export/Download buttons, or url= "
            "for a direct file link (ISO, installer, zip). Saves under temp/downloads "
            "or dest= under temp/ / LIMEBOT_STATE_DIR. Returns the exact local path. "
            "Large files may take minutes; raise timeout_ms up to 1800000."
        ),
        "params": {
            "element_id": {
                "type": "string",
                "description": "Element ID from the latest browser snapshot. Optional when url is set.",
            },
            "url": {
                "type": "string",
                "description": "Direct http(s) file URL when the page serves the file immediately.",
            },
            "filename": {
                "type": "string",
                "description": "Optional safe output filename. The website suggestion is used by default.",
            },
            "dest": {
                "type": "string",
                "description": "Optional allowlisted destination file or directory under temp/.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Maximum wait in milliseconds (default: 30000, max: 1800000).",
            },
        },
        "required": [],
    },
    {
        "name": "browser_type",
        "description": "Type into a known browser input element. Use only after browser_snapshot or browser_navigate identified the correct element ID.",
        "params": {
            "element_id": {
                "type": "string",
                "description": "Element ID of the input field (e.g. 'e5').",
            },
            "text": {"type": "string", "description": "Text to type into the field."},
        },
        "required": ["element_id", "text"],
    },
    {
        "name": "browser_snapshot",
        "description": "Inspect the current browser page and get the interactive element tree. Use this after navigation and before clicking or typing when you need fresh element IDs.",
        "params": {},
        "required": [],
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the current page to reveal more content when a snapshot or extract did not show everything you need.",
        "params": {
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Direction to scroll.",
            },
            "amount": {
                "type": "integer",
                "description": "Pixels to scroll (default: 500).",
            },
        },
        "required": ["direction"],
    },
    {
        "name": "browser_wait",
        "description": "Pause briefly after browser actions when the page needs time to update, load results, or render new elements.",
        "params": {
            "ms": {
                "type": "integer",
                "description": "Milliseconds to wait (default: 1000, max: 30000).",
            }
        },
        "required": [],
    },
    {
        "name": "browser_press_key",
        "description": "Press a key in the browser for form submission or UI control, such as Enter, Escape, Tab, or arrows.",
        "params": {
            "key": {
                "type": "string",
                "description": "Key name (e.g. 'Enter', 'Escape', 'Tab', 'ArrowDown').",
            }
        },
        "required": ["key"],
    },
    {
        "name": "browser_go_back",
        "description": "Go back one page in browser history when navigation went to the wrong place or you need the previous page again.",
        "params": {},
        "required": [],
    },
    {
        "name": "browser_tabs",
        "description": "List open browser tabs when the site spawned multiple pages and you need to inspect or switch between them.",
        "params": {},
        "required": [],
    },
    {
        "name": "browser_switch_tab",
        "description": "Switch to a specific browser tab after browser_tabs identified the correct index.",
        "params": {
            "index": {"type": "integer", "description": "Tab index from browser_tabs."}
        },
        "required": ["index"],
    },
    {
        "name": "browser_extract",
        "description": (
            "Extract visible text from the page or a CSS selector when you need page content, article text, or table data. "
            "Prefer this over browser_snapshot for reading content. Use limit=100000 for large tables or long articles."
        ),
        "params": {
            "selector": {
                "type": "string",
                "description": "CSS selector to extract from (default: 'body').",
            },
            "limit": {
                "type": "integer",
                "description": "Max characters to return (default: 5000, max: 100000).",
            },
        },
        "required": [],
    },
    {
        "name": "browser_get_page_text",
        "description": "Return all visible page text for reading-heavy tasks. Prefer browser_extract if you can target a specific selector.",
        "params": {},
        "required": [],
    },
    {
        "name": "browser_list_media",
        "description": "List major images/media on the current page when the user wants photos, assets, or visual content from a site.",
        "params": {},
        "required": [],
    },
    {
        "name": "google_search",
        "description": "Alias for web_search. Discover pages when you don't already have a URL. Prefer web_search, which returns more results and supports news. Example: google_search(query='LimeBot Discord bot docs').",
        "params": {"query": {"type": "string", "description": "Search query string."}},
        "required": ["query"],
    },
]


# Search tools are available when a search API key is configured OR the browser
# skill is enabled (a keyless DuckDuckGo fallback needs no browser). They route
# through core/web_search.py, not the Playwright browser stack.
SEARCH_TOOLS = [
    {
        "name": "web_search",
        "description": "Search the live web for pages, facts, or current information. Returns ranked results with titles, URLs, and snippets (plus a direct answer when the provider supplies one). Use kind='news' for recent news. A common first step before browser_navigate or deep_research. Example: web_search(query='best pizza in Rome', count=8).",
        "params": {
            "query": {"type": "string", "description": "Search query string."},
            "count": {
                "type": "integer",
                "description": "Number of results to return (default 8, max 20).",
            },
            "kind": {
                "type": "string",
                "enum": ["web", "news"],
                "description": "'web' (default) or 'news' for recent news results.",
            },
        },
        "required": ["query"],
    },
    {
        "name": "image_search",
        "description": (
            "Search the web for existing images. Returns image URLs, source pages, and dimensions. "
            "For 'send/download me a picture of X in this chat', call this first, then pass one "
            "Image URL to send_media(path='<Image URL>'). Do not use generate_image for that request. "
            "Example: image_search(query='Rosé BLACKPINK')."
        ),
        "params": {
            "query": {"type": "string", "description": "Image search query."},
            "count": {
                "type": "integer",
                "description": "Number of images to return (default 8, max 20).",
            },
        },
        "required": ["query"],
    },
    {
        "name": "deep_research",
        "description": "Run multi-source research on one focused question: searches the web, reads the top sources, and returns a synthesized answer with inline [n] citations and a numbered sources list. It is slower than web_search and is not sufficient by itself for an exhaustive table across named providers; for those comparisons, run a separate web_search scoped to each provider's official domain. Example: deep_research(query='pros and cons of RAG vs fine-tuning in 2026').",
        "params": {
            "query": {"type": "string", "description": "The research question."},
            "depth": {
                "type": "integer",
                "description": "Research depth hint (0-2, default 1).",
            },
        },
        "required": ["query"],
    },
]


_TOOL_FAMILIES = {
    "capability_search": "capability",
    "read_file": "filesystem",
    "edit_file": "filesystem",
    "write_file": "filesystem",
    "create_spreadsheet": "spreadsheet",
    "calculate": "calculation",
    "delete_file": "filesystem",
    "list_dir": "filesystem",
    "search_files": "filesystem",
    "verify_files": "filesystem",
    "diagnose_files": "filesystem",
    "create_skill": "filesystem",
    "run_command": "command",
    "memory_search": "memory",
    "memory_save": "memory",
    "generate_image": "media",
    "send_discord_message": "discord",
    "send_discord_embed": "discord",
    "list_discord_channels": "discord",
    "spawn_agent": "agent",
    "get_task_output": "agent",
    "wait_tasks": "agent",
    "kill_task": "agent",
    "cron_add": "scheduler",
    "cron_list": "scheduler",
    "cron_remove": "scheduler",
    "google_search": "search",
    "web_search": "search",
    "image_search": "search",
    "deep_research": "search",
    "send_media": "media",
    "send_voice": "media",
    "analyze_video": "video",
    "browser_navigate": "browser",
    "browser_click": "browser",
    "browser_download": "browser",
    "browser_type": "browser",
    "browser_snapshot": "browser",
    "browser_scroll": "browser",
    "browser_wait": "browser",
    "browser_press_key": "browser",
    "browser_go_back": "browser",
    "browser_tabs": "browser",
    "browser_switch_tab": "browser",
    "browser_extract": "browser",
    "browser_get_page_text": "browser",
    "browser_list_media": "browser",
}

_FAMILY_HINTS = {
    "capability": {
        "capability",
        "capabilities",
        "integration",
        "integrations",
        "skill",
        "skills",
        "tool",
        "tools",
        "jira",
        "mcp",
        "connected",
        "available",
        "disponible",
        "integracion",
        "integración",
        "conexion",
        "conexión",
    },
    "filesystem": {
        "file",
        "files",
        "archivo",
        "archivos",
        "fichero",
        "ficheros",
        "folder",
        "folders",
        "carpeta",
        "carpetas",
        "directory",
        "directories",
        "path",
        "paths",
        "ruta",
        "rutas",
        "repo",
        "repository",
        "repositorio",
        "code",
        "codigo",
        "project",
        "proyecto",
        "source",
        "excel",
        "xlsx",
        "csv",
        "pdf",
    },
    "spreadsheet": {
        "excel",
        "xlsx",
        "spreadsheet",
        "workbook",
        "worksheet",
        "hoja",
        "libro",
    },
    "calculation": {
        "calculate",
        "calculator",
        "calculation",
        "math",
        "total",
        "cost",
        "price",
        "pricing",
        "percent",
        "percentage",
        "calcula",
        "calcular",
        "calculadora",
        "costo",
        "precio",
        "porcentaje",
    },
    "command": {
        "command",
        "terminal",
        "shell",
        "script",
        "scripts",
        "bash",
        "powershell",
        "python",
        "pytest",
        "git",
        "npm",
        "node",
        "pip",
        "exec",
        "run",
        "ejecuta",
        "ejecutar",
        "procesa",
        "procesar",
        "convierte",
        "convertir",
        "exporta",
        "exportar",
        "descarga",
        "descargar",
    },
    "browser": {
        "website",
        "browser",
        "page",
        "pages",
        "url",
        "click",
        "form",
        "scrape",
        "article",
        "open",
        "navigate",
        "sitio",
        "pagina",
        "navegador",
        "clic",
        "abrir",
        "visitar",
        "navegar",
        "calculadora",
    },
    "search": {
        "web",
        "search",
        "google",
        "news",
        "research",
        "internet",
        "lookup",
        "buscar",
        "busca",
        "investigar",
        "investiga",
        "investigue",
        "investigacion",
        "fuentes",
    },
    "scheduler": {
        "remind",
        "reminder",
        "schedule",
        "scheduled",
        "cron",
        "tomorrow",
        "later",
        "daily",
        "weekly",
        "monthly",
        "every",
    },
    "memory": {
        "memory",
        "remember",
        "remembered",
        "recall",
        "history",
        "journal",
        "past",
    },
    "agent": {
        "delegate",
        "delegated",
        "background",
        "parallel",
        "subagent",
        "complex",
        "long",
    },
    "media": {
        "image",
        "images",
        "picture",
        "pictures",
        "photo",
        "pic",
        "pics",
        "download",
        "send",
        "draw",
        "render",
        "art",
        "illustration",
        "voice",
        "audio",
        "attach",
        "imagen",
        "imagenes",
        "captura",
        "capturas",
        "adjuntar",
        "foto",
        "fotos",
    },
    "discord": {
        "discord",
        "dm",
        "dms",
        "direct",
        "message",
        "channel",
        "guild",
        "server",
        "user",
    },
    "video": {
        "video", "watch", "transcript", "captions", "screen", "recording",
        "youtube", "youtu", "vimeo", "tiktok", "loom", "mp4", "mov", "mkv", "webm", "m4v", "avi",
    },
}

_MANDATORY_FAMILY_TOOLS = {
    "capability": {"capability_search"},
    "filesystem": {"search_files", "read_file", "list_dir"},
    "command": {"run_command"},
    "browser": {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_wait",
    },
    "search": {"web_search", "image_search"},
    "scheduler": {"cron_add", "cron_list", "cron_remove"},
    "memory": {"memory_search", "memory_save"},
    "agent": {"spawn_agent", "get_task_output", "wait_tasks", "kill_task"},
    "media": {"send_media", "image_search"},
    "discord": {"send_discord_message", "send_discord_embed", "list_discord_channels"},
    "video": {"analyze_video"},
    "spreadsheet": {"create_spreadsheet", "send_media"},
    "calculation": {"calculate"},
}

_TOOL_HINTS = {
    "capability_search": {
        "capability", "capabilities", "integration", "integrations", "skill", "skills",
        "tool", "tools", "jira", "mcp", "available", "connected", "resolve", "lookup",
        "disponible", "integracion", "integración", "conexion", "conexión",
    },
    "read_file": {"read", "open", "show", "file", "contents", "content"},
    "edit_file": {
        "edit", "patch", "modify", "change", "replace", "fix", "refactor", "file", "code",
    },
    "write_file": {"write", "edit", "save", "create", "overwrite", "file"},
    "create_spreadsheet": {
        "excel", "xlsx", "spreadsheet", "workbook", "worksheet", "table", "hoja", "libro",
    },
    "calculate": {
        "calculate", "calculator", "math", "total", "cost", "price", "percent",
        "calcula", "calcular", "calculadora", "costo", "precio", "porcentaje",
    },
    "delete_file": {"delete", "remove", "erase", "cleanup"},
    "list_dir": {"list", "dir", "directory", "folder", "files", "browse"},
    "search_files": {"search", "find", "grep", "rg", "ripgrep", "match", "locate"},
    "verify_files": {"verify", "validate", "check", "syntax", "test", "lint", "whitespace", "conflict"},
    "diagnose_files": {"diagnose", "diagnostics", "lint", "linter", "typecheck", "type-check", "pyright", "ruff", "eslint", "tsc", "language-server", "lsp"},
    "run_command": {"run", "command", "terminal", "shell", "script", "git", "pytest", "npm", "python"},
    "memory_search": {"memory", "remember", "recall", "history", "journal"},
    "memory_save": {"memory", "remember", "save", "journal", "fact", "preference"},
    "generate_image": {"draw", "render", "generate", "art", "create", "imagine", "paint", "dalle"},
    "send_discord_message": {"discord", "dm", "direct", "message", "send", "user", "channel"},
    "send_discord_embed": {"discord", "embed", "structured", "send", "channel", "dm"},
    "list_discord_channels": {"discord", "channels", "guild", "server", "list"},
    "spawn_agent": {"delegate", "background", "subagent", "parallel"},
    "get_task_output": {"task", "subagent", "background", "output", "status", "result", "poll"},
    "wait_tasks": {"task", "subagent", "background", "wait", "finish", "complete"},
    "kill_task": {"task", "subagent", "background", "cancel", "stop", "kill"},
    "cron_add": {"remind", "schedule", "later", "daily", "weekly", "every"},
    "cron_list": {"scheduled", "reminders", "jobs", "cron"},
    "cron_remove": {"cancel", "remove", "delete", "scheduled", "reminder"},
    "create_skill": {"skill", "scaffold", "template"},
    "google_search": {"google", "search", "web", "website", "results"},
    "web_search": {"search", "web", "google", "find", "lookup", "news", "results", "internet"},
    "image_search": {
        "image", "images", "picture", "photo", "pic", "pics", "photos",
        "find", "download", "foto", "imagen",
    },
    "deep_research": {
        "research", "investigate", "deep", "compare", "analysis", "report", "sources", "cite",
        "investigar", "investiga", "investigue", "investigacion", "fuentes", "citar",
    },
    "send_media": {
        "send", "share", "picture", "photo", "pic", "image", "file", "attach",
        "download", "chat", "foto", "imagen",
    },
    "send_voice": {"voice", "audio", "speak", "say", "voicenote", "tts", "read", "aloud", "message"},
    "analyze_video": {"video", "watch", "transcript", "caption", "youtube", "youtu", "vimeo", "tiktok", "loom", "mp4", "mov", "mkv", "webm", "m4v", "avi", "recording"},
    "browser_navigate": {"url", "open", "visit", "navigate", "website", "web"},
    "browser_snapshot": {"snapshot", "page", "elements", "buttons", "form"},
    "browser_click": {"click", "press", "tap", "select"},
    "browser_download": {
        "download", "export", "save", "file", "excel", "xlsx", "csv", "pdf",
        "descarga", "descargar", "exporta", "exportar", "guardar",
    },
    "browser_type": {"type", "enter", "fill", "input", "search"},
    "browser_wait": {"wait", "loading", "load"},
    "browser_extract": {"extract", "article", "text", "table", "content", "scrape"},
    "browser_get_page_text": {"read", "text", "page", "article"},
    "browser_list_media": {"image", "images", "photo", "photos", "media"},
}


def _tokenize(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return {
        token
        for token in re.findall(r"[a-z0-9_./:-]+", normalized)
        if len(token) >= 2
    }


def shortlist_tool_definitions(
    tool_defs: List[Dict[str, Any]],
    user_text: str,
    max_tools: int = 12,
    required_tool_names: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Return a coherent subset of tools for the current user turn."""
    text = (user_text or "").strip()
    if not text or len(tool_defs) <= max_tools:
        return tool_defs

    lowered = text.lower()
    tokens = _tokenize(text)
    selected_families = set()
    media_delivery = is_chat_media_delivery(text) and not is_image_generation_request(text)

    for family, hints in _FAMILY_HINTS.items():
        if tokens & hints:
            selected_families.add(family)

    if media_delivery:
        selected_families.update({"media", "search"})
        selected_families.discard("command")
        selected_families.discard("agent")
        selected_families.discard("capability")

    # Exporting an existing workbook from a website needs browser_download, not
    # the native workbook creator. Keep the spreadsheet family for explicit
    # create/build/generate requests.
    if (
        "spreadsheet" in selected_families
        and "browser" in selected_families
        and tokens & {"export", "exportar", "download", "descargar", "descarga"}
        and not tokens
        & {
            "create",
            "build",
            "generate",
            "make",
            "crear",
            "crea",
            "generar",
            "genera",
            "construir",
            "nuevo",
            "nueva",
        }
    ):
        selected_families.discard("spreadsheet")

    if any(marker in lowered for marker in ("http://", "https://", "www.")):
        selected_families.add("browser")
    artifact_tokens = {
        "download", "downloaded", "export", "exported", "spreadsheet", "excel",
        "xlsx", "csv", "screenshot", "capture", "attach", "descarga", "descargar",
        "descargado", "exporta", "exportar", "exportado", "hoja", "captura", "adjuntar",
    }
    if "browser" in selected_families and tokens & artifact_tokens:
        # A browser artifact is not complete at the click: the agent must be able
        # to locate, inspect or transform the download, then deliver it.
        selected_families.update({"filesystem", "command", "media"})
    if any(
        marker in lowered
        for marker in (
            ".py", ".ts", ".js", ".md", ".json", ".xlsx", ".xls", ".csv", ".pdf",
            ".docx", ".png", ".jpg", "./", "../", ".\\", "..\\",
        )
    ):
        selected_families.add("filesystem")
    if any(marker in lowered for marker in ("git ", "pytest", "npm ", "python ", "bash", "powershell", "cmd ")):
        selected_families.add("command")

    available_names = {
        str(tool.get("function", {}).get("name") or "") for tool in tool_defs
    }
    explicitly_required = {
        str(name).strip()
        for name in (required_tool_names or [])
        if str(name).strip() in available_names
    }

    mandatory = set(explicitly_required)
    # A skill that is already carrying required tools is an active capability
    # context. Keep the recovery lookup beside that context even when the
    # current follow-up is only an acknowledgement and contains no task noun.
    if explicitly_required and "capability_search" in available_names:
        mandatory.add("capability_search")
    for family in selected_families:
        mandatory.update(_MANDATORY_FAMILY_TOOLS.get(family, set()))
    if "filesystem" in selected_families and tokens & {
        "edit", "patch", "modify", "change", "fix", "refactor", "implement",
        "editar", "parche", "modificar", "cambiar", "corregir", "refactorizar",
        "implementar",
    }:
        mandatory.add("edit_file")
    if "browser" in selected_families and tokens & artifact_tokens:
        mandatory.add("browser_download")

    scored: list[tuple[int, str, Dict[str, Any]]] = []
    for tool in tool_defs:
        function = tool.get("function", {})
        name = function.get("name", "")
        family = _TOOL_FAMILIES.get(name, "other")
        hints = set(_TOOL_HINTS.get(name, set()))
        hints.update(_tokenize(name.replace("_", " ")))
        score = len(tokens & hints)

        if family in selected_families:
            score += 8
        if name in mandatory:
            score += 20
        if name in explicitly_required:
            score += 100
        if name in lowered:
            score += 50

        if name == "run_command" and "command" not in selected_families and selected_families:
            score -= 10
        if media_delivery:
            if name in CHAT_MEDIA_TOOLS or name in CHAT_MEDIA_SUPPORTING_TOOLS:
                score += 40
            if name in CHAT_MEDIA_BLOCKED_TOOLS:
                score -= 50

        scored.append((score, name, tool))

    scored.sort(key=lambda item: (-item[0], item[1]))

    if scored and scored[0][0] <= 0:
        return tool_defs

    selected_names = []
    blocked = set(CHAT_MEDIA_BLOCKED_TOOLS) if media_delivery else set()
    for _, name, _ in scored:
        if name in blocked:
            continue
        if name in mandatory and name not in selected_names:
            selected_names.append(name)
        if len(selected_names) >= max_tools:
            break

    for score, name, _ in scored:
        if score <= 0:
            continue
        if name in blocked:
            continue
        if name not in selected_names:
            selected_names.append(name)
        if len(selected_names) >= max_tools:
            break

    selected_set = set(selected_names)
    shortlisted = [
        tool for tool in tool_defs if tool.get("function", {}).get("name") in selected_set
    ]
    return shortlisted or tool_defs


def _expand_param(name: str, schema) -> dict:
    """Expand shorthand param ('string') into a full JSON Schema property dict."""
    if isinstance(schema, str):
        return {"type": schema, "description": f"The {name}."}
    return schema


def _inflate_tool(tool_def: dict) -> dict:
    """Inflate a compact tool definition into the OpenAI function-calling format."""
    properties = {
        name: _expand_param(name, schema)
        for name, schema in tool_def.get("params", {}).items()
    }
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def["description"],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": tool_def.get("required", []),
            },
        },
    }


def _build_spawn_agent_definition(
    available_agents: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    base = next(
        copy.deepcopy(tool_def)
        for tool_def in BASE_TOOLS
        if tool_def["name"] == "spawn_agent"
    )
    description = (
        "Optional named subagent profile to use. "
        "Choose one when the task clearly matches that specialist's description. "
        "If omitted, LimeBot uses the generic built-in worker."
    )
    if available_agents:
        summary = "; ".join(
            f"{name}: {text}"
            for name, text in sorted(available_agents.items())
        )
        description += f" Available subagents: {summary}"
        base["params"]["agent"] = {
            "type": "string",
            "description": description,
            "enum": sorted(available_agents),
        }
    else:
        base["params"]["agent"] = {
            "type": "string",
            "description": description,
        }
    base["params"]["background"] = {
        "type": "boolean",
        "description": (
            "Optional background override. If true, start the subagent and return "
            "immediately. If omitted, the subagent profile decides."
        ),
    }
    return base


def build_tool_definitions(
    enabled_skills: List[str],
    available_agents: Dict[str, str] | None = None,
    search_available: bool = False,
) -> List[Dict[str, Any]]:
    """
    Build the full list of tool definitions for the LLM.

    Args:
        enabled_skills: List of enabled skill names from config.
        available_agents: Named subagent profiles for spawn_agent.
        search_available: True when a search API key is configured. Search tools
            are always registered because keyless DuckDuckGo is available even
            without a paid key or the browser skill.

    Returns:
        List of OpenAI-compatible tool definition dicts.
    """
    tools: List[Dict[str, Any]] = []
    for tool_def in BASE_TOOLS:
        if tool_def["name"] == "spawn_agent":
            tools.append(
                _inflate_tool(
                    _build_spawn_agent_definition(available_agents=available_agents)
                )
            )
        else:
            tools.append(_inflate_tool(tool_def))

    browser_enabled = "browser" in enabled_skills

    # search_available is retained for callers; DuckDuckGo needs no key.
    _ = search_available
    tools.extend(_inflate_tool(t) for t in SEARCH_TOOLS)

    if browser_enabled:
        tools.extend(_inflate_tool(t) for t in BROWSER_TOOLS)

    return tools
