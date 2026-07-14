from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .config import ConfigError, load_config


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="独立的微信-WeFlow-MaiBot 双向桥接")
    value.add_argument("--config", default="config.toml", help="TOML 配置路径")
    value.add_argument("--check-config", action="store_true", help="仅验证配置")
    value.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return value


async def _run(app) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
    task = asyncio.create_task(app.run())
    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if stop_task in done:
        await app.stop()
        await asyncio.gather(task, return_exceptions=True)
    else:
        stop_task.cancel()
        await task


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(Path(args.config))
        if args.check_config:
            print("配置有效")
            return 0
        from .app import BridgeApp

        asyncio.run(_run(BridgeApp(config)))
        return 0
    except (ConfigError, RuntimeError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
