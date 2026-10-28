# Go Runtime Lab

Companion to the Chinese article `2026-10-22-go-runtime-gc-goroutine-source-notes.md`.
Recorded on 2026-10-22 in WSL2, Linux 6.6.87.2-microsoft-standard-WSL2,
linux/amd64, using `/home/lism/work/go/bin/go` (go1.26.2).
Green Tea is enabled by the toolchain baseline; GOEXPERIMENT is empty.

## Build and Run

```bash
GO=/home/lism/work/go/bin/go
LAB=/home/lism/work/lishuming.github.io/static/code/go-runtime-lab
"$GO" build -o /tmp/go-runtime-lab "$LAB/main.go"
for gc in 50 100 200; do
  GOMAXPROCS=2 GOGC="$gc" GOMEMLIMIT=off GODEBUG=gctrace=1 \
    /tmp/go-runtime-lab -mode gc -kind nodes \
    > "/tmp/nodes-gogc${gc}.jsonl" 2> "/tmp/nodes-gogc${gc}.gctrace"
done
GOMAXPROCS=2 GOGC=100 GOMEMLIMIT=off GODEBUG=gctrace=1 \
  /tmp/go-runtime-lab -mode gc -kind bytes \
  > /tmp/bytes-gogc100.jsonl 2> /tmp/bytes-gogc100.gctrace
GOMAXPROCS=2 GOGC=100 GOMEMLIMIT=off /tmp/go-runtime-lab -mode roots \
  > /tmp/roots.jsonl
GOMAXPROCS=1 GOGC=100 GOMEMLIMIT=off /tmp/go-runtime-lab \
  -mode syscall -trace /tmp/go-runtime-syscall.trace
"$GO" tool trace -d=parsed /tmp/go-runtime-syscall.trace
"$GO" build -gcflags=-m -o /tmp/go-runtime-lab-escape "$LAB/main.go"
```

The trace command uses the Go 1.26.2 `parsed` mode. Older instructions using
`-d=1` do not work with this toolchain. The binary trace is left in `/tmp`, not
published with the site. The recorded text output is in `results/syscall.txt`.
`results/syscall-transitions.txt` extracts reader G1, writer G10, and P0
state transitions from that run's parsed trace. IDs vary between runs.

## Workload and Interpretation

- `gc`: 64 rounds, 32768 units of 64 bytes per round, eight retained batches.
  Nodes use one 64-byte allocation per unit; bytes use one 2 MiB backing array
  per round. Both retain about 16 MiB of payload and allocate about 128 MiB,
  but allocation counts, graph topology, and writing costs differ. This is not
  a throughput comparison.
- `roots`: 2000 Gs each retain 16 KiB while waiting on a channel. KeepAlive
  after the receive makes the payload live during the wait. All workers join.
- `syscall`: a blocking Pipe2 descriptor, syscall.Read, a reader locked to its
  OS thread, one P, and a writer sleeping 100 ms. Checks the received byte and
  records both Linux thread IDs. It does not exercise the netpoll I/O path.
- Snapshots use MemStats and runtime/metrics, read sequentially, not atomically
  together. JSON encoding and instrumentation allocate small amounts of memory.
- GC workload snapshots are baseline, retained after forced GC, released after
  dropping references and forced GC, and scavenged after debug.FreeOSMemory.
- Automatic cycle counts exclude forced cycles. CPU metrics are cumulative
  seconds, not request latency. Results are single runs, not benchmark samples.
- No RSS sampling, memory-limit stress test, or Green Tea A/B benchmark was run.

## Verified Source Hashes

The following local SHA-256 hashes match the corresponding raw upstream files
at `https://github.com/golang/go/tree/go1.26.2/src/runtime`:

```text
b0ea864c1dd251cd9146644863ae9126ca57761dff05862dc80ab0799925fec6  proc.go
8ac2523177e5915edfaf4e122a903eceef08b5abd765f8d7ed96a8cc1ed3a504  mgc.go
b9399df0e9db1b8fb8bc701accfbebfe4615303b1f649ec1c716024847969e73  mgcpacer.go
34cd31c189bfc011739dd738656f5e698521a736c3b05e2a2f3da49fc6011f3b  mgcmark_greenteagc.go
```

The source directory did not provide a usable Git HEAD. Matching these four
files does not assert an unmodified checkout of every file in the source tree.
