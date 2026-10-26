"""The two things this site reader exists for: squashing and deduping."""

import json

import pytest

from processor.records import Mark, slug
from processor.sites.ieeg import (
    EXACT,
    FIRST,
    LAST,
    WIDEST,
    Row,
    convert,
    dedupe,
    read_rows,
    squash,
    to_bodies,
)

HEADER = (
    "annotation_id,layer,type,description,annotator,created_at,"
    "time_us,duration_us,channel"
)


def _row(annotation_id="1", channel="C3", **overrides):
    fields = {
        "annotation_id": annotation_id,
        "layer": "Imported Natus ENT annotations",
        "type": "Seizure",
        "description": "Seizure",
        "annotator": "Review",
        "created_at": "2022-11-09 02:12:31",
        "time_us": 1000,
        "duration_us": 0,
        "channel": channel,
    }
    fields.update(overrides)
    return Row(**fields)


def _csv(tmp_path, lines, name="export.csv"):
    path = tmp_path / name
    path.write_text("\n".join([HEADER, *lines]) + "\n")
    return path


def test_squash_turns_the_per_channel_fanout_into_one_mark():
    rows = [_row(channel=name) for name in ("RP03", "C3", "RP01")]
    marks = squash(rows)
    assert len(marks) == 1
    assert marks[0].channels == ("C3", "RP01", "RP03")


def test_squash_sorts_channels_so_copies_compare_equal():
    one = squash([_row(channel="B"), _row(channel="A")])[0]
    two = squash([_row(channel="A"), _row(channel="B")])[0]
    assert one.channels == two.channels


def test_squash_keeps_separate_annotations_separate():
    rows = [_row("1", "C3"), _row("2", "C3", time_us=2000)]
    assert len(squash(rows)) == 2


def test_an_empty_channel_means_the_whole_recording():
    """No channels at all, rather than a list with an empty string in it."""
    assert squash([_row(channel="")])[0].channels == ()


def test_description_is_dropped_when_it_only_repeats_the_type():
    """Natus imports set both; writing both puts one string in two places."""
    assert squash([_row()])[0].text is None


def test_description_is_kept_when_it_says_something_else():
    mark = squash([_row(description="Onset right temporal.")])[0]
    assert mark.text == "Onset right temporal."


def _three_imports(channel_counts=(68, 68, 60)):
    """The measured HUP247 shape: one mark, three imports, one narrower."""
    return [
        Mark(
            time_us=3590954742,
            label="RP1-3 very active",
            channels=tuple(f"CH{i:02}" for i in range(count)),
            source_id=str(source_id),
        )
        for source_id, count in zip(
            (36462407, 36463559, 36464711), channel_counts, strict=True
        )
    ]


def test_dedupe_collapses_copies_that_are_identical():
    """Three identical imports carry no information between them."""
    assert len(dedupe(_three_imports((68, 68, 68)))) == 1


def test_dedupe_keeps_copies_that_differ_rather_than_dropping_channels():
    """The default errs toward a visible duplicate over a silent loss."""
    kept = dedupe(_three_imports((68, 68, 60)))
    assert sorted(len(mark.channels) for mark in kept) == [60, 68]


def test_exact_keeps_one_copy_per_distinct_channel_set():
    kept = dedupe(_three_imports((68, 60, 60)), EXACT)
    assert sorted(len(mark.channels) for mark in kept) == [60, 68]


def test_dedupe_first_keeps_the_oldest_import():
    assert dedupe(_three_imports(), FIRST)[0].source_id == "36462407"


def test_dedupe_last_keeps_the_newest_import():
    assert dedupe(_three_imports(), LAST)[0].source_id == "36464711"


def test_dedupe_widest_keeps_the_copy_that_named_most_channels():
    """The rule changes the data, not just which row survives."""
    kept = dedupe(_three_imports(), WIDEST)[0]
    assert len(kept.channels) == 68


def test_dedupe_warns_when_the_copies_disagree(caplog):
    dedupe(_three_imports(), EXACT)
    assert "disagree on channels" in caplog.text
    assert "keeping all" in caplog.text


def test_a_picking_rule_says_it_is_discarding(caplog):
    dedupe(_three_imports(), FIRST)
    assert "discards the rest" in caplog.text


def test_dedupe_is_quiet_when_the_copies_agree(caplog):
    dedupe(_three_imports((68, 68, 68)), EXACT)
    assert "disagree" not in caplog.text


