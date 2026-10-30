# Btechnics VIGI

Home Assistant-integratie voor een TP-Link VIGI NVR via de VIGI NVR OpenAPI (v1.4) en RTSP.

- Instelscherm (IP, gebruiker, wachtwoord), opnieuw aanmelden en opnieuw configureren.
- Dashboardkaart `custom:btechnics-vigi-card` (enkel beheerders): live raster en live beeld, opnames met kalender
  (puntjes op dagen met opnames), uren en zoombare tijdlijn, speler met spoelen, 0,5x tot 16x, beeld per beeld en
  download als mp4 (elke download in het logboek).
- Afspelen: de recorder levert opnames op normale snelheid en kent max 4 afspeelsessies (gemeten). Daarom kijk je
  meteen via een stream terwijl HA het stuk in 3 delen tegelijk als mp4 binnenhaalt (zonder omzetten).
- Verbinding per kanaal als binary_sensor. Camera-entiteiten optioneel (standaard uit).
- Tests: `../venv/bin/python -m pytest -q` en `node tests/js/card.test.mjs`.
