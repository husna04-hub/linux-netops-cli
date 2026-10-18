"""Plain-text table rendering for terminal output."""

from __future__ import annotations

from typing import Iterable, Sequence


def truncate(value: object, width: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    max_widths: Sequence[int] | None = None,
) -> str:
    row_list = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in row_list:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    if max_widths:
        widths = [
            min(width, limit) for width, limit in zip(widths, max_widths)
        ]
    trimmed = [
        [truncate(cell, widths[index]) for index, cell in enumerate(row)]
        for row in row_list
    ]
    header_line = "  ".join(
        header.upper().ljust(widths[index])
        for index, header in enumerate(headers)
    )
    separator = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        for row in trimmed
    ]
    return "\n".join([header_line, separator, *body])


def print_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    max_widths: Sequence[int] | None = None,
) -> None:
    print(table(headers, rows, max_widths=max_widths))
