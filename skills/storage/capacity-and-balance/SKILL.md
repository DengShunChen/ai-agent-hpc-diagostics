---
name: capacity-and-balance
description: "Judging filesystem and OST capacity, OST imbalance and health against the site thresholds."
agents: [storage-agent]
---
# Is storage capacity or balance a plausible cause?

Use the site thresholds given in your instructions; they differ per site. Every conclusion
must cite values you fetched.

## Capacity
- A filesystem or an OST is full only when its `used_pct` is at or above the site's full
  threshold. Below it, it is not full, however high the number looks.
- A full OST can make writes fail even when the filesystem as a whole has room: check each OST.
- Category `storage.capacity`, supported by the full component's `used_pct`.

## Imbalance
- OSTs are imbalanced only when (highest `used_pct` - lowest `used_pct`) is at or above the
  site's spread threshold. A smaller spread is normal and is not a cause.
- Category `storage.ost_imbalance`, citing the highest and lowest OST values.

## Health
- A filesystem `health` or an OST `state` other than healthy/active (for example degraded,
  inactive or offline) supports `storage.health`.

## When nothing crosses a threshold
- If capacity, balance and health are all within limits, the data does not support a storage
  cause. Give no storage hypothesis; you may rule a cause out by citing the values.
- If storage data is missing (no_data), say what is missing instead of guessing.
