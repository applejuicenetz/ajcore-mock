# core-mock

## Scope

- Generic, standalone synthetic appleJuice Core HTTP/XML service for any API client. Do not couple behavior to phpGUI or another consumer.
- Code, identifiers, docstrings and inline comments are English. README is German user documentation; technical maintenance instructions belong here.
- Never contact real Core/server instances or public P2P services. Fixture server addresses use reserved `.example` domains; IPs use documentation ranges.
- Preserve stable scenario IDs so existing integration tests can address known objects.

## API contract

- The planned separate appleJuiceNETZ OpenAPI repository is the future contract reference. Its name/URL is not established yet: do not invent a link. Add it when confirmed.
- Supported endpoints are implemented in `Handler.route`; unsupported operations are not proof of full Core compatibility. Add contract and regression tests with behavior changes.

## Contract source

- Never reference Core source code in code, comments, docstrings, tests, README or this file: no Core class, method or file names (e.g. `XmlServer`, `Download.setStatus`, `AJClient`) and no paths into `core-src`. Describe the observable API behavior instead.
- All objects share one ID space: shares, downloads, users, uploads, servers, searches, search entries, `information`, `networkinfo`. Every reference (`shareid`, `downloadid`, `searchid`) must resolve; a download `shareid` is `-1` until a part-file share exists (`State.attach_part_share`).
- Uploads only use status 1 (active) and 2 (queued). Queued slots have positions `-1` and speed 0; `loaded` is a percent (`-1` unknown). Hashes come from `file_hash(name, size)`.
- `modified.xml`: no `session` returns full `ids`; a valid `session` (from `getsession.xml`, bound to client IP, expires after `SESSION_TTL`) returns the `removed` queue, which reading clears. `timestamp` filters with `>=`. `sync_objects` stamps changes and fills the queues.
- `mode=zip` is zlib. Auth failure is an empty 302 to `/wrongpassword`, unknown path 302 to `/help`, bad session 302 to `/wrongsession`.

## Searches

- Finished fixture searches (`debian`) never change. A search started through the API is running and delivers one synthetic result every `SEARCH_RESULT_INTERVAL` seconds (3 s) until `SEARCH_RESULTS` (4) results exist. Then it finishes and `opensearches` is 0. `cancelsearch` stops delivery.
- Progress is driven by wall-clock time in `State.advance_searches`, called from `tick`. Tests move `started` into the past instead of sleeping.

## Share index

- `src/share_index.py` generates a deterministic Core-style `<database><file ...><subhash .../></file></database>` fixture.
- Default target: 3,500,000 bytes (decimal MB). Small trailing whitespace padding gives an exact byte count; document it rather than treating padding as additional metadata.
- Index records and `/xml/share.xml` refer to the same synthetic shares. Subhashes appear only in the index, not the HTTP API. Hashes do not represent real files.
- Generated indexes go under ignored `runtime/` or a user-selected output path. Do not commit large generated fixtures.

## Verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 src/mock_core.py --help
```

The service defaults to loopback-only port 19851, empty password and in-memory state. Startup can export the generated index with `--shareidx-output`. Restart resets state.

## GitHub

Repository: `applejuicenetz/core-mock`. Inspect status before changes; no unrelated files or credentials in commits. Publishing requires explicit user authorization.
