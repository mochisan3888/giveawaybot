package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"sync"
	"syscall"
	"time"

	nethernet "github.com/df-mc/go-nethernet"
	"github.com/pion/ice/v4"
	"github.com/pion/webrtc/v4"
	"github.com/sandertv/go-raknet"
	"github.com/sandertv/gophertunnel/minecraft"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
)

// Keep UDP frames inside the VPS/container MTU, including on tunnelled clients.
type boundedRakNet struct{}

func (boundedRakNet) DialContext(ctx context.Context, address string) (net.Conn, error) {
	return (raknet.Dialer{MaxMTU: 1400}).DialContext(ctx, address)
}
func (boundedRakNet) PingContext(ctx context.Context, address string) ([]byte, error) {
	return (raknet.Dialer{MaxMTU: 1400}).PingContext(ctx, address)
}
func (boundedRakNet) Listen(address string) (minecraft.NetworkListener, error) {
	return (raknet.ListenConfig{MaxMTU: 1400}).Listen(address)
}

type profile struct {
	XUID string `json:"xuid"`
	Name string `json:"name"`
}
type issuer struct {
	client      *http.Client
	url, secret string
	mu          sync.Mutex
	recent      map[string]time.Time
}

var codePattern = regexp.MustCompile(`^[0-9A-F]{24}$`)

func validProfile(p profile) bool {
	n, e := strconv.ParseUint(p.XUID, 10, 64)
	return e == nil && n > 0 && p.XUID == strconv.FormatUint(n, 10) && len([]rune(p.Name)) >= 1 && len([]rune(p.Name)) <= 32
}
func (s *issuer) issue(ctx context.Context, p profile) (string, error) {
	if !validProfile(p) {
		return "", errors.New("invalid authenticated profile")
	}
	s.mu.Lock()
	now := time.Now()
	for id, until := range s.recent {
		if now.After(until) {
			delete(s.recent, id)
		}
	}
	if now.Before(s.recent[p.XUID]) || len(s.recent) >= 1000 {
		s.mu.Unlock()
		return "", errors.New("retry later")
	}
	s.recent[p.XUID] = now.Add(20 * time.Second)
	s.mu.Unlock()
	data, _ := json.Marshal(p)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(data))
	if err != nil {
		return "", errors.New("callback configuration")
	}
	req.Header.Set("Authorization", "Bearer "+s.secret)
	req.Header.Set("Content-Type", "application/json")
	res, err := s.client.Do(req)
	if err != nil {
		return "", errors.New("callback unavailable")
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return "", errors.New("callback rejected")
	}
	var result struct {
		OK   bool   `json:"ok"`
		Code string `json:"code"`
	}
	if json.NewDecoder(io.LimitReader(res.Body, 4096)).Decode(&result) != nil || !result.OK || !codePattern.MatchString(result.Code) {
		return "", errors.New("invalid callback result")
	}
	return result.Code, nil
}
func listenerConfig(group *minecraft.ListenerGroup) minecraft.ListenConfig {
	return minecraft.ListenConfig{AuthenticationDisabled: false, DisablePacketEncryption: false, MaximumPlayers: 8, ListenerGroup: group, LoginTimeout: 20 * time.Second, HTTPClient: &http.Client{Timeout: 10 * time.Second}, MaxDecompressedLen: 4 << 20, StatusProvider: minecraft.NewStatusProvider("Discord Minecraft verification", "Xbox verification")}
}
func serve(l *minecraft.Listener, s *issuer) {
	for {
		raw, err := l.Accept()
		if err != nil {
			return
		}
		go func() {
			c := raw.(*minecraft.Conn)
			defer c.Close()
			// Accept is reached only after verified Xbox login, encrypted handshake and pack negotiation.
			if !c.Authenticated() {
				_ = l.Disconnect(c, "Xbox authentication is required.")
				return
			}
			id := c.IdentityData()
			ctx, cancel := context.WithTimeout(c.Context(), 8*time.Second)
			defer cancel()
			code, err := s.issue(ctx, profile{XUID: id.XUID, Name: id.DisplayName})
			if err != nil {
				log.Print("Authenticated connection: code issuance unsuccessful")
				_ = l.Disconnect(c, "確認コードを発行できませんでした。20秒待って接続し直してください。")
				return
			}
			log.Print("Authenticated connection: verification code issued")
			_ = l.Disconnect(c, "Discord確認コード / Verification code\n\n"+code+"\n\nDiscordの「確認コードを入力」に入力してください。\n10分間有効・1回限り。他人に渡さないでください。")
		}()
	}
}
func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
func main() {
	secret := os.Getenv("VERIFY_SHARED_SECRET")
	if len(secret) < 32 {
		log.Fatal("Shared secret required")
	}
	s := &issuer{client: &http.Client{Timeout: 8 * time.Second}, url: env("BEDROCK_CALLBACK", "http://bot:8080/bedrock/issue"), secret: secret, recent: map[string]time.Time{}}
	cfg := listenerConfig(new(minecraft.ListenerGroup))
	port := env("PORT", "19132")
	rak, err := cfg.ListenNetwork(boundedRakNet{}, ":"+port)
	if err != nil {
		log.Fatal("RakNet listener could not start")
	}
	defer rak.Close()
	go serve(rak, s)
	log.Printf("Xbox verification ready: version=%s protocol=%d RakNet UDP=%s", protocol.CurrentVersion, protocol.CurrentProtocol, port)
	var nn *minecraft.Listener
	if os.Getenv("ENABLE_NETHERNET") == "true" {
		udpPort := env("NETHERNET_UDP_PORT", "19133")
		udp, err := net.ListenPacket("udp4", ":"+udpPort)
		if err != nil {
			log.Fatal("NetherNet UDP could not start")
		}
		defer udp.Close()
		mux := ice.NewUDPMuxDefault(ice.UDPMuxParams{UDPConn: udp})
		defer mux.Close()
		se := webrtc.SettingEngine{}
		se.SetICEUDPMux(mux)
		se.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP4})
		if publicIP := os.Getenv("PUBLIC_IP"); publicIP != "" {
			if net.ParseIP(publicIP) == nil {
				log.Fatal("PUBLIC_IP invalid")
			}
			se.SetNAT1To1IPs([]string{publicIP}, webrtc.ICECandidateTypeHost)
		}
		api := webrtc.NewAPI(webrtc.WithSettingEngine(se))
		nn, err = cfg.ListenNetwork(minecraft.NetherNet{ListenConfig: nethernet.ListenConfig{API: api, AllowAnonymous: false}}, ":"+port)
		if err != nil {
			log.Fatal("NetherNet listener could not start")
		}
		defer nn.Close()
		go serve(nn, s)
		log.Printf("NetherNet ready: TCP=%s UDP=%s", port, udpPort)
	}
	done := make(chan os.Signal, 1)
	signal.Notify(done, syscall.SIGTERM, syscall.SIGINT)
	<-done
	fmt.Println("Verification listeners stopping")
}
