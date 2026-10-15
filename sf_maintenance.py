"""Deferrable system scheduling for non-urgent local maintenance."""
from sf_constants import BUNDLE_ID
from sf_logging import log_swallow


def _pin_completion_block_signature():
    import objc

    # PyObjC 12.2.2 flattens the nested completion block into an NSInteger.
    objc.registerMetaDataForSelector(
        b"NSBackgroundActivityScheduler", b"scheduleWithBlock:",
        {"arguments": {2: {
            "type": b"@?",
            "callable": {
                "retval": {"type": b"v"},
                "arguments": {
                    0: {"type": b"^v"},
                    1: {
                        "type": b"@?",
                        "callable": {
                            "retval": {"type": b"v"},
                            "arguments": {
                                0: {"type": b"^v"},
                                1: {"type": objc._C_NSInteger},
                            },
                        },
                    },
                },
            },
        }}},
    )


class BackgroundMaintenance:
    def __init__(self, interval, factory=None):
        self.interval = float(interval)
        self._factory = factory
        self._scheduler = None
        self._callback = None
        self.active = False
        self.failure_count = 0

    def start(self, callback):
        try:
            if self._factory is None:
                from Foundation import NSBackgroundActivityScheduler
                _pin_completion_block_signature()
                scheduler = NSBackgroundActivityScheduler.alloc().initWithIdentifier_(BUNDLE_ID + ".updates")
            else:
                scheduler = self._factory()
            scheduler.setInterval_(self.interval)
            scheduler.setTolerance_(min(self.interval / 4, 3600))
            scheduler.setRepeats_(True)
            self._callback = callback

            scheduler.scheduleWithBlock_(self._run_scheduled)
            self._scheduler = scheduler
            self.active = True
            return True
        except Exception as exc:
            self.failure_count += 1
            self._callback = None
            log_swallow("sf_maintenance.start", exc)
            return False

    def _run_scheduled(self, completion):
        if not callable(completion):
            self.failure_count += 1
            log_swallow("sf_maintenance.completion",
                        TypeError("native maintenance completion is not callable"))
            return
        try:
            if self._callback is not None:
                self._callback()
        except Exception as exc:
            self.failure_count += 1
            log_swallow("sf_maintenance.callback", exc)
        finally:
            # Exceptions escaping an ObjC block terminate the whole application.
            try:
                completion(1)
            except Exception as exc:
                self.failure_count += 1
                log_swallow("sf_maintenance.completion", exc)

    def stop(self):
        self._callback = None
        if self._scheduler is not None:
            try:
                self._scheduler.invalidate()
            except Exception as exc:
                self.failure_count += 1
                log_swallow("sf_maintenance.stop", exc)
                return False
        self._scheduler = None
        self.active = False
        return True

    def diagnostics(self):
        return {"active": self.active, "interval_seconds": self.interval,
                "failure_count": self.failure_count}