def test_dedupe_keeps_marks_that_differ_in_time():
    marks = [
        Mark(time_us=1, label="a", source_id="1"),
        Mark(time_us=2, label="a", source_id="2"),
    ]
    assert len(dedupe(marks)) == 2


def test_dedupe_keeps_marks_that_differ_in_text():
    marks = [
        Mark(time_us=1, label="a", text="one", source_id="1"),
        Mark(time_us=1, label="a", text="two", source_id="2"),
    ]
    assert len(dedupe(marks)) == 2


def test_dedupe_rejects_an_unknown_rule():
    with pytest.raises(ValueError, match="unknown duplicate rule"):
        dedupe([], "newest")


def test_read_rows_rejects_a_file_that_is_not_this_export(tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(ValueError, match="missing columns"):
        list(read_rows(path))


def test_read_rows_treats_a_blank_duration_as_a_point(tmp_path):
    path = _csv(tmp_path, ["1,L,T,,Review,2022-11-09 02:12:31,500,,C3"])
    assert list(read_rows(path))[0].duration_us == 0


def test_convert_splits_layers_into_separate_sources(tmp_path):
    path = _csv(
        tmp_path,
        [
            "1,Ictal,Seizure,,Review,2022-11-09 02:12:31,500,90,C3",
            "2,stability,Bookmark,,Review,2022-11-09 02:12:31,900,0,C3",
        ],
    )
    sources = convert(path)
    assert [header.id for header, _ in sources] == ["ictal", "stability"]


def test_convert_carries_the_duration_through(tmp_path):
    path = _csv(
        tmp_path, ["1,Ictal,Seizure,,Review,2022-11-09 02:12:31,500,90000,C3"]
    )
    (_, marks), = convert(path)
    assert marks[0].duration_us == 90000


def test_convert_prefixes_channel_ids_for_workflow_uniqueness(tmp_path):
    """Two sites with a layer called Ictal would otherwise fail the merge."""
    path = _csv(
        tmp_path, ["1,Ictal,Seizure,,Review,2022-11-09 02:12:31,500,0,C3"]
    )
    (header, _), = convert(path, source_prefix="hup247-")
    assert header.id == "hup247-ictal"


def test_the_default_rule_is_exact(tmp_path):
    path = _csv(
        tmp_path, ["1,Ictal,Seizure,,Review,2022-11-09 02:12:31,500,0,C3"]
    )
    (header, _), = convert(path)
    assert header.provenance["duplicate_rule"] == EXACT


def test_convert_records_the_rule_it_used(tmp_path):
    path = _csv(
        tmp_path, ["1,Ictal,Seizure,,Review,2022-11-09 02:12:31,500,0,C3"]
    )
    (header, _), = convert(path, duplicate_rule=WIDEST)
    assert header.provenance["duplicate_rule"] == WIDEST


def test_json_bodies_carry_the_source_id_for_an_audit_trail():
    """A migration wants to trace a bundle mark back to its ts_annotation row."""
    marks = to_bodies([Mark(time_us=1, label="Seizure", source_id="36462407")])
    assert marks[0].body == {
        "description": "Seizure",
        "ts_annotation_id": "36462407",
    }
    assert marks[0].text is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Imported Natus ENT annotations", "imported-natus-ent-annotations"),
        ("*Tech notation: Video/EEG", "tech-notation-video-eeg"),
        ("  stability  ", "stability"),
        ("***", "annotations"),
    ],
)
def test_slug(text, expected):
    assert slug(text) == expected


def test_end_to_end_on_the_measured_natus_shape(tmp_path):
    """400 rows, six annotation rows, two real marks."""
    lines = []
    for source_id, created in (
        ("36462406", "2022-11-09 02:12:31"),
        ("36463558", "2022-11-09 02:12:57"),
        ("36464710", "2022-11-09 03:56:18"),
    ):
        for index in range(68):
            lines.append(
                f"{source_id},Imported Natus ENT annotations,"
                f"*Tech notation,*Tech notation,Acquisition,{created},"
                f"3080610992,0,CH{index:02}"
            )
    path = _csv(tmp_path, lines)
    (header, marks), = convert(path)
    assert len(marks) == 1
    assert len(marks[0].channels) == 68
    assert json.loads(json.dumps(header.as_record()))["schema"].startswith(
        "pennsieve/annotations-"
    )


# ---- the XML export ----

from processor.sites.ieeg import read_export, read_xml_rows  # noqa: E402


