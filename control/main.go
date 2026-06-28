package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// main.go — process entrypoint: read config from the environment, open the
// Postgres pool (FATAL if DATABASE_URL is unset — no silent fallback), apply the
// control-plane migrations, build the object store, wire the single
// http.ServeMux, start the background workers, and run with graceful shutdown.
//
// The whole control plane is one binary, one flat package main. The Postgres-
// backed task queue (scheduler.go) means there is no NATS, no message broker,
// and no second dev service to run — just this binary, a Postgres, and an
// S3-compatible object store.
//
// `control seed` runs the idempotent demo seed (seed.go) and exits without
// starting the server, so a human can obtain an api_key + worker_token to drive
// the system end to end.

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("control: ")

	// `control healthcheck`: in-container liveness probe against the running server's
	// /healthz — the distroless image has no shell/curl for a Docker HEALTHCHECK. It
	// asks the already-running server, so it needs no DB. Exit 0 = healthy, 1 = not.
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		os.Exit(runHealthcheck())
	}

	// DATABASE_URL is mandatory. BLACKHOLE doctrine: surface every failure —
	// a missing DSN is a fatal misconfiguration, never a fallback to nothing.
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		log.Fatal("DATABASE_URL is not set — refusing to start (set it to a Postgres connection string)")
	}
	// LISTEN_ADDR (CONTROL_PLANE_ADDR is the .env alias) sets the bind address.
	addr := os.Getenv("LISTEN_ADDR")
	if addr == "" {
		addr = os.Getenv("CONTROL_PLANE_ADDR")
	}
	if addr == "" {
		addr = ":8080"
	}

	// Hardening secrets: without these, OAuth tokens are stored unencrypted and OAuth
	// state is unsigned (CSRF-open). In production this is unacceptable, so a missing
	// secret is FATAL — a prod deploy must never silently run unhardened. Outside
	// production (CX_ENV unset or any other value) local dev + tests run without them
	// by design, so we only surface the insecure state loudly. Production sets both in .env.
	if os.Getenv("CX_TOKEN_KEY") == "" || os.Getenv("CX_STATE_SECRET") == "" {
		cxEnv := os.Getenv("CX_ENV")
		if strings.EqualFold(cxEnv, "production") || strings.EqualFold(cxEnv, "prod") {
			log.Fatal("CX_TOKEN_KEY and/or CX_STATE_SECRET unset with CX_ENV=" + cxEnv + " — refusing to start in production: OAuth tokens would be stored UNENCRYPTED and OAuth state UNSIGNED; set both")
		}
		log.Print("WARNING: CX_TOKEN_KEY and/or CX_STATE_SECRET unset — OAuth tokens stored UNENCRYPTED and OAuth state UNSIGNED; set both before production")
	}

	// Open the pool with bounded sizing so a request burst can't exhaust Postgres
	// connections, and idle/aged conns are recycled. MaxConns overridable via
	// DB_MAX_CONNS. We ping once at startup so a bad DSN fails fast and loud.
	ctx := context.Background()
	poolCfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		log.Fatalf("invalid DATABASE_URL: %v", err)
	}
	poolCfg.MaxConns = 20
	if v := os.Getenv("DB_MAX_CONNS"); v != "" {
		if n, perr := strconv.Atoi(v); perr == nil && n > 0 {
			poolCfg.MaxConns = int32(n)
		}
	}
	poolCfg.MaxConnLifetime = 30 * time.Minute
	poolCfg.MaxConnIdleTime = 5 * time.Minute
	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		log.Fatalf("failed to create pgx pool: %v", err)
	}
	defer pool.Close()
	log.Printf("db pool: max_conns=%d conn_max_lifetime=%s conn_max_idle=%s", poolCfg.MaxConns, poolCfg.MaxConnLifetime, poolCfg.MaxConnIdleTime)

	pingCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := pool.Ping(pingCtx); err != nil {
		log.Fatalf("database unreachable at startup: %v", err)
	}

	store := NewStore(pool)

	// `control seed`: run the demo seed and exit (no server, no object store
	// required). Stable demo UUIDs + ON CONFLICT DO NOTHING make it re-runnable.
	if len(os.Args) > 1 && os.Args[1] == "seed" {
		if err := seedDemo(ctx, pool); err != nil {
			log.Fatalf("seed failed: %v", err)
		}
		return
	}

	// Apply the control-plane schema additions (idempotent). Fatal on error —
	// a half-migrated DB is never a silent fallback.
	if err := store.Migrate(ctx); err != nil {
		log.Fatalf("migrate failed: %v", err)
	}

	// Object storage is mandatory: a missing/unreachable store is fatal here, not
	// a silent degradation of the job lifecycle.
	storage, err := NewStorage(ctx)
	if err != nil {
		log.Fatalf("object storage init failed: %v", err)
	}

	// Payout rail: real Stripe Connect (STRIPE_SECRET_KEY), else the alpha manual
	// export (CX_PAYOUT_EXPORT — owed credits appended to a CSV for out-of-band
	// settlement), else the honest stub (credits reach 'ready'/owed, never
	// 'released' without a real transfer). One selection, shared by the server and
	// the background release worker.
	var payout Payout = stubPayout{}
	if key := os.Getenv("STRIPE_SECRET_KEY"); key != "" {
		payout = newStripePayout(store, key)
		log.Print("payout rail: Stripe Connect (STRIPE_SECRET_KEY set)")
	} else if path := os.Getenv("CX_PAYOUT_EXPORT"); path != "" {
		payout = newManualExportPayout(path)
		log.Printf("payout rail: manual export (alpha) → %s — owed credits appended for out-of-band settlement", path)
	} else {
		log.Print("payout rail: none configured — credits reach 'ready' (owed), never 'released'")
	}

	server := NewServer(store, storage, NewVerifier(store).WithStorage(storage), payout)

	// Background workers (payout release, stale-task requeue, webhook delivery /
	// job sweep) run for the life of the process, bound to a context cancelled on
	// shutdown so the tickers stop cleanly.
	workersCtx, stopWorkers := context.WithCancel(ctx)
	defer stopWorkers()
	workers := NewWorkers(store, storage, payout)
	go workers.Run(workersCtx)
	go server.startRateLimitSweeper(workersCtx) // evict idle rate-limit buckets

	srv := &http.Server{
		Addr:              addr,
		Handler:           server.Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Run the server until a signal arrives.
	go func() {
		log.Printf("listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server error: %v", err)
		}
	}()

	// Graceful shutdown on SIGINT/SIGTERM.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Print("shutdown signal received, draining...")

	stopWorkers() // stop the background tickers first

	shutCtx, shutCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer shutCancel()
	if err := srv.Shutdown(shutCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
	log.Print("stopped")
}

// runHealthcheck probes the running control plane's /healthz over loopback and
// returns a process exit code (0 healthy, 1 not). It backs the distroless image's
// Docker HEALTHCHECK (no shell/curl available there) and needs no DB of its own.
func runHealthcheck() int {
	addr := os.Getenv("LISTEN_ADDR")
	if addr == "" {
		addr = os.Getenv("CONTROL_PLANE_ADDR")
	}
	if addr == "" {
		addr = ":8080"
	}
	if strings.HasPrefix(addr, ":") {
		addr = "127.0.0.1" + addr
	} else {
		addr = strings.Replace(addr, "0.0.0.0:", "127.0.0.1:", 1)
	}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		log.Printf("healthcheck: %v", err)
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.Printf("healthcheck: status %d", resp.StatusCode)
		return 1
	}
	return 0
}
