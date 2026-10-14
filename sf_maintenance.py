"""Deferrable system scheduling for non-urgent local maintenance."""
from sf_constants import BUNDLE_ID


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
                scheduler = NSBackgroundActivityScheduler.alloc().initWithIdentifier_(BUNDLE_ID + ".updates")
            else:
                scheduler = self._factory()
            scheduler.setInterval_(self.interval)
            scheduler.setTolerance_(min(self.interval / 4, 3600))
            scheduler.setRepeats_(True)
            self._callback = callback

            def scheduled(completion):
                try:
                    if self._callback is not None:
                        self._callback()
                except Exception:
                    self.failure_count += 1
                finally:
                    completion(1)

            scheduler.scheduleWithBlock_(scheduled)
            self._scheduler = scheduler
            self.active = True
            return True
        except Exception:
            self.failure_count += 1
            self._callback = None
            return False

    def stop(self):
        self._callback = None
        if self._scheduler is not None:
            try:
                self._scheduler.invalidate()
            except Exception:
                self.failure_count += 1
                return False
        self._scheduler = None
        self.active = False
        return True

    def diagnostics(self):
        return {"active": self.active, "interval_seconds": self.interval,
                "failure_count": self.failure_count}
