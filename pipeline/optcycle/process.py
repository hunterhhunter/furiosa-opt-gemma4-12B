import os
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    started_at: str
    finished_at: str
    log_path: Path


def run_streaming(
    argv: Sequence[str],
    cwd: Path,
    log_path: Path,
    env: Mapping[str, str] | None,
    stdout: TextIO,
    on_line: Callable[[str], None] | None = None,
) -> CommandResult:
    started_at = datetime.now(timezone.utc).isoformat()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = None if env is None else {**os.environ, **env}
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                stdout.write(line)
                stdout.flush()
                log.write(line)
                log.flush()
                if on_line is not None:
                    on_line(line)
            exit_code = process.wait()
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
    return CommandResult(
        argv=tuple(argv),
        exit_code=exit_code,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        log_path=log_path,
    )
