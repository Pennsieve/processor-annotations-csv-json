"""Site rules for the IEEG portal's annotation exports.

The portal exports annotations two ways and this module reads both. The CSV
export is one row per annotation-channel pair, which is how
ts_annotation_time_series stores the link. The XML export is one element per
annotation with the ts_annotation columns as attributes and no channel link at
all. Both become the same rows, and from there on nothing cares which file
they came from. Two things have to happen before the rows are an annotation
stream:

**Squash.** A mark on 68 channels arrives as 68 rows carrying the same
ts_annotation_id, timestamp and text. They are one annotation with 68 channels,
not 68 annotations.

**Dedupe.** The same Natus ENT file imported three times leaves three
ts_annotation rows per mark, with different ids and create times but the same
timestamp and text. Written through, a viewer draws each mark three times on
top of itself.

The imports are not always identical. On the measured HUP247 export one of the
three copies linked 60 channels rather than 68, dropping RY04 upward from one
electrode. So collapsing them to a single mark means choosing which channels
survive, which is why the default does not choose: copies that differ are all
kept. A mark drawn twice is visible and fixable; an electrode that quietly
vanished from an annotation is neither.
"""

import csv
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from processor.records import (
    JSON_MEDIA_TYPE,
    RECORDING_ONSET,
    TEXT_MEDIA_TYPE,
    ChannelHeader,
    Mark,
    slug,
    utc_now,
)

log = logging.getLogger(__name__)

EXTRACTOR = "ieeg-annotations"
VERSION = "1.1.0"

REQUIRED_COLUMNS = frozenset(
    {"annotation_id", "layer", "type", "time_us", "duration_us", "channel"}
)

XML_ROOT = "annotations"
XML_ELEMENT = "annotation"
XML_REQUIRED_ATTRIBUTES = frozenset(
    {"id", "type", "startOffsetUsecs", "endOffsetUsecs"}
)

# An EDF+ file carries one timekeeping stamp per data record, "+1.640000" for
# the record starting 1.64 s in. The portal exports them as annotations. They
# say nothing a reviewer wants to see.
TIMEKEEPING = re.compile(r"^[+-]\d+(\.\d+)?$")

EXACT = "exact"
FIRST = "first"
LAST = "last"
WIDEST = "widest"
DUPLICATE_RULES = (EXACT, FIRST, LAST, WIDEST)


@dataclass(frozen=True, slots=True)
class Row:
    """One exported row: one annotation seen on one channel."""

    annotation_id: str
    layer: str
    type: str
    description: str
    annotator: str
    created_at: str
    time_us: int
    duration_us: int
    channel: str


def read_rows(path: Path) -> Iterator[Row]:
    """Yield the export's rows, rejecting a file that is not this shape.

    A missing column is raised rather than defaulted, because every default
    here would be a silent wrong answer: a missing time is not zero and a
    missing channel list is not the whole recording.
    """
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{path.name} is missing columns {sorted(missing)}; "
                f"it has {sorted(reader.fieldnames or ())}"
            )
        for line in reader:
            yield Row(
                annotation_id=line["annotation_id"],
                layer=line["layer"] or "",
                type=line["type"] or "",
                description=(line.get("description") or "").strip(),
                annotator=(line.get("annotator") or "").strip(),
                created_at=(line.get("created_at") or "").strip(),
                time_us=int(line["time_us"]),
                duration_us=int(line["duration_us"] or 0),
                channel=(line["channel"] or "").strip(),
            )


