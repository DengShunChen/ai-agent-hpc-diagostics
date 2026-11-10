---
name: link-health
description: "Reading interface and bond status: link down, flapping, packet errors and degraded bonds."
agents: [network-agent]
---
# Is a network link a plausible cause?

Cite the node, interface and the fetched values. The saved data comes from the node side only.

## Link state
- An interface whose `state` is not up: `network.link_down`. Cite the state and `last_change` if present.
- A bond that is up while one or more `members` are not up: `network.bond_degraded`; redundancy
  is reduced even though traffic may still flow.

## Instability and errors
- `link_flaps` above 0: `network.link_flap` (intermittent connectivity).
- Non-zero `rx_errors` or `tx_errors`: `network.packet_errors`; how many is significant depends on
  the interface's normal baseline, so say so.

## Always missing from node data
- Switch-side port status and counters are not in the saved data. When a link looks unhealthy,
  list the switch-side port status as missing information.
