#!/usr/bin/env python3
"""Turn a measured CSV into a par-baked Croissant file.

Par-baked means machine-generated and not reviewed by anybody. It is the start
of a Croissant file, not one. A human has to write the field descriptions, the
data types, the licence, the citation and the caveats before it means anything.

## Why these files deliberately fail validation

A par-baked file that passes `mlcroissant validate` is dangerous, because
passing validation is the thing people check before treating a file as done.
Structure is not meaning: a file can be perfectly well formed and still say
nothing true about the data.

So the generated file fails on purpose. `conformsTo` names a version that does
not exist, which mlcroissant reports as an error and not a warning.

The file is also built so that fixing it is a ladder rather than a maze. Every
error a person sees is real work they have to do, and it validates exactly when
that work is finished. Measured against mlcroissant 1.1.0:

    as generated      2 errors   the bad conformsTo, and the @type it implies
    fix conformsTo   17 errors   a missing checksum, and 16 fields with no dataType
    add a checksum   16 errors   the 16 fields with no dataType
    add dataTypes     0 errors   it validates

That ladder is why the file uses `cr:FileObject` rather than `sc:FileObject`.
Under the par-baked conformsTo, mlcroissant falls back to schema.org type
expectations, so `cr:` raises one extra error there. But `sc:` would leave the
file structurally wrong under a real conformsTo, and fixing the version would
produce 35 errors about "malformed source data" that have nothing to do with
what is actually missing. One clear extra error now beats a maze later.

Nothing here writes a file named `.croissant.json`. That name is for a file a
person has finished.
"""

import datetime
import json

from sources import dataset_stem

# The version string that makes the file fail. Not a real Croissant version,
# and it is meant to be read by people as much as by validators.
PARBAKED_CONFORMS_TO = "http://mlcommons.org/croissant/PARBAKED-DO-NOT-SUBMIT"

# Said at the top of the file, and again on every field. Repetition is the
# point: someone skimming a rendered document should not be able to miss it.
BANNER = (
    "PAR-BAKED. MACHINE GENERATED. NOT REVIEWED BY A HUMAN. "
    "DO NOT CITE. DO NOT SUBMIT. DO NOT PUBLISH."
)

FIELD_PLACEHOLDER = "NOT REVIEWED -- a human must write this description."

# What a person still has to do. Written into the file so the list travels with
# it rather than living in someone's head.
OUTSTANDING = [
    "Write a description for every field.",
    "Decide and record a dataType for every field.",
    "Say what each field means, and its unit, where it has one.",
    "Record the licence and the required citation.",
    "Record the responsible-AI notes: limitations, biases, personal information.",
    "Check the release status before the data goes anywhere.",
    "Remove the PARBAKED conformsTo once all of the above is true.",
]

# The standard Croissant vocabulary. Kept here rather than read from a sibling
# file so the tool has no dependency on any particular existing dataset.
CROISSANT_CONTEXT = {
    "@language": "en",
    "@vocab": "https://schema.org/",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "cr": "http://mlcommons.org/croissant/",
    "data": {"@id": "cr:data", "@type": "@json"},
    "dataBiases": "rai:dataBiases",
    "dataCollectionType": "rai:dataCollectionType",
    "dataSocialImpact": "rai:dataSocialImpact",
    "dataType": {"@id": "cr:dataType", "@type": "@vocab"},
    "dct": "http://purl.org/dc/terms/",
    "equivalentProperty": "cr:equivalentProperty",
    "examples": {"@id": "cr:examples", "@type": "@json"},
    "extract": "cr:extract",
    "field": "cr:field",
    "fileObject": "cr:fileObject",
    "fileProperty": "cr:fileProperty",
    "fileSet": "cr:fileSet",
    "format": "cr:format",
    "hasSyntheticData": "rai:hasSyntheticData",
    "includes": "cr:includes",
    "isLiveDataset": "cr:isLiveDataset",
    "jsonPath": "cr:jsonPath",
    "key": "cr:key",
    "md5": "cr:md5",
    "parentField": "cr:parentField",
    "path": "cr:path",
    "rai": "http://mlcommons.org/croissant/RAI/",
    "recordSet": "cr:recordSet",
    "references": "cr:references",
    "regex": "cr:regex",
    "repeated": "cr:repeated",
    "replace": "cr:replace",
    "samplingRate": "cr:samplingRate",
    "sc": "https://schema.org/",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform",
}


