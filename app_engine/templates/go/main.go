package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8000"
	}
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" && r.URL.Path != "/health" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"app": "{{APP_ID}}", "ready": true})
	}
	http.HandleFunc("/", handler)
	log.Printf("{{APP_LABEL}} listening on %s", port)
	log.Fatal(http.ListenAndServe("127.0.0.1:"+port, nil))
}
