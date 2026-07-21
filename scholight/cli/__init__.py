"""CLI subcommands — lazy-imported so scheduler commands don't pull search deps."""

from __future__ import annotations

from typing import Any

import click

# ── Lazy subcommand factory ────────────────────────────────────────────


def _lazy(
    module: str, attr: str, name: str, *, help: str = "", group: bool = False
) -> click.Command:
    """Return a lazy subcommand that imports from *module.attr* on first use."""
    if group:
        return _LazyGroup(module, attr, name, help=help)
    return _LazyCommand(module, attr, name, help=help)


class _LazyCommand(click.Command):
    """Import the real :class:`click.Command` on first use."""

    def __init__(self, module: str, attr: str, name: str, help: str) -> None:
        super().__init__(name=name, help=help)
        self._module = module
        self._attr = attr

    def _resolve(self) -> click.Command:
        mod = __import__(self._module, fromlist=[self._attr])
        return getattr(mod, self._attr)  # type: ignore[no-any-return]

    def make_parser(self, ctx: click.Context) -> Any:
        real = self._resolve()
        self.__class__ = real.__class__  # type: ignore[assignment]
        self.__dict__.update(real.__dict__)
        return real.make_parser(ctx)

    def get_short_help_str(self, limit: int = 45) -> str:
        return self.help or self.name  # type: ignore[return-value]


class _LazyGroup(click.Group):
    """Graft subcommands from a lazy-loaded :class:`click.Group`."""

    def __init__(self, module: str, attr: str, name: str, *, help: str = "") -> None:
        super().__init__(name=name, help=help)
        self._module = module
        self._attr = attr
        self._loaded: bool = False

    def _load(self) -> None:
        if self._loaded:
            return
        mod = __import__(self._module, fromlist=[self._attr])
        target: click.Group = getattr(mod, self._attr)
        self.commands.clear()
        self.commands.update(target.commands)
        self._loaded = True

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        self._load()
        return self.commands.get(cmd_name)

    def list_commands(self, ctx: click.Context) -> list[str]:
        self._load()
        return sorted(self.commands.keys())


# ── Top-level CLI ─────────────────────────────────────────────────────


@click.group()
def cli() -> None:
    """Scholight — AI-focused arXiv academic paper search engine."""


cli.add_command(
    _lazy(
        "scholight.cli.scheduler",
        "scheduler_group",
        "scheduler",
        group=True,
        help="Daily arXiv pipeline daemons — paper-sync, pdf-daemon, md-daemon, chunk-daemon, status",
    )
)
cli.add_command(
    _lazy(
        "scholight.cli.store",
        "store_group",
        "store",
        group=True,
        help="Manage Zilliz storage and Scholight PostgreSQL migrations",
    )
)
cli.add_command(
    _lazy(
        "scholight.cli.search",
        "search_cmd",
        "search",
        help="Search arxiv_papers with hybrid dense+sparse retrieval",
    )
)

__all__ = ["cli"]
