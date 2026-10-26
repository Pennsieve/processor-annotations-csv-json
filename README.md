# processor-annotations-csv-json

Turns an annotation export into the interchange NDJSON the TS-Zarr bundle
writer reads. One processor per source format, all emitting the same shape, so
the writer never learns what a Natus export or an IEEG portal is.

The format is specified in [ANNOTATIONS_NDJSON.md](./ANNOTATIONS_NDJSON.md).

## Where it sits

```
         ┌─> timeseries conversion ─────────────> session.nwb ──────────┐
source ──┤                                                              ├─> merge ─> TS-Zarr writer
         └─> this processor ──> <id>.annotations.ndjson ────────────────┘
```

The two branches run in parallel, which is why this cannot read the NWB and
cannot know the recording onset. It only declares which clock its numbers are
on, in `time_reference`; the writer converts.

**One output file is one event channel.** Its stem is `channel.id` and must be
unique across the whole workflow run — the branch merge flattens every output
into one directory and fails the run on a filename collision. `SOURCE_PREFIX`
is there for when one run carries annotations from more than one source.

## Run it

```bash
make test          # unit tests
make local         # against data/input, no docker
make run           # build and run through docker-compose
```

```bash
INPUT_DIR=data/input OUTPUT_DIR=data/output python3 -m processor.main
```

Every `*.csv` and `*.xml` in `INPUT_DIR` is read. Stdlib only, no dependencies.

| Variable | Governs | Default |
|---|---|---|
| `INPUT_DIR` | where the CSVs are | `/data/input` |
| `OUTPUT_DIR` | where the NDJSON goes | `/data/output` |
| `SITE` | which reader interprets the export | `ieeg` |
| `DUPLICATE_RULE` | `exact`, `first`, `last` or `widest` | `exact` |
| `JSON_BODIES` | structured bodies with an audit trail | `false` |
| `SOURCE_PREFIX` | prepended to every channel id | empty |

## The IEEG site

The export is one row per annotation-channel pair, which is how
`ts_annotation_time_series` stores the link. Produce it with:

```sql
SELECT a.ts_annotation_id AS annotation_id, a.layer, a.type, a.description,
       a.annotator, a.create_time AS created_at,
       a.start_time AS time_us, a.end_time - a.start_time AS duration_us,
       ts.label AS channel
FROM ts_annotation a
JOIN data_snapshot ds ON ds.data_snapshot_id = a.parent_id
LEFT JOIN ts_annotation_time_series ats ON ats.ts_annotation_id = a.ts_annotation_id
LEFT JOIN time_series ts ON ts.time_series_id = ats.time_series_id
WHERE ds.label = 'HUP247_phaseII'
ORDER BY a.start_time, a.ts_annotation_id, ts.label;
```

`start_time` and `end_time` are already microsecond offsets from the recording
onset (`startOffsetUsecs` in `TsAnnotationEntity`), so they go straight into
`time_us` with no conversion. That is the highest-risk field in the format and
this source happens to line up exactly.

`LEFT JOIN` is deliberate: an annotation with no channel rows applies to the
whole recording, `channel` comes back NULL, and the mark is written with no
`channels` field.

Two things then happen to it.

**Squash.** A mark on 68 channels arrives as 68 rows sharing one
`ts_annotation_id`. That is one annotation with 68 channels, not 68
annotations.

**Dedupe.** The same Natus ENT file imported three times leaves three rows per
mark with different ids and create times. Written through, a viewer draws each
mark three times on top of itself.

The imports are not always identical. On the measured HUP247 export, one of the
three copies linked 60 channels rather than 68, dropping `RY04` upward from one
electrode. So collapsing them to a single mark means choosing which channels
survive.

**The default, `exact`, does not choose.** Copies that are byte-identical
collapse, because they carry no information between them. Copies that differ
are all kept. A mark drawn twice is visible and fixable; an electrode that
quietly vanished from an annotation is neither.

`first`, `last` and `widest` each keep one copy and discard the rest, and say
so in the log. On this data `last` is the one that loses the eight contacts.

Measured output:

```
hup247.csv    400 rows -> 6 marks -> 3 after dedupe   (1 layer, one mark kept twice)
hup138.csv    600 rows -> 5 marks -> 5 after dedupe   (1 layer, real durations)
```

### The XML export

The portal also exports a layer as XML: one `<annotations>` root holding one
`<annotation>` element per mark, with the same `ts_annotation` columns as
attributes (`id`, `layer`, `type`, `description`, `startOffsetUsecs`,
`endOffsetUsecs`). The offsets are already microseconds from the recording
onset. The export carries no channel link, so every mark applies to the whole
recording. A `.xml` in `INPUT_DIR` goes through the same squash, dedupe and
layer split as the CSV.

One thing is dropped on the way: an EDF+ recording carries a timekeeping stamp
per data record (`+1.640000`), and the portal exports those as annotations.
They are recognised by their shape, a bare signed number of seconds with no
description of its own, and counted in the log rather than written.

### Two mapping decisions worth knowing

`type` becomes the record's `label`. `description` becomes `text` **only when it
differs from `type`** — Natus imports set both to the same string, and writing
both would put one string in the viewer's label and again in its body.

Per-mark `annotator` has no home in the record fields, so the distinct
annotators for a layer are recorded in the header's `provenance`. Set
`JSON_BODIES=true` to keep the description and the source `ts_annotation_id`
per mark instead, which is what a migration wants for traceability.

## Adding a site

Add a module under `processor/sites/` exposing `convert(path, *, duplicate_rule,
json_bodies, source_prefix) -> list[tuple[ChannelHeader, list[Mark]]]`, and
register it in `SITES` in `main.py`. Everything about the file format itself
lives in `records.py`, so a site reader only produces `Mark`s.

## Don't

- Don't write `duration_us: 0` or `channels: []`. Absence is the signal: no
  duration is a point event and no channels is the whole recording, and a
  written zero makes those indistinguishable from a real value.
- Don't emit the same `channel.id` twice in one run. This fails loudly for that
  reason; the branch merge would fail later and name neither file.
- Don't let the database's `latin1` text reach the file untranscoded. The
  NDJSON is UTF-8, and mojibake in a body defeats the point of bodies being
  greppable and redactable with ordinary text tools.
