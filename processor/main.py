#!/usr/bin/env python3
"""Annotation exports in, interchange NDJSON out.

One processor per source format, all writing the same shape, so the TS-Zarr
writer never learns what a Natus export or an IEEG portal is. It takes NWB plus
these files and turns each one into an event channel.

This runs in a branch parallel to the signal conversion, so it cannot read the
NWB and cannot know the recording onset. It only declares which clock its
numbers are on; the writer converts.
"""

import logging
import sys
from pathlib import Path

from processor.config import Config
from processor.records import output_path, write_ndjson
from processor.sites import ieeg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

SITES = {"ieeg": ieeg.convert}


def main() -> int:
    """Convert every export in INPUT_DIR and return a process exit code."""
    config = Config()
    input_dir = Path(config.INPUT_DIR).resolve()
    output_dir = Path(config.OUTPUT_DIR).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    convert = SITES.get(config.SITE)
    if convert is None:
        log.error("unknown SITE %r; known sites: %s", config.SITE, sorted(SITES))
        return 2

    # Every export the site reads, whatever else the workflow put beside them.
    paths = sorted(
        path
        for pattern in ("*.csv", "*.xml")
        for path in input_dir.glob(pattern)
    )
    if not paths:
        log.error("no *.csv or *.xml in %s", input_dir)
        return 2

    log.info("site=%s  inputs=%d  -> %s", config.SITE, len(paths), output_dir)

    written = 0
    seen: dict[str, Path] = {}
    for path in paths:
        for header, marks in convert(
            path,
            duplicate_rule=config.DUPLICATE_RULE,
            json_bodies=config.JSON_BODIES,
            source_prefix=config.SOURCE_PREFIX,
        ):
            target = output_path(output_dir, header.id)
            if header.id in seen:
                # Fail here rather than let the workflow's branch merge do it:
                # this message names both files, that one names neither.
                log.error(
                    "channel id %r produced twice, by %s and %s; ids must be "
                    "unique or the branch merge fails the run",
                    header.id,
                    seen[header.id].name,
                    path.name,
                )
                return 1
            seen[header.id] = path
            count = write_ndjson(target, header, marks)
            written += 1
            log.info("wrote %s (%d marks)", target.name, count)

    if not written:
        log.error("no annotations found in any input")
        return 1
    log.info("done: %d annotation channel(s)", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
