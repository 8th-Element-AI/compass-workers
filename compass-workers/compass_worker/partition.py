"""Slot ownership for horizontally-scaled lens workers.

A fixed virtual slot space (TOTAL_SLOTS, default 16) divides spans
deterministically via `partition_id = cityHash64(trace_id) % TOTAL_SLOTS`,
computed at ingestion as a MATERIALIZED column on compass_raw_spans.

At runtime, each pod owns a subset of slots determined by its
(partition_index, partition_count). Slot checkpoints live in
worker_checkpoints under partition_key = f"slot:{slot}" — they survive
pod count changes, so scaling is automatic.

Examples:
    compute_slots(0, 1, 16)  -> [0..15]   # single pod owns everything
    compute_slots(0, 4, 16)  -> [0,1,2,3]
    compute_slots(2, 4, 16)  -> [8,9,10,11]
    compute_slots(0, 3, 16)  -> [0..5]    # first pod gets the extra slot
    compute_slots(1, 3, 16)  -> [6..10]
    compute_slots(2, 3, 16)  -> [11..15]
"""
from __future__ import annotations

DEFAULT_TOTAL_SLOTS = 16


def compute_slots(
    pod_index: int,
    pod_count: int,
    total_slots: int = DEFAULT_TOTAL_SLOTS,
) -> list[int]:
    """Return the slot indices this pod owns.

    Distributes `total_slots` across `pod_count` pods as evenly as possible.
    The first (total_slots % pod_count) pods each receive one extra slot.

    Args:
        pod_index: This pod's 0-based index. Must be < pod_count.
        pod_count: Total number of pods sharing the slot space. 1 <= pod_count <= total_slots.
        total_slots: Size of the slot space. Must match the value of the
            cityHash64 modulus used on compass_raw_spans.partition_id.

    Raises:
        ValueError: If pod_index or pod_count is out of range.
    """
    if pod_count < 1 or pod_count > total_slots:
        raise ValueError(
            f"pod_count must be in [1, {total_slots}]; got {pod_count}"
        )
    if pod_index < 0 or pod_index >= pod_count:
        raise ValueError(
            f"pod_index must be in [0, {pod_count}); got {pod_index}"
        )

    base = total_slots // pod_count
    extra = total_slots % pod_count

    if pod_index < extra:
        # This pod gets one of the extra slots
        start = pod_index * (base + 1)
        return list(range(start, start + base + 1))
    else:
        # This pod gets the base allocation
        start = extra * (base + 1) + (pod_index - extra) * base
        return list(range(start, start + base))


def slot_partition_key(slot: int) -> str:
    """Stable PG partition_key for a slot. Pairs with worker_checkpoints."""
    return f"slot:{slot}"


def is_partitioned(pod_count: int) -> bool:
    """True if this deployment is using the slot-based scaling path."""
    return pod_count > 1