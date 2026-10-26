# Official bridge regression fixtures

These unmodified AstrBot SDK files exercise the real host `postMessage` protocol
and the script insertion order used by `PluginPageService`.

- `astrbot-plugin-page-v4.27.5.js`: [AstrBot v4.27.5](https://github.com/AstrBotDevs/AstrBot/blob/v4.27.5/astrbot/dashboard/plugin_page_bridge.js).
- `astrbot-plugin-view-v4.29.3.js`: AstrBot master retrieved 2026-10-26, version 4.29.3. It exports `AstrBotPluginView` and the compatible `AstrBotPluginPage` alias.

Source: AstrBotDevs/AstrBot, GNU AGPL v3, the same license as this repository.
These scripts are used only in browser tests; the dashboard uses the host SDK.
