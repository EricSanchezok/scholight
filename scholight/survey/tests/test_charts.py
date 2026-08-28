"""Survey declarative chart block rendering contracts."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from scholight.survey.charts import (
    ChartSpecError,
    extract_chart_blocks,
    render_chart,
    render_section_charts,
    validate_chart_spec,
)


def _spec_line() -> dict[str, object]:
    return {
        "type": "line",
        "title": "Growth",
        "x": [2018, 2019, 2020],
        "series": [
            {"name": "papers", "y": [1.0, 4.0, 9.0]},
            {"name": "citations", "y": [2.0, 3.0, 5.0]},
        ],
    }


def _spec_bar() -> dict[str, object]:
    return {
        "type": "bar",
        "x": ["CNN", "RNN", "MLP"],
        "series": [{"name": "accuracy", "y": [0.8, 0.7, 0.6]}],
        "orientation": "horizontal",
        "x_label": "Family",
        "y_label": "Accuracy",
    }


def _spec_grouped_bar() -> dict[str, object]:
    return {
        "type": "grouped_bar",
        "x": ["2020", "2021"],
        "series": [
            {"name": "papers", "y": [10.0, 12.0]},
            {"name": "citations", "y": [30.0, 44.0]},
        ],
    }


def _spec_scatter() -> dict[str, object]:
    return {
        "type": "scatter",
        "x": [1.5, 2.5, 3.5],
        "series": [{"name": "points", "y": [2.0, 3.0, 4.0]}],
    }


def _spec_pie() -> dict[str, object]:
    return {
        "type": "pie",
        "labels": ["transformer", "cnn", "rnn"],
        "series": [{"name": "share", "y": [60.0, 30.0, 10.0]}],
    }


def _spec_flow() -> dict[str, object]:
    return {
        "type": "flow",
        "direction": "LR",
        "nodes": [
            {"id": "input", "label": "Input"},
            {"id": "encoder", "label": "Encoder"},
            {"id": "output", "label": "Output"},
        ],
        "edges": [
            {"from": "input", "to": "encoder", "label": "embed"},
            {"from": "encoder", "to": "output"},
        ],
    }


_LEGAL_FACTORIES: tuple[Callable[[], dict[str, object]], ...] = (
    _spec_line,
    _spec_bar,
    _spec_grouped_bar,
    _spec_scatter,
    _spec_pie,
    _spec_flow,
)


@pytest.mark.parametrize("spec_factory", _LEGAL_FACTORIES, ids=lambda factory: factory.__name__)
def test_validate_accepts_each_legal_chart_type(
    spec_factory: Callable[[], dict[str, object]],
) -> None:
    spec = spec_factory()

    assert validate_chart_spec(spec) == spec


def _invalid_specs() -> list[tuple[str, dict[str, object]]]:
    return [
        (
            "missing_type",
            {"x": [1.0], "series": [{"name": "a", "y": [1.0]}]},
        ),
        (
            "unknown_type",
            {"type": "area", "x": [1.0], "series": [{"name": "a", "y": [1.0]}]},
        ),
        (
            "missing_x",
            {"type": "line", "series": [{"name": "a", "y": [1.0]}]},
        ),
        ("unknown_field", {**_spec_line(), "color": "red"}),
        ("empty_series", {"type": "line", "x": [1.0], "series": []}),
        (
            "series_length_mismatch",
            {"type": "line", "x": [1.0, 2.0], "series": [{"name": "a", "y": [1.0]}]},
        ),
        (
            "non_finite_value",
            {
                "type": "line",
                "x": [1.0, float("nan")],
                "series": [{"name": "a", "y": [1.0, 2.0]}],
            },
        ),
        (
            "boolean_in_x",
            {"type": "scatter", "x": [True, 2], "series": [{"name": "a", "y": [1.0, 2.0]}]},
        ),
        (
            "unknown_series_field",
            {"type": "bar", "x": ["a"], "series": [{"name": "a", "values": [1.0]}]},
        ),
        (
            "bar_with_multiple_series",
            {
                "type": "bar",
                "x": ["a"],
                "series": [{"name": "a", "y": [1.0]}, {"name": "b", "y": [2.0]}],
            },
        ),
        (
            "grouped_bar_with_single_series",
            {"type": "grouped_bar", "x": ["a"], "series": [{"name": "a", "y": [1.0]}]},
        ),
        ("invalid_orientation", {**_spec_bar(), "orientation": "diagonal"}),
        ("orientation_on_pie", {**_spec_pie(), "orientation": "vertical"}),
        (
            "pie_negative_value",
            {"type": "pie", "labels": ["a", "b"], "series": [{"name": "s", "y": [-1.0, 2.0]}]},
        ),
        (
            "pie_zero_sum",
            {"type": "pie", "labels": ["a", "b"], "series": [{"name": "s", "y": [0.0, 0.0]}]},
        ),
        (
            "pie_single_label",
            {"type": "pie", "labels": ["a"], "series": [{"name": "s", "y": [1.0]}]},
        ),
        (
            "too_many_series",
            {
                "type": "grouped_bar",
                "x": ["a"],
                "series": [{"name": f"s{index}", "y": [1.0]} for index in range(9)],
            },
        ),
        (
            "too_many_points",
            {"type": "line", "x": list(range(201)), "series": [{"name": "a", "y": [1.0] * 201}]},
        ),
        (
            "category_too_long",
            {"type": "bar", "x": ["x" * 201], "series": [{"name": "a", "y": [1.0]}]},
        ),
        ("title_too_long", {**_spec_line(), "title": "x" * 201}),
        (
            "flow_edge_to_missing_node",
            {**_spec_flow(), "edges": [{"from": "input", "to": "ghost"}]},
        ),
        (
            "flow_duplicate_node_id",
            {
                "type": "flow",
                "nodes": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}],
                "edges": [{"from": "a", "to": "a"}],
            },
        ),
        (
            "flow_node_without_label",
            {"type": "flow", "nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "a"}]},
        ),
        ("flow_with_series_field", {**_spec_flow(), "series": []}),
        (
            "flow_without_edges",
            {"type": "flow", "nodes": [{"id": "a", "label": "A"}], "edges": []},
        ),
        ("invalid_direction", {**_spec_flow(), "direction": "RL"}),
    ]


@pytest.mark.parametrize("case,spec", _invalid_specs())
def test_validate_rejects_invalid_chart_specs(case: str, spec: dict[str, object]) -> None:
    with pytest.raises(ChartSpecError):
        validate_chart_spec(spec)


def test_extract_chart_blocks_returns_exact_spans_and_skips_other_fences() -> None:
    first = '```chart\n{"type": "line", "x": [1], "series": [{"name": "a", "y": [1]}]}\n```'
    second = '```chart\n{"type": "scatter", "x": [1], "series": [{"name": "b", "y": [2]}]}\n```'
    third = (
        '   ```chart\n{"type": "pie", "labels": ["a", "b"], '
        '"series": [{"name": "s", "y": [1, 2]}]}\n   ```'
    )
    text = f"# Heading\n\n{first}\n\n```python\nprint(1)\n```\n\n{second}\n\n{third}\n"

    blocks = extract_chart_blocks(text)

    assert [text[start:end] for _, (start, end) in blocks] == [first, second, third]
    assert blocks[0][0] == {"type": "line", "x": [1], "series": [{"name": "a", "y": [1]}]}
    assert blocks[1][0] == {"type": "scatter", "x": [1], "series": [{"name": "b", "y": [2]}]}
    assert blocks[2][0] == {
        "type": "pie",
        "labels": ["a", "b"],
        "series": [{"name": "s", "y": [1, 2]}],
    }


def test_extract_chart_blocks_marks_unparseable_bodies() -> None:
    blocks = extract_chart_blocks("```chart\nnot json\n```")

    assert blocks[0][0] == {"__invalid__": "not json\n"}


def test_extract_chart_blocks_ignores_unterminated_block() -> None:
    assert extract_chart_blocks('```chart\n{"type": "line"}\n') == []


@pytest.mark.parametrize(
    "spec_factory",
    (_spec_line, _spec_bar, _spec_grouped_bar, _spec_scatter, _spec_pie),
    ids=lambda factory: factory.__name__,
)
def test_render_chart_writes_png(
    spec_factory: Callable[[], dict[str, object]],
    tmp_path: Path,
) -> None:
    output = tmp_path / "chart.png"

    render_chart(spec_factory(), output)

    assert output.read_bytes()[:4] == b"\x89PNG"


def test_render_flow_writes_png_or_fails_without_dot(tmp_path: Path) -> None:
    output = tmp_path / "flow.png"

    if shutil.which("dot") is None:
        with pytest.raises(ChartSpecError):
            render_chart(_spec_flow(), output)
    else:
        render_chart(_spec_flow(), output)

        assert output.read_bytes()[:4] == b"\x89PNG"


def test_render_chart_validates_before_rendering(tmp_path: Path) -> None:
    with pytest.raises(ChartSpecError):
        render_chart({"type": "line"}, tmp_path / "chart.png")

    assert not (tmp_path / "chart.png").exists()


def test_render_section_charts_replaces_valid_blocks_and_counts_rejected(
    tmp_path: Path,
) -> None:
    figures_dir = tmp_path / "figures"
    text = (
        "# Attention family\n\n"
        "Intro paragraph.\n\n"
        f"```chart\n{json.dumps(_spec_line())}\n```\n\n"
        "Middle paragraph.\n\n"
        "```chart\nnot json\n```\n\n"
        f"```chart\n{json.dumps({'type': 'line'})}\n```\n\n"
        f"```chart\n{json.dumps(_spec_pie())}\n```\n\n"
        "Closing paragraph."
    )

    new_text, rendered, rejected = render_section_charts(
        text,
        figures_dir,
        prefix="03-attention-family",
    )

    assert rendered == 2
    assert rejected == 2
    assert "```chart" not in new_text
    assert "not json" not in new_text
    assert "![Growth](figures/03-attention-family-1.png)" in new_text
    assert "![Figure](figures/03-attention-family-2.png)" in new_text
    assert "Intro paragraph." in new_text
    assert "Closing paragraph." in new_text
    assert (figures_dir / "03-attention-family-1.png").read_bytes()[:4] == b"\x89PNG"
    assert (figures_dir / "03-attention-family-2.png").read_bytes()[:4] == b"\x89PNG"


def test_render_section_charts_escapes_markdown_characters_in_captions(
    tmp_path: Path,
) -> None:
    spec = {
        "type": "bar",
        "caption": "Share (2020) [all]",
        "x": ["cnn"],
        "series": [{"name": "share", "y": [1.0]}],
    }
    text = f"```chart\n{json.dumps(spec)}\n```"

    new_text, rendered, rejected = render_section_charts(text, tmp_path / "figures", prefix="esc")

    assert (rendered, rejected) == (1, 0)
    assert "![Share \\(2020\\) \\[all\\]](figures/esc-1.png)" in new_text


def test_render_section_charts_keeps_surrounding_text_when_all_blocks_rejected(
    tmp_path: Path,
) -> None:
    text = "Keep this.\n\n```chart\nnot json\n```\n\nAlso keep this."

    new_text, rendered, rejected = render_section_charts(
        text,
        tmp_path / "figures",
        prefix="x",
    )

    assert (rendered, rejected) == (0, 1)
    assert "Keep this." in new_text
    assert "Also keep this." in new_text
    assert "not json" not in new_text


def test_render_section_charts_returns_text_unchanged_without_chart_blocks(
    tmp_path: Path,
) -> None:
    text = "Plain section text."

    assert render_section_charts(text, tmp_path / "figures", prefix="p") == (text, 0, 0)
