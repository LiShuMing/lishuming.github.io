//go:build linux

// Reproducible observations for the Go 1.26.2 runtime article.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"runtime"
	"runtime/debug"
	"runtime/metrics"
	"runtime/trace"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

type node struct {
	next *node
	data [56]byte
}

var metricNames = []string{
	"/gc/scan/heap:bytes",
	"/gc/scan/stack:bytes",
	"/gc/scan/globals:bytes",
	"/gc/cycles/automatic:gc-cycles",
	"/gc/cycles/forced:gc-cycles",
	"/cpu/classes/gc/mark/assist:cpu-seconds",
	"/cpu/classes/gc/mark/dedicated:cpu-seconds",
	"/cpu/classes/gc/mark/idle:cpu-seconds",
}

func snapshot(label string) {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	samples := make([]metrics.Sample, len(metricNames))
	for i, name := range metricNames {
		samples[i].Name = name
	}
	metrics.Read(samples)
	values := make(map[string]any, len(samples))
	for _, s := range samples {
		switch s.Value.Kind() {
		case metrics.KindUint64:
			values[s.Name] = s.Value.Uint64()
		case metrics.KindFloat64:
			values[s.Name] = s.Value.Float64()
		default:
			panic("unavailable metric: " + s.Name)
		}
	}
	check(json.NewEncoder(os.Stdout).Encode(map[string]any{
		"label": label, "version": runtime.Version(),
		"gomaxprocs": runtime.GOMAXPROCS(0), "goroutines": runtime.NumGoroutine(),
		"heap_alloc": m.HeapAlloc, "heap_inuse": m.HeapInuse,
		"heap_idle": m.HeapIdle, "heap_released": m.HeapReleased,
		"runtime_memory": m.Sys - m.HeapReleased, "stack_inuse": m.StackInuse,
		"next_gc": m.NextGC, "num_gc": m.NumGC,
		"total_alloc": m.TotalAlloc, "mallocs": m.Mallocs, "metrics": values,
	}))
}

// The returned ring keeps exactly keep batches alive after the loop.
func allocate(kind string, rounds, objects, keep int) any {
	if kind == "bytes" {
		ring := make([][]byte, keep)
		for r := 0; r < rounds; r++ {
			b := make([]byte, objects*64)
			for i := 0; i < len(b); i += 4096 {
				b[i] = byte(r + 1)
			}
			ring[r%keep] = b
		}
		return ring
	}
	ring := make([]*node, keep)
	for r := 0; r < rounds; r++ {
		var head *node
		for i := 0; i < objects; i++ {
			head = &node{next: head, data: [56]byte{byte(i)}}
		}
		ring[r%keep] = head
	}
	return ring
}

func runGC(kind string, rounds, objects, keep int) {
	if unsafe.Sizeof(node{}) != 64 {
		panic("this workload requires a 64-byte node")
	}
	runtime.GC()
	snapshot("baseline")
	retained := allocate(kind, rounds, objects, keep)
	runtime.GC()
	snapshot("retained")
	runtime.KeepAlive(retained)
	retained = nil
	runtime.GC()
	snapshot("released")
	debug.FreeOSMemory()
	snapshot("scavenged")
}

func runRoots(count, payloadBytes int) {
	runtime.GC()
	snapshot("baseline")
	var ready, done sync.WaitGroup
	ready.Add(count)
	done.Add(count)
	release := make(chan struct{})
	for i := 0; i < count; i++ {
		go func() {
			defer done.Done()
			payload := make([]byte, payloadBytes)
			for j := 0; j < len(payload); j += 4096 {
				payload[j] = 1
			}
			ready.Done()
			<-release
			runtime.KeepAlive(payload)
		}()
	}
	ready.Wait()
	runtime.GC()
	snapshot("waiting")
	close(release)
	done.Wait()
	runtime.GC()
	snapshot("joined")
	debug.FreeOSMemory()
	snapshot("scavenged")
}

func runSyscall() {
	runtime.GOMAXPROCS(1)
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	var fds [2]int
	check(syscall.Pipe2(fds[:], syscall.O_CLOEXEC))
	defer syscall.Close(fds[0])
	defer syscall.Close(fds[1])
	done := make(chan error, 1)
	start := time.Now()
	fmt.Printf("reader tid=%d gomaxprocs=%d\n", syscall.Gettid(), runtime.GOMAXPROCS(0))
	go func() {
		time.Sleep(100 * time.Millisecond)
		fmt.Printf("writer tid=%d after=%s\n", syscall.Gettid(), time.Since(start).Round(time.Millisecond))
		n, err := syscall.Write(fds[1], []byte{42})
		if err == nil && n != 1 {
			err = fmt.Errorf("short write: %d", n)
		}
		done <- err
	}()
	// A blocking descriptor and syscall.Read exercise entersyscall, not netpoll.
	var b [1]byte
	for {
		n, err := syscall.Read(fds[0], b[:])
		if err == syscall.EINTR {
			continue
		}
		check(err)
		if n != 1 || b[0] != 42 {
			panic("unexpected pipe data")
		}
		break
	}
	check(<-done)
	fmt.Printf("reader resumed tid=%d after=%s byte=%d\n", syscall.Gettid(), time.Since(start).Round(time.Millisecond), b[0])
}

func check(err error) {
	if err != nil {
		panic(err)
	}
}

func main() {
	mode := flag.String("mode", "gc", "gc, roots, or syscall")
	kind := flag.String("kind", "nodes", "nodes or bytes")
	rounds := flag.Int("rounds", 64, "allocation rounds")
	objects := flag.Int("objects", 32768, "64-byte units per round")
	keep := flag.Int("keep", 8, "retained batches")
	workers := flag.Int("goroutines", 2000, "waiting goroutines")
	payload := flag.Int("payload", 16384, "bytes retained per waiting goroutine")
	tracePath := flag.String("trace", "", "optional runtime trace output")
	flag.Parse()
	if *rounds <= 0 || *objects <= 0 || *keep <= 0 || *keep > *rounds || *workers <= 0 || *payload <= 0 {
		panic("counts must be positive and keep must not exceed rounds")
	}
	if *kind != "nodes" && *kind != "bytes" {
		panic("kind must be nodes or bytes")
	}
	if *tracePath != "" {
		f, err := os.Create(*tracePath)
		check(err)
		defer f.Close()
		check(trace.Start(f))
		defer trace.Stop()
	}
	switch *mode {
	case "gc":
		runGC(*kind, *rounds, *objects, *keep)
	case "roots":
		runRoots(*workers, *payload)
	case "syscall":
		runSyscall()
	default:
		panic("unknown mode")
	}
}
