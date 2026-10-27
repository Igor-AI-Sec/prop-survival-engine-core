"""Live identity checks for factory resources; no account/RNG state is shared."""

from threading import RLock
from weakref import ref


class _OwnershipRegistry:
    def __init__(self):
        self._objects = {}
        self._lock = RLock()

    def claim(self, resource, owner, kind):
        identity = id(resource)
        with self._lock:
            previous = self._objects.get(identity)
            if previous is not None and previous[0]() is resource:
                if previous[1] is not owner:
                    raise ValueError(f"factory must return fresh {kind}; resource belongs to another path")
                return  # A path source may also implement its own stream.

            def release(reference):
                with self._lock:
                    entry = self._objects.get(identity)
                    if entry is not None and entry[0] is reference:
                        del self._objects[identity]

            try:
                reference = ref(resource, release)
            except TypeError:
                raise ValueError(f"owned {kind} must support weak references") from None
            self._objects[identity] = (reference, owner)


# Cross-evaluation/evaluator protection without retaining completed engines and
# their event histories. Cached resources stay alive and therefore stay claimed.
ownership = _OwnershipRegistry()
