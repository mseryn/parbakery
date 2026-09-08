#!/usr/bin/env python3
"""Render a Croissant JSON-LD metadata file as human-readable Markdown.

The Croissant JSON is the source of truth; the Markdown is a deterministic
projection of it. Edit the JSON and regenerate -- never hand-edit the Markdown.

The same input always produces the same output, byte for byte, so a regenerated
document can be diffed against the previous one to see exactly what changed in
the metadata.

Sections rendered, in order:
  * title and description
  * dataset metadata (publisher, version, dates, landing page, keywords,
    conformsTo), license (a CreativeWork object or a plain string), citation
  * files (the `distribution` entries)
  * field tables (one per non-enumeration RecordSet)
  * value decodings (one table per enumeration RecordSet)
  * responsible-AI notes (the `rai:` field set)

Usage:
    python3 croissant_to_md.py dataset.croissant.json              # -> stdout
    python3 croissant_to_md.py dataset.croissant.json out.md       # -> file
    python3 croissant_to_md.py dataset.croissant.json -o out.md    # same thing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__version__ = "1.0.0"
__author__ = "Melanie Cornelius"

# Croissant files spell the same property with or without a namespace prefix
# (`dataLimitations` vs `rai:dataLimitations`), and which one appears depends on
# how the file was authored. Property lookups try the bare name, then each of
# these prefixes.
NAMESPACE_PREFIXES = ("rai:", "cr:", "sc:", "dct:")

# Responsible-AI properties, as (property, heading, is_single_value).
# Single-value properties render as a paragraph; the rest as a bullet list.
# Ordered for human reading, not to match the spec's ordering.
RAI_SECTIONS = (
    ("dataUseCases", "Intended use / linking", False),
    ("dataLimitations", "Known limitations & data-quality notes", False),
    ("dataBiases", "Known biases", False),
    ("dataSocialImpact", "Social impact", True),
    ("personalSensitiveInformation", "Personal / sensitive information", False),
    ("dataCollectionType", "Data collection", True),
)

FOOTER_NOTE = (
    "*Rendered from the Croissant metadata file. The JSON is the source of "
    "truth — edit it and regenerate; do not hand-edit this document.*"
)


# --- reading Croissant values ------------------------------------------------

def get_property(node: Any, *names: str, default: Any = None) -> Any:
    """Return the first of `names` present on `node`, ignoring namespace prefixes.

    Looks for each bare name first, then the same name under every prefix in
    NAMESPACE_PREFIXES, so `get_property(meta, "dataBiases")` finds either
    `dataBiases` or `rai:dataBiases`.
    """
    if not isinstance(node, dict):
        return default
    for name in names:
        if name in node:
            return node[name]
        for prefix in NAMESPACE_PREFIXES:
            prefixed = prefix + name
            if prefixed in node:
                return node[prefixed]
    return default


def as_list(value: Any) -> list:
    """Normalize a Croissant property that may be a single value or a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def display_name(node: Any) -> str:
    """Human-facing label for a node: its `name`, else its `@id`, else itself."""
    if isinstance(node, dict):
        return str(node.get("name") or node.get("@id") or "")
    return str(node)


def short_data_type(data_type: Any) -> str:
    """Shorten a dataType URI or CURIE to its last segment (`sc:Text` -> `Text`)."""
    if isinstance(data_type, list):
        data_type = data_type[0] if data_type else ""
    if isinstance(data_type, dict):
        data_type = data_type.get("@id", "")
    return str(data_type).split("/")[-1].split(":")[-1]


def is_enumeration(record_set: Any) -> bool:
    """True for a RecordSet that defines value decodings rather than data fields."""
    return bool(get_property(record_set, "isEnumeration"))


