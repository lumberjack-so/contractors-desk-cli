# Changelog

## v0.1.1

- Fix `/test` endpoint MCP reachability check: send a proper `initialize` JSON-RPC request with correct headers and validate the response, instead of relying on cached session state

## v0.1.0

Initial release.

- WebSocket relay between browser and OpenAI Realtime API
- Voice widget at `/voice` with mic capture and PCM16 playback
- Tool bridge to Cratchit MCP server (Gmail, Calendar, delegate)
- Health and connectivity test endpoints
- Systemd service unit
- Server VAD turn detection with 500ms silence threshold
