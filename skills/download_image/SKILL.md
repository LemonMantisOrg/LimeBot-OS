---
name: download_image
description: A robust, "Honey Badger" image downloader that bypasses strict CDNs, ignores misleading Content-Types, and falls back to scraping if a direct link fails.
dependencies:
  python:
    - requests
    - beautifulsoup4
  node: []
  binaries: []
---

# Image Downloader (Honey Badger Edition) 🖼️
Use this skill only when a **direct image URL or page URL is already known** and native `send_media` cannot fetch it (strict CDN, HTML interstitial, wrong Content-Type).

For ordinary chat requests such as "download a picture of X and send it in this chat", do **not** use this skill. Call native `web_search(query=..., kind="images")` once and stop — the host attaches the photo. Do not use `run_command`, `send_media`, or markdown image links for that path.

### 🛡️ Robust Features:
1.  **Byte Sniffing**: Ignores `Content-Type` headers (often `binary/octet-stream`) and checks the file signature (magic numbers) to detect JPEGs, PNGs, GIFs, and WEBPs.
2.  **Smart Fallback**: If the URL returns HTML instead of an image, it automatically switches to scraping mode to find the high-res `og:image` or `twitter:image` tags.
3.  **Stealth Mode**: Uses a modern Chrome User-Agent to bypass basic anti-bot protections.

### 🚀 Execution Command:
`run_command("python skills/download_image/main.py '<url>' 'temp/<filename>'")`

### 📋 Requirements:
- **URL**: A direct image link OR a page URL (Reddit, Pinterest, 4KWallpapers, etc.).
- **Filename**: Descriptive name (e.g., `lisa_figaro.jpg`). **Always save to `temp/`.**

### 📤 Output Handling:
To show the image to the user, use a concise description: `![image](temp/filename.jpg)`.

> [!WARNING]
> DO NOT wrap your entire response inside the `![alt-text]` part of the image tag. Keep the alt-text short (e.g., "image" or "Lisa photo").

### ⚠️ Dependencies:
- `requests`
- `beautifulsoup4`
