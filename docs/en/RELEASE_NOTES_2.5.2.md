# WeChat Background MCP 2.5.2

**Release date:** 2026-09-29
**Platform:** Windows x64, CPython 3.12 x64
**Supported Weixin build:** 4.1.13.12 x64 with exact DLL hash validation

## Download

- [Windows x64 wheel](https://github.com/13954nb-hub/wechat-background-mcp/releases/download/v2.5.2/wechat_background_mcp-2.5.2-cp312-cp312-win_amd64.whl)
- Size: 554,022 bytes
- SHA-256: **95df0b182ba67f85664a592acba70a1c6c920e56f8b86cb9466759b4ee278cd7**
- Weixin client installers are not redistributed. See [official download and version notes](GETTING_STARTED.md#weixin-client-version-and-download).

## Changes

- Correct minimized-window layout calibration while retaining strict root/render dimension and native-origin checks.
- Classify message sender direction only from exact account/contact evidence or the current visible UI row; ambiguous or unsupported cases remain unknown.
- Keep per-action accessibility, target, geometry, clipping, identity, and draft checks.
- Publish the matching native bridge manifest and 2.5.2 deployment guide inside the wheel.

## Verification

- Source suite: 1,263 passed, 2 dependency warnings, 1,084 subtests.
- Clean installed-wheel suite: 1,263 passed, 2 dependency warnings, 1,084 subtests.
- Synthetic geometry: 90 cases across window sizes, DPI scales, and desktop origins; the below-minimum layout failed closed.
- Post-restart live probe: five safe metadata/status/synthetic no-match calls passed; no real conversation content was read and no message or attachment was sent.

The 29 tool endpoints and 3 prompts are not a claim that all operations were exercised live. Real message reads, monitoring, sends, and attachment viewing were not part of this release check. See the [full installation guide](GETTING_STARTED.md) and [interface guide](INTERFACE_AND_DISPLAY.md).
