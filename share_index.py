"""Generate deterministic Core-style share indexes and matching API metadata."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from xml.sax.saxutils import quoteattr

BLOCK_SIZE = 1048576
DEFAULT_INDEX_BYTES = 3500000  # Decimal MB, not MiB.


def file_record(share: dict) -> bytes:
    """Use the on-disk index format; subhashes are absent from the HTTP share API."""
    opening = (
        f'<file name={quoteattr(share["filename"])} checksum={quoteattr(share["checksum"])} '
        f'size="{share["size"]}" lastModified="1700000000000">\n'
    )
    hashes = (
        f'<subhash checksum="{hashlib.md5(f"{share["id"]}:{block}".encode()).hexdigest()}"/>\n'
        for block in range(math.ceil(share['size'] / BLOCK_SIZE))
    )
    return (opening + ''.join(hashes) + '</file>\n').encode('utf-8')


def populate(state, target_bytes: int = DEFAULT_INDEX_BYTES) -> bytes:
    """Add synthetic shares until a valid index reaches exactly target_bytes.

    Existing scenario objects retain their IDs. Additional files live in a separate
    shared directory so clients can test a large catalog without one huge folder.
    The final whitespace padding is less than one generated file record.
    """
    header = b'<?xml version="1.0" encoding="UTF-8"?>\n<database>\n'
    footer = b'</database>\n'
    records = [file_record(share) for share in state.shares.values()]
    used = len(header) + len(footer) + sum(map(len, records))
    if target_bytes < used:
        raise ValueError(f'Index target {target_bytes} is smaller than scenario minimum {used}')
    directory = '/mock/catalog'
    number = 0
    while True:
        name = f'sample-{number:06d}.bin'
        share = {
            'id': state.next_id + 1, 'filename': f'{directory}/{name}', 'short': name,
            'size': 64 * BLOCK_SIZE, 'checksum': hashlib.md5(name.encode()).hexdigest(),
            'priority': 1, 'lastasked': 0, 'askcount': number % 17, 'searchcount': number % 11,
        }
        record = file_record(share)
        if used + len(record) > target_bytes:
            break
        state.shares[state.new_id()] = share
        records.append(record)
        used += len(record)
        number += 1
    if number and (directory, 'subdirectory') not in state.share_dirs:
        state.share_dirs.append((directory, 'subdirectory'))
    return header + b''.join(records) + b' ' * (target_bytes - used) + footer


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
