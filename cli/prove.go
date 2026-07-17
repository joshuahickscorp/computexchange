// cx source-id / cx prove / cx verify — the Go evidence/proof authority (Phase C),
// replacing the Python scripts/ proof programs. Each subcommand reuses the shared
// evidence core (evidence.go) rather than re-implementing hashing/JSON/exec.
package main

import (
	"fmt"
	"os"
	"path/filepath"
)

// cmdSourceID implements `cx source-id` — the Go port of scripts/source_fingerprint.py.
//
//	cx source-id [--root DIR] [--field head|dirty|file_count|status_sha256|source_sha256]
func cmdSourceID(args []string) {
	root := repoRootOrCwd()
	field := ""
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--root":
			if i+1 >= len(args) {
				fatalf("--root needs a directory")
			}
			root = args[i+1]
			i++
		case "--field":
			if i+1 >= len(args) {
				fatalf("--field needs a name")
			}
			field = args[i+1]
			i++
		default:
			fatalf("unknown flag %q", args[i])
		}
	}
	res, err := sourceFingerprint(root)
	if err != nil {
		fmt.Fprintf(os.Stderr, "source-fingerprint: %s\n", err)
		os.Exit(2)
	}
	if field != "" {
		switch field {
		case "head":
			fmt.Println(res.Head)
		case "dirty":
			fmt.Println(boolLower(res.Dirty))
		case "file_count":
			fmt.Println(res.FileCount)
		case "status_sha256":
			fmt.Println(res.StatusSHA256)
		case "source_sha256":
			fmt.Println(res.SourceSHA256)
		default:
			fatalf("unknown field %q", field)
		}
		return
	}
	b, err := canonicalJSON(res.toMap())
	if err != nil {
		fatalf("encode: %v", err)
	}
	fmt.Println(string(b))
}

func boolLower(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

// repoRootOrCwd defaults --root to the proof script's historical default: the
// repository root (source_fingerprint.py used the script's parent-of-parent).
func repoRootOrCwd() string {
	if out, err := gitBytes(".", "rev-parse", "--show-toplevel"); err == nil {
		return trimNL(string(out))
	}
	wd, _ := os.Getwd()
	return filepath.Clean(wd)
}

func trimNL(s string) string {
	for len(s) > 0 && (s[len(s)-1] == '\n' || s[len(s)-1] == '\r') {
		s = s[:len(s)-1]
	}
	return s
}