def _xml(tmp_path, annotations, name="export.xml", root="annotations"):
    """Write an export of (id, type, description, start_us, end_us) tuples."""
    body = "".join(
        f'  <annotation id="{id_}" type="{kind}" description="{desc}" '
        f'startOffsetUsecs="{start}" endOffsetUsecs="{end}" '
        f'layer="EDF Annotations">\n  </annotation>\n'
        for id_, kind, desc, start, end in annotations
    )
    path = tmp_path / name
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<{root}>\n{body}</{root}>\n",
        encoding="utf-8",
    )
    return path


def test_xml_rows_carry_the_portal_columns_and_no_channel(tmp_path):
    path = _xml(tmp_path, [("36214912", "IMP CHECK ON", "IMP CHECK ON", 16320000, 16320000)])
    (row,) = read_xml_rows(path)
    assert row.annotation_id == "36214912"
    assert row.layer == "EDF Annotations"
    assert row.type == "IMP CHECK ON"
    assert row.time_us == 16320000
    assert row.duration_us == 0
    assert row.channel == ""


def test_xml_duration_comes_from_the_two_offsets(tmp_path):
    path = _xml(tmp_path, [("1", "Seizure", "Seizure", 1000, 91000)])
    (row,) = read_xml_rows(path)
    assert row.duration_us == 90000


def test_xml_drops_edf_timekeeping_stamps_and_says_so(tmp_path, caplog):
    path = _xml(
        tmp_path,
        [
            ("1", "+0.000000", "+0.000000", 0, 0),
            ("2", "Segment: REC START", "Segment: REC START", 0, 0),
            ("3", "+1.640000", "+1.640000", 1440000, 1440000),
            ("4", "A1+A2 OFF", "A1+A2 OFF", 1440000, 1440000),
        ],
    )
    with caplog.at_level("INFO"):
        rows = list(read_xml_rows(path))
    assert [row.type for row in rows] == ["Segment: REC START", "A1+A2 OFF"]
    assert "dropped 2 EDF+ timekeeping stamps" in caplog.text


def test_xml_keeps_a_numeric_type_that_carries_a_description(tmp_path):
    """Only the bare stamp is noise; a number with a note is a mark."""
    path = _xml(tmp_path, [("1", "+5.000000", "dose given", 5000000, 5000000)])
    assert len(list(read_xml_rows(path))) == 1


def test_xml_rejects_a_file_that_is_not_an_annotation_export(tmp_path):
    path = _xml(tmp_path, [], root="metadata")
    with pytest.raises(ValueError, match="root is <metadata>"):
        list(read_xml_rows(path))


def test_xml_rejects_malformed_xml(tmp_path):
    path = tmp_path / "broken.xml"
    path.write_text("<annotations><annotation id='1'")
    with pytest.raises(ValueError, match="not well-formed"):
        list(read_xml_rows(path))


def test_xml_rejects_an_annotation_missing_a_time(tmp_path):
    path = tmp_path / "short.xml"
    path.write_text(
        '<annotations><annotation id="1" type="T" description="T" '
        'startOffsetUsecs="5"/></annotations>'
    )
    with pytest.raises(ValueError, match="missing attributes"):
        list(read_xml_rows(path))


def test_read_export_routes_by_suffix(tmp_path):
    csv_path = _csv(tmp_path, ["1,L,T,,Review,2022-11-09 02:12:31,500,0,C3"])
    xml_path = _xml(tmp_path, [("1", "T", "T", 500, 500)])
    assert list(read_export(csv_path))[0].channel == "C3"
    assert list(read_export(xml_path))[0].channel == ""
    with pytest.raises(ValueError, match="neither"):
        list(read_export(tmp_path / "notes.txt"))


def test_convert_turns_the_xml_export_into_one_whole_recording_layer(tmp_path):
    path = _xml(
        tmp_path,
        [
            ("1", "+0.000000", "+0.000000", 0, 0),
            ("2", "IMP CHECK ON", "IMP CHECK ON", 16320000, 16320000),
            ("3", "IMP CHECK OFF", "IMP CHECK OFF", 20160000, 20160000),
        ],
    )
    ((header, marks),) = convert(path, json_bodies=True)
    assert header.id == "edf-annotations"
    assert header.name == "EDF Annotations"
    assert header.provenance["extractor"] == "ieeg-annotations-xml"
    assert [mark.label for mark in marks] == ["IMP CHECK ON", "IMP CHECK OFF"]
    assert all(mark.channels == () for mark in marks)
    assert marks[0].body == {
        "description": "IMP CHECK ON",
        "ts_annotation_id": "2",
    }
