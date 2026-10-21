# Lustre OST utilization and imbalance
Tags: storage, lustre, ost, imbalance, striping, capacity, utilization

Files are striped across one or more OSTs. When OST utilization is uneven, new objects
placed on the fullest OSTs can fail with out-of-space errors even though aggregate
filesystem utilization is below its limit. The spread between the least and most used OST
is a useful indicator; the acceptable spread is site-specific.
