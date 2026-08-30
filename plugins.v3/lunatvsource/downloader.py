"""Persistent m3u8 download/STRM queue."""

from __future__ import annotations

import ctypes
import ctypes.util
import http.server
import logging
import os
import re
import socketserver
import stat
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .cms import _fetch_public_url, _request_public_url

from .m3u8_engine import (
    M3U8EngineCancelled,
    M3U8EngineError,
    M3U8EngineUnavailable,
    N_M3U8DL_RE_VERSION,
    N_m3u8DLEngine,
)
from .naming import media_path


LOGGER = logging.getLogger("LunaTVSource")

DEFAULT_MAX_CONCURRENT_TASKS = 2
MIN_MAX_CONCURRENT_TASKS = 1
MAX_MAX_CONCURRENT_TASKS = 4
DEFAULT_SEGMENT_THREAD_COUNT = 16
MIN_SEGMENT_THREAD_COUNT = 4
MAX_SEGMENT_THREAD_COUNT = 32
MAX_TOTAL_SEGMENT_THREADS = 64
DEFAULT_HLS_AD_FILTER_REGEX = (
    r"(?i)(?:adjump|redtraffic|alimama|chenggao|laomaotao|"
    r"[/_.-](?:ad|ads|advert|advertisement|promo|sponsor)[/_.-])"
)
_HLS_PLAYLIST_MAX_BYTES = 4 * 1024 * 1024
_HLS_PLAYLIST_TOTAL_BYTES = 8 * 1024 * 1024
_HLS_PLAYLIST_MAX_COUNT = 32
_HLS_PLAYLIST_MAX_DEPTH = 4
_HLS_PREPARE_TIMEOUT_SECONDS = 60.0


def _regular_file_size(value: str) -> int:
    """Return a completed regular file size without following symlinks."""
    try:
        info = Path(value).lstat()
    except (OSError, TypeError, ValueError):
        return 0
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return 0
    return max(0, int(info.st_size))


