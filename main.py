import sys

from tui_app import DepthApp


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "btcusdt"
    app = DepthApp(symbol)
    app.run()


if __name__ == "__main__":
    main()