def enumeration_rows(record_set: dict) -> list[dict]:
    """Inline rows of an enumeration RecordSet.

    `data` is normally a list of row dicts, but some writers emit it as a JSON
    string. Anything unparseable yields no rows rather than an error, so one bad
    enumeration cannot stop the whole document from rendering.
    """
    data = get_property(record_set, "data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return []
    return [row for row in as_list(data) if isinstance(row, dict)]


def column_keys(rows: list[dict]) -> list[str]:
    """Union of the keys across `rows`, in first-seen order.

    Taking the union rather than the first row's keys means a ragged
    enumeration -- where a later row carries a field the first one omits --
    still renders every column instead of silently dropping it.
    """
    keys: dict[str, None] = {}
    for row in rows:
        keys.update(dict.fromkeys(row))
    return list(keys)


def yes_no(value: Any) -> str:
    """Render a boolean flag as Yes/No; pass anything else through as text."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


# --- writing Markdown --------------------------------------------------------

def table_cell(value: Any) -> str:
    """Flatten a value into something safe to put inside a Markdown table cell.

    Newlines and runs of whitespace collapse to single spaces, and `|` is
    escaped, so a multi-line or pipe-bearing description cannot break the table.
    """
    return " ".join(str(value if value is not None else "").split()).replace("|", "\\|")


class MarkdownWriter:
    """Accumulates Markdown lines, keeping block spacing consistent.

    Every block-level helper leaves exactly one blank line behind it, and
    `blank()` never stacks two, so callers do not have to track spacing.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def line(self, text: str = "") -> None:
        self._lines.append(text)

    def blank(self) -> None:
        """Append a blank line unless the output already ends with one."""
        if self._lines and self._lines[-1] != "":
            self._lines.append("")

    def paragraph(self, text: str) -> None:
        self.line(str(text))
        self.blank()

    def heading(self, level: int, text: str) -> None:
        self.blank()
        self.line(f"{'#' * level} {text}")
        self.blank()

    def bullet(self, text: str, indent: int = 0) -> None:
        self.line(f"{'    ' * indent}- {text}")

    def quote(self, text: str) -> None:
        self.paragraph(f"> {text}")

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        """Write a pipe table. Cells are escaped; no-row tables are skipped."""
        if not headers or not rows:
            return
        self.line("| " + " | ".join(table_cell(h) for h in headers) + " |")
        self.line("|" + "|".join("---" for _ in headers) + "|")
        for row in rows:
            self.line("| " + " | ".join(table_cell(c) for c in row) + " |")
        self.blank()

    def text(self) -> str:
        """The finished document, ending in exactly one newline."""
        return "\n".join(self._lines).rstrip("\n") + "\n"


# --- sections ----------------------------------------------------------------

def write_title(md: MarkdownWriter, meta: dict) -> None:
    md.line(f"# {meta.get('name') or 'unnamed dataset'}")
    md.blank()
    if meta.get("description"):
        md.paragraph(meta["description"])


def write_dataset_metadata(md: MarkdownWriter, meta: dict) -> None:
    """Bulleted provenance block. Omitted entirely when nothing is recorded."""
    bullets: list[str] = []

    # Fall back to creator when no publisher is recorded; for the
    # facility-generated datasets this renders they are the same party.
    publishers = as_list(get_property(meta, "publisher")) or as_list(get_property(meta, "creator"))
    if publishers:
        bullets.append(f"**Publisher:** {', '.join(display_name(p) for p in publishers)}")

    for prop, label in (
        ("version", "Version"),
        ("datePublished", "Published"),
        ("dateModified", "Last modified"),
        ("url", "Landing page"),
    ):
        value = get_property(meta, prop)
        if value:
            bullets.append(f"**{label}:** {value}")

    for prop, label in (("keywords", "Keywords"), ("conformsTo", "Conforms to")):
        values = as_list(get_property(meta, prop))
        if values:
            bullets.append(f"**{label}:** {', '.join(str(v) for v in values)}")

    if not bullets:
        return
    md.heading(2, "Dataset metadata")
    for bullet in bullets:
        md.bullet(bullet)
    md.blank()


def write_license(md: MarkdownWriter, meta: dict) -> None:
    """Render the license, which may be a CreativeWork object or a plain string."""
    license_ = get_property(meta, "license")
    if not license_:
        return

    md.heading(3, "License")
    if not isinstance(license_, dict):
        md.paragraph(license_)
        return

    if license_.get("name"):
        md.paragraph(f"**{license_['name']}**")
    if get_property(license_, "text"):
        md.quote(get_property(license_, "text"))
    if get_property(license_, "url"):
        md.paragraph(f"See: {get_property(license_, 'url')}")


def write_citation(md: MarkdownWriter, meta: dict) -> None:
    citation = get_property(meta, "citeAs")
    if citation:
        md.heading(3, "Citation")
        md.quote(citation)


def write_files(md: MarkdownWriter, meta: dict) -> None:
    files = as_list(get_property(meta, "distribution"))
    if not files:
        return

    md.heading(2, "Files")
    for file_ in files:
        headline = f"- **{display_name(file_)}**"
        if isinstance(file_, dict) and file_.get("description"):
            headline += f" — {file_['description']}"
        md.line(headline)
        for prop, label in (("encodingFormat", "Format"), ("contentUrl", "URL")):
            value = get_property(file_, prop)
            if value:
                md.bullet(f"{label}: {value}", indent=1)
    md.blank()


def write_fields(md: MarkdownWriter, record_sets: list[dict]) -> None:
    """One table per data RecordSet, under a single `## Fields` heading.

    With several RecordSets each gets a named subheading, so the tables stay
    distinguishable instead of appearing under repeated identical headings.
    """
    if not record_sets:
        return

    md.heading(2, "Fields")
    for record_set in record_sets:
        if len(record_sets) > 1:
            md.heading(3, f"`{display_name(record_set)}`")
        if record_set.get("description"):
            md.paragraph(record_set["description"])
        md.table(
            ["Field", "Type", "Meaning"],
            [
                [
                    f"`{display_name(field)}`",
                    short_data_type(field.get("dataType", "") if isinstance(field, dict) else ""),
                    field.get("description", "") if isinstance(field, dict) else "",
                ]
                for field in as_list(get_property(record_set, "field"))
            ],
        )


def write_value_decodings(md: MarkdownWriter, record_sets: list[dict]) -> None:
    """One table per enumeration RecordSet, decoding coded values to meanings."""
    if not record_sets:
        return

    md.heading(2, "Value decodings")
    for record_set in record_sets:
        md.heading(3, f"`{display_name(record_set)}`")
        if record_set.get("description"):
            md.paragraph(record_set["description"])

        rows = enumeration_rows(record_set)
        keys = column_keys(rows)
        # Column keys are qualified ids like `exit_code_enum/code`; the trailing
        # segment is the readable column name.
        md.table(
            [key.split("/")[-1] for key in keys],
            [[row.get(key, "") for key in keys] for row in rows],
        )


def write_responsible_ai(md: MarkdownWriter, meta: dict) -> None:
    present = [(prop, heading, single)
               for prop, heading, single in RAI_SECTIONS if get_property(meta, prop)]
    # Checked against None, not truthiness: `false` is a meaningful answer here.
    synthetic = get_property(meta, "hasSyntheticData")
    if not present and synthetic is None:
        return

    md.heading(2, "Responsible AI notes")
    for prop, heading, single in present:
        md.heading(3, heading)
        if single:
            md.paragraph(get_property(meta, prop))
        else:
            for value in as_list(get_property(meta, prop)):
                md.bullet(str(value))
            md.blank()

    if synthetic is not None:
        md.heading(3, "Synthetic data")
        md.paragraph(f"Contains synthetic data: **{yes_no(synthetic)}**")


def write_footer(md: MarkdownWriter) -> None:
    md.blank()
    md.line("---")
    md.line(FOOTER_NOTE)


# --- top level ---------------------------------------------------------------

def render_markdown(meta: dict) -> str:
    """Render parsed Croissant metadata as a Markdown document."""
    record_sets = [rs for rs in as_list(get_property(meta, "recordSet")) if isinstance(rs, dict)]

    md = MarkdownWriter()
    write_title(md, meta)
    write_dataset_metadata(md, meta)
    write_license(md, meta)
    write_citation(md, meta)
    write_files(md, meta)
    write_fields(md, [rs for rs in record_sets if not is_enumeration(rs)])
    write_value_decodings(md, [rs for rs in record_sets if is_enumeration(rs)])
    write_responsible_ai(md, meta)
    write_footer(md)
    return md.text()


def load_metadata(path: Path) -> dict:
    """Read and parse a Croissant file, exiting with a readable message on failure."""
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"No such file: {path}")
    except OSError as exc:
        sys.exit(f"Could not read {path}: {exc}")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}")

    if not isinstance(meta, dict):
        sys.exit(f"{path} does not contain a Croissant metadata object "
                 f"(found a {type(meta).__name__} at the top level).")
    return meta


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Render a Croissant JSON-LD metadata file as Markdown.",
        epilog="With no output path the document is written to stdout.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("croissant_json", type=Path, metavar="croissant_json",
                        help="Croissant metadata file to render.")
    parser.add_argument("output_md", type=Path, nargs="?", metavar="output_md",
                        help="Markdown file to write (default: stdout).")
    parser.add_argument("-o", "--output", type=Path, dest="output_flag", metavar="PATH",
                        help="Same as the positional output path.")
    args = parser.parse_args(argv)

    if args.output_flag and args.output_md and args.output_flag != args.output_md:
        parser.error(f"two different output paths given: {args.output_md} and {args.output_flag}")
    output_path = args.output_flag or args.output_md
    markdown = render_markdown(load_metadata(args.croissant_json))

    if output_path is None:
        sys.stdout.write(markdown)
        return
    try:
        output_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        sys.exit(f"Could not write {output_path}: {exc}")
    print(f"Wrote {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