def read_xml_rows(path: Path) -> Iterator[Row]:
    """Yield the portal's XML export as rows, one per annotation.

    The file is one <annotations> element holding one <annotation> per mark,
    with the ts_annotation columns as attributes: id, layer, type,
    description, startOffsetUsecs, endOffsetUsecs. The offsets already count
    microseconds from the recording onset. No channel link travels in this
    export, so every mark applies to the whole recording.

    EDF+ timekeeping stamps, whose type is a bare signed number of seconds
    with no description of its own, are dropped and counted in the log.

    Raises ValueError for a file that is not well-formed XML, whose root is
    not <annotations>, or whose annotation lacks a required attribute. A
    missing attribute is raised rather than defaulted because a missing time
    is not zero.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise ValueError(
            f"{path.name} is not well-formed XML: {error}"
        ) from error
    if root.tag != XML_ROOT:
        raise ValueError(
            f"{path.name} is not an annotation export; its root is "
            f"<{root.tag}>, expected <{XML_ROOT}>"
        )

    dropped = 0
    for element in root.iter(XML_ELEMENT):
        missing = XML_REQUIRED_ATTRIBUTES - element.attrib.keys()
        if missing:
            raise ValueError(
                f"{path.name}: an {XML_ELEMENT} is missing attributes "
                f"{sorted(missing)}; it has {sorted(element.attrib)}"
            )
        kind = element.get("type", "").strip()
        description = element.get("description", "").strip()
        if TIMEKEEPING.match(kind) and description in ("", kind):
            dropped += 1
            continue
        if len(element):
            log.warning(
                "%s: annotation %s has child elements %s, which this reader "
                "does not use",
                path.name,
                element.get("id"),
                sorted({child.tag for child in element}),
            )
        start_us = int(element.get("startOffsetUsecs", ""))
        end_us = int(element.get("endOffsetUsecs", ""))
        if end_us < start_us:
            log.warning(
                "%s: annotation %s ends %d us before it starts; written as a "
                "point event",
                path.name,
                element.get("id"),
                start_us - end_us,
            )
        yield Row(
            annotation_id=element.get("id", ""),
            layer=element.get("layer", "").strip(),
            type=kind,
            description=description,
            annotator=element.get("annotator", "").strip(),
            created_at=element.get("createTime", "").strip(),
            time_us=start_us,
            duration_us=max(0, end_us - start_us),
            channel="",
        )
    if dropped:
        log.info("%s: dropped %d EDF+ timekeeping stamps", path.name, dropped)


def read_export(path: Path) -> Iterator[Row]:
    """Yield the rows of a CSV or an XML export, by the file's suffix.

    Raises ValueError for any other suffix, naming the file.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_rows(path)
    if suffix == ".xml":
        return read_xml_rows(path)
    raise ValueError(
        f"{path.name} is neither a .csv nor an .xml export; "
        "the IEEG site reads those two"
    )


def squash(rows: Iterable[Row]) -> list[Mark]:
    """Collapse the per-channel rows into one mark per annotation id.

    Channels are sorted so two copies of the same mark compare equal whatever
    order the database returned them in. A row whose channel is empty
    contributes none, which is how the export says "the whole recording".
    """
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row.annotation_id, []).append(row)

    marks: list[Mark] = []
    for annotation_id, group in grouped.items():
        head = group[0]
        channels = sorted({row.channel for row in group if row.channel})
        marks.append(
            Mark(
                time_us=head.time_us,
                duration_us=head.duration_us,
                label=head.type or None,
                # The export repeats type as description on Natus imports;
                # writing it twice would put the same string in the viewer's
                # label and its body.
                text=(
                    head.description
                    if head.description and head.description != head.type
                    else None
                ),
                channels=tuple(channels),
                source_id=annotation_id,
            )
        )
    return marks


def _identity(mark: Mark) -> tuple[int, int, str | None, str | None]:
    """What makes two marks the same mark, ignoring which import made them."""
    return (mark.time_us, mark.duration_us, mark.label, mark.text)


def dedupe(marks: Iterable[Mark], rule: str = EXACT) -> list[Mark]:
    """Drop repeated imports of the same mark.

    exact, the default, collapses only copies that are genuinely identical --
    same time, same text, same channels -- and keeps copies that differ. A
    repeated import is then removed with nothing at risk, while an import that
    scoped a mark differently survives as its own mark. The cost is a mark drawn
    twice; the alternative is channels silently disappearing, and a duplicate is
    visible where a missing electrode is not.

    first, last and widest each pick one copy and discard the rest, so they can
    lose channels. first and last go by the source annotation id, which the
    database issues in creation order, so they mean oldest and newest import.
    widest keeps whichever copy named the most channels.
    """
    if rule not in DUPLICATE_RULES:
        raise ValueError(
            f"unknown duplicate rule {rule!r}; expected one of "
            f"{list(DUPLICATE_RULES)}"
        )

    groups: dict[tuple[int, int, str | None, str | None], list[Mark]] = {}
    for mark in marks:
        groups.setdefault(_identity(mark), []).append(mark)

    kept: list[Mark] = []
    for identity, group in groups.items():
        distinct = _distinct_by_channels(group)
        if len(group) > 1 and len(distinct) > 1:
            sizes = " vs ".join(
                str(len(mark.channels)) for mark in distinct
            )
            if rule == EXACT:
                log.warning(
                    "%d copies of the mark at %d us disagree on channels "
                    "(%s); keeping all %d rather than dropping channels",
                    len(group),
                    identity[0],
                    sizes,
                    len(distinct),
                )
            else:
                log.warning(
                    "%d copies of the mark at %d us disagree on channels "
                    "(%s); rule %r keeps one and discards the rest",
                    len(group),
                    identity[0],
                    sizes,
                    rule,
                )
        kept.extend(distinct if rule == EXACT else [_pick(group, rule)])
    return kept


