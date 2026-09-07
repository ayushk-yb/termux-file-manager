"""Background jobs with progress, cancellation and bounded resource use.

Copying, cross-volume moving, recursive deletion, searching and recursive size
are all unbounded operations: a 40 GB season copy on Android's FUSE-backed
shared storage takes minutes, which is far past any browser's patience.  So the
HTTP layer starts a job, returns ``202`` with its id, and the UI polls.

Two lanes keep a phone's I/O from thrashing:

``fs``    one worker -- mutating operations run strictly one at a time.
``scan``  two workers -- read-only walks (search, recursive size).
"""

import threading
import time
import traceback
import uuid

MAX_ERRORS_KEPT = 100
REAP_AFTER = 600.0          # seconds a finished job stays queryable
MAX_JOBS_KEPT = 200

PENDING, RUNNING, DONE, ERROR, CANCELLED = (
    "pending", "running", "done", "error", "cancelled"
)


class Cancelled(Exception):
    """Raised inside a worker when the client cancels the job."""


class Job:
    def __init__(self, kind, label):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.label = label
        self.state = PENDING
        self.total_bytes = 0
        self.done_bytes = 0
        self.total_items = 0
        self.done_items = 0
        self.current = ""
        self.message = ""
        self.errors = []
        self.error_count = 0
        self.result = None
        self.created = time.time()
        self.started = None
        self.finished = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- worker-side helpers -------------------------------------------

    def set_total(self, items=None, nbytes=None):
        with self._lock:
            if items is not None:
                self.total_items = items
            if nbytes is not None:
                self.total_bytes = nbytes

    def advance(self, nbytes=0, items=0, current=None):
        with self._lock:
            self.done_bytes += nbytes
            self.done_items += items
            if current is not None:
                self.current = current

    def add_error(self, path, message):
        with self._lock:
            self.error_count += 1
            if len(self.errors) < MAX_ERRORS_KEPT:
                self.errors.append({"path": path, "message": str(message)})

    def check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    # -- client-side view ----------------------------------------------

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "state": self.state,
                "total_bytes": self.total_bytes,
                "done_bytes": self.done_bytes,
                "total_items": self.total_items,
                "done_items": self.done_items,
                "current": self.current,
                "message": self.message,
                "errors": list(self.errors),
                "error_count": self.error_count,
                "result": self.result,
                "elapsed": round((self.finished or time.time())
                                 - (self.started or self.created), 2),
                "done": self.state in (DONE, ERROR, CANCELLED),
            }


class _Lane:
    def __init__(self, name, workers, registry):
        self.name = name
        self._queue = []
        self._cv = threading.Condition()
        self._registry = registry
        self._threads = []
        for i in range(workers):
            t = threading.Thread(
                target=self._run, name="termuxfm-%s-%d" % (name, i), daemon=True
            )
            t.start()
            self._threads.append(t)

    def put(self, job, fn):
        with self._cv:
            self._queue.append((job, fn))
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job, fn = self._queue.pop(0)
            self._execute(job, fn)

    def _execute(self, job, fn):
        if job.cancelled:
            job.state = CANCELLED
            job.finished = time.time()
            return
        job.state = RUNNING
        job.started = time.time()
        try:
            job.result = fn(job)
            job.state = CANCELLED if job.cancelled else DONE
        except Cancelled:
            job.state = CANCELLED
            job.message = "Cancelled"
        except Exception as exc:                     # noqa: BLE001 -- last resort
            job.state = ERROR
            job.message = str(exc) or exc.__class__.__name__
            self._registry.log("job %s (%s) failed: %s\n%s"
                               % (job.id, job.kind, exc, traceback.format_exc()))
        finally:
            job.finished = time.time()
            job.current = ""


class JobRegistry:
    def __init__(self, logger=None):
        self._jobs = {}
        self._lock = threading.Lock()
        self._logger = logger
        self._lanes = {
            "fs": _Lane("fs", 1, self),
            "scan": _Lane("scan", 2, self),
        }

    def set_logger(self, logger):
        self._logger = logger

    def log(self, message):
        if self._logger:
            self._logger(message)

    def submit(self, kind, label, fn, lane="fs"):
        job = Job(kind, label)
        with self._lock:
            self._reap_locked()
            self._jobs[job.id] = job
        self._lanes[lane].put(job, fn)
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id):
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel()
        return True

    def active(self):
        with self._lock:
            return [j.snapshot() for j in self._jobs.values()
                    if j.state in (PENDING, RUNNING)]

    def _reap_locked(self):
        now = time.time()
        stale = [
            jid for jid, job in self._jobs.items()
            if job.finished and now - job.finished > REAP_AFTER
        ]
        for jid in stale:
            self._jobs.pop(jid, None)
        if len(self._jobs) > MAX_JOBS_KEPT:
            finished = sorted(
                (j for j in self._jobs.values() if j.finished),
                key=lambda j: j.finished,
            )
            for job in finished[: len(self._jobs) - MAX_JOBS_KEPT]:
                self._jobs.pop(job.id, None)