def build_parbaked_croissant(result, generator_version="parbake 0.1"):
    """Build a par-baked Croissant document from a describe_csv result.

    Only mechanically determinable things go in: the file, its size, the column
    names and their order. No types, no meanings, no descriptions.
    """
    file_details = result["file"]
    filename = file_details["path"].rsplit("/", 1)[-1]
    # Strips ".csv.gz" as well as ".csv"; rsplit(".", 1) would leave ".csv" on.
    dataset_name = dataset_stem(filename)

    # cr:FileObject, not sc:. See the note at the top of this file: this costs
    # one extra error while par-baked and saves 35 confusing ones later.
    file_object = {
        "@type": "cr:FileObject",
        "@id": filename,
        "name": filename,
        "contentUrl": filename,
        "contentSize": f"{file_details['size_in_bytes']} B",
        "encodingFormat": "text/csv",
        "description": f"{BANNER} File details are measured; nothing else here is.",
    }

    fields = []
    for column_name, column in result["columns"].items():
        fields.append({
            "@type": "cr:Field",
            "@id": f"records/{column_name}",
            "name": column_name,
            # No dataType. Guessing one from a sample is the judgement this tool
            # must not make, and its absence is one of the two things that makes
            # the file fail validation.
            "description": FIELD_PLACEHOLDER,
            "source": {
                "fileObject": {"@id": filename},
                "extract": {"column": column_name},
            },
        })

    return {
        "@context": CROISSANT_CONTEXT,
        "@type": "sc:Dataset",
        "conformsTo": PARBAKED_CONFORMS_TO,
        "name": dataset_name,
        "description": _dataset_description(result),
        "distribution": [file_object],
        "recordSet": [{
            "@type": "cr:RecordSet",
            "@id": "records",
            "name": "records",
            "description": f"{BANNER} One record per row. "
                           f"{len(fields)} fields, none of them described or typed.",
            "field": fields,
        }],
        "_parbake": _provenance(result, generator_version),
        "_parbake_measurements": {
            name: _measurements_for(column) for name, column in result["columns"].items()
        },
    }


def _dataset_description(result):
    """The dataset description, which is mostly the warning."""
    file_details = result["file"]
    return (
        f"{BANNER}\n\n"
        f"This file was generated by reading {file_details['path']} and measuring it. "
        f"Scope: {file_details['scope']}, {file_details['rows_read']:,} rows over "
        f"{file_details['column_count']} columns.\n\n"
        "Everything in this file is either a measurement or a placeholder. No field has "
        "a description, a data type, a unit or a meaning, because those are judgements "
        "and no human has made them yet. It deliberately fails Croissant validation and "
        "must not be submitted, cited or published as it stands.\n\n"
        "Outstanding before this file means anything:\n"
        + "\n".join(f"  - {item}" for item in OUTSTANDING)
    )


def _provenance(result, generator_version):
    """A clearly non-standard block recording where this came from."""
    file_details = result["file"]
    return {
        "status": "PAR-BAKED -- MACHINE GENERATED, NOT REVIEWED BY A HUMAN",
        "warning": BANNER,
        "validates": False,
        "why_it_does_not_validate": [
            "conformsTo names a version that does not exist, on purpose.",
            "No field has a dataType, because guessing one is not this tool's job.",
            "The file has no checksum recorded.",
        ],
        "outstanding_for_a_human": list(OUTSTANDING),
        "generated_by": generator_version,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": file_details["path"],
        "source_size_in_bytes": file_details["size_in_bytes"],
        "scope": file_details["scope"],
        "rows_read": file_details["rows_read"],
        "rows_read_is_the_whole_file": file_details["scope"] == "full scan",
        # The anonymity flags belong in the machine-readable record, not only
        # in the prose report: they are the most consequential thing measured,
        # and anything reading this file back needs to see them. "checked"
        # says whether the check ran at all -- an empty list from a run that
        # skipped the check does not mean there is nothing to find.
        "anonymity_check_ran": result["identifiers"]["checked"],
        "columns_of_concern": list(result["identifiers"]["columns_of_concern"]),
    }


def _measurements_for(column):
    """The measurements for one column, in a clearly non-standard block.

    Kept out of the Croissant fields themselves: a measurement is not a
    property of the schema, and mapping one into a semantic slot would be
    exactly the guess this tool avoids.
    """
    measurements = {
        "position": column["position"],
        "rows_seen": column["rows_seen"],
        "empty_count": column["empty_count"],
        "null_like_text_count": column["null_like_text_count"],
        "distinct_count": column["distinct_count"],
        "distinct_count_is_at_least": column["distinct_count_is_at_least"],
        "shortest_value_length": column["shortest_value_length"],
        "longest_value_length": column["longest_value_length"],
        "most_common_values": column["most_common_values"],
        "number_count": column["number_count"],
        "not_a_number_count": column["not_a_number_count"],
        "is_all_empty": column["is_all_empty"],
        "holds_one_value": column["holds_one_value"],
    }
    for optional in ("smallest_number", "largest_number"):
        if optional in column:
            measurements[optional] = column[optional]
    return measurements


def write_parbaked_croissant(result, output_path, generator_version="parbake 0.1"):
    """Write the par-baked Croissant to disk. Returns the document."""
    document = build_parbaked_croissant(result, generator_version)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return document


# --- rendering, using the existing croissant_to_md.py ---------------------

def render_parbaked_markdown(document, renderer=None):
    """Render a par-baked Croissant as Markdown.

    Uses croissant_to_md.py -- deliberately the same renderer that produces the
    reviewed documents, so a par-baked file reads as an unfinished version of
    the real thing rather than output from a different tool.

    `renderer` is only for tests; normally the module next door is used.
    """
    if renderer is None:
        import croissant_to_md as renderer
    return renderer.render_markdown(document)
