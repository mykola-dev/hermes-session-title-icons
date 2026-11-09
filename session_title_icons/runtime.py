"""A single gateway-resident queue consumer; managed workers only enqueue IDs."""
import asyncio
import threading
import time


class Resident:
    def __init__(self, queue, engine_factory, profile, allowed):
        self.queue, self.engine_factory = queue, engine_factory
        self.profile, self.allowed = profile, allowed
        self.stopped = threading.Event()
        self.loop = self.task = self.thread = self.lock_file = None

    def start(self):
        # The supported deployment is the Linux composed gateway. A managed
        # worker MUST NOT win the service lock and own the timer lifecycle.
        from agent.safe_worker_policy import worker_config_snapshot
        if worker_config_snapshot() is not None:
            return False
        import fcntl
        self.lock_file = (self.queue.directory / 'service.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            return False
        self.thread = threading.Thread(target=self._main, name='session-title-icons', daemon=True)
        self.thread.start()
        return True

    def _main(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception:
            # Do not let a thread traceback expose host/provider details.
            self.stopped.set()
        finally:
            self.loop = self.task = None
            self.lock_file.close()
            self.lock_file = None

    async def _run(self):
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        engine = self.engine_factory()
        active = set()
        try:
            await asyncio.to_thread(self.queue.recover)
            while not self.stopped.is_set():
                if self.allowed() and len(active) < 4:
                    job = await asyncio.to_thread(self.queue.claim)
                    if job:
                        task = asyncio.create_task(self._process(engine, job))
                        active.add(task)
                        task.add_done_callback(active.discard)
                        continue
                await asyncio.sleep(0.25)
        finally:
            engine.consent = False
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            await engine.close()

    async def _process(self, engine, job):
        result = {'error': 'job_failed'}
        try:
            if time.time() - job['due'] > 300:
                result = {'skipped': 1, 'reason': 'expired'}
            elif not self.allowed():
                result = {'skipped': 1, 'reason': 'disabled'}
            elif job['kind'] == 'auto':
                outcome = await engine.decorate(self.profile, job['session_id'])
                result = {outcome.value: 1}
            else:
                result = await engine.recent(self.profile, job['limit_n'])
        except asyncio.CancelledError:
            result = {'error': 'interrupted_outcome_unknown'}
            raise
        except Exception:
            pass  # fixed error only, never exception/title/model output logging
        finally:
            await asyncio.to_thread(self.queue.finish, job['id'], result)

    def stop(self):
        self.stopped.set()
        loop, task = self.loop, self.task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=5)
