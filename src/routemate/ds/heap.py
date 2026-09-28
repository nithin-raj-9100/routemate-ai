from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Hashable, Iterator, TypeVar

K = TypeVar("K", bound=Hashable)
T = TypeVar("T")


@dataclass
class HeapCounters:
    pushes: int = 0
    pops: int = 0
    decrease_keys: int = 0
    max_size: int = 0


class BinaryHeap(Generic[T]):
    """Array-backed binary min-heap. Ties are broken by insertion order so items never need to be comparable."""

    def __init__(self) -> None:
        self._data: list[tuple[Any, int, T]] = []
        self._seq = 0
        self.counters = HeapCounters()

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        return bool(self._data)

    def push(self, priority: Any, item: T) -> None:
        self._data.append((priority, self._seq, item))
        self._seq += 1
        self._sift_up(len(self._data) - 1)
        self.counters.pushes += 1
        self.counters.max_size = max(self.counters.max_size, len(self._data))

    def peek(self) -> tuple[Any, T]:
        if not self._data:
            raise IndexError("peek from empty heap")
        priority, _, item = self._data[0]
        return priority, item

    def pop(self) -> tuple[Any, T]:
        if not self._data:
            raise IndexError("pop from empty heap")
        last = self._data.pop()
        if self._data:
            top = self._data[0]
            self._data[0] = last
            self._sift_down(0)
        else:
            top = last
        self.counters.pops += 1
        return top[0], top[2]

    def _sift_up(self, i: int) -> None:
        data = self._data
        entry = data[i]
        while i > 0:
            parent = (i - 1) >> 1
            if entry[:2] < data[parent][:2]:
                data[i] = data[parent]
                i = parent
            else:
                break
        data[i] = entry

    def _sift_down(self, i: int) -> None:
        data = self._data
        n = len(data)
        entry = data[i]
        while True:
            child = 2 * i + 1
            if child >= n:
                break
            right = child + 1
            if right < n and data[right][:2] < data[child][:2]:
                child = right
            if data[child][:2] < entry[:2]:
                data[i] = data[child]
                i = child
            else:
                break
        data[i] = entry


@dataclass(order=True)
class _Slot(Generic[K]):
    priority: Any
    seq: int
    key: K = field(compare=False)


class IndexedMinHeap(Generic[K]):
    """Binary min-heap keyed by hashable ids with O(log n) decrease-key, as used by textbook Dijkstra."""

    def __init__(self) -> None:
        self._heap: list[_Slot[K]] = []
        self._pos: dict[K, int] = {}
        self._seq = 0
        self.counters = HeapCounters()

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def __contains__(self, key: object) -> bool:
        return key in self._pos

    def __iter__(self) -> Iterator[K]:
        return iter(list(self._pos))

    def priority(self, key: K) -> Any:
        return self._heap[self._pos[key]].priority

    def push_or_decrease(self, key: K, priority: Any) -> bool:
        """Insert key, or lower its priority. Returns False when the existing priority is already <= priority."""
        if key in self._pos:
            i = self._pos[key]
            if not priority < self._heap[i].priority:
                return False
            self._heap[i].priority = priority
            self._sift_up(i)
            self.counters.decrease_keys += 1
            return True
        slot = _Slot(priority, self._seq, key)
        self._seq += 1
        self._heap.append(slot)
        self._pos[key] = len(self._heap) - 1
        self._sift_up(len(self._heap) - 1)
        self.counters.pushes += 1
        self.counters.max_size = max(self.counters.max_size, len(self._heap))
        return True

    def pop(self) -> tuple[K, Any]:
        if not self._heap:
            raise IndexError("pop from empty heap")
        top = self._heap[0]
        last = self._heap.pop()
        del self._pos[top.key]
        if self._heap:
            self._heap[0] = last
            self._pos[last.key] = 0
            self._sift_down(0)
        self.counters.pops += 1
        return top.key, top.priority

    def _swap(self, i: int, j: int) -> None:
        h = self._heap
        h[i], h[j] = h[j], h[i]
        self._pos[h[i].key] = i
        self._pos[h[j].key] = j

    def _sift_up(self, i: int) -> None:
        while i > 0:
            parent = (i - 1) >> 1
            if self._heap[i] < self._heap[parent]:
                self._swap(i, parent)
                i = parent
            else:
                break

    def _sift_down(self, i: int) -> None:
        n = len(self._heap)
        while True:
            smallest = i
            for child in (2 * i + 1, 2 * i + 2):
                if child < n and self._heap[child] < self._heap[smallest]:
                    smallest = child
            if smallest == i:
                return
            self._swap(i, smallest)
            i = smallest
