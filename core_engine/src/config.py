import yaml
import os


class Config:
    def __init__(self, path: str = "config.yaml"):
        self._path = path
        with open(path, "r") as f:
            self._raw = yaml.safe_load(f)

    def reload(self):
        """Re-read the config file from disk so live changes (e.g. AI keys
        set via the control bot) are picked up without restarting the engine.
        Best-effort: on any read/parse error the previous config is kept."""
        try:
            with open(self._path, "r") as f:
                self._raw = yaml.safe_load(f)
        except Exception:
            pass  # keep the last-known-good config

    @property
    def network(self) -> str:
        return self._raw.get("network", "testnet")

    @property
    def hyperliquid(self) -> dict:
        return self._raw["hyperliquid"]

    @property
    def llm(self) -> dict:
        return self._raw["llm"]

    @property
    def telegram(self) -> dict:
        return self._raw["telegram"]

    @property
    def symbols(self) -> list:
        return self._raw.get("symbols", ["BTC"])

    @property
    def timeframes(self) -> list:
        return self._raw.get("timeframes", ["1h"])

    @property
    def loop_interval_seconds(self) -> int:
        return self._raw.get("loop_interval_seconds", 900)

    @property
    def hyperliquid_config(self) -> dict:
        net = self.network
        return self._raw.get("hyperliquid", {}).get(net, {})

    @property
    def hyperliquid_account_address(self) -> str:
        return self.hyperliquid_config.get("account_address", "")

    @property
    def hyperliquid_secret_key(self) -> str:
        return self.hyperliquid_config.get("secret_key", "")

    @property
    def shared_db_path(self) -> str:
        base = os.path.dirname(os.path.abspath(__file__))
        return os.path.normpath(os.path.join(base, "..", self._raw.get("shared_db_path", "../shared/bot_state.db")))

    @property
    def defaults(self) -> dict:
        return self._raw.get("defaults", {})