def normalize_download_concurrency(
    max_concurrent_tasks: object,
    segment_thread_count: object,
) -> tuple[int, int]:
    """Bound per-task work and keep aggregate segment concurrency predictable."""
    try:
        max_tasks = int(max_concurrent_tasks)
    except (TypeError, ValueError):
        max_tasks = DEFAULT_MAX_CONCURRENT_TASKS
    max_tasks = max(
        MIN_MAX_CONCURRENT_TASKS,
        min(MAX_MAX_CONCURRENT_TASKS, max_tasks),
    )

    try:
        segment_threads = int(segment_thread_count)
    except (TypeError, ValueError):
        segment_threads = DEFAULT_SEGMENT_THREAD_COUNT
    segment_threads = max(
        MIN_SEGMENT_THREAD_COUNT,
        min(MAX_SEGMENT_THREAD_COUNT, segment_threads),
    )
    segment_threads = min(
        segment_threads,
        max(MIN_SEGMENT_THREAD_COUNT, MAX_TOTAL_SEGMENT_THREADS // max_tasks),
    )
    return max_tasks, segment_threads


class _QueueControl(RuntimeError):
    """Internal signal used to stop the active download process safely."""

    def __init__(self, action: str) -> None:
        super().__init__(action)
        self.action = action


def _mpegts_payload_offset(data: bytes) -> int:
    """Return the start of an MPEG-TS payload hidden behind a JPEG header."""

    if not data.startswith(b"\xff\xd8\xff"):
        return 0
    limit = max(0, min(len(data) - 376, 4096))
    for offset in range(limit + 1):
        if data[offset] == 0x47 and data[offset + 188] == 0x47 and data[offset + 376] == 0x47:
            return offset
    return 0


class _LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    """HTTP server without a reverse-DNS lookup during loopback binding."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _SegmentProxy:
    """Loopback-only streaming proxy that removes fake JPEG segment headers."""

    def __init__(self, allowed_private_ranges: Iterable[str] = ()) -> None:
        self._urls: Dict[str, str] = {}
        self._reverse: Dict[str, str] = {}
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._allowed_private_ranges = tuple(allowed_private_ranges or ())

    def __enter__(self) -> "_SegmentProxy":
        return self

    def _start(self) -> None:
        if self._server is not None:
            return
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                token = self.path.partition("?")[0].removeprefix("/segment/")
                remote_url = proxy._urls.get(token)
                if not remote_url:
                    self.send_error(404)
                    return
                try:
                    request_headers = {
                        "User-Agent": "MoviePilot-LunaTV/1.0",
                        "Accept": "*/*",
                        "Accept-Encoding": "identity",
                    }
                    range_header = self.headers.get("Range")
                    if range_header:
                        request_headers["Range"] = range_header
                    connection, response, _ = _request_public_url(
                        remote_url,
                        30,
                        proxy._allowed_private_ranges,
                        headers=request_headers,
                    )
                    try:
                        partial = response.status == 206
                        prefix = response.read(4096)
                        offset = 0 if partial else _mpegts_payload_offset(prefix)
                        length = response.headers.get("Content-Length")
                        content_length = (
                            int(length)
                            if length is not None and str(length).isdigit()
                            else None
                        )
                        self.send_response(206 if partial else 200)
                        self.send_header("Content-Type", "video/mp2t" if offset else (
                            response.headers.get("Content-Type") or "application/octet-stream"
                        ))
                        if partial:
                            for name in ("Content-Range", "Accept-Ranges"):
                                value = response.headers.get(name)
                                if value:
                                    self.send_header(name, value)
                        if content_length is not None and content_length >= offset:
                            self.send_header("Content-Length", str(content_length - offset))
                        else:
                            # HTTP/1.1 responses without a length must close so
                            # ffmpeg can delimit the segment body.
                            self.send_header("Connection", "close")
                            self.close_connection = True
                        self.end_headers()
                        self.wfile.write(prefix[offset:])
                        while True:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    finally:
                        connection.close()
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception:
                    try:
                        self.send_error(502)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._server = _LoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def url_for(self, remote_url: str, *, ad: bool = False) -> str:
        self._start()
        token = self._reverse.get(remote_url)
        if token is None:
            token = uuid.uuid4().hex
            self._reverse[remote_url] = token
            self._urls[token] = remote_url
        if self._server is None:
            raise RuntimeError("分片代理尚未启动")
        host, port = self._server.server_address[:2]
        marker = "?cue=lunatv-cue-ad" if ad else ""
        return f"http://{host}:{port}/segment/{token}{marker}"

    def __exit__(self, *_args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


@dataclass
class DownloadTask:
    task_id: str
    source_key: str
    media_id: str
    title: str
    year: str
    media_type: str
    season: int
    episode: int
    url: str
    root: str
    host_media_source: Optional[str] = None
    host_media_id: Optional[str] = None
    source_name: Optional[str] = None
    mode: str = "download"
    ffmpeg_path: str = "ffmpeg"
    state: str = "pending"
    error: str = ""
    output: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    attempts: int = 0
    # MoviePilot's native download projection uses a 0..1 value.  Older
    # persisted tasks do not have this field and are restored with 0.0.
    progress: float = 0.0
    # Empty keeps persisted tasks created before engine attribution compatible.
    download_engine: str = ""
    # Persist before MoviePilot moves the completed file so season totals stay
    # stable across plugin/container restarts.
    downloaded_bytes: int = 0

    @classmethod
    def from_episode(
        cls,
        episode: Any,
        *,
        title: str,
        year: str,
        media_type: str,
        root: str,
        mode: str,
        ffmpeg_path: str,
        source_name: Optional[str] = None,
        media_source: str,
        media_id: str,
    ) -> "DownloadTask":
        season_value = getattr(episode, "season", None)
        return cls(
            task_id=str(uuid.uuid4()),
            source_key=media_source,
            media_id=media_id,
            title=title,
            year=year,
            media_type=media_type,
            season=int(season_value) if season_value not in (None, "") else 1,
            episode=int(getattr(episode, "episode", 1) or 1),
            url=str(getattr(episode, "url", "")),
            root=root,
            source_name=source_name,
            mode=mode,
            ffmpeg_path=ffmpeg_path,
        )

    @property
    def identity_key(self) -> str:
        host_source = str(self.host_media_source or "").strip().casefold()
        host_id = str(self.host_media_id or "").strip()
        media_identity = (
            f"{host_source}:{host_id}"
            if host_source and host_id
            else str(self.media_id or "").strip()
        )
        return (
            f"{self.source_key}|{media_identity}|"
            f"{self.season}|{self.episode}|{self.mode}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _SerialDownloadQueue:
    """One-at-a-time queue; no worker fan-out or parallel download."""

    DATA_KEY = "download_tasks_v1"

    def __init__(
        self,
        load: Callable[..., Any],
        save: Callable[..., Any],
        notify: Callable[[str, str], None],
        on_complete: Optional[Callable[[DownloadTask, str], None]] = None,
        data_path: Optional[Path] = None,
        allowed_private_ranges: Iterable[str] = (),
    ) -> None:
        self._load = load
        self._save = save
        self._notify = notify
        self._on_complete = on_complete
        self._lock = threading.RLock()
        self._stop = False
        self._running = False
        self._drain_running = False
        self._drain_wakeup = threading.Event()
        self._current_task_id = ""
        self._active_owner_id: Optional[int] = None
        self._control_action = ""
        self._control_event = threading.Event()
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._delete_file_tasks: set[str] = set()
        self._allowed_private_ranges = tuple(allowed_private_ranges or ())
        # Standalone/legacy hosts retain the historical N-only behavior.
        # MoviePilot passes its plugin data directory and enables the managed
        # N_m3u8DL-RE is the sole VOD download engine.
        self._m3u8_engines = (
            (N_m3u8DLEngine(Path(data_path)),)
            if data_path is not None
            else ()
        )
        self._recover_interrupted_tasks()

    def _recover_interrupted_tasks(self) -> None:
        """Put tasks left in ``running`` back into the download queue.

        MoviePilot may restart while ffmpeg is running.  Persisting the
        transient state is useful for UI feedback, but it must not strand a
        task forever after the process comes back.
        """

        with self._lock:
            tasks = self._read()
            changed = False

            # A task id is the queue's persistence and control identity. Older
            # versions could append a second record after a failed submission,
            # then update only the first matching row on completion. Collapse
            # those historical duplicates before recovering interrupted work.
            task_order: List[str] = []
            tasks_by_id: Dict[str, DownloadTask] = {}
            tasks_without_id: List[DownloadTask] = []
            for task in tasks:
                if not task.task_id:
                    tasks_without_id.append(task)
                    continue
                current = tasks_by_id.get(task.task_id)
                if current is None:
                    task_order.append(task.task_id)
                    tasks_by_id[task.task_id] = task
                    continue
                if task.state == "completed" or (
                    current.state != "completed"
                    and task.created_at >= current.created_at
                ):
                    tasks_by_id[task.task_id] = task
                changed = True
            if changed:
                tasks = [tasks_by_id[task_id] for task_id in task_order]
                tasks.extend(tasks_without_id)

            # Old subscription refreshes generated a fresh task id each time.
            # Collapse those rows by the stable episode identity as well, so
            # a completed item always wins over a newer ghost execution.
            state_rank = {
                "completed": 5,
                "running": 4,
                "pending": 3,
                "paused": 2,
                "failed": 1,
            }
            identity_order: List[str] = []
            tasks_by_identity: Dict[str, DownloadTask] = {}
            tasks_without_identity: List[DownloadTask] = []
            identity_duplicates = False
            for task in tasks:
                has_identity = bool(
                    str(task.source_key or "").strip()
                    and (
                        str(task.media_id or "").strip()
                        or (
                            str(task.host_media_source or "").strip()
                            and str(task.host_media_id or "").strip()
                        )
                    )
                )
                if not has_identity:
                    tasks_without_identity.append(task)
                    continue
                identity = task.identity_key
                current = tasks_by_identity.get(identity)
                if current is None:
                    identity_order.append(identity)
                    tasks_by_identity[identity] = task
                    continue
                current_rank = state_rank.get(current.state, 0)
                task_rank = state_rank.get(task.state, 0)
                if task_rank > current_rank or (
                    task_rank == current_rank and task.created_at >= current.created_at
                ):
                    tasks_by_identity[identity] = task
                identity_duplicates = True
            if identity_duplicates:
                tasks = [tasks_by_identity[key] for key in identity_order]
                tasks.extend(tasks_without_identity)
                changed = True

            for task in tasks:
                if task.state == "running":
                    task.state = "pending"
                    task.progress = 0.0
                    task.error = "上次进程中断，已恢复排队"
                    changed = True
                elif task.state == "paused" and task.progress:
                    task.progress = 0.0
                    changed = True
                elif task.state == "completed" and task.error:
                    task.progress = 1.0
                    task.error = ""
                    changed = True
            if changed:
                self._write(tasks)

    def _read(self) -> List[DownloadTask]:
        raw = self._load(self.DATA_KEY, []) or []
        tasks: List[DownloadTask] = []
        for item in raw:
            if isinstance(item, dict):
                try:
                    tasks.append(DownloadTask(**item))
                except TypeError:
                    continue
        return tasks

    def _write(self, tasks: List[DownloadTask]) -> None:
        terminal_states = {"completed", "failed"}
        terminal_to_discard = max(
            0,
            sum(task.state in terminal_states for task in tasks) - 500,
        )
        persisted = []
        for task in tasks:
            if task.state in terminal_states and terminal_to_discard:
                terminal_to_discard -= 1
                continue
            persisted.append(task.to_dict())
        self._save(self.DATA_KEY, persisted)

    def _persist_removal(
        self,
        tasks: List[DownloadTask],
        task: DownloadTask,
        *,
        delete_file: bool,
    ) -> None:
        """Persist task removal before deleting its local artifacts.

        Some MoviePilot data stores may durably apply a write and still raise
        while reporting the result.  Re-read in that case: delete files only
        when the task is already absent, otherwise leave both state and files
        intact so a restart cannot resurrect a task whose file was removed.
        This method must be called while ``_lock`` is held.
        """

        remaining = [item for item in tasks if item.task_id != task.task_id]
        try:
            self._write(remaining)
        except Exception:
            try:
                removal_persisted = not any(
                    item.task_id == task.task_id for item in self._read()
                )
            except Exception:
                removal_persisted = False
            if removal_persisted:
                self._cleanup_m3u8_cache(task)
                if delete_file:
                    self._delete_task_files(task)
            raise
        self._cleanup_m3u8_cache(task)
        if delete_file:
            self._delete_task_files(task)

    def enqueue(self, task: DownloadTask) -> bool:
        if not task.url or not task.root:
            return False
        with self._lock:
            tasks = self._read()
            for existing in tasks:
                if existing.identity_key == task.identity_key and existing.state in {"pending", "running", "paused", "completed"}:
                    return False
            tasks.append(task)
            self._write(tasks)
            return True

    def reconcile_completed(self, task: DownloadTask, *, output: str = "") -> bool:
        """Persist an already-downloaded episode for season-level counters."""

        if not task.url or not task.root:
            return False
        with self._lock:
            tasks = self._read()
            existing = next(
                (item for item in tasks if item.identity_key == task.identity_key),
                None,
            )
            if existing and existing.state in {"pending", "running", "paused"}:
                return False
            target = existing or task
            target.source_key = task.source_key
            target.media_id = task.media_id
            target.title = task.title
            target.year = task.year
            target.media_type = task.media_type
            target.season = task.season
            target.episode = task.episode
            target.url = task.url
            target.root = task.root
            target.host_media_source = task.host_media_source
            target.host_media_id = task.host_media_id
            target.source_name = task.source_name
            target.mode = task.mode
            target.ffmpeg_path = task.ffmpeg_path
            target.state = "completed"
            target.progress = 1.0
            target.error = ""
            target.output = str(output or "")
            target.completed_at = target.completed_at or time.time()
            target.downloaded_bytes = max(
                0,
                int(getattr(target, "downloaded_bytes", 0) or 0),
                int(getattr(task, "downloaded_bytes", 0) or 0),
            )
            if existing is None:
                tasks.append(target)
            self._write(tasks)
            return True

    def retry(self, task_id: str) -> bool:
        with self._lock:
            tasks = self._read()
            for task in tasks:
                if task.task_id == task_id and task.state == "failed":
                    task.state = "pending"
                    task.progress = 0.0
                    task.error = ""
                    task.downloaded_bytes = 0
                    self._write(tasks)
                    return True
        return False

    def pause(self, task_id: str) -> bool:
        """Pause a queued task, or request safe termination of active ffmpeg."""
        with self._lock:
            tasks = self._read()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None:
                return False
            if task.state == "paused":
                return True
            if task.state == "pending":
                task.state = "paused"
                task.progress = 0.0
                task.error = ""
                self._write(tasks)
                return True
            if task.state == "running" and self._current_task_id == task_id:
                if self._control_action == "remove":
                    return True
                self._control_action = "pause"
                self._control_event.set()
                return True
        return False

    def resume(self, task_id: str) -> bool:
        """Return a paused task to the pending queue."""
        with self._lock:
            tasks = self._read()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None:
                return False
            if task.state in {"pending", "running"}:
                return True
            if task.state == "paused":
                task.state = "pending"
                task.progress = 0.0
                task.error = ""
                self._write(tasks)
                return True
        return False

    def remove(self, task_id: str, delete_file: bool = False) -> bool:
        """Remove a task, optionally deleting its safe local files."""
        with self._lock:
            tasks = self._read()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None:
                return False
            if task.state == "running" and self._current_task_id == task_id:
                if delete_file:
                    self._delete_file_tasks.add(task_id)
                self._control_action = "remove"
                self._control_event.set()
                return True
            self._persist_removal(tasks, task, delete_file=delete_file)
            return True

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [task.to_dict() for task in reversed(self._read())]

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
        for task in self._read():
            counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    def finalizing_task_ids(self) -> set[str]:
        """Return active workers whose download is durable but hook still runs."""
        with self._lock:
            states = {task.task_id: task.state for task in self._read()}
            active = {
                task_id
                for task_id in self._active
                if states.get(task_id) == "completed"
            }
            pending = {
                task_id
                for task_id, intent in getattr(
                    self, "_pending_terminal", {}
                ).items()
                if intent.state == "completed"
            }
            return active | pending

    def engine_status(self) -> Dict[str, Any]:
        """Report the pinned engine without triggering a network install."""
        status: Dict[str, Any] = {
            "name": "N_m3u8DL-RE",
            "version": N_M3U8DL_RE_VERSION.removeprefix("v"),
            "supported": False,
            "ready": False,
            "install_source": "插件内置官方固定版本（缺失时 GitHub）",
            "managed_path": "",
        }
        if not self._m3u8_engines:
            return status
        installer = getattr(self._m3u8_engines[0], "_installer", None)
        if installer is None:
            return status
        try:
            status["supported"] = installer.asset() is not None
            status["managed_path"] = str(installer.managed_path)
            status["ready"] = bool(installer._managed_binary_is_verified())
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        return status

    @staticmethod
    def _notification_text(task: DownloadTask) -> str:
        if str(task.media_type or "").lower() == "tv":
            return f"{task.title} S{int(task.season):02d}E{int(task.episode):02d}"
        return task.title

    def _clear_active_state(self, *, keep_control: bool = False) -> None:
        """Clear transient active-task state while ``_lock`` is held."""
        self._running = False
        self._active_owner_id = None
        if not keep_control:
            self._current_task_id = ""
            self._control_action = ""
        self._control_event.clear()
        self._idle_event.set()

    def wake(self) -> bool:
        """Run pending tasks in one background worker without fan-out."""
        with self._lock:
            if self._stop:
                return False
            self._drain_wakeup.set()
            if self._drain_running:
                return True
            self._drain_running = True
        try:
            threading.Thread(
                target=self._drain,
                name="lunatvsource-download",
                daemon=True,
            ).start()
        except RuntimeError:
            with self._lock:
                self._drain_running = False
                self._drain_wakeup.clear()
            return False
        return True

    def _drain(self) -> None:
        control_retry_used = False
        while True:
            self._drain_wakeup.clear()
            failed = False
            try:
                self._drain_until_idle()
            except Exception:
                # A wake arriving during a transient persistence failure must
                # get one retry instead of being lost behind the live worker.
                failed = True

            with self._lock:
                if self._drain_wakeup.is_set():
                    continue
                pending_control = (
                    self._control_action in {"pause", "remove"}
                    and bool(self._current_task_id)
                )
                if failed and pending_control and not control_retry_used:
                    control_retry_used = True
                    continue
                self._drain_running = False
                if self._stop:
                    self._drain_wakeup.clear()
                return

    def _drain_until_idle(self) -> None:
        """Continue legacy single-worker processing until no task remains.

        The idle check and worker hand-off share ``_lock`` with enqueue(), so
        a task added as another task finishes cannot lose its wake-up.
        """
        while True:
            result = self.run_one()
            if result.get("processed"):
                continue

            with self._lock:
                stopped = self._stop
                active_elsewhere = self._running
            if stopped:
                return
            if active_elsewhere:
                # A legacy/direct run_one() call owns the active task. Keep
                # this worker alive until it finishes, then drain its wake-up.
                self._idle_event.wait()
                continue

            with self._lock:
                if any(task.state == "pending" for task in self._read()):
                    continue
                return

    def _replay_control_intent(
        self,
        tasks: List[DownloadTask],
    ) -> Optional[Dict[str, Any]]:
        """Persist an interrupted pause/remove before any task can restart.

        Must be called with ``_lock`` held. The intent is cleared only after
        its durable state transition succeeds, so a transient save failure
        cannot resurrect a paused or removed download.
        """
        action = self._control_action
        task_id = self._current_task_id
        if action not in {"pause", "remove"} or not task_id:
            return None

        task = next((item for item in tasks if item.task_id == task_id), None)
        if action == "remove":
            delete_file = task_id in self._delete_file_tasks
            if task is not None:
                self._persist_removal(tasks, task, delete_file=delete_file)
            self._delete_file_tasks.discard(task_id)
        else:
            if task is not None:
                task.state = "paused"
                task.progress = 0.0
                task.error = ""
                self._write(tasks)

        self._clear_active_state()
        return {
            "processed": 1 if task is not None else 0,
            "task_id": task_id,
            "state": action,
        }

    def run_one(self) -> Dict[str, Any]:
        owner_id = threading.get_ident()
        try:
            return self._run_one(owner_id)
        except Exception:
            with self._lock:
                if self._active_owner_id == owner_id:
                    keep_control = (
                        self._control_action in {"pause", "remove"}
                        and bool(self._current_task_id)
                    )
                    self._clear_active_state(keep_control=keep_control)
            raise

    def _run_one(self, owner_id: int) -> Dict[str, Any]:
        with self._lock:
            if self._running:
                return {"processed": 0, "stopped": True}
            tasks = self._read()
            replayed = self._replay_control_intent(tasks)
            if replayed is not None:
                return replayed
            if self._stop:
                return {"processed": 0, "stopped": True}
            task = next(
                (
                    item
                    for item in tasks
                    if item.state == "pending" and item.task_id not in self._pending_terminal
                ),
                None,
            )
            if task is None:
                task = next((item for item in tasks if item.state == "running"), None)
                if task is not None:
                    task.progress = 0.0
                    task.error = "上次队列执行异常，已恢复排队"
            if task is None:
                return {"processed": 0}
            task.state = "running"
            task.progress = max(0.0, min(1.0, float(task.progress or 0.0)))
            task.attempts += 1
            self._running = True
            self._current_task_id = task.task_id
            self._active_owner_id = owner_id
            self._control_action = ""
            self._control_event.clear()
            self._idle_event.clear()
            self._write(tasks)
        try:
            output = self._execute(task)
        except _QueueControl as exc:
            with self._lock:
                action = self._control_action or exc.action
                tasks = self._read()
                current = next((item for item in tasks if item.task_id == task.task_id), None)
                if action == "remove":
                    delete_file = task.task_id in self._delete_file_tasks
                    self._persist_removal(tasks, task, delete_file=delete_file)
                elif current is not None:
                    current.state = "paused"
                    current.progress = 0.0
                    current.error = ""
                    self._write(tasks)
                self._delete_file_tasks.discard(task.task_id)
                self._clear_active_state()
                return {"processed": 1, "task_id": task.task_id, "state": action}
        except Exception as exc:
            with self._lock:
                tasks = self._read()
                if (
                    self._control_action == "remove"
                    and self._current_task_id == task.task_id
                ):
                    delete_file = task.task_id in self._delete_file_tasks
                    self._persist_removal(tasks, task, delete_file=delete_file)
                    self._delete_file_tasks.discard(task.task_id)
                    self._clear_active_state()
                    return {
                        "processed": 1,
                        "task_id": task.task_id,
                        "state": "remove",
                    }
                if (
                    self._control_action == "pause"
                    and self._current_task_id == task.task_id
                ):
                    current = next(
                        (item for item in tasks if item.task_id == task.task_id),
                        None,
                    )
                    if current is not None:
                        current.state = "paused"
                        current.progress = 0.0
                        current.error = ""
                        self._write(tasks)
                    self._delete_file_tasks.discard(task.task_id)
                    self._clear_active_state()
                    return {
                        "processed": 1,
                        "task_id": task.task_id,
                        "state": "pause",
                    }
                self._delete_file_tasks.discard(task.task_id)
                current = next((item for item in tasks if item.task_id == task.task_id), task)
                current.state = "failed"
                current.error = str(exc)
                self._write(tasks)
                self._clear_active_state()
            self._notify("LunaTV 下载失败", f"{self._notification_text(task)}：{exc}")
            return {"processed": 1, "task_id": task.task_id, "state": "failed", "error": str(exc)}

        with self._lock:
            tasks = self._read()
            if (
                self._control_action == "remove"
                and self._current_task_id == task.task_id
            ):
                task.output = output
                delete_file = task.task_id in self._delete_file_tasks
                self._persist_removal(tasks, task, delete_file=delete_file)
                self._delete_file_tasks.discard(task.task_id)
                self._clear_active_state()
                return {
                    "processed": 1,
                    "task_id": task.task_id,
                    "state": "remove",
                }
            self._delete_file_tasks.discard(task.task_id)
            current = next((item for item in tasks if item.task_id == task.task_id), task)
            current.state = "completed"
            current.progress = 1.0
            current.output = output
            current.completed_at = time.time()
            current.downloaded_bytes = _regular_file_size(output)
            task.state = current.state
            task.output = output
            task.completed_at = current.completed_at
            task.downloaded_bytes = current.downloaded_bytes
            self._write(tasks)
        if self._on_complete is not None:
            try:
                self._on_complete(task, output)
            except Exception:
                # History/host integration must never turn a completed file
                # into a failed download.
                pass
        with self._lock:
            self._clear_active_state()
        self._notify("LunaTV 已完成", self._notification_text(task))
        return {"processed": 1, "task_id": task.task_id, "state": "completed", "output": output}

    @staticmethod
    def _open_parent_below_root(
        root: Path,
        target: Path,
        *,
        create: bool = False,
    ) -> tuple[List[int], str, tuple[str, ...]]:
        """Open a target parent without following directory symlinks."""

        root_path = Path(os.path.abspath(os.fspath(root)))
        target_path = Path(os.path.abspath(os.fspath(target)))
        try:
            relative = target_path.relative_to(root_path)
        except ValueError as exc:
            raise ValueError("目标路径越界") from exc
        if not relative.parts:
            raise ValueError("目标路径无效")

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory:
            raise OSError("当前平台缺少安全目录操作支持")

        if create:
            root_path.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        descriptors = [os.open(root_path, flags)]
        try:
            for part in relative.parts[:-1]:
                try:
                    descriptor = os.open(part, flags, dir_fd=descriptors[-1])
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=descriptors[-1])
                    descriptor = os.open(part, flags, dir_fd=descriptors[-1])
                descriptors.append(descriptor)
        except Exception:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise
        return descriptors, relative.parts[-1], relative.parts[:-1]

    @staticmethod
    def _close_descriptors(descriptors: Iterable[int]) -> None:
        for descriptor in reversed(tuple(descriptors)):
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

    @classmethod
    def _ensure_parent_below_root(cls, root: Path, target: Path) -> None:
        descriptors, _name, _parents = cls._open_parent_below_root(
            root,
            target,
            create=True,
        )
        cls._close_descriptors(descriptors)

    @classmethod
    def _atomic_write_text_below_root(
        cls,
        root: Path,
        target: Path,
        value: str,
    ) -> None:
        descriptors, name, _parents = cls._open_parent_below_root(
            root,
            target,
            create=True,
        )
        parent_fd = descriptors[-1]
        temporary_name = f".{name}.{uuid.uuid4().hex}.part"
        descriptor = -1
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = ""
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            cls._close_descriptors(descriptors)

    @classmethod
    def _unlink_below_root(
        cls,
        root: Path,
        target: Path,
        *,
        cleanup_empty_parents: bool = False,
    ) -> bool:
        try:
            descriptors, name, parent_parts = cls._open_parent_below_root(
                root,
                target,
            )
        except FileNotFoundError:
            return False

        removed = False
        try:
            try:
                os.unlink(name, dir_fd=descriptors[-1])
                removed = True
            except FileNotFoundError:
                pass

            if cleanup_empty_parents:
                for index in range(len(parent_parts) - 1, -1, -1):
                    child_index = index + 1
                    os.close(descriptors[child_index])
                    descriptors[child_index] = -1
                    try:
                        os.rmdir(parent_parts[index], dir_fd=descriptors[index])
                    except OSError:
                        break
            return removed
        finally:
            cls._close_descriptors(descriptors)

    def _execute(self, task: DownloadTask) -> str:
        root, destination = self._destination_for_task(task)
        self._ensure_parent_below_root(root, destination)
        if _regular_file_size(destination) > 0:
            self._cleanup_m3u8_cache(task, destination.parent)
            return str(destination)

        if task.mode == "strm":
            self._unlink_below_root(
                root,
                destination.with_suffix(destination.suffix + ".part"),
            )
            self._atomic_write_text_below_root(
                root,
                destination,
                task.url + "\n",
            )
            self._cleanup_m3u8_cache(task, destination.parent)
            return str(destination)

        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            if not self._run_m3u8_engines(task, temp_path):
                raise RuntimeError("N_m3u8DL-RE 不可用或下载失败")
        except Exception:
            # 失败任务不把残留缓存留在媒体库目录，避免 Emby/监控把半成品当成文件夹内容。
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._remove_empty_parents(destination.parent, root)
            raise
        if not temp_path.exists() or temp_path.stat().st_size <= 0:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._remove_empty_parents(destination.parent, root)
            raise IOError("N_m3u8DL-RE 未生成有效文件")
        os.replace(temp_path, destination)
        self._cleanup_m3u8_cache(task, destination.parent)
        return str(destination)

    def _cleanup_m3u8_cache(
        self, task: DownloadTask, destination_parent: Optional[Path] = None
    ) -> None:
        """Clear controlled engine cache only after success or durable deletion."""
        if not self._m3u8_engines:
            return
        parent = destination_parent
        if parent is None:
            try:
                relative_dir, filename = media_path(
                    task.root,
                    task.title,
                    task.year,
                    task.media_type,
                    task.season,
                    task.episode,
                    task.url,
                    task.mode,
                )
                root = Path(task.root).expanduser().resolve()
                destination = (root / relative_dir / filename).resolve()
                if root in destination.parents:
                    parent = destination.parent
            except (OSError, TypeError, ValueError):
                parent = None
        for engine in self._m3u8_engines:
            cleanup = getattr(engine, "cleanup_task", None)
            if callable(cleanup):
                try:
                    cleanup(task.task_id, parent)
                except Exception:
                    pass

    @staticmethod
    def _playlist_segment_count(path: Path, visited: Optional[set[str]] = None) -> int:
        """Count materialized HLS media segments for conservative cache progress."""
        visited = visited or set()
        try:
            resolved = str(path.resolve())
        except OSError:
            return 0
        if resolved in visited:
            return 0
        visited.add(resolved)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return 0
        count = 0
        for line in lines:
            value = line.strip()
            if value.startswith("#EXTINF:"):
                count += 1
            elif value and not value.startswith("#"):
                child = Path(value)
                if child.is_file() and child.suffix.lower() in {".m3u8", ".m3u"}:
                    count += DownloadQueue._playlist_segment_count(child, visited)
        return count

    def _run_m3u8_engines(self, task: DownloadTask, output: Path) -> bool:
        """Run N_m3u8DL-RE through the established HLS proxy seam."""
        if self._control_event.is_set():
            raise _QueueControl("controlled")
        if not self._m3u8_engines:
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="lunatv-hls-") as temp_dir, _SegmentProxy(
                self._allowed_private_ranges
            ) as proxy:
                if self._control_event.is_set():
                    raise _QueueControl("controlled")
                input_url = self._prepare_hls_input(
                    task.url,
                    Path(temp_dir),
                    proxy.url_for,
                    self._allowed_private_ranges,
                )
                if self._control_event.is_set():
                    raise _QueueControl("controlled")
                segments = self._playlist_segment_count(Path(input_url))
                for engine in self._m3u8_engines:
                    if self._control_event.is_set():
                        raise _QueueControl("controlled")
                    try:
                        engine.download(
                            input_url,
                            output,
                            task_id=task.task_id,
                            ffmpeg_path=task.ffmpeg_path,
                            control_event=self._control_event,
                            progress_callback=lambda progress: self._update_progress(
                                task.task_id, progress
                            ),
                            expected_segments=segments,
                        )
                        return True
                    except M3U8EngineCancelled as exc:
                        raise _QueueControl("controlled") from exc
                    except (M3U8EngineError, OSError):
                        if self._control_event.is_set():
                            raise _QueueControl("controlled")
                        LOGGER.warning(
                    "LunaTV %s M3U8 engine failed", engine.name
                        )
        except _QueueControl:
            raise
        except Exception as exc:
            if self._control_event.is_set():
                raise _QueueControl("controlled") from exc
            # Avoid logging a source URL, which can contain an access token.
            LOGGER.warning("LunaTV M3U8 engine preparation failed; using ffmpeg")
            return False

    def _delete_task_files(self, task: DownloadTask) -> None:
        """Delete only task-owned paths without following symlinks."""

        try:
            root = Path(task.root).expanduser().resolve()
        except (OSError, RuntimeError):
            return

        candidates: List[Path] = []
        if task.output:
            output = Path(task.output).expanduser()
            candidates.append(output if output.is_absolute() else root / output)
        try:
            relative_dir, filename = media_path(
                task.root,
                task.title,
                task.year,
                task.media_type,
                task.season,
                task.episode,
                task.url,
                task.mode,
            )
        except (TypeError, ValueError, OSError):
            relative_dir = ""
            filename = ""
        if filename and filename != task.output:
            candidates.append(root / relative_dir / filename)

        seen: set[Path] = set()
        for candidate in candidates:
            output = Path(os.path.abspath(os.fspath(candidate)))
            if output in seen:
                continue
            try:
                output.relative_to(root)
            except ValueError:
                continue
            seen.add(output)
            paths = (output, Path(f"{output}.part"))
            for index, path in enumerate(paths):
                try:
                    self._unlink_below_root(
                        root,
                        path,
                        cleanup_empty_parents=index == len(paths) - 1,
                    )
                except (OSError, ValueError):
                    continue

    @staticmethod
    def _remove_empty_parents(path: Path, root: Path) -> None:
        """Remove only empty directories below the configured download root."""
        current = path
        while current != root and root in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _update_progress(self, task_id: str, progress: float) -> None:
        """Persist N_m3u8DL-RE progress for the native download page."""

        value = max(0.0, min(0.99, float(progress or 0.0)))
        with self._lock:
            tasks = self._read()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None or task.state != "running":
                return
            task.progress = value
            self._write(tasks)

    @staticmethod
    def _validate_hls_remote_uri(uri: str) -> str:
        """Accept only remote HTTP(S) HLS references before materializing them."""
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("M3U8 URI 仅支持 http/https")
        return uri

    @staticmethod
    def _detect_hls_markers(text: str) -> Dict[str, Any]:
        """Summarize explicit HLS splice markers without changing the playlist."""

        result: Dict[str, Any] = {
            "cue_out": 0,
            "cue_out_cont": 0,
            "cue_in": 0,
            "cue_ranges": [],
            "unclosed_cue": 0,
            "daterange": 0,
            "daterange_candidates": 0,
            "discontinuity": 0,
        }
        elapsed = 0.0
        cue_start: Optional[float] = None
        for line in text.splitlines():
            marker = line.strip()
            upper = marker.upper()
            if upper.startswith("#EXTINF:"):
                try:
                    elapsed += max(0.0, float(marker.partition(":")[2].partition(",")[0]))
                except ValueError:
                    pass
            elif upper.startswith("#EXT-X-CUE-OUT-CONT"):
                result["cue_out_cont"] += 1
                if cue_start is None:
                    cue_start = elapsed
            elif upper.startswith("#EXT-X-CUE-OUT"):
                result["cue_out"] += 1
                if cue_start is None:
                    cue_start = elapsed
            elif upper.startswith("#EXT-X-CUE-IN"):
                result["cue_in"] += 1
                if cue_start is not None:
                    result["cue_ranges"].append((cue_start, elapsed))
                    cue_start = None
            elif upper.startswith("#EXT-X-DATERANGE:"):
                result["daterange"] += 1
                if any(
                    attribute in upper
                    for attribute in (
                        'CLASS="COM.APPLE.HLS.INTERSTITIAL"',
                        "X-ASSET-URI=",
                        "X-ASSET-LIST=",
                        "SCTE35-OUT=",
                        "SCTE35-IN=",
                        "SCTE35-CMD=",
                    )
                ):
                    result["daterange_candidates"] += 1
            elif upper == "#EXT-X-DISCONTINUITY":
                result["discontinuity"] += 1
        result["unclosed_cue"] = int(cue_start is not None)
        return result

    @staticmethod
    def _strip_closed_cue_segments(text: str) -> tuple[str, int, float]:
        """Remove only structurally closed CUE ad blocks; otherwise keep all data."""

        report = DownloadQueue._detect_hls_markers(text)
        if not report["cue_ranges"] or report["unclosed_cue"]:
            return text, 0, 0.0

        output: List[str] = []
        in_cue = False
        pending_extinf = False
        removed_segments = 0
        removed_seconds = 0.0

        for line in text.splitlines():
            stripped = line.strip()
            upper = stripped.upper()

            if upper.startswith("#EXT-X-CUE-OUT-CONT"):
                if pending_extinf:
                    return text, 0, 0.0
                in_cue = True
                continue
            if upper.startswith("#EXT-X-CUE-OUT"):
                if pending_extinf:
                    return text, 0, 0.0
                in_cue = True
                continue
            if upper.startswith("#EXT-X-CUE-IN"):
                if not in_cue or pending_extinf:
                    return text, 0, 0.0
                in_cue = False
                continue

            if not in_cue:
                output.append(line)
                continue

            if upper.startswith("#EXTINF:"):
                if pending_extinf:
                    return text, 0, 0.0
                try:
                    removed_seconds += max(
                        0.0, float(stripped.partition(":")[2].partition(",")[0])
                    )
                except ValueError:
                    return text, 0, 0.0
                pending_extinf = True
            elif stripped and not stripped.startswith("#"):
                if not pending_extinf:
                    return text, 0, 0.0
                removed_segments += 1
                pending_extinf = False

        if in_cue or pending_extinf or removed_segments == 0:
            return text, 0, 0.0
        if "#EXTINF:" in text.upper() and "#EXTINF:" not in "\n".join(output).upper():
            return text, 0, 0.0
        newline = "\n" if text.endswith(("\n", "\r")) else ""
        return "\n".join(output) + newline, removed_segments, removed_seconds

    @staticmethod
    def _prepare_hls_input(
        url: str,
        temp_dir: Path,
        segment_url_mapper: Optional[Callable[[str], str]] = None,
        allowed_private_ranges: Iterable[str] = (),
        ad_segment_url_mapper: Optional[Callable[[str], str]] = None,
        ad_url_matcher: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Materialize playlists locally so ffmpeg can read zstd HTTP responses.

        Some Apple CMS CDNs apply ``Content-Encoding: zstd`` even when the
        client did not request it. ffmpeg does not decode that HTTP content
        encoding, so passing the remote URL directly makes a valid playlist
        look like corrupt binary data.
        """

        visited: Dict[str, str] = {}
        marker_totals = DownloadQueue._detect_hls_markers("")
        removed_cue_segments = 0
        removed_cue_seconds = 0.0
        playlist_count = 0
        total_playlist_bytes = 0
        deadline = time.monotonic() + _HLS_PREPARE_TIMEOUT_SECONDS

        def materialize(playlist_url: str, depth: int = 0) -> str:
            nonlocal playlist_count, total_playlist_bytes
            nonlocal removed_cue_segments, removed_cue_seconds
            requested_url = DownloadQueue._validate_hls_remote_uri(playlist_url)
            if requested_url in visited:
                return visited[requested_url]
            if depth > _HLS_PLAYLIST_MAX_DEPTH:
                raise RuntimeError("m3u8 播放列表嵌套层级过深")
            if playlist_count >= _HLS_PLAYLIST_MAX_COUNT:
                raise RuntimeError("m3u8 播放列表数量过多")
            if time.monotonic() >= deadline:
                raise RuntimeError("m3u8 播放列表准备超时")
            payload, final_url = _fetch_public_url(
                requested_url,
                30,
                _HLS_PLAYLIST_MAX_BYTES + 1,
                allowed_private_ranges,
                deadline=deadline,
            )
            if time.monotonic() >= deadline:
                raise RuntimeError("m3u8 播放列表准备超时")
            if len(payload) > _HLS_PLAYLIST_MAX_BYTES:
                raise RuntimeError("m3u8 播放列表响应过大")
            playlist_url = DownloadQueue._validate_hls_remote_uri(final_url)
            if playlist_url in visited:
                visited[requested_url] = visited[playlist_url]
                return visited[playlist_url]
            if payload.startswith(b"\x28\xb5\x2f\xfd"):
                payload = DownloadQueue._decompress_zstd(
                    payload,
                    _HLS_PLAYLIST_MAX_BYTES,
                )
            if len(payload) > _HLS_PLAYLIST_MAX_BYTES:
                raise RuntimeError("m3u8 播放列表解压后过大")
            if total_playlist_bytes + len(payload) > _HLS_PLAYLIST_TOTAL_BYTES:
                raise RuntimeError("m3u8 播放列表累计大小过大")
            total_playlist_bytes += len(payload)
            try:
                text = payload.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise RuntimeError("m3u8 响应不是可识别的文本格式") from exc
            if "#EXTM3U" not in text[:100]:
                raise RuntimeError("资源站返回的不是有效 m3u8")

            detected = DownloadQueue._detect_hls_markers(text)
            for key in (
                "cue_out",
                "cue_out_cont",
                "cue_in",
                "unclosed_cue",
                "daterange",
                "daterange_candidates",
                "discontinuity",
            ):
                marker_totals[key] += detected[key]
            marker_totals["cue_ranges"].extend(detected["cue_ranges"])
            _preview, cue_segments, cue_seconds = (
                DownloadQueue._strip_closed_cue_segments(text)
            )
            removed_cue_segments += cue_segments
            removed_cue_seconds += cue_seconds

            local_path = temp_dir / f"playlist-{playlist_count}.m3u8"
            playlist_count += 1
            visited[requested_url] = str(local_path)
            visited[playlist_url] = str(local_path)
            lines = text.splitlines()
            rewritten: List[str] = []
            child_playlist = False
            in_closed_cue = False
            cue_filter_enabled = cue_segments > 0 and ad_segment_url_mapper is not None
            for line in lines:
                stripped = line.strip()
                upper = stripped.upper()
                if cue_filter_enabled:
                    if upper.startswith(
                        ("#EXT-X-CUE-OUT-CONT", "#EXT-X-CUE-OUT")
                    ):
                        in_closed_cue = True
                    elif upper.startswith("#EXT-X-CUE-IN"):
                        in_closed_cue = False
                if stripped.startswith("#EXT-X-STREAM-INF"):
                    child_playlist = True
                    rewritten.append(line)
                    continue
                if stripped and not stripped.startswith("#"):
                    absolute = DownloadQueue._validate_hls_remote_uri(
                        urllib.parse.urljoin(playlist_url, stripped)
                    )
                    if child_playlist or urllib.parse.urlparse(absolute).path.lower().endswith(".m3u8"):
                        rewritten.append(materialize(absolute, depth + 1))
                    elif in_closed_cue and ad_segment_url_mapper is not None:
                        rewritten.append(ad_segment_url_mapper(absolute))
                    elif (
                        ad_segment_url_mapper is not None
                        and ad_url_matcher is not None
                        and ad_url_matcher(absolute)
                    ):
                        rewritten.append(ad_segment_url_mapper(absolute))
                    else:
                        rewritten.append(segment_url_mapper(absolute) if segment_url_mapper else absolute)
                    child_playlist = False
                    continue

                def replace_uri(match: re.Match[str]) -> str:
                    absolute = DownloadQueue._validate_hls_remote_uri(
                        urllib.parse.urljoin(playlist_url, match.group(1))
                    )
                    if stripped.startswith(
                        ("#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-RENDITION-REPORT")
                    ):
                        absolute = materialize(absolute, depth + 1)
                    elif segment_url_mapper:
                        absolute = segment_url_mapper(absolute)
                    return f'URI="{absolute}"'

                rewritten.append(re.sub(r'URI="([^"]+)"', replace_uri, line))
            local_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            return str(local_path)

        local_input = materialize(url)
        ranges = ", ".join(
            f"{start:g}-{end:g}s" for start, end in marker_totals["cue_ranges"][:8]
        ) or "-"
        if len(marker_totals["cue_ranges"]) > 8:
            ranges += ", ..."
        LOGGER.info(
            "LunaTV HLS 标记扫描: CUE-OUT=%d, CUE-IN=%d, CUE-OUT-CONT=%d, "
            "闭合候选=%d [%s], 待过滤=%d分片/%.1f秒, 未闭合=%d, "
            "DATERANGE广告候选=%d/%d, "
            "DISCONTINUITY边界=%d",
            marker_totals["cue_out"],
            marker_totals["cue_in"],
            marker_totals["cue_out_cont"],
            len(marker_totals["cue_ranges"]),
            ranges,
            removed_cue_segments,
            removed_cue_seconds,
            marker_totals["unclosed_cue"],
            marker_totals["daterange_candidates"],
            marker_totals["daterange"],
            marker_totals["discontinuity"],
        )
        return local_input

    @staticmethod
    def _decompress_zstd(
        payload: bytes,
        max_output_bytes: int = _HLS_PLAYLIST_MAX_BYTES,
    ) -> bytes:
        output_limit = max(1, int(max_output_bytes))
        try:
            import zstandard  # type: ignore[import-not-found]
        except ImportError:
            pass
        else:
            frame_size = None
            frame_content_size = getattr(zstandard, "frame_content_size", None)
            if callable(frame_content_size):
                try:
                    frame_size = int(frame_content_size(payload))
                except Exception:
                    frame_size = None
            unknown_sizes = {
                int(getattr(zstandard, "CONTENTSIZE_UNKNOWN", 0xFFFFFFFFFFFFFFFF)),
                int(getattr(zstandard, "CONTENTSIZE_ERROR", 0xFFFFFFFFFFFFFFFE)),
            }
            if frame_size is not None and frame_size not in unknown_sizes and frame_size > output_limit:
                raise RuntimeError("zstd m3u8 解压后过大")
            try:
                decompressed = zstandard.ZstdDecompressor().decompress(
                    payload,
                    max_output_size=output_limit,
                )
            except Exception as exc:
                raise RuntimeError("zstd m3u8 解压失败") from exc
            if len(decompressed) > output_limit:
                raise RuntimeError("zstd m3u8 解压后过大")
            return decompressed

        candidates = [
            ctypes.util.find_library("zstd"),
            "libzstd.so.1",
            "libzstd.so",
            "/opt/homebrew/lib/libzstd.dylib",
            "libzstd.dylib",
        ]
        library = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                library = ctypes.CDLL(candidate)
                break
            except OSError:
                continue
        if library is None:
            raise RuntimeError("资源站使用 zstd 压缩，但运行环境缺少 zstd 解码库")

        library.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        library.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        library.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        library.ZSTD_decompress.restype = ctypes.c_size_t
        library.ZSTD_isError.argtypes = [ctypes.c_size_t]
        library.ZSTD_isError.restype = ctypes.c_uint
        source = ctypes.create_string_buffer(payload)
        size = int(library.ZSTD_getFrameContentSize(source, len(payload)))
        if size in {0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFE} or size <= 0:
            raise RuntimeError("无法确定 zstd m3u8 的解压大小")
        if size > output_limit:
            raise RuntimeError("zstd m3u8 解压后过大")
        target = ctypes.create_string_buffer(size)
        result = int(library.ZSTD_decompress(target, size, source, len(payload)))
        if library.ZSTD_isError(result):
            raise RuntimeError("zstd m3u8 解压失败")
        if result > output_limit:
            raise RuntimeError("zstd m3u8 解压后过大")
        return target.raw[:result]

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            if self._running:
                self._control_action = "pause"
                self._control_event.set()


@dataclass
class _TaskControl:
    """Control plane owned by exactly one running task."""

    event: threading.Event = field(default_factory=threading.Event)
    action: str = ""
    delete_file: bool = False


@dataclass
class _TerminalIntent:
    """A terminal transition that could not yet be made durable."""

    task: DownloadTask
    control: _TaskControl
    state: str
    output: str = ""
    error: str = ""


class DownloadQueue(_SerialDownloadQueue):
    """Persistent bounded-concurrency queue with task-local cancellation."""

    COMPLETION_OUTBOX_KEY = "download_completion_outbox_v1"

    def __init__(
        self,
        load: Callable[[str, Any], Any],
        save: Callable[[str, Any], None],
        notify: Callable[[str, str], None],
        on_complete: Optional[Callable[[DownloadTask, str], None]] = None,
        data_path: Optional[Path] = None,
        max_concurrent_tasks: int = 1,
        segment_thread_count: int = DEFAULT_SEGMENT_THREAD_COUNT,
        allowed_private_ranges: Iterable[str] = (),
        ad_filter_regex: str = "",
    ) -> None:
        self._load = load
        self._save = save
        self._notify = notify
        self._on_complete = on_complete
        self._lock = threading.RLock()
        self._stop = False
        (
            self.max_concurrent_tasks,
            self.segment_thread_count,
        ) = normalize_download_concurrency(
            max_concurrent_tasks,
            segment_thread_count,
        )
        self._drain_running = False
        self._drain_failed = False
        self._wake_generation = 0
        self._drain_wakeup = threading.Event()
        self._active: Dict[str, _TaskControl] = {}
        self._active_destinations: Dict[str, str] = {}
        self._pending_terminal: Dict[str, _TerminalIntent] = {}
        self._dispatching = 0
        self._execution = threading.local()
        self._compat_control_event = threading.Event()
        # Legacy attributes remain observable, but are not the source of truth.
        self._running = False
        self._current_task_id = ""
        self._active_owner_id: Optional[int] = None
        self._control_action = ""
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._delete_file_tasks: set[str] = set()
        self._allowed_private_ranges = tuple(allowed_private_ranges or ())
        self._ad_filter_regex = str(ad_filter_regex or "").strip()
        self._ad_filter_pattern: Optional[re.Pattern[str]] = None
        if self._ad_filter_regex:
            try:
                self._ad_filter_pattern = re.compile(self._ad_filter_regex)
            except re.error as exc:
                LOGGER.warning("LunaTV HLS 广告正则无效，已停用：%s", exc)
                self._ad_filter_regex = ""
        self._ad_keyword = "lunatv-cue-ad"
        self._data_path = Path(data_path).resolve() if data_path is not None else None
        self._m3u8_engines = (self._new_n_engine(self._data_path),) if self._data_path else ()
        self._recover_interrupted_tasks()
        self._restore_completion_outbox()

    def _new_n_engine(self, data_path: Path) -> N_m3u8DLEngine:
        try:
            return N_m3u8DLEngine(data_path, thread_count=self.segment_thread_count)
        except TypeError as exc:
            if "thread_count" not in str(exc):
                raise
            return N_m3u8DLEngine(data_path)

    def _read_completion_outbox(self) -> Dict[str, _TerminalIntent]:
        intents: Dict[str, _TerminalIntent] = {}
        raw = self._load(self.COMPLETION_OUTBOX_KEY, []) or []
        if not isinstance(raw, list):
            return intents
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("task"), dict):
                continue
            try:
                task = DownloadTask(**item["task"])
            except TypeError:
                continue
            if not task.task_id:
                continue
            output = str(item.get("output") or task.output or "")
            intents[task.task_id] = _TerminalIntent(
                task=task,
                control=_TaskControl(),
                state="completed",
                output=output,
            )
        return intents

    def _write_completion_outbox(
        self, intents: Dict[str, _TerminalIntent]
    ) -> None:
        payload = []
        for intent in intents.values():
            task = intent.task.to_dict()
            task.update(
                state="completed",
                error="",
                progress=1.0,
                output=intent.output,
            )
            payload.append({"task": task, "output": intent.output})
        self._save(self.COMPLETION_OUTBOX_KEY, payload)

    def _persist_completion_outbox(self, intent: _TerminalIntent) -> None:
        intents = self._read_completion_outbox()
        intents[intent.task.task_id] = intent
        try:
            self._write_completion_outbox(intents)
        except Exception:
            if intent.task.task_id in self._read_completion_outbox():
                return
            raise

    def _clear_completion_outbox(self, task_id: str) -> None:
        intents = self._read_completion_outbox()
        if task_id not in intents:
            return
        intents.pop(task_id, None)
        try:
            self._write_completion_outbox(intents)
        except Exception:
            if task_id not in self._read_completion_outbox():
                return
            raise

    def _restore_completion_outbox(self) -> None:
        for task_id, intent in self._read_completion_outbox().items():
            self._pending_terminal[task_id] = intent
            self._active_destinations[task_id] = self._destination_key(intent.task)
            self._persist_state_transition(
                intent.task,
                "completed",
                output=intent.output,
            )

    @staticmethod
    def _destination_for_task(task: DownloadTask) -> tuple[Path, Path]:
        relative_dir, filename = media_path(
            task.root,
            task.title,
            task.year,
            task.media_type,
            task.season,
            task.episode,
            task.url,
            task.mode,
        )
        root = Path(task.root).expanduser().resolve()
        destination = Path(
            os.path.abspath(os.fspath(root / relative_dir / filename))
        )
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise ValueError("目标路径越界") from exc
        return root, destination

    @classmethod
    def _destination_key(cls, task: DownloadTask) -> str:
        try:
            _, destination = cls._destination_for_task(task)
        except (OSError, TypeError, ValueError):
            return f"invalid:{task.task_id}"
        return os.path.normcase(str(destination))

    def _next_claimable_task(
        self,
        tasks: List[DownloadTask],
    ) -> Optional[DownloadTask]:
        active_destinations = set(self._active_destinations.values())
        return next(
            (
                item
                for item in tasks
                if item.state == "pending"
                and self._destination_key(item) not in active_destinations
            ),
            None,
        )

    @property
    def _control_event(self) -> threading.Event:
        control = getattr(self._execution, "control", None)
        return control.event if control is not None else self._compat_control_event

    def _claim_next(self) -> Optional[tuple[DownloadTask, _TaskControl]]:
        with self._lock:
            if self._stop or len(self._active) >= self.max_concurrent_tasks:
                return None
            tasks = self._read()
            task = self._next_claimable_task(tasks)
            if task is None:
                return None
            task.state = "running"
            task.progress = max(0.0, min(1.0, float(task.progress or 0.0)))
            task.attempts += 1
            task.download_engine = "N_m3u8DL-RE"
            control = _TaskControl()
            self._active[task.task_id] = control
            self._active_destinations[task.task_id] = self._destination_key(task)
            self._running = True
            self._current_task_id = task.task_id
            self._active_owner_id = threading.get_ident()
            self._idle_event.clear()
            if getattr(self._execution, "dispatched", False):
                self._dispatching -= 1
                self._execution.dispatched = False
            try:
                self._write(tasks)
            except Exception:
                self._active.pop(task.task_id, None)
                self._active_destinations.pop(task.task_id, None)
                self._running = bool(self._active)
                # Stop this drain generation after a persistence failure.
                # Otherwise the drain loop can immediately reclaim the same
                # pending task and hide the failure in a tight retry race.
                # A later explicit/scheduled wake clears this backoff flag.
                self._drain_failed = True
                # A failed running-state write must not strand the persisted
                # task or let this drain execute it from an in-memory claim.
                rollback = self._read()
                current = next((item for item in rollback if item.task_id == task.task_id), None)
                if current is not None and current.state == "running":
                    current.state = "pending"
                    current.progress = 0.0
                    current.error = ""
                    current.attempts = max(0, current.attempts - 1)
                    current.download_engine = ""
                    try:
                        self._write(rollback)
                    except Exception:
                        LOGGER.exception("LunaTV queue claim rollback failed")
                if not self._running:
                    self._current_task_id = ""
                    self._active_owner_id = None
                    self._idle_event.set()
                raise
            return task, control

    def run_one(self) -> Dict[str, Any]:
        claimed = self._claim_next()
        if claimed is None:
            return {"processed": 0, "stopped": True} if self._stop else {"processed": 0}
        return self._run_claimed(*claimed)

    def enqueue(self, task: DownloadTask) -> bool:
        """Persist a new task while preserving the legacy boolean contract."""
        if not task.url or not task.root:
            return False
        with self._lock:
            tasks = self._read()
            for existing in tasks:
                if existing.task_id != task.task_id:
                    continue
                if existing.identity_key != task.identity_key:
                    return False
                break
            for existing in tasks:
                if existing.identity_key != task.identity_key:
                    continue
                if existing.state in {"pending", "running", "paused", "completed"}:
                    return False
                if existing.state != "failed":
                    return False
                existing.state = "pending"
                existing.progress = 0.0
                existing.error = ""
                existing.output = ""
                existing.completed_at = 0.0
                existing.downloaded_bytes = 0
                existing.source_key = task.source_key
                existing.media_id = task.media_id
                existing.title = task.title
                existing.year = task.year
                existing.media_type = task.media_type
                existing.season = task.season
                existing.episode = task.episode
                existing.url = task.url
                existing.root = task.root
                existing.host_media_source = task.host_media_source
                existing.host_media_id = task.host_media_id
                existing.source_name = task.source_name
                existing.mode = task.mode
                existing.ffmpeg_path = task.ffmpeg_path
                existing.download_engine = ""
                self._write(tasks)
                return True
            tasks.append(task)
            self._write(tasks)
        return True

    def summary(self) -> Dict[str, int]:
        counts = {"pending": 0, "running": 0, "paused": 0, "completed": 0, "failed": 0}
        with self._lock:
            for task in self._read():
                counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    def retry(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._pending_terminal:
                return False
            retried = super().retry(task_id)
        if retried:
            self.wake()
        return retried

    def resume(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._pending_terminal:
                return False
            resumed = super().resume(task_id)
        if resumed:
            self.wake()
        return resumed

    def _run_claimed(self, task: DownloadTask, control: _TaskControl) -> Dict[str, Any]:
        self._execution.control = control
        try:
            output = self._execute(task)
        except _QueueControl as exc:
            action = control.action or exc.action
            if action not in {"pause", "remove"}:
                action = "pause"
            return self._finish_controlled(task, control, action)
        except Exception as exc:
            return self._finish_failed(task, control, exc)
        finally:
            self._execution.control = None
        return self._finish_completed(task, control, output)

    def _finish_controlled(
        self, task: DownloadTask, control: _TaskControl, action: str
    ) -> Dict[str, Any]:
        with self._lock:
            try:
                self._persist_control_transition(task, control, action)
            except Exception as exc:
                self._defer_terminal(task, control, action, error=str(exc))
            else:
                self._release(task.task_id)
        return {"processed": 1, "task_id": task.task_id, "state": action or "pause"}

    def _persist_control_transition(
        self, task: DownloadTask, control: _TaskControl, action: str
    ) -> None:
        """Durably apply pause/remove without allowing a second execution."""
        last_error: Optional[Exception] = None
        for _attempt in range(2):
            tasks = self._read()
            current = next((item for item in tasks if item.task_id == task.task_id), None)
            try:
                if action == "remove":
                    if current is not None:
                        current.output = task.output
                        self._persist_removal(tasks, current, delete_file=control.delete_file)
                elif current is not None:
                    if current.state == "paused":
                        return
                    current.state = "paused"
                    current.progress = 0.0
                    current.error = ""
                    self._write(tasks)
                return
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _persist_state_transition(
        self,
        task: DownloadTask,
        state: str,
        *,
        output: str = "",
        error: str = "",
    ) -> None:
        """Persist a failed/completed transition, tolerating an applied write."""
        last_error: Optional[Exception] = None
        for _attempt in range(2):
            tasks = self._read()
            current = next((item for item in tasks if item.task_id == task.task_id), None)
            if current is None:
                return
            if state == "failed" and current.state == "failed":
                return
            if state == "completed" and current.state == "completed" and current.output == output:
                return
            try:
                current.state = state
                if state == "failed":
                    current.error = error
                    current.downloaded_bytes = 0
                else:
                    current.error = ""
                    current.progress = 1.0
                    current.output = output
                    current.completed_at = time.time()
                    current.downloaded_bytes = _regular_file_size(output)
                task.state = current.state
                task.error = current.error
                task.progress = current.progress
                task.output = current.output
                task.completed_at = current.completed_at
                task.downloaded_bytes = current.downloaded_bytes
                self._write(tasks)
                return
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _persist_terminal_intent(self, intent: _TerminalIntent) -> None:
        if intent.state in {"pause", "remove"}:
            if intent.state == "remove" and intent.output:
                intent.task.output = intent.output
            self._persist_control_transition(intent.task, intent.control, intent.state)
            return
        if intent.state == "completed":
            self._persist_completion_outbox(intent)
        self._persist_state_transition(
            intent.task,
            intent.state,
            output=intent.output,
            error=intent.error,
        )

    def _defer_terminal(
        self,
        task: DownloadTask,
        control: _TaskControl,
        state: str,
        *,
        output: str = "",
        error: str = "",
    ) -> None:
        """Release execution capacity while retaining the durable transition intent."""
        self._pending_terminal[task.task_id] = _TerminalIntent(
            task=task,
            control=control,
            state=state,
            output=output,
            error=error,
        )
        self._release(task.task_id, release_destination=False)

    def _replay_terminal_intents(self) -> None:
        completed: List[_TerminalIntent] = []
        failed: List[_TerminalIntent] = []
        with self._lock:
            for task_id, intent in list(self._pending_terminal.items()):
                try:
                    self._persist_terminal_intent(intent)
                except Exception:
                    LOGGER.warning("LunaTV terminal transition replay failed for %s", task_id)
                    continue
                if intent.state == "completed":
                    completed.append(intent)
                else:
                    self._pending_terminal.pop(task_id, None)
                    self._active_destinations.pop(task_id, None)
                    if intent.state == "failed":
                        failed.append(intent)
        for intent in completed:
            self._deliver_completion(intent)
        for intent in failed:
            self._notify(
                "LunaTV 下载失败",
                f"{self._notification_text(intent.task)}：{intent.error}",
            )

    def _deliver_completion(self, intent: _TerminalIntent) -> bool:
        task_id = intent.task.task_id
        with self._lock:
            if self._pending_terminal.get(task_id) is not intent:
                return False
            self._pending_terminal.pop(task_id, None)
        try:
            if self._on_complete is not None:
                self._on_complete(intent.task, intent.output)
        except Exception:
            LOGGER.exception("LunaTV completion hook failed")
            with self._lock:
                self._pending_terminal[task_id] = intent
                self._drain_failed = True
                self._release(task_id, release_destination=False)
            return False
        try:
            with self._lock:
                self._clear_completion_outbox(task_id)
        except Exception:
            LOGGER.exception("LunaTV completion outbox acknowledgement failed")
            with self._lock:
                self._pending_terminal[task_id] = intent
                self._drain_failed = True
                self._release(task_id, release_destination=False)
            return False
        try:
            self._notify("LunaTV 已完成", self._notification_text(intent.task))
        finally:
            with self._lock:
                self._release(task_id)
        return True

    def _finish_failed(
        self, task: DownloadTask, control: _TaskControl, exc: Exception
    ) -> Dict[str, Any]:
        with self._lock:
            if control.action == "remove":
                state = "remove"
            elif control.action == "pause":
                state = "pause"
            else:
                state = "failed"
            try:
                self._persist_terminal_intent(
                    _TerminalIntent(
                        task=task,
                        control=control,
                        state=state,
                        error=str(exc),
                    )
                )
            except Exception:
                self._defer_terminal(task, control, state, error=str(exc))
                persisted = False
            else:
                self._release(task.task_id)
                persisted = True
        if state == "failed" and persisted:
            self._notify("LunaTV 下载失败", f"{self._notification_text(task)}：{exc}")
            return {"processed": 1, "task_id": task.task_id, "state": state, "error": str(exc)}
        return {"processed": 1, "task_id": task.task_id, "state": state}

    def _finish_completed(
        self, task: DownloadTask, control: _TaskControl, output: str
    ) -> Dict[str, Any]:
        with self._lock:
            if control.action == "remove":
                state = "remove"
            else:
                state = "completed"
            final_output = task.output or output if state == "remove" else output
            intent = _TerminalIntent(
                task=task,
                control=control,
                state=state,
                output=final_output,
            )
            if state == "completed":
                self._pending_terminal[task.task_id] = intent
            try:
                self._persist_terminal_intent(intent)
            except Exception as exc:
                self._defer_terminal(
                    task,
                    control,
                    state,
                    output=final_output,
                    error=str(exc),
                )
                persisted = False
            else:
                persisted = True
                if state != "completed":
                    self._release(task.task_id)

        if state != "completed":
            return {"processed": 1, "task_id": task.task_id, "state": state}
        if not persisted:
            return {"processed": 1, "task_id": task.task_id, "state": state, "output": output}
        self._deliver_completion(intent)
        return {"processed": 1, "task_id": task.task_id, "state": "completed", "output": output}

    def _release(self, task_id: str, *, release_destination: bool = True) -> None:
        self._active.pop(task_id, None)
        if release_destination:
            self._active_destinations.pop(task_id, None)
        self._delete_file_tasks.discard(task_id)
        self._running = bool(self._active)
        self._current_task_id = next(iter(self._active), "")
        self._active_owner_id = None
        if not self._running:
            self._idle_event.set()
        self._drain_wakeup.set()

    def pause(self, task_id: str) -> bool:
        with self._lock:
            intent = self._pending_terminal.get(task_id)
            if intent is not None:
                return intent.state in {"pause", "remove"}
            tasks = self._read()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None or task.state == "completed":
                return False
            if task.state == "paused":
                return True
            control = self._active.get(task_id)
            if task.state == "running" and control is not None:
                if control.action == "remove":
                    return True
                control.action = "pause"
                control.event.set()
                return True
            if task.state == "pending":
                task.state = "paused"
                task.progress = 0.0
                task.error = ""
                self._write(tasks)
                return True
            return False

    def remove(self, task_id: str, delete_file: bool = False) -> bool:
        with self._lock:
            intent = self._pending_terminal.get(task_id)
            if intent is not None:
                intent.state = "remove"
                intent.control.action = "remove"
                intent.control.delete_file = intent.control.delete_file or delete_file
                self._drain_wakeup.set()
                return True
            tasks = self._read()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None:
                return False
            control = self._active.get(task_id)
            if task.state == "completed" and control is not None:
                # The completion hook may currently be moving/organizing the
                # file. Removing queue state underneath that operation can
                # lose both its durable history and the remaining season.
                return False
            if task.state == "running" and control is not None:
                control.action = "remove"
                control.delete_file = control.delete_file or delete_file
                control.event.set()
                return True
            self._persist_removal(tasks, task, delete_file=delete_file)
            return True

    def wake(self) -> bool:
        with self._lock:
            if self._stop:
                return False
            self._wake_generation += 1
            self._drain_failed = False
            self._drain_wakeup.set()
            if self._drain_running:
                return True
            self._drain_running = True
        try:
            threading.Thread(target=self._drain, name="lunatvsource-download", daemon=True).start()
        except RuntimeError:
            with self._lock:
                self._drain_running = False
            return False
        return True

    def _drain(self) -> None:
        try:
            while True:
                self._replay_terminal_intents()
                with self._lock:
                    if self._stop or self._drain_failed:
                        return
                    tasks = self._read()
                    pending = any(task.state == "pending" for task in tasks)
                    can_dispatch = (
                        pending
                        and self._next_claimable_task(tasks) is not None
                        and len(self._active) + self._dispatching < self.max_concurrent_tasks
                    )
                    if can_dispatch:
                        self._dispatching += 1
                        dispatch_generation = self._wake_generation
                if can_dispatch:
                    try:
                        threading.Thread(
                            target=self._run_dispatched,
                            args=(dispatch_generation,),
                            name="lunatvsource-download-worker",
                            daemon=True,
                        ).start()
                    except RuntimeError:
                        with self._lock:
                            self._dispatching -= 1
                            self._drain_failed = self._wake_generation <= dispatch_generation
                            self._drain_wakeup.set()
                        LOGGER.exception("LunaTV queue worker start failed")
                    continue
                with self._lock:
                    active = bool(self._active)
                    pending = any(task.state == "pending" for task in self._read())
                    terminal_pending = bool(self._pending_terminal)
                    stopped = self._stop
                if stopped:
                    return
                if not active and not pending and not terminal_pending:
                    return
                self._drain_wakeup.wait(timeout=0.25)
                self._drain_wakeup.clear()
        finally:
            with self._lock:
                self._drain_running = False
                if (
                    not self._stop
                    and not self._drain_failed
                    and (
                        self._active
                        or self._pending_terminal
                        or any(task.state == "pending" for task in self._read())
                    )
                ):
                    self.wake()

    def _run_dispatched(self, dispatch_generation: int) -> None:
        self._execution.dispatched = True
        try:
            # Keep wake()/run_one() monkeypatch compatibility for host tests
            # and integrations while the scheduler owns only the slot count.
            self.run_one()
        except Exception:
            with self._lock:
                # A wake requested while this worker was failing is an
                # explicit retry intent; do not discard it with this failure.
                self._drain_failed = self._wake_generation <= dispatch_generation
            LOGGER.exception("LunaTV queue worker failed")
        finally:
            with self._lock:
                if getattr(self._execution, "dispatched", False):
                    self._dispatching -= 1
                    self._execution.dispatched = False
                self._drain_wakeup.set()

    def stop(self, wait: bool = False, timeout: Optional[float] = None) -> bool:
        with self._lock:
            self._stop = True
            for control in self._active.values():
                if not control.action:
                    control.action = "pause"
                control.event.set()
            self._drain_wakeup.set()
        return self.wait_until_idle(timeout) if wait else True

    def wait_until_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait for active workers and the scheduler to exit without polling callers."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                if (
                    not self._active
                    and not self._pending_terminal
                    and not self._dispatching
                    and not self._drain_running
                ):
                    return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self._drain_wakeup.wait(timeout=0.02)
            self._drain_wakeup.clear()

    def stop_and_wait(self, timeout: float = 5.0) -> bool:
        """Compatibility-friendly bounded shutdown for plugin hot reload."""
        stopped = self.stop(wait=True, timeout=timeout)
        if stopped:
            return True
        with self._lock:
            self._stop = False
            self._drain_failed = False
            self._drain_wakeup.set()
        self.wake()
        return False

    def _execute(self, task: DownloadTask) -> str:
        root, destination = self._destination_for_task(task)
        self._ensure_parent_below_root(root, destination)
        if _regular_file_size(destination) > 0:
            self._cleanup_m3u8_cache(task, destination.parent)
            return str(destination)
        if task.mode == "strm":
            self._unlink_below_root(
                root,
                destination.with_suffix(destination.suffix + ".part"),
            )
            self._atomic_write_text_below_root(
                root,
                destination,
                task.url + "\n",
            )
            self._cleanup_m3u8_cache(task, destination.parent)
            return str(destination)
        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            if not self._run_m3u8_engines(task, temp_path):
                raise RuntimeError("N_m3u8DL-RE 不可用或下载失败")
        except Exception:
            # Never invoke a plugin fallback. Keep engine cache for retry.
            temp_path.unlink(missing_ok=True)
            self._remove_empty_parents(destination.parent, root)
            raise
        if not temp_path.exists() or temp_path.stat().st_size <= 0:
            temp_path.unlink(missing_ok=True)
            self._remove_empty_parents(destination.parent, root)
            raise IOError("N_m3u8DL-RE 未生成有效文件")
        os.replace(temp_path, destination)
        self._cleanup_m3u8_cache(task, destination.parent)
        return str(destination)

    def _run_m3u8_engines(self, task: DownloadTask, output: Path) -> bool:
        if self._control_event.is_set():
            raise _QueueControl("controlled")
        if not self._m3u8_engines:
            return False
        with tempfile.TemporaryDirectory(prefix="lunatv-hls-") as temp_dir, _SegmentProxy(
            self._allowed_private_ranges
        ) as proxy:
            input_url = self._prepare_hls_input(
                task.url,
                Path(temp_dir),
                proxy.url_for,
                self._allowed_private_ranges,
                lambda url: proxy.url_for(url, ad=True),
                (
                    lambda url: bool(self._ad_filter_pattern.search(url))
                    if self._ad_filter_pattern is not None
                    else False
                ),
            )
            segments = self._playlist_segment_count(Path(input_url))
            engine = self._m3u8_engines[0]
            kwargs = dict(
                task_id=task.task_id,
                ffmpeg_path=task.ffmpeg_path,
                control_event=self._control_event,
                progress_callback=lambda progress: self._update_progress(task.task_id, progress),
                expected_segments=segments,
                ad_keyword=self._ad_keyword,
            )
            try:
                engine.download(input_url, output, thread_count=self.segment_thread_count, **kwargs)
            except TypeError as exc:
                if "thread_count" not in str(exc):
                    raise
                engine.download(input_url, output, **kwargs)
            except M3U8EngineCancelled:
                raise _QueueControl("controlled")
            except M3U8EngineUnavailable as exc:
                if self._control_event.is_set():
                    raise _QueueControl("controlled") from exc
                raise RuntimeError(
                    "N_m3u8DL-RE 安装不可用："
                    f"{exc}；仅允许插件内置或 GitHub 官方固定版本，请重新安装插件；"
                    "若内置包缺失，再检查 NAS 到 GitHub Release 的网络"
                ) from exc
            except (M3U8EngineError, OSError) as exc:
                if self._control_event.is_set():
                    raise _QueueControl("controlled") from exc
                LOGGER.warning("LunaTV N_m3u8DL-RE failed for %s: %s", task.task_id, exc)
                return False
        return True

    def task_cache_size(self, task_id: str) -> int:
        """Return bytes in the controlled cache/stage tree for one task only."""
        if not task_id or self._data_path is None or not self._m3u8_engines:
            return 0
        try:
            cache_base_path = self._data_path / "m3u8-cache"
            task_root_path = self._m3u8_engines[0]._cache_root(task_id)
            cache_base_path.lstat()
            task_root_path.lstat()
            if cache_base_path.is_symlink() or task_root_path.is_symlink() or not task_root_path.is_dir():
                return 0
            cache_base = cache_base_path.resolve(strict=True)
            task_root = task_root_path.resolve(strict=True)
            task_root.relative_to(cache_base)
            if task_root == cache_base:
                return 0
        except (AttributeError, OSError, ValueError):
            return 0
        total = 0
        for current, dirs, files in os.walk(task_root, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            for name in files:
                path = current_path / name
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
        return total
