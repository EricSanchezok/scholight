"""Deterministic rendering of declarative chart blocks inside Survey sections."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

import graphviz
import matplotlib
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

matplotlib.use("Agg")

_ChartSpec = dict[str, Any]

_CHART_TYPES = frozenset({"line", "bar", "grouped_bar", "scatter", "pie", "flow"})
_TEXT_MAX_CHARS = 200
_SERIES_MAX = 8
_POINTS_MAX = 200
_CATEGORY_MAX = 60
_PIE_SLICES_MAX = 12
_FLOW_NODES_MAX = 30
_FLOW_EDGES_MAX = 60
_CHART_BODY_MAX_BYTES = 8192
_PALETTE = (
    "#4C72B0",
    "#DD8452",
    "#55A868",
    "#C44E52",
    "#8172B3",
    "#937860",
    "#DA8BC3",
    "#8C8C8C",
)
_INVALID_SPEC_MARKER = "__invalid__"
_CHART_FENCE_OPEN = re.compile(r"(?m)^( {0,3})```chart[ \t]*$")
_CHART_FENCE_CLOSE = re.compile(r"(?m)^ {0,3}```+[ \t]*$")

_BASE_FIELDS = frozenset({"type", "title", "caption"})
_XY_FIELDS = _BASE_FIELDS | {"x", "series"}
_BAR_FIELDS = _BASE_FIELDS | {"x", "series", "orientation", "x_label", "y_label"}
_PIE_FIELDS = _BASE_FIELDS | {"labels", "series"}
_FLOW_FIELDS = _BASE_FIELDS | {"direction", "nodes", "edges"}


class ChartSpecError(Exception):
    """A stable, client-safe declarative chart spec failure."""


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_common_fields(spec: _ChartSpec, allowed: frozenset[str]) -> None:
    for key in spec:
        if key not in allowed:
            raise ChartSpecError(f"unknown chart field: {key}")
    for key in ("title", "caption"):
        if key in spec:
            value = spec[key]
            if not isinstance(value, str):
                raise ChartSpecError(f"chart {key} must be a string")
            if len(value) > _TEXT_MAX_CHARS:
                raise ChartSpecError(f"chart {key} exceeds {_TEXT_MAX_CHARS} characters")


def _validated_numeric_axis(spec: _ChartSpec, key: str) -> list[float]:
    values = spec.get(key)
    if not isinstance(values, list) or not values or len(values) > _POINTS_MAX:
        raise ChartSpecError(
            f"chart {key} must be a non-empty list of at most {_POINTS_MAX} values"
        )
    if not all(_is_finite_number(value) for value in values):
        raise ChartSpecError(f"chart {key} values must be finite numbers")
    return values


def _validated_series(spec: _ChartSpec, *, minimum: int, maximum: int) -> list[_ChartSpec]:
    series = spec.get("series")
    if not isinstance(series, list) or not minimum <= len(series) <= maximum:
        raise ChartSpecError(f"chart series count must be between {minimum} and {maximum}")
    for entry in series:
        if not isinstance(entry, dict) or set(entry) != {"name", "y"}:
            raise ChartSpecError("chart series entry must contain exactly name and y")
        name = entry["name"]
        if not isinstance(name, str):
            raise ChartSpecError("chart series name must be a string")
        values = entry["y"]
        if not isinstance(values, list) or not values or len(values) > _POINTS_MAX:
            raise ChartSpecError(
                f"chart series y must be a non-empty list of at most {_POINTS_MAX} values"
            )
        if not all(_is_finite_number(value) for value in values):
            raise ChartSpecError("chart series y values must be finite numbers")
    return series


def _validated_categories(spec: _ChartSpec) -> list[str]:
    categories = spec.get("x")
    if not isinstance(categories, list) or not categories or len(categories) > _CATEGORY_MAX:
        raise ChartSpecError(
            f"chart x must be a non-empty list of at most {_CATEGORY_MAX} categories"
        )
    if not all(isinstance(value, str) and len(value) <= _TEXT_MAX_CHARS for value in categories):
        raise ChartSpecError(
            f"chart x categories must be strings of at most {_TEXT_MAX_CHARS} characters"
        )
    return categories


def _validated_orientation(spec: _ChartSpec) -> None:
    orientation = spec.get("orientation", "vertical")
    if orientation not in ("vertical", "horizontal"):
        raise ChartSpecError("chart orientation must be vertical or horizontal")


def _validated_axis_labels(spec: _ChartSpec) -> None:
    for key in ("x_label", "y_label"):
        if key in spec:
            value = spec[key]
            if not isinstance(value, str) or len(value) > _TEXT_MAX_CHARS:
                raise ChartSpecError(
                    f"chart {key} must be a string of at most {_TEXT_MAX_CHARS} characters"
                )


def _validate_xy_chart(spec: _ChartSpec) -> None:
    _check_common_fields(spec, _XY_FIELDS)
    x_values = _validated_numeric_axis(spec, "x")
    series = _validated_series(spec, minimum=1, maximum=_SERIES_MAX)
    if any(len(entry["y"]) != len(x_values) for entry in series):
        raise ChartSpecError("chart series y length must match x length")


def _validate_bar_chart(spec: _ChartSpec) -> None:
    _check_common_fields(spec, _BAR_FIELDS)
    _validated_categories(spec)
    _validated_series(spec, minimum=1, maximum=1)
    _validated_orientation(spec)
    _validated_axis_labels(spec)


def _validate_grouped_bar_chart(spec: _ChartSpec) -> None:
    _check_common_fields(spec, _BAR_FIELDS)
    _validated_categories(spec)
    _validated_series(spec, minimum=2, maximum=_SERIES_MAX)
    _validated_orientation(spec)
    _validated_axis_labels(spec)


def _validate_pie_chart(spec: _ChartSpec) -> None:
    _check_common_fields(spec, _PIE_FIELDS)
    labels = spec.get("labels")
    if (
        not isinstance(labels, list)
        or not 2 <= len(labels) <= _PIE_SLICES_MAX
        or not all(isinstance(value, str) and len(value) <= _TEXT_MAX_CHARS for value in labels)
    ):
        raise ChartSpecError(
            f"chart labels must be 2 to {_PIE_SLICES_MAX} strings "
            f"of at most {_TEXT_MAX_CHARS} characters"
        )
    series = _validated_series(spec, minimum=1, maximum=1)
    values = series[0]["y"]
    if len(values) != len(labels):
        raise ChartSpecError("chart series y length must match labels length")
    if any(value < 0 for value in values) or sum(values) <= 0:
        raise ChartSpecError("chart pie values must be non-negative with a positive sum")


def _validate_flow_chart(spec: _ChartSpec) -> None:
    _check_common_fields(spec, _FLOW_FIELDS)
    direction = spec.get("direction", "TB")
    if direction not in ("LR", "TB"):
        raise ChartSpecError("chart direction must be LR or TB")
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes or len(nodes) > _FLOW_NODES_MAX:
        raise ChartSpecError(
            f"chart nodes must be a non-empty list of at most {_FLOW_NODES_MAX} entries"
        )
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "label"}:
            raise ChartSpecError("chart node must contain exactly id and label")
        node_id = node["id"]
        label = node["label"]
        if not isinstance(node_id, str) or not node_id:
            raise ChartSpecError("chart node id must be a non-empty string")
        if node_id in node_ids:
            raise ChartSpecError("chart node id is duplicated")
        if not isinstance(label, str) or len(label) > _TEXT_MAX_CHARS:
            raise ChartSpecError(
                f"chart node label must be a string of at most {_TEXT_MAX_CHARS} characters"
            )
        node_ids.add(node_id)
    edges = spec.get("edges")
    if not isinstance(edges, list) or not edges or len(edges) > _FLOW_EDGES_MAX:
        raise ChartSpecError(
            f"chart edges must be a non-empty list of at most {_FLOW_EDGES_MAX} entries"
        )
    for edge in edges:
        if (
            not isinstance(edge, dict)
            or set(edge) - {"from", "to", "label"}
            or not {"from", "to"} <= set(edge)
        ):
            raise ChartSpecError("chart edge must contain from and to with an optional label")
        source = edge["from"]
        target = edge["to"]
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in node_ids
            or target not in node_ids
        ):
            raise ChartSpecError("chart edge must reference existing nodes")
        if "label" in edge and not isinstance(edge["label"], str):
            raise ChartSpecError("chart edge label must be a string")


def validate_chart_spec(spec: _ChartSpec) -> _ChartSpec:
    """Strictly validate one declarative chart spec, rejecting unknown fields."""
    if not isinstance(spec, dict):
        raise ChartSpecError("chart spec must be an object")
    chart_type = spec.get("type")
    if not isinstance(chart_type, str) or chart_type not in _CHART_TYPES:
        raise ChartSpecError("chart type is unsupported")
    if chart_type in ("line", "scatter"):
        _validate_xy_chart(spec)
    elif chart_type == "bar":
        _validate_bar_chart(spec)
    elif chart_type == "grouped_bar":
        _validate_grouped_bar_chart(spec)
    elif chart_type == "pie":
        _validate_pie_chart(spec)
    else:
        _validate_flow_chart(spec)
    return spec


def _parse_chart_body(raw_body: str) -> _ChartSpec:
    if len(raw_body.encode("utf-8")) > _CHART_BODY_MAX_BYTES:
        return {_INVALID_SPEC_MARKER: raw_body}
    try:
        parsed = json.loads(raw_body)
    except ValueError:
        return {_INVALID_SPEC_MARKER: raw_body}
    if not isinstance(parsed, dict):
        return {_INVALID_SPEC_MARKER: raw_body}
    return parsed


def extract_chart_blocks(text: str) -> list[tuple[_ChartSpec, tuple[int, int]]]:
    """Extract every fenced chart block with its (start, end) span in the text."""
    blocks: list[tuple[_ChartSpec, tuple[int, int]]] = []
    position = 0
    while True:
        opened = _CHART_FENCE_OPEN.search(text, position)
        if opened is None:
            return blocks
        body_start = opened.end() + 1
        closed = _CHART_FENCE_CLOSE.search(text, body_start)
        if closed is None:
            return blocks
        raw_body = text[body_start : closed.start()]
        blocks.append((_parse_chart_body(raw_body), (opened.start(), closed.end())))
        position = closed.end()


def _write_figure_png(figure: Figure, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp.png")
    figure.savefig(temporary, format="png")
    os.replace(temporary, output_path)


def _draw_pie(axes: Axes, spec: _ChartSpec) -> None:
    values = spec["series"][0]["y"]
    colors = [_PALETTE[index % len(_PALETTE)] for index in range(len(values))]
    axes.pie(values, labels=spec["labels"], colors=colors, autopct="%1.1f%%")


def _draw_series_plot(axes: Axes, spec: _ChartSpec, *, scatter: bool) -> None:
    x_values = spec["x"]
    for index, entry in enumerate(spec["series"]):
        color = _PALETTE[index % len(_PALETTE)]
        if scatter:
            axes.scatter(x_values, entry["y"], color=color, label=entry["name"])
        else:
            axes.plot(
                x_values,
                entry["y"],
                color=color,
                marker="o",
                linewidth=2,
                label=entry["name"],
            )
    axes.legend()


def _draw_bars(axes: Axes, spec: _ChartSpec, *, grouped: bool) -> None:
    categories = spec["x"]
    series = spec["series"]
    horizontal = spec.get("orientation", "vertical") == "horizontal"
    count = len(series) if grouped else 1
    width = 0.8 / count
    for index, entry in enumerate(series):
        color = _PALETTE[index % len(_PALETTE)]
        positions = [base + (index - (count - 1) / 2) * width for base in range(len(categories))]
        if horizontal:
            axes.barh(positions, entry["y"], height=width, color=color, label=entry["name"])
        else:
            axes.bar(positions, entry["y"], width=width, color=color, label=entry["name"])
    ticks = range(len(categories))
    if horizontal:
        axes.set_yticks(ticks, categories)
    else:
        axes.set_xticks(ticks, categories)
    if len(series) > 1:
        axes.legend()
    x_label = spec.get("x_label")
    if x_label is not None:
        axes.set_xlabel(x_label)
    y_label = spec.get("y_label")
    if y_label is not None:
        axes.set_ylabel(y_label)


def _render_matplotlib_chart(spec: _ChartSpec, output_path: Path) -> None:
    figure = Figure(figsize=(8.0, 4.5), dpi=150)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(1, 1, 1)
    chart_type = spec["type"]
    if chart_type == "pie":
        _draw_pie(axes, spec)
    elif chart_type in ("line", "scatter"):
        _draw_series_plot(axes, spec, scatter=chart_type == "scatter")
    else:
        _draw_bars(axes, spec, grouped=chart_type == "grouped_bar")
    title = spec.get("title")
    if title is not None:
        axes.set_title(title)
    _write_figure_png(figure, output_path)


def _render_flow_chart(spec: _ChartSpec, output_path: Path) -> None:
    if shutil.which("dot") is None:
        raise ChartSpecError("graphviz dot is unavailable")
    graph = graphviz.Digraph(graph_attr={"dpi": "150", "rankdir": spec.get("direction", "TB")})
    title = spec.get("title")
    if title is not None:
        graph.attr("graph", label=title, labelloc="t")
    for node in spec["nodes"]:
        graph.node(node["id"], label=node["label"])
    for edge in spec["edges"]:
        edge_label = edge.get("label")
        if edge_label is None:
            graph.edge(edge["from"], edge["to"])
        else:
            graph.edge(edge["from"], edge["to"], label=edge_label)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp.png")
    graph.render(outfile=str(temporary), format="png", cleanup=True)
    os.replace(temporary, output_path)


def render_chart(spec: _ChartSpec, output_path: Path) -> None:
    """Render one chart spec to a PNG file after strict validation."""
    validated = validate_chart_spec(spec)
    if validated["type"] == "flow":
        _render_flow_chart(validated, output_path)
    else:
        _render_matplotlib_chart(validated, output_path)


def _alt_text(spec: _ChartSpec) -> str:
    label = spec.get("caption") or spec.get("title") or "Figure"
    return label.replace("[", "\\[").replace("]", "\\]").replace("(", "\\(").replace(")", "\\)")


def render_section_charts(
    section_text: str,
    figures_dir: Path,
    *,
    prefix: str,
    render_budget: int | None = None,
) -> tuple[str, int, int]:
    """Render chart blocks, replacing valid blocks and discarding invalid ones.

    ``render_budget`` caps how many blocks this call may render; blocks beyond
    the budget are dropped and counted as rejected so callers can enforce a
    document-wide maximum across sections.
    """
    figures_dir.mkdir(parents=True, exist_ok=True)
    replacements: list[tuple[int, int, str]] = []
    rendered_count = 0
    rejected_count = 0
    for spec, (start, end) in extract_chart_blocks(section_text):
        if render_budget is not None and rendered_count >= render_budget:
            replacements.append((start, end, ""))
            rejected_count += 1
            continue
        filename = f"{prefix}-{rendered_count + 1}.png"
        try:
            render_chart(spec, figures_dir / filename)
        except Exception:
            replacements.append((start, end, ""))
            rejected_count += 1
            continue
        replacements.append((start, end, f"\n\n![{_alt_text(spec)}](figures/{filename})\n\n"))
        rendered_count += 1
    segments: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        segments.append(section_text[cursor:start])
        segments.append(replacement)
        cursor = end
    segments.append(section_text[cursor:])
    return "".join(segments), rendered_count, rejected_count


__all__ = [
    "ChartSpecError",
    "extract_chart_blocks",
    "render_chart",
    "render_section_charts",
    "validate_chart_spec",
]
