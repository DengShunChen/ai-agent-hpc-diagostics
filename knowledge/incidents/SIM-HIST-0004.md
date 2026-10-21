# CUDA illegal memory access with XID 13 caused by application bug
Tags: gpu, xid 13, cuda, illegal memory access, application

Synthetic past incident. A training job aborted with an illegal memory access and XID 13
on one GPU. The error reproduced on other nodes; GPU diagnostics passed. Resolution: an
out-of-bounds index in the application was fixed.
