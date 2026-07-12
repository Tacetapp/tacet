"""Manages the Swift CaptureHelper subprocess for audio and screen capture."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Binary protocol message types
MSG_AUDIO = 0x01          # system audio
MSG_SCREEN_FRAME = 0x02
MSG_STATUS = 0x03
MSG_MIC_AUDIO = 0x04      # microphone audio

# Default path to the Swift binary
DEFAULT_BINARY = os.path.join(os.path.dirname(__file__), "..", "..", "bin", "capture-helper")


class AudioCaptureSource:
    """Manages Swift capture subprocess, dispatches audio and screen frames."""

    def __init__(self, binary_path: str = DEFAULT_BINARY):
        self.binary_path = binary_path
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None
        self._running = False
        self.on_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.on_mic_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.on_frame: Callable[[bytes], Awaitable[None]] | None = None
        self.on_status: Callable[[dict], Awaitable[None]] | None = None
        self.available_apps: list[dict] = []

    async def start(
        self,
        system_audio: bool = True,
        mic: bool = True,
        screen_interval: float = 10.0,
        screen_target: str | None = None,
        screen_app_bundle: str | None = None,
    ):
        """Launch the capture helper and start capturing."""
        if self._running:
            logger.warning("Capture already running")
            return

        if not os.path.exists(self.binary_path):
            raise FileNotFoundError(
                f"Capture helper not found at {self.binary_path}. "
                "Run scripts/build_swift.sh to build it."
            )

        self._process = await asyncio.create_subprocess_exec(
            self.binary_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True

        # Start reading stdout and stderr in background
        self._read_task = asyncio.create_task(self._read_loop())
        self._read_task.add_done_callback(self._task_done)
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._stderr_task.add_done_callback(self._task_done)

        # Wait briefly for the "ready" status
        await asyncio.sleep(0.2)

        # Send start command
        cmd = {
            "command": "start",
            "system_audio": system_audio,
            "mic": mic,
            "screen_interval": screen_interval,
        }
        if screen_target:
            cmd["screen_target"] = screen_target
        if screen_app_bundle:
            cmd["screen_app_bundle"] = screen_app_bundle

        await self._send_command(cmd)

    async def stop(self):
        """Stop capture and terminate the subprocess."""
        if not self._running:
            return
        self._running = False

        if self._process and self._process.stdin:
            try:
                await self._send_command({"command": "stop"})
                # Give it a moment to clean up
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._process.terminate()
                    await self._process.wait()
            except Exception as e:
                logger.warning(f"Error stopping capture: {e}")
                if self._process:
                    self._process.kill()

        for task in [self._read_task, getattr(self, '_stderr_task', None)]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._process = None
        self._read_task = None
        self._stderr_task = None

    async def enumerate_apps(self):
        """Request the helper to enumerate available apps."""
        await self._send_command({"command": "enumerate"})

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None and self._process.returncode is None

    async def _send_command(self, cmd: dict):
        """Send a JSON command to the helper's stdin."""
        if self._process and self._process.stdin:
            line = json.dumps(cmd) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()

    async def _read_loop(self):
        """Read framed messages from stdout and dispatch to callbacks."""
        stdout = self._process.stdout
        if not stdout:
            return

        audio_chunks = 0
        frame_count = 0
        try:
            while self._running:
                # Read header: 1 byte type + 4 bytes length
                header = await stdout.readexactly(5)
                msg_type = header[0]
                payload_len = struct.unpack(">I", header[1:5])[0]

                # Read payload
                payload = await stdout.readexactly(payload_len)

                if msg_type == MSG_AUDIO and self.on_audio:
                    audio_chunks += 1
                    if audio_chunks == 1:
                        logger.info(f"First system audio chunk received ({payload_len} bytes)")
                    elif audio_chunks % 100 == 0:
                        logger.info(f"System audio chunks received: {audio_chunks}")
                    await self.on_audio(payload)
                elif msg_type == MSG_MIC_AUDIO and self.on_mic_audio:
                    await self.on_mic_audio(payload)
                elif msg_type == MSG_SCREEN_FRAME and self.on_frame:
                    frame_count += 1
                    logger.info(f"Screen frame #{frame_count} received ({payload_len} bytes)")
                    await self.on_frame(payload)
                elif msg_type == MSG_STATUS and self.on_status:
                    try:
                        status = json.loads(payload)
                        logger.info(f"Capture status: {status}")
                        # Cache available apps
                        if status.get("type") == "apps_available":
                            self.available_apps = status.get("apps", [])
                        await self.on_status(status)
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid status JSON: {payload[:100]}")

        except asyncio.IncompleteReadError:
            if self._running:
                logger.warning("Capture helper stdout closed unexpectedly")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._running:
                logger.error(f"Read loop error: {e}", exc_info=True)
        finally:
            self._running = False

    async def _read_stderr(self):
        """Read stderr from the capture helper and log it."""
        stderr = self._process.stderr
        if not stderr:
            return
        _ERROR_KEYWORDS = ("error", "fail", "denied", "cannot")
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    lower = text.lower()
                    if any(kw in lower for kw in _ERROR_KEYWORDS):
                        logger.warning(f"[capture-helper] {text}")
                    else:
                        logger.info(f"[capture-helper] {text}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"stderr reader ended: {e}")

    @staticmethod
    def _task_done(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Capture read task failed: {type(exc).__name__}: {exc}")
