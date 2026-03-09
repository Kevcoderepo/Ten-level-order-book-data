"""Textual TUI for Binance order-book depth visualisation."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, Static

from ws_manager import BinanceDepthWSManager

# ── maximum bar width (in characters) ─────────────────────────────────
BAR_MAX = 25


class DepthTable(Widget):
    """Displays ASK10..ASK01 + SPREAD + BID01..BID10 with quantity bars."""

    DEFAULT_CSS = """
    DepthTable {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    """

    snapshot: reactive[dict[str, Any] | None] = reactive(None)
    cn_mode: reactive[bool] = reactive(False)

    def render(self) -> str:
        snap = self.snapshot
        if snap is None:
            return "Waiting for data…"

        ask_color = "green" if self.cn_mode else "red"
        bid_color = "red" if self.cn_mode else "green"

        asks: list[list[str]] = snap["asks"][:10]
        bids: list[list[str]] = snap["bids"][:10]

        # compute max qty for bar scaling
        all_qty = [float(q) for _, q in asks] + [float(q) for _, q in bids]
        max_qty = max(all_qty) if all_qty else 1.0

        lines: list[str] = []

        # header
        symbol = snap["symbol"]
        uid = snap["update_id"]
        lines.append(f"  {symbol}  Depth-10  │  UpdateId: {uid}")
        lines.append(f"  {'─' * 60}")
        lines.append(f"  {'Level':<8} {'Price':>12}  {'Qty':>12}  Bar")
        lines.append(f"  {'─' * 60}")

        # asks – reversed so highest ask on top
        for i, (price, qty) in enumerate(reversed(asks), 1):
            rank = 11 - i
            fp, fq = float(price), float(qty)
            bar_len = int(fq / max_qty * BAR_MAX) if max_qty else 0
            bar = "█" * bar_len
            lines.append(
                f"  ASK{rank:02d}   {fp:>12.2f}  {fq:>12.6f}  [{ask_color}]{bar}[/]"
            )

        # spread
        if asks and bids:
            best_ask = float(asks[0][0])
            best_bid = float(bids[0][0])
            spread = best_ask - best_bid
            spread_pct = spread / best_ask * 100 if best_ask else 0
            lines.append(
                f"  {'─' * 18}  SPREAD  {spread:.2f}  ({spread_pct:.4f}%)  {'─' * 10}"
            )
        else:
            lines.append(f"  {'─' * 18}  SPREAD  {'─' * 22}")

        # bids
        for i, (price, qty) in enumerate(bids, 1):
            fp, fq = float(price), float(qty)
            bar_len = int(fq / max_qty * BAR_MAX) if max_qty else 0
            bar = "█" * bar_len
            lines.append(
                f"  BID{i:02d}   {fp:>12.2f}  {fq:>12.6f}  [{bid_color}]{bar}[/]"
            )

        lines.append(f"  {'─' * 60}")
        return "\n".join(lines)


class StatusBar(Static):
    """Bottom bar: messages / reconnects / uptime / state."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 2;
    }
    """

    def update_stats(self, snap: dict[str, Any], paused: bool = False) -> None:
        msgs = snap["total_messages"]
        recon = snap["total_reconnects"]
        uptime = snap["uptime"]
        state = snap["state"]
        text = (
            f" Messages: {msgs}  │  Reconnects: {recon}"
            f"  │  Uptime: {uptime}  │  {state}"
        )
        if paused:
            text += "  │  ⏸ PAUSED"
        self.update(text)


class DepthApp(App):
    """Main TUI application."""

    TITLE = "Binance Depth"
    CSS = """
    Screen {
        layout: vertical;
    }
    #header {
        dock: top;
        height: 1;
        background: $accent;
        color: $text;
        text-align: center;
        padding: 0 2;
    }
    #symbol-input {
        dock: top;
        height: 1;
        display: none;
    }
    #symbol-input.visible {
        display: block;
    }
    """
    BINDINGS = [
        ("escape", "quit", "Quit"),
        ("f2", "toggle_color", "Toggle Color"),
    ]

    def __init__(self, symbol: str = "btcusdt") -> None:
        super().__init__()
        self._symbol = symbol
        self._manager: BinanceDepthWSManager | None = None
        self._paused = False

    def compose(self) -> ComposeResult:
        yield Static(
            f" Binance {self._symbol.upper()} Order Book",
            id="header",
        )
        yield Input(
            placeholder="Enter symbol (e.g. ethusdt) then press Enter, Esc to cancel",
            id="symbol-input",
        )
        yield DepthTable()
        yield StatusBar("")

    def on_mount(self) -> None:
        self._manager = BinanceDepthWSManager(
            symbol=self._symbol,
            on_data=self._on_depth_data,
        )
        self.run_worker(self._manager.run_async(), exclusive=True)
        self.set_focus(None)

    def _on_depth_data(self, snap: dict[str, Any]) -> None:
        """Called from ws_manager on each message (same asyncio loop)."""
        try:
            self.query_one(StatusBar).update_stats(snap, paused=self._paused)
            if self._paused:
                return
            self.query_one(DepthTable).snapshot = snap
        except Exception:
            pass  # widget not yet mounted

    # ── symbol switching ─────────────────────────────────────────
    def on_key(self, event) -> None:
        inp = self.query_one("#symbol-input", Input)
        # input visible: Esc hides it
        if inp.has_class("visible"):
            if event.key == "escape":
                inp.remove_class("visible")
                inp.value = ""
                self.set_focus(None)
                event.prevent_default()
                event.stop()
            return
        # input hidden: space toggles pause
        if event.key == "space":
            self._paused = not self._paused
            event.prevent_default()
            event.stop()
            return
        # input hidden: any letter/digit opens it
        if len(event.character or "") == 1 and event.character.isalnum():
            inp.value = event.character
            inp.add_class("visible")
            inp.focus()
            inp.cursor_position = len(inp.value)
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inp = self.query_one("#symbol-input", Input)
        symbol = event.value.strip().lower()
        inp.value = ""
        inp.remove_class("visible")
        self.set_focus(None)
        if symbol and symbol != self._symbol:
            self._switch_to(symbol)

    def _switch_to(self, symbol: str) -> None:
        # stop old manager
        if self._manager:
            self._manager.shutdown()
        self._symbol = symbol
        self._paused = False
        # clear table
        self.query_one(DepthTable).snapshot = None
        # update header
        self.query_one("#header", Static).update(
            f" Binance {symbol.upper()} Order Book"
        )
        # start new manager
        self._manager = BinanceDepthWSManager(
            symbol=symbol,
            on_data=self._on_depth_data,
        )
        self.run_worker(self._manager.run_async(), exclusive=True)

    def action_toggle_color(self) -> None:
        table = self.query_one(DepthTable)
        table.cn_mode = not table.cn_mode

    def action_quit(self) -> None:
        if self._manager:
            self._manager.shutdown()
        self.exit()
