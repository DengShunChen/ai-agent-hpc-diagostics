# Investigating link errors and bond degradation
Tags: network, nic, bond, link down, link flap, packet errors, ethernet, interface, switch

1. Record interface state, bond member states and error counters for the affected node.
2. A single counter snapshot cannot show whether errors are increasing; compare at least two snapshots.
3. A degraded bond with one member down reduces redundancy but does not by itself cause an outage.
4. Check the switch-side port status and logs for the same link and time window.
5. Do not change interface or switch configuration without an approved change.
