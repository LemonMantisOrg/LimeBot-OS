---
name: browser
description: Control a real, local web browser to navigate pages and extract information. Search is a separate host-owned web_search tool.
dependencies:
  python: []
  node: []
  binaries: []
---

# Web Browser 🌐
LimeBot's window to the live internet. It uses a local instance of Chrome/Chromium to interact with websites just like a human.

Search is **host-owned**. Call `web_search` — do not open Google or another search engine with browser tools.

### Core Commands:
- `web_search(query, count, kind)`: Host-owned search. `kind='web'|'news'|'images'`. For a send-photo request use `kind='images'`; the host attaches the image.
- `browser_navigate(url)`: Open a page you already have a URL for. Returns the page title and interactive elements with IDs (e.g., `[e12]`).
- `browser_act(action, ...)`: `snapshot`, `click`, `type`, `scroll`, `wait`, `press`, `back`, `tabs`, `switch_tab`, or `download`.
- `browser_extract(mode, selector)`: `mode='text'` (default) or `mode='media'`.

If a browser tool says Playwright is missing, tell the user to run
`npm run lime-bot setup -- --recommended` once. Do not invent a successful browse.

### Strategy:
1. **Search** with `web_search` (never by navigating to a search engine).
2. **Navigate** only when you already have a URL.
3. **Act** (`snapshot` then click/type/download) to interact.
4. **Extract** the final information needed.

### Sending a picture to the user:
When the user asks you to send/show a picture of something, call
`web_search(query=..., kind='images')` once and stop. The host downloads the best
image and attaches it. Do not call `send_media` or `generate_image` for that request.
