"""Separate-process lease contender; exits without mutating a claimed thread."""

import asyncio
import sys
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from novacode_cli.session.checkpoint_coordinator import CoordinatedSqliteSaver, CheckpointOwnershipError


async def main():
    async with aiosqlite.connect(sys.argv[1]) as conn:
        saver = CoordinatedSqliteSaver(conn)
        try:
            lease = await saver.acquire("thread")
        except CheckpointOwnershipError as error:
            print(str(error))
            return 3
        await saver.release(lease)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