def _distinct_by_channels(group: list[Mark]) -> list[Mark]:
    """Return one copy per distinct channel set, oldest first.

    Copies naming the same channels are the same annotation imported twice and
    carry no information between them, so only the first is kept.
    """
    seen: dict[tuple[str, ...], Mark] = {}
    for mark in sorted(group, key=lambda mark: int(mark.source_id or 0)):
        seen.setdefault(mark.channels, mark)
    return list(seen.values())


def _pick(group: list[Mark], rule: str) -> Mark:
    """Return the copy the rule selects."""
    by_id = sorted(group, key=lambda mark: int(mark.source_id or 0))
    if rule == FIRST:
        return by_id[0]
    if rule == LAST:
        return by_id[-1]
    return max(by_id, key=lambda mark: len(mark.channels))


def to_bodies(marks: Iterable[Mark]) -> list[Mark]:
    """Move each mark's text and origin into a JSON body.

    For a migration this is the audit trail: the body carries the
    ts_annotation_id a bundle annotation came from, which nothing else in the
    format has a home for.
    """
    out: list[Mark] = []
    for mark in marks:
        body = {"description": mark.text or mark.label or ""}
        if mark.source_id:
            body["ts_annotation_id"] = mark.source_id
        out.append(
            Mark(
                time_us=mark.time_us,
                duration_us=mark.duration_us,
                label=mark.label,
                body=body,
                channels=mark.channels,
                source_id=mark.source_id,
            )
        )
    return out


def convert(
    path: Path,
    *,
    duplicate_rule: str = EXACT,
    json_bodies: bool = False,
    source_prefix: str = "",
) -> list[tuple[ChannelHeader, list[Mark]]]:
    """Turn one exported CSV or XML file into one annotation source per layer.

    1. Read the rows, by the file's suffix.
    2. Squash the per-channel fan-out into one mark per annotation.
    3. Drop repeated imports.
    4. Split by layer, since a layer is the grouping a reviewer already thinks
       in and becomes one event channel the viewer can toggle.

    source_prefix is prepended to every channel id. Ids must be unique across a
    whole workflow run, and two sites with a layer called "Seizures" would
    otherwise collide and fail the merge.
    """
    rows = list(read_export(path))
    by_layer: dict[str, list[Row]] = {}
    for row in rows:
        by_layer.setdefault(row.layer, []).append(row)

    sources: list[tuple[ChannelHeader, list[Mark]]] = []
    for layer, layer_rows in sorted(by_layer.items()):
        marks = squash(layer_rows)
        kept = dedupe(marks, duplicate_rule)
        log.info(
            "layer %r: %d rows -> %d marks -> %d after dedupe",
            layer,
            len(layer_rows),
            len(marks),
            len(kept),
        )
        if json_bodies:
            kept = to_bodies(kept)

        annotators = sorted({row.annotator for row in layer_rows if row.annotator})
        header = ChannelHeader(
            id=f"{source_prefix}{slug(layer)}",
            name=layer or "Annotations",
            time_reference=RECORDING_ONSET,
            body_media_type=(
                JSON_MEDIA_TYPE if json_bodies else TEXT_MEDIA_TYPE
            ),
            provenance={
                "extractor": f"{EXTRACTOR}-{path.suffix.lower().lstrip('.')}",
                "version": VERSION,
                "source_file": path.name,
                "extracted_at": utc_now(),
                "duplicate_rule": duplicate_rule,
                "annotators": annotators,
            },
        )
        sources.append((header, kept))
    return sources
