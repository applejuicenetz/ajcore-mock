# appleJuice Core Mock

Lokaler Testdienst für Anwendungen, die die HTTP/XML-API des appleJuice Core nutzen. Liefert synthetische Downloads, Uploads, Freigaben, Suchergebnisse und Server. Keine Verbindung zu echten Netzwerken, keine Dateiübertragung.

## Start

Python 3.12+, keine zusätzlichen Pakete.

```sh
python3 src/mock_core.py
```

Core-Adresse im Client: `http://127.0.0.1:19851`, Passwort leer. Ein Neustart setzt den Zustand zurück.

Docker:

```sh
docker run --rm -p 127.0.0.1:19851:19851 ghcr.io/applejuicenetz/core-mock:latest
```

## Optionen

```sh
python3 src/mock_core.py --scenario busy          # busy, empty, firewalled, disconnected
python3 src/mock_core.py --port 19852 --password test
python3 src/mock_core.py --shareidx-bytes 0       # keine zusätzlichen Katalogdateien
python3 src/mock_core.py --iso-count 0            # keine synthetischen ISOs
python3 src/mock_core.py --shareidx-output runtime/shareidx.xml
```

`--help` zeigt alle Optionen. Prüfsummen und Share-Index (Standard 3,5 MB) sind deterministische Testwerte, Dateiinhalte werden nicht erzeugt.

## Sicherheit

Nur für Tests. Lauscht standardmäßig nur auf `127.0.0.1`, nicht öffentlich betreiben. Serveradressen sind reservierte `.example`-Adressen.

