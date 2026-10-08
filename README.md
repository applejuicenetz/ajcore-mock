# appleJuice Core Mock

Lokaler Testdienst für Anwendungen, die die HTTP/XML-API des appleJuice Core verwenden. Der Mock liefert synthetische Downloads, Uploads, Freigaben, Suchergebnisse und Server. Er verbindet sich mit keinem echten Filesharing-Netzwerk und überträgt keine Dateien.

## Start

Benötigt wird Python 3.12 oder neuer. Keine zusätzlichen Pakete nötig.

```sh
python3 mock_core.py
```

Core-Adresse im Client: `http://127.0.0.1:19851`. Passwort leer lassen.

Aktionen ändern den Zustand des Mocks; aktive Downloads schreiten mit der Zeit voran. Ein Neustart setzt die Daten zurück.

## Szenarien und Optionen

```sh
python3 mock_core.py --scenario busy
python3 mock_core.py --scenario empty --shareidx-bytes 0
python3 mock_core.py --scenario disconnected
python3 mock_core.py --port 19852 --password test
```

Szenarien: `busy`, `empty`, `firewalled`, `disconnected`. Mit `--shareidx-bytes 0` entfallen die zusätzlichen Katalogdateien. `--help` zeigt alle Optionen.

Standardmäßig wird ein synthetischer Share-Index von **3.500.000 Bytes (3,5 MB)** modelliert. Die darin enthaltenen Dateimetadaten stehen über `/xml/share.xml` bereit. Der Index enthält zusätzlich Subhashes; die API-Antwort ist deshalb deutlich kleiner als die Indexdatei.

Index als Datei erzeugen:

```sh
python3 mock_core.py --shareidx-output runtime/shareidx.xml
```

Dateiinhalte werden nicht erzeugt. Die Prüfsummen sind deterministische Testwerte, keine Prüfsummen real vorhandener Dateien.

## Sicherheit

Der Dienst ist nur für Tests gedacht. Er lauscht standardmäßig ausschließlich auf `127.0.0.1`. Nicht öffentlich betreiben und keine echten Zugangsdaten verwenden. Alle vorgegebenen Serveradressen sind reservierte `.example`-Adressen.

## API-Dokumentation

Ein separates **OpenAPI-Repository von appleJuiceNETZ ist geplant**. Dessen endgültiger Repositoryname und URL stehen noch nicht fest; ein konkreter Link wird ergänzt, sobald es angelegt ist.

