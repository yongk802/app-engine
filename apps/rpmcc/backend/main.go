// Command rpmcc-server is the app-engine wrapper for RPM Command Center.
//
// app-engine builds and starts this binary (see app.json) with
//
//	rpmcc-server web --addr 127.0.0.1:$PORT
//
// It is a thin shell around cmd.Execute from the real rpm-command-center
// module, so the app and the `rpmcc` CLI you run in a terminal are the same
// program built from the same source.
//
// Note on $PORT: app-engine launches the start command with
// asyncio.create_subprocess_exec, which does NOT go through a shell. So the
// argv string "127.0.0.1:$PORT" reaches us with "$PORT" unexpanded — there is
// no shell to expand it. The engine does set PORT in the child's environment,
// which is the documented app-engine convention (see its Go/Node templates),
// so expandAddr substitutes an env reference here.
//
// The only app-engine-specific addition is /healthz, which the engine polls
// before serving the iframe. Rather than add that route to rpmcc proper (where
// it would be dead weight for CLI users), a tiny reverse proxy sits in front:
//
//	              ┌──────────────────────────────┐
//	app-engine ──▶│ rpmcc-server (this binary)   │
//	              │  /healthz  ── handled here   │
//	              │  everything else ────────────┼──▶ rpmcc web on a loopback port
//	              └──────────────────────────────┘
//
// The inner server binds its own ephemeral port, started as a goroutine, so the
// process owns exactly one listener and app-engine's PID accounting stays
// correct.
package main

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/servicenow/rpm-command-center/rpmcc"
)

func main() {
	// app-engine passes --addr 127.0.0.1:$PORT; parse just enough to find it.
	// expandAddr resolves the literal token from PORT, then falls back to the
	// CLI default, so the binary works both under app-engine and by hand.
	addr := expandAddr(flagAddr(os.Args[1:]))

	// The inner rpmcc server binds an ephemeral loopback port.
	//
	// ConfigPath matters: a packaged app has no repo checkout, so with no path
	// the server would fall back to the built-in workflow defaults, which leave
	// direct_build EMPTY — and the very first step then fails with
	// "direct_build.buildmaster ... must be set". Pointing at a writable file
	// and seeding it on first run gives the Settings screen something to edit
	// and the workflow something to run.
	configPath := os.Getenv("RPMCC_CONFIG")
	if configPath == "" {
		configPath = rpmcc.DefaultConfigPath()
	}
	if err := seedConfig(configPath); err != nil {
		fmt.Fprintf(os.Stderr, "rpmcc-server: warning: could not seed %s: %v\n", configPath, err)
	}

	inner, err := rpmcc.NewWebServer(rpmcc.WebOptions{
		Addr:       "127.0.0.1:0",
		RPMType:    "snc-provision-go",
		ConfigPath: configPath,
	})
	if err != nil {
		fatal("constructing rpmcc web: %v", err)
	}

	ln, err := net.Listen("tcp", inner.Addr())
	if err != nil {
		fatal("binding inner listener: %v", err)
	}

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := http.Serve(ln, inner.Handler()); err != nil {
			// http.Serve returns ErrServerClosed on a clean stop; anything else
			// is worth printing since the process is about to look healthy.
			fmt.Fprintf(os.Stderr, "rpmcc-server: inner server stopped: %v\n", err)
		}
	}()

	target, err := url.Parse("http://" + ln.Addr().String())
	if err != nil {
		fatal("parsing inner address: %v", err)
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	// The app is served from <app-id>.localhost and the engine injects a
	// <base href>; the proxy must not rewrite paths, only forward them.
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "rpmcc backend unreachable: "+err.Error(), http.StatusBadGateway)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true}`)
	})
	mux.Handle("/", proxy)

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	fmt.Printf("rpmcc-server: serving %s (inner %s)\n", addr, ln.Addr().String())
	if err := srv.ListenAndServe(); err != nil {
		fatal("serving: %v", err)
	}
	wg.Wait()
}

// flagAddr extracts the value of --addr / -addr / --addr=… / -addr=… from argv
// without pulling in cobra (the inner command parses its own flags). Both the
// space and the equals form, and both dash counts, are accepted because argv is
// written by hand in app.json and read by hand here.
func flagAddr(args []string) string {
	for i, a := range args {
		for _, name := range []string{"--addr", "-addr"} {
			if a == name {
				if i+1 < len(args) {
					return args[i+1]
				}
				return ""
			}
			if strings.HasPrefix(a, name+"=") {
				return strings.TrimPrefix(a, name+"=")
			}
		}
	}
	return ""
}

// expandAddr substitutes a literal "$PORT" / "${PORT}" with the PORT environment
// variable. app-engine does not run the start command through a shell, so the
// "$PORT" in the manifest's argv arrives here unexpanded.
func expandAddr(addr string) string {
	port := os.Getenv("PORT")

	// Substitute the literal token if one is present. This covers the manifest
	// spelling "127.0.0.1:$PORT".
	if port != "" {
		addr = strings.ReplaceAll(addr, "${PORT}", port)
		addr = strings.ReplaceAll(addr, "$PORT", port)
	}

	host, portPart := splitHostPort(addr)

	// Use the parsed port when it is a real number; otherwise fall back to the
	// environment, and only then give up. This is what makes an empty or
	// unexpanded "$PORT" land on a usable address instead of "127.0.0.1:".
	if _, err := strconv.Atoi(portPart); err != nil || portPart == "" {
		portPart = port
	}
	if portPart == "" {
		portPart = "8777"
	}
	if host == "" {
		host = "127.0.0.1"
	}
	return host + ":" + portPart
}

// splitHostPort splits host:port without strconv, tolerating a missing port.
func splitHostPort(addr string) (host, port string) {
	if i := strings.LastIndex(addr, ":"); i >= 0 {
		return strings.TrimSuffix(addr[:i], ":"), addr[i+1:]
	}
	return addr, ""
}

func fatal(format string, a ...interface{}) {
	fmt.Fprintf(os.Stderr, "rpmcc-server: "+format+"\n", a...)
	os.Exit(1)
}

// seedConfig writes a config file with built-in defaults when none exists, so
// the Settings screen opens on real values rather than a blank form and the
// first workflow step has something to validate against.
//
// An existing file is left completely alone — the human's edits win.
func seedConfig(path string) error {
	if rpmcc.ConfigExists(path) {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return rpmcc.DefaultConfig().Save(path)
}
