---
name: browser
description: Control a real, local web browser to search, navigate, and extract information.
dependencies:
  python: []
  node: []
  binaries: []
---

# Web Browser 🌐
LimeBot's window to the live internet. It uses a local instance of Chrome/Chromium to interact with websites just like a human.

### Core Commands:
- `web_search(query, count, kind)`: Preferred web search. `kind='news'` for recent news. Returns ranked titles/URLs/snippets.
- `image_search(query)`: Find images. Returns Image URLs + source pages.
- `deep_research(query)`: Multi-source research with a cited synthesized answer. Use for questions needing several sources.
- `browser_navigate(url)`: Open a page. Returns the page title and a list of interactive elements with IDs (e.g., `[e12]`).
- `browser_click(element_id)`: Interact with buttons or links using the IDs from the navigation result.
- `browser_download(element_id=..., dest=..., timeout_ms=...)`: Click a Download/Export control and save the file under `temp/`. For a direct file URL use `url=` instead of `element_id`. Raise `timeout_ms` for large ISOs.
- `browser_type(element_id, text)`: Fill out forms and search bars.
- `browser_scroll(direction='down')`: Move through a page to reveal more content.
- `browser_extract(selector='body')`: Get the text content of a page.
- `google_search(query)`: Legacy alias for `web_search`.

If a browser tool says Playwright is missing, tell the user to run
`npm run lime-bot setup -- --recommended` once. Do not invent a successful browse.

### Strategy:
1. **Search** (`web_search` / `image_search` / `deep_research`) or **Navigate**.
2. **Snapshot** to see the elements.
3. **Click** or **Type** to interact.
4. **Extract** the final information needed.

### Sending a picture to the user:
When the user asks you to send/show a picture of something, call `image_search(query=...)`,
pick the best result, then call `send_media(path='<Image URL>')`. `send_media` downloads the
remote URL and delivers it as a real image in web, Discord, and WhatsApp — do **not** just paste
the raw URL as text.
