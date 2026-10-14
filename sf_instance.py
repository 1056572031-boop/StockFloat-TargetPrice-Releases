"""Process-lifetime advisory lock; the lock file is never unlinked."""
import fcntl
import os
import stat


class InstanceLease:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def acquire_instance_lock(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise PermissionError("instance lock is not an owned regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return InstanceLease(descriptor)
    except Exception:
        os.close(descriptor)
        raise
