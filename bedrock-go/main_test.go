package main

import (
	"bytes"
	"context"
	"encoding/json"
	"github.com/df-mc/go-nethernet/endpoint"
	"github.com/sandertv/go-raknet"
	"github.com/sandertv/gophertunnel/minecraft"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/login"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestIdentityValidation(t *testing.T) {
	for _, x := range []string{"", "0", "-1", "abc", "001", "18446744073709551616"} {
		if validProfile(profile{x, "Steve"}) {
			t.Fatal("invalid XUID accepted")
		}
	}
	if !validProfile(profile{"123", "Player One"}) {
		t.Fatal("valid identity rejected")
	}
}
func TestIssueCallback(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer secret" {
			t.Error("missing auth")
		}
		var p profile
		_ = json.NewDecoder(r.Body).Decode(&p)
		if p.XUID != "123" {
			t.Error("wrong identity")
		}
		_, _ = w.Write([]byte(`{"ok":true,"code":"ABCDEF0123456789ABCDEF01"}`))
	}))
	defer srv.Close()
	s := &issuer{client: srv.Client(), url: srv.URL, secret: "secret", recent: map[string]time.Time{}}
	code, err := s.issue(context.Background(), profile{"123", "Steve"})
	if err != nil || len(code) != 24 {
		t.Fatal("valid callback failed")
	}
	if _, err = s.issue(context.Background(), profile{"123", "Steve"}); err == nil {
		t.Fatal("missing rate limit")
	}
	if calls.Load() != 1 {
		t.Fatal("duplicate callback")
	}
}
func TestRejectsMalformedCallback(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{"ok":true,"code":"invalid"}`)) }))
	defer srv.Close()
	s := &issuer{client: srv.Client(), url: srv.URL, secret: "secret", recent: map[string]time.Time{}}
	if _, err := s.issue(context.Background(), profile{"123", "Steve"}); err == nil {
		t.Fatal("invalid code accepted")
	}
}
func TestProtocolAndSecuritySettings(t *testing.T) {
	if protocol.CurrentProtocol != 2225 || protocol.CurrentVersion != "1.26.60" {
		t.Fatal("wrong protocol")
	}
	cfg := listenerConfig(new(minecraft.ListenerGroup))
	if cfg.AuthenticationDisabled || cfg.DisablePacketEncryption {
		t.Fatal("authentication weakened")
	}
}

type safeLog struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *safeLog) Write(p []byte) (int, error) { s.mu.Lock(); defer s.mu.Unlock(); return s.b.Write(p) }
func (s *safeLog) String() string              { s.mu.Lock(); defer s.mu.Unlock(); return s.b.String() }

func TestLatestRakNetRejectsOffline(t *testing.T) {
	if os.Getenv("RUN_NETWORK_TESTS") != "true" {
		t.Skip("set RUN_NETWORK_TESTS=true for live OIDC discovery")
	}
	cfg := listenerConfig(new(minecraft.ListenerGroup))
	var logs safeLog
	cfg.ErrorLog = slog.New(slog.NewTextHandler(&logs, nil))
	l, err := cfg.ListenNetwork(boundedRakNet{}, "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	var issued atomic.Int32
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { issued.Add(1) }))
	defer callback.Close()
	go serve(l, &issuer{client: callback.Client(), url: callback.URL, secret: "secret", recent: map[string]time.Time{}})
	pong, err := raknet.PingTimeout(l.Addr().String(), 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(pong), ";2225;1.26.60;") {
		t.Fatalf("wrong protocol advertisement: %s", pong)
	}
	d := minecraft.Dialer{IdentityData: login.IdentityData{DisplayName: "Untrusted", XUID: ""}}
	conn, err := d.DialTimeout("raknet", l.Addr().String(), 8*time.Second)
	if conn != nil {
		_ = conn.Close()
	}
	if err == nil {
		t.Fatal("offline login accepted")
	}
	for n := 0; n < 20 && !strings.Contains(strings.ToLower(logs.String()), "verify id token"); n++ {
		time.Sleep(10 * time.Millisecond)
	}
	if !strings.Contains(strings.ToLower(logs.String()), "verify id token") {
		t.Fatalf("not an authentication rejection: %s", logs.String())
	}
	if issued.Load() != 0 {
		t.Fatal("unauthenticated code issued")
	}
}
func TestLatestNetherNetRejectsAnonymous(t *testing.T) {
	if os.Getenv("RUN_NETWORK_TESTS") != "true" {
		t.Skip("network integration")
	}
	handler := endpoint.HandlerConfig{}.New()
	server := httptest.NewServer(handler)
	defer server.Close()
	cfg := listenerConfig(new(minecraft.ListenerGroup))
	l, err := cfg.ListenNetwork(minecraft.NetherNet{Signaling: handler}, handler.NetworkID())
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	pong, err := (minecraft.NetherNet{}).PingContext(ctx, server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(pong), ";2225;1.26.60;") {
		t.Fatalf("wrong status %s", pong)
	}
	c, err := (minecraft.Dialer{}).DialContextNetwork(ctx, minecraft.NetherNet{}, server.URL)
	if c != nil {
		_ = c.Close()
	}
	if err == nil {
		t.Fatal("anonymous connection accepted")
	}
}
