"""Constanten voor Btechnics VIGI (TP-Link VIGI NVR via OpenAPI)."""
DOMAIN = "btechnics_vigi"
OPENAPI_PORT = 20443
RTSP_PORT = 554
SCAN_INTERVAL_S = 60
# Opnames: enkel hoofdstream (= storage stream op de recorder), zie VIGI NVR OpenAPI v1.4 hoofdstuk 5.3
REPLAY_STREAM = 1
LIVE_STREAM = 2  # substream, H.264 sinds 30/10/2026
MAX_REPLAY_S = 4 * 3600
