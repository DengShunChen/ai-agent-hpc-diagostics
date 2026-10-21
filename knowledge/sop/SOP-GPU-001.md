# Triage of GPU XID events and CUDA errors
Tags: gpu, xid, cuda, dcgm, ecc, nvlink, pcie, illegal memory access, hardware

1. Record the XID code, GPU index, node and time, and the application's CUDA error message.
2. Application-class XIDs (for example 13, 31, 43) are commonly caused by application bugs or the CUDA software environment; check whether the error reproduces on other GPUs or nodes.
3. Hardware-class XIDs (for example 48, 79, 94, 95) or non-zero ECC double-bit counters warrant an operator-run GPU diagnostic.
4. Collect ECC counters, temperature and power history, PCIe replay and NVLink error counters before any hardware conclusion.
5. GPU diagnostics are run by an operator under change control, never automatically.
