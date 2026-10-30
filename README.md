# Btechnics VIGI

Home Assistant-integratie voor een TP-Link VIGI NVR via de VIGI NVR OpenAPI (v1.4).

- Instelscherm met IP, gebruiker en wachtwoord (OpenAPI aan in de recorder: Settings > Network > OpenAPI, poort 20443).
- Camera per kanaal: live via de substream, beeld via /openapi/snapshot.
- Verbinding per kanaal.
- Opnames (enkel beheerders): WebSocket `btechnics_vigi/record/days`, `record/search`, `replay` (HLS via de stream-integratie).